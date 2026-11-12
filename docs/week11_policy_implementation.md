# 11주차 정책 구현 명세와 논문 대응

## 범위

T-LRU Algorithm 1과 HotPrefix의 hotness 기록·GPU 회수·선택적 CPU admission(§3.2–3.3)을 현재 KV-Bench 실행 모델에 구현했다. **HotPrefix 전체 시스템, §3.4의 선제 승격, CUDA pipeline, 실제 LLM tensor, 세션 간 prefix 공유의 재현은 아니다.** 기본 `hotprefix_admission`은 GPU 회수 순서를 LRU로 고정해 CPU admission 효과를 분리한다. GPU hotness 순서는 ablation으로 제공한다.

정책은 `kvbench/policies/prefix.py`, block 엔진은 `kvbench/prefix_engine.py`에 있다. `Simulator`는 `uses_prefix_cache`에 따라 새 엔진을 선택한다. 기존 7개 정책은 기존 엔진에서 실행된다. registry·CLI·JSON 설정에 새 정책/기준선 4개를 등록했다. Dynamic TTL은 여전히 미구현이다.

## 공통 자원 모델

- 기존과 같은 16-token block이며 마지막 block의 유효 토큰 수는 따로 추적한다. 세션 내부 append-only block chain을 사용한다.
- 입장 시 context+output 전체를 예약한다. FIFO 입장·prefill·PCIe, 세션별 고정 decode 속도를 유지한다.
- **TOOL_WAIT의 block만** GPU/CPU 회수 후보가 된다. WAIT_ADMIT, 실행 요청, 진행 중 전송은 보호한다.
- GPU·CPU·삭제·전송 중 상태를 block별로 기록한다. GPU와 CPU에 일부씩 남은 상태에서 없는 토큰만 재계산한다.
- offload 시작에 CPU 용량을 예약하고 block copy 완료에 GPU를 반환한다. reload 목적지는 입장 때 예약하고 copy 완료에 CPU를 반환한다.
- 새 엔진의 모든 정책은 이미 반환이 예정된 in-flight block을 부족량에서 제외해 중복 회수를 막는다. 기존 엔진은 변경하지 않았다.
- copy 중 복귀하면 마지막 해당 copy 완료 후 후속 요청을 등록한다. admission timeout은 기존처럼 등록 시점부터다. 실제 복귀 지연은 도구 종료부터 계산해 등록 지연도 포함한다.
- 새 엔진의 offload/eviction 사건 수는 block 단위다. 기존 session 단위 사건 수와 직접 비교하지 않고 bytes/tokens를 비교한다.

## 공정한 기준선

| 정책 | GPU 회수 | CPU 보관 |
|---|---|---|
| `prefix_lru_evict` | 오래 idle인 세션부터 필요한 block만 suffix에서 삭제 | 사용 안 함 |
| `prefix_lru_offload` | 같은 block LRU | 항상 보관 시도; 부족하면 회수 가능한 CPU LRU block 교체 |
| `tlru` | TEL-safe suffix 우선, 이후 같은 block LRU | 사용 안 함 |
| `hotprefix_admission` | 기본 block LRU, 선택적으로 hotness | 빈도·hotness에 따른 admission/교체 |

기존 `lru_offload`는 CPU 포화 시 incoming session을 삭제하는 fallback이다. 새 기준선과는 회수 단위·in-flight 처리·CPU 교체도 다르므로 엔진 차이를 별도 참조 비교로 보고한다.

## T-LRU

