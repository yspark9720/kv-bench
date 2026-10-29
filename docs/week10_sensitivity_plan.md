# 10주차 민감도 실험 계획: 옮기는 비용이 비싸지면 (실험 전 확정본)

작성 2026-10-29 · 전규빈. [10주차 실험](week10_experiment.md)의 결론이 **"옮기는 비용이 거의 공짜"라는 가정**에 기대고 있는지 확인한다. 실험을 돌리기 전에 격자 · 정책 · 판정 · 해석을 고정한다.

## 1. 질문

10주차 결과에서 Watermark Offload 는 Evict/Recompute 와 같은 SLO 를 지키면서 재계산 0, TTFT 절반이었다. 그러나 시스템 가정이 offload 에 유리하다.

- PCIe 12 GB/s 를 다른 전송과 나누지 않는다 (세션 하나 약 450 MB → 약 40 ms).
- CPU KV 풀 64 GiB 는 한 번도 차지 않는다 (180초 · 0.2 에서 최대 사용 6.35 GiB).

**질문.** PCIe 가 느려지거나 CPU 풀이 작아지면 Watermark 의 이점이 유지되는가. 유지되지 않는다면 **어디서 Evict 와 역전되는가**.

**가설.**
- S1. PCIe 가 느려질수록 offload · reload 시간이 입장 대기와 복귀 지연에 더해져 Watermark 의 normal TTFT 가 늘고, 어느 속도 아래에서는 Evict 보다 나빠진다.
- S2. CPU 풀이 사용량(약 6 GiB)보다 작아지면 offload 대신 evict 로 떨어져 재계산이 생기고 Evict 에 가까워진다.

어느 결과든 쓸모가 있다: 유지되면 "옮기는 비용이 상당해도 입장 제어 + offload 가 낫다", 역전되면 **"PCIe ≥ X GB/s · CPU ≥ Y GiB 이면 offload, 아니면 재계산"** 이라는 정책 선택 기준이 된다.

## 2. 격자 (고정)

| 변수 | 값 | 근거 |
|---|---|---|
| PCIe 단방향 | 12 · 6 · 3 · 1.5 GB/s | 12 = 현재 가정(Gen3 ×16 실측 근처). 절반씩 낮춰 공유 · 낮은 레인 수 · 다른 전송과의 경합을 흉내 |
| CPU KV 풀 | 64 · 4 · 2 · 1 GiB | 64 = 현재(사실상 무제한). 최대 사용 약 6 GiB 보다 작은 값만 의미가 있어 4 · 2 · 1 |
| 나머지 시스템 값 | `system_1080ti_llama3b.json` 그대로 | 한 번에 두 변수만 |

16 조합. PCIe 와 CPU 는 offload 를 하는 정책에만 영향을 준다.

## 3. 정책 · 워크로드 · seed (고정)

| 항목 | 값 |
|---|---|
| 정책 (격자 전체) | `watermark_offload` · `lru_offload` · `duration_aware` |
| 정책 (기준, 한 번만) | `evict_recompute` · `fixed_ttl` · `pin_all` — PCIe · CPU 를 쓰지 않으므로 격자와 무관. 무관함은 한 조합(1.5 GB/s · 1 GiB)에서 기본값 결과와 바이트 비교로 확인 |
| 워크로드 | 180초 · 0.2 (`week7_180s_stress02.json`), 9주차 9조건 |
| seed | 0 ~ 14 (15개: 10주차 본 실험 5 + 홀드아웃 10) |
| 규모 | 16 조합 × 3 정책 × 9 조건 × 15 seed = 6,480 runs + 기준 3 정책 405 runs |

## 4. 판정과 해석 (실험 전에 정함)

보고 지표: SLO 통과 seed 수(팀 기준 그대로), normal P95 TTFT 절대값(seed 평균), 거절률, 최고 점유율, 재계산 토큰, 전송 GiB, stress 완료율.

**역전 판정.** 조합마다 9조건을 합쳐 Watermark 와 Evict 를 비교한다.

- **Watermark 우위 유지**: SLO 통과 seed 합계가 Evict 이상 **이고** 9조건 평균 normal P95 TTFT 가 Evict 이하.
- **역전**: SLO 통과 합계가 Evict 보다 적거나, 평균 TTFT 가 Evict 보다 길다.
- **경계**: CPU 64 GiB 에서 우위가 유지되는 가장 느린 PCIe, PCIe 12 GB/s 에서 우위가 유지되는 가장 작은 CPU 풀.

결과를 본 뒤 격자 값이나 판정을 바꾸지 않는다. 경계가 격자 사이에 있으면 "6 과 3 GB/s 사이" 처럼 구간으로만 보고하고, 사이 값을 더 돌리려면 별도 기록을 남긴다.

## 5. 하지 않는 것

- 상한 값 변경(0.9 고정), 정책 로직 변경, 판정 기준 변경
- prefill · decode 속도 변경 (재계산 비용 쪽 민감도는 별도 실험)
- 엔진의 회수 이중 계산 수정 (10주차 문서 7절: 결론 영향 없음)

## 6. 구현

`kvbench cost-sensitivity` 서브커맨드: 조합마다 시스템 설정을 결과 폴더에 저장하고 `run_w2_factorial` 을 그대로 호출한 뒤, 조합 · 정책 · 조건별 결과를 `sensitivity.csv` 하나로 모은다. 판정 코드는 팀 `aggregate_week7` 을 그대로 쓴다.
