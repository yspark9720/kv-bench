# 정책 시뮬레이터

`kvbench simulate`는 생성기가 만든 JSONL 트레이스를 하나의 KV Cache 정책 아래에서 시간 순으로 재생합니다. 실제 LLM 추론이나 GPU 실행은 없고, 세션·턴·도구 대기 이벤트와 KV block·PCIe·prefill 비용만 계산합니다. 같은 트레이스를 정책만 바꿔 재생하면 정책 외 요인이 통제됩니다.

## 세션 상태

```text
WAIT_ADMIT -(block 확보)-> [RELOADING] -> PREFILLING -> DECODING -> TOOL_WAIT -(도구 완료)-> WAIT_ADMIT(다음 턴) ... -> DONE
     \-(admission_max_wait_ms 초과)-> REJECTED (세션의 남은 턴은 제출하지 않음)
```

첫 턴은 `arrival_ms`에 도착하고, 후속 턴은 직전 턴의 decode 완료 시각 + `tool_duration_ms`에 재입장을 시도합니다. offload가 아직 진행 중이면 전송 완료까지 재입장이 지연됩니다. 실제 실행 시각은 입력 JSONL의 필드가 아니라 시뮬레이션 결과로 결정됩니다. 도착한 턴은 FIFO 입장 대기열에 들어가고, 필요한 KV block을 받으면 복원·prefill을 진행합니다. `admission_max_wait_ms` 안에 block을 받지 못하면 거절됩니다.

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

| 정책 | idle 진입 | block 부족 시 | KV 복원 | 비교에서의 역할 |
|---|---|---|---|---|
| `pin_all` | 유지 | 대기, 설정된 입장 제한 시간 초과 시 거절 | 보존 KV 재사용 | 상시 보유 기준선 |
| `evict_recompute` | 즉시 삭제 | idle KV는 이미 회수됨 | prefix 전체 재 prefill | 즉시 삭제·재계산 기준선 |
| `fixed_ttl` | `ttl_ms` 동안 유지 후 삭제 | 기본은 대기 (`evict_on_pressure`로 변경) | 유지 중이면 재사용, 삭제됐으면 재계산 | 고정 idle TTL 기준선 |
| `lru_offload` | 유지 | 가장 오래 idle인 세션부터 CPU로 | CPU에서 재적재 | 메모리 부족 시 반응하는 offload 기준선 |
| `dynamic_ttl` | 미구현 | | | 실행 대상에서 제외 |
| `duration_aware` | 여유 있으면 유지, 아니면 예측 대기로 OFFLOAD/EVICT 선택 | 남은 대기가 긴 세션부터 정리 | 재적재 또는 재계산 | 실제 미래 대기를 입력으로 쓰는 단순화한 참조 정책 |

각 이름은 이 저장소의 단순화한 구현을 뜻하며 외부 추론 엔진이나 논문 전체를 그대로 재현했다는 의미는 아닙니다. `duration_aware`는 headroom이 설정값(현재 30%) 이상이면 KEEP하고, 그렇지 않으면 예상 대기와 전송 왕복시간·CPU 여유를 사용해 OFFLOAD/EVICT를 선택합니다. 로그의 재계산 추정값은 현재 이 선택식에 직접 사용하지 않습니다. oracle 입력을 사용한다는 사실도 이 휴리스틱이 최적 정책이라는 의미는 아닙니다.

## 토큰과 block

턴이 입장할 때 `context_tokens + output_tokens`만큼의 block을 한 번에 예약합니다. KV가 GPU에 남아 있으면 새 `prompt_tokens`만 prefill하고, 삭제됐으면 `context_tokens - prompt_tokens`를 재계산한 뒤 prompt를 prefill합니다. 이 규칙은 [트레이스 스키마](trace_schema.md)의 정의를 그대로 따릅니다. block 크기는 시스템 설정의 `block_size_tokens`이고 올림 처리합니다.

offload를 시작한 순간에는 GPU block이 비워지지 않고, 전송 완료 시 회수됩니다. 현재 입장 로직은 실제 풀 용량 100% 안에서 예약하며, 평가 SLO의 90%는 별도의 여유 공간 목표입니다. 따라서 낮은 normal TTFT와 Peak ≤ 90% 달성은 서로 다른 결과입니다.

## 단순화 가정

- prefill은 하나의 FIFO 서버로, 한 번에 한 턴만 처리합니다. decode는 세션마다 고정 속도로 진행하며 세션 간 경합은 없습니다. PCIe는 하나의 FIFO 링크입니다.
- 도구 대기시간은 트레이스 값 그대로이며 정책 결정의 영향을 받지 않습니다.
- `duration_aware`의 예측 대기는 트레이스의 실제 값(oracle)입니다. 예측기가 생기면 `IdleView.predicted_wait_ms`만 바꾸면 됩니다.
- 거절된 턴이 있으면 그 세션은 종료됩니다.

## 지표

지표마다 집계 대상과 시간 구간이 다릅니다. `warmup_ms`는 트레이스 메타데이터에서 읽으며 `--warmup-ms`로 덮어쓸 수 있습니다. 요청 지연·normal 거절률은 warm-up 이후 도착한 요청, 작업 완료 지표는 warm-up 이후 최초 도착한 세션을 대상으로 합니다. 비용 누계와 자원 점유는 아래 구간 정의를 따릅니다.