[논문 Algorithm 1](https://arxiv.org/html/2510.15152v1#S4), [SGLang 구현](https://github.com/sgl-project/sglang/blob/main/python/sglang/srt/mem_cache/evict_policy.py).

```text
xi = floor(threshold_ms * prefill_tokens_per_s / 1000)
keep_blocks = ceil(max(L + Q_hat - xi, 0) / block_size_tokens)
TEL-safe = GPU block 중 index >= keep_blocks
```

현재 알려진 history L과 다음 prompt 추정 Q_hat을 사용한다. memory pressure에서 TEL-safe block부터 회수하고, 부족하면 일반 block LRU로 회수한다. safe block 간 tie도 LRU이며 suffix부터 제거한다. 보관량은 block 경계를 넘겨 예산을 깨지 않도록 올림한다.

기본값은 200ms, Q_hat 160 tokens다. 160은 기존 follow-up 설정 [64,256]의 중간값이며 실제 미래 prompt를 읽지 않는다. 예산은 prefill service 모델에 대응한다. **대기열·reload 포함 TTFT 보장이나 현재 엔진에서의 이론적 최적성을 주장하지 않는다.** 현재 모델의 active 예약·FIFO 보호는 논문의 추상적 post-turn allocation과 다르고, 같은 제약을 block LRU에도 적용했다.

## HotPrefix

[논문 DOI](https://doi.org/10.1145/3749168), [저자 PDF](https://cs.nju.edu.cn/tianchen/lunwen/2026/sigmod26-liyuhang.pdf).

### 기록 (§3.2)

frequency는 과거 실행한 요청에서 block을 접근/재계산/복원할 때 증가하며 한 턴에서 한 번만 증가한다. prompt는 prefill 완료, 새 output block은 decode 완료 시 기록한다. 아직 생성되지 않은 output이나 다음 prompt는 사용하지 않는다. 마지막 partial page에 새 토큰이 추가되는 경우 metadata도 같은 block 단위를 쓴다.

clock은 사용 시 max_age로 reset하고 age_every_requests개 prefill이 완료될 때 모든 기록에서 1 감소하며 0 아래로 내려가지 않는다. **15는 논문의 예시값이고, 100-request aging은 논문에 수가 명시되지 않아 사전에 정한 재현 설정**이다. 저자의 기본값이라고 주장하지 않는다. 논문의 cuckoo filter 대신 exact dictionary를 쓰므로 hash 오차·metadata 처리 성능은 재현하지 않는다.

### GPU 회수 (§3.3.1)

기본은 LRU다. `gpu_eviction=hotness`를 켜면 GPU leaf 중 `frequency + clock / tokens`가 낮은 block부터 회수하고 이후 부모를 후보로 노출한다. 식 주변 prose에는 clock을 경과시간이라고 쓴 부분이 있으나 명시적인 §3.2 reset/aging 알고리즘을 따른다.

### CPU admission (§3.3.2)

1. frequency가 기준(기본 2) 미만이면 copy하지 않고 삭제한다.
2. 회수 가능한 CPU block이 있으면 최소 `frequency * clock`과 비교하고 후보가 낮으면 삭제한다.
3. 통과하고 공간이 있으면 offload한다.
4. 공간이 부족하면 회수 가능한 CPU block 중 hotness 최솟값을 교체한다. 모든 CPU 공간이 전송/실행 요청에 묶여 있거나 한 block도 못 담으면 후보를 삭제한다.

tie는 최근 접근 시각·session ID·block index로 결정한다. metadata는 copy/reload에 걸쳐 유지한다. `selective=False`는 CPU admission/교체를 block LRU와 같게 하는 음성 대조군이며 동일 조건에서 요청 기록·점유율이 정확히 같아야 한다.

## 미래 정보·입력의 한계

BlockView에는 도구 대기시간, 실제 다음 prompt, 총/남은 턴 수가 없다. 상위 idle view의 oracle wait도 0으로 가린다. 평가기만 실제 도구 종료 시각을 계산한다.

normal은 한 턴 뒤 KV를 해제하고 stress의 중간 tool call은 반드시 복귀한다. 죽은 session이나 다시 쓰지 않을 공유 prompt 검출을 검증하지 않는다. 짧은 3–5턴에서 frequency 보관이 불리할 수도 있다.

## 지표·재현

`kvbench/week11.py`의 복귀 P95는 warm-up 이후 최초 도착한 stress 세션의 완료 후속 턴에 대해 `first_token - (이전 complete + tool_duration)`을 구한다. planned turn 수는 거절 때문에 실행되지 않은 후속 턴도 포함한다. P95 성분은 **동일한 실제 P95 요청**에서 잔여 offload, 입장, reload/PCIe 대기, prefill 대기, 자기 prefill로 분해한다. 개별 성분의 P95를 더하지 않는다.

`cohort_*` 비용은 warm-up 이후 최초 도착 세션에 귀속한다. 전체 run 비용과 분모가 다른 기존 per-job 비용을 주 판단에 사용하지 않는다. 실제 복귀 지연의 200ms 초과분 TEL도 제공한다. 기존 GPU 90% SLO 통과 수는 보조값이다.

```powershell
python -m unittest discover -s tests -v
python scripts/verify_week11_regression.py docs/data/week11/legacy_fingerprints.json
python scripts/run_week11.py --output results/week11_new --workers 4
python scripts/report_week11.py results/week11_new
python scripts/build_week11_notebook.py
```

출력 폴더를 덮어쓰지 않는다. manifest에 source/config·seed·조건·trace hash를 기록한다. raw outputs는 results에 저장하고 git에서 제외하며 공유용 CSV·manifest·검증 자료·실행된 notebook·그림은 저장소에 포함한다.
