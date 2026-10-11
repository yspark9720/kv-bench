# 정책 시뮬레이터

`kvbench simulate`는 생성기가 만든 JSONL 트레이스를 하나의 KV Cache 정책 아래에서 시간 순으로 재생합니다. 실제 LLM 추론이나 GPU 실행은 없고, 세션·턴·도구 대기 이벤트와 KV block·PCIe·prefill 비용만 계산합니다. 같은 트레이스를 정책만 바꿔 재생하면 정책 외 요인이 통제됩니다.

## 세션 상태

```text
WAIT_ADMIT -(block 확보)-> [RELOADING] -> PREFILLING -> DECODING -> TOOL_WAIT -(도구 완료)-> WAIT_ADMIT(다음 턴) ... -> DONE
     \-(admission_max_wait_ms 초과)-> REJECTED (세션의 남은 턴은 제출하지 않음)
```

첫 턴은 `arrival_ms`에 도착하고, 후속 턴은 직전 턴의 decode 완료 시각 + `tool_duration_ms`에 도착합니다. 스키마의 `tool_start_ms`, `resume_ms`가 이 시각들입니다. 도착한 턴은 FIFO 입장 대기열에 들어가고, 필요한 KV block을 받으면 prefill을 시작합니다. `admission_max_wait_ms` 안에 block을 받지 못하면 거절됩니다.

## KV 위치와 정책 결정

```text
NONE -(입장: prefill 또는 재계산)-> GPU -(offload)-> TO_CPU -> CPU -(입장: 재적재)-> TO_GPU -> GPU
GPU -(evict)-> NONE
```

정책은 세 지점에서만 결정합니다.

| 지점 | 언제 | 결정 |
|---|---|---|
| `on_idle_start` | 세션이 도구 대기에 들어갈 때 | KEEP / OFFLOAD / EVICT, KEEP이면 TTL 지정 가능 |
| `on_ttl_expire` | KEEP의 TTL이 끝났을 때 | EVICT(기본) / OFFLOAD / KEEP |
| `select_victims` | 새 턴이 block을 못 받을 때 | 정리할 idle 세션과 동작의 목록. 기본은 빈 목록(턴이 대기) |

정책은 엔진 상태를 직접 바꾸지 않고 `IdleView`(idle 세션 하나의 block 수, 보유 토큰, idle 시작 시각, 예측 대기)와 `PolicyContext`(여유 block, CPU 여유, 전송·재계산 시간 계산)만 봅니다. 새 정책은 `kvbench/policies/`에 파일을 추가하고 `REGISTRY`에 넣습니다.

| 정책 | idle 진입 | block 부족 시 | 복귀 | 출처 |
|---|---|---|---|---|
| `pin_all` | 유지 | 대기, 30초 넘으면 거절 | 즉시 | vLLM 기본 동작 |
| `evict_recompute` | 즉시 삭제 | 이미 회수됨 | prefix 전체 재 prefill | vLLM preemption (recompute) |
| `fixed_ttl` | `ttl_ms` 동안 유지 후 삭제 | 기본은 대기 (`evict_on_pressure`로 변경) | 유지 중이면 즉시, 삭제됐으면 재계산 | Continuum ablation |
| `lru_offload` | 유지 | 가장 오래 idle인 세션부터 CPU로 | CPU에서 재적재 | LMCache 계열 |
| `dynamic_ttl` | 미구현 | | | Continuum |
| `duration_aware` | 여유 있으면 유지, 아니면 예측 대기로 OFFLOAD/EVICT 선택 | 남은 대기가 긴 세션부터 정리 | 재적재 또는 재계산 | Continuum·MORI 규칙 단순화, 참조 정책 |

## 토큰과 block

턴이 입장할 때 `context_tokens + output_tokens`만큼의 block을 한 번에 예약합니다. KV가 GPU에 남아 있으면 새 `prompt_tokens`만 prefill하고, 삭제됐으면 `context_tokens - prompt_tokens`를 재계산한 뒤 prompt를 prefill합니다. 이 규칙은 [트레이스 스키마](trace_schema.md)의 정의를 그대로 따릅니다. block 크기는 시스템 설정의 `block_size_tokens`이고 올림 처리합니다.

## 단순화 가정

- prefill은 하나의 FIFO 서버로, 한 번에 한 턴만 처리합니다. decode는 세션마다 고정 속도로 진행하며 세션 간 경합은 없습니다. PCIe는 하나의 FIFO 링크입니다.
- 도구 대기시간은 트레이스 값 그대로이며 정책 결정의 영향을 받지 않습니다.
- `duration_aware`의 예측 대기는 트레이스의 실제 값(oracle)입니다. 예측기가 생기면 `IdleView.predicted_wait_ms`만 바꾸면 됩니다.
- 거절된 턴이 있으면 그 세션은 종료됩니다.

## 지표

warm-up 구간(`warmup_ms`, 트레이스 메타데이터의 값, `--warmup-ms`로 덮어쓰기 가능) 이전에 도착한 요청은 모든 지표에서 제외합니다.

| 지표 | 정의 |
|---|---|
| `normal_p95_ttft_ms` | 완료된 normal 요청의 `first_token_ms - arrival_ms` P95 |
| `normal_reject_rate_pct` | 거절된 normal ÷ 제출된 normal × 100 |
| `peak_kv_occupancy_pct` | warm-up 이후 `used_blocks ÷ total_blocks × 100`의 최댓값 |
| 보조 | 완료 작업 수, 처리량, 작업 완료시간, eviction·offload·reload 횟수, 전송 바이트, 재계산 토큰, prefill·PCIe 점유율 |

## 출력

`--output` 디렉터리에 다음 파일을 씁니다. 디렉터리가 이미 있으면 덮어쓰지 않고 종료합니다.

| 파일 | 내용 |
|---|---|
| `summary.json` | 위 지표와 실행 정보(trace, workload, stress_level, seed, warmup_ms), 사용한 시스템·정책 설정 |
| `requests.csv` | 턴별 도착·입장·첫 토큰·완료 시각, 결과(completed/rejected), 복귀 방식(none/reload/recompute), 재계산 토큰 |
| `occupancy.csv` | block 수가 바뀔 때마다 `(time_ms, used_blocks, total_blocks)` — 점유율 곡선용 |
| `events.csv` | ARRIVE, ADMIT, PREFILL_QUEUED, FIRST_TOKEN, DECODE_END, TOOL_START, TTL_EXPIRE, EVICT, OFFLOAD_START/DONE, RELOAD_START/DONE, RECOMPUTE, REJECT, COMPLETE. `--no-events`로 생략 |

`kvbench collect results/`는 `results/*/summary.json`을 `results/summary.csv` 한 표로 모읍니다.