| 지표 | 정의 |
|---|---|
| `normal_p95_ttft_ms` | 완료된 normal 요청의 `first_token_ms - arrival_ms` P95 |
| `normal_reject_rate_pct` | 거절된 normal ÷ 제출된 normal × 100 |
| `peak_kv_occupancy_pct` | warm-up 이후 `used_blocks ÷ total_blocks × 100`의 최댓값 |
| `mean_kv_occupancy_pct` | `[warmup_ms, duration_ms)`에서 시간 가중 평균 GPU KV 점유율 |
| `kv_above_90_ms`, `kv_above_90_pct_of_time` | 같은 고정 구간에서 점유율이 90%를 **초과한** 시간과 구간 비율 |
| `stress_p95_completion_ms` | warm-up 이후 도착해 완료된 stress 세션의 작업 완료시간 P95 |
| `stress_submitted_jobs`, `stress_completed_jobs` | warm-up 이후 최초 도착한 stress 세션 수와 그중 완료한 수 |
| `eviction_count`, `offload_count`, `reload_count`, `transfer_bytes`, `recomputed_tokens` | warm-up·drain을 포함한 전체 run 누계 |
| `eviction_per_job`, `transfer_gib_per_job`, `recomputed_tokens_per_job` | 전체 run 누계 ÷ warm-up 이후 도착해 완료한 작업 수. 엄밀한 비용 효율 평가 전 분자·분모의 집계 범위를 맞춰야 함 |
| `throughput_jobs_per_s` | warm-up 이후 도착해 완료한 작업 수 ÷ `(시뮬레이션 종료 시각 - warmup_ms)`. drain을 포함해 기간·대기 규칙의 영향도 받음 |
| `prefill_busy_pct`, `pcie_busy_pct` | 전체 run의 해당 서버 busy 시간 ÷ 시뮬레이션 종료 시각 × 100 |
| `sessions`, `rejected_jobs` | 전체 run의 세션 수와 거절 작업 수. `rejected_jobs`는 warm-up 이전 도착도 포함 |

기존 Peak 정의는 7주차 결과와 비교할 수 있도록 유지한다. 새 시간 가중 지표는 정책별 시뮬레이션 종료 시각 차이를 피하기 위해 외부 도착 구간의 끝 `duration_ms`에서 측정을 멈춘다. 메타데이터가 없는 trace는 시뮬레이션 종료 시각을 사용한다. 새 지표는 기존 SLO 판정에 넣지 않는다.

P95는 nearest-rank 방식으로 구합니다. 완료 요청만의 TTFT이므로 거절률과 완료 수도 함께 읽어야 합니다. `comparison.csv`의 `*_mean`과 `*_std`는 seed별 run 지표의 평균·표본 표준편차이며 요청을 전부 합친 분포가 아닙니다. seed별 SLO는 해당 정책·같은 seed의 W0 TTFT 대비 3배 이하, normal 거절률 10% 이하, Peak 90% 이하를 모두 만족해야 합니다.

9주차 노트북 마지막 절의 **도구 대기 제외 stress 작업시간**은 `summary.csv`의 새 필드가 아닌 보조 분석입니다. warm-up 이후 도착해 모든 턴을 완료한 작업별로 `(마지막 완료 시각 - 최초 도착 시각) - 도구 대기 합`을 계산한 뒤 run별 P95를 구합니다. 생성·입장 대기·복원 시간이 포함되므로 KV 정책만의 순수 비용으로 해석하지 않습니다. 결과와 측정 구간 민감도는 [9주차 분석](week9_experiment.MD)에 정리돼 있습니다.

## 출력

`--output` 디렉터리에 다음 파일을 씁니다. 디렉터리가 이미 있으면 덮어쓰지 않고 종료합니다.

| 파일 | 내용 |
|---|---|
| `summary.json` | 위 지표와 실행 정보(trace, workload, stress_level, seed, warmup_ms), 사용한 시스템·정책 설정 |
| `requests.csv` | 턴별 도착·입장·첫 토큰·완료 시각, 결과(completed/rejected), 복귀 방식(none/reload/recompute), 재계산 토큰 |
| `occupancy.csv` | block 수가 바뀔 때마다 `(time_ms, used_blocks, total_blocks)` — 점유율 곡선용 |
| `events.csv` | ARRIVE, ADMIT, PREFILL_QUEUED, FIRST_TOKEN, DECODE_END, TOOL_START, TTL_EXPIRE, EVICT, OFFLOAD_START/DONE, RELOAD_START/DONE, RECOMPUTE, REJECT, COMPLETE. TOOL_START에는 정책 선택 당시 GPU 여유·예상 대기·비용 추정값도 기록. `--no-events`로 생략 |

`kvbench collect <run들의_상위_폴더>`는 바로 아래 `*/summary.json`을 모으며 재귀 탐색하지 않습니다. 매트릭스 출력에서는 `python -m kvbench collect results/<실험>/runs --output results/<실험>/summary_recollected.csv`처럼 입력을 지정합니다.
