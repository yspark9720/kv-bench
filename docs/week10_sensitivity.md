# 10주차 민감도 실험: 옮기는 비용이 비싸지면

작성 2026-10-29 · 전규빈. [실험 전 확정 계획](week10_sensitivity_plan.md) 그대로 실행했다: 180초 · 0.2, 9조건, seed 0 ~ 14, PCIe 4단계 × CPU 풀 4단계 = 16 조합 × offload 정책 3종 = 6,480 runs, 기준 정책 3종 405 runs. 판정 기준 · 상한(0.9) · 격자는 결과를 본 뒤 바꾸지 않았다.

## 1. 결론

- **격자 16 조합 모두에서 역전이 없다.** PCIe 를 8배 느리게(12 → 1.5 GB/s), CPU 풀을 64배 작게(64 → 1 GiB) 해도 Watermark Offload 는 SLO 통과 seed 합계가 Evict/Recompute(98/135) 이상이고 평균 normal P95 TTFT 가 Evict(722 ms)보다 짧다.
- **이유는 비용이 쌓이는 자리가 다르기 때문이다.** 재계산은 일반 요청과 공유하는 prefill 서버를 쓴다(Evict 의 prefill 사용률 30 %, Watermark 12 %). 그래서 재계산 비용이 **다른 사용자의 TTFT** 로 넘어간다. offload · reload 는 별도의 PCIe 를 써서 비용이 대부분 **도구 세션 자신** 에 머문다.
- 비용이 커지는 만큼 Watermark 도 나빠지기는 한다: PCIe 1.5 GB/s 에서 TTFT 272 → 343 ms, CPU 1 GiB 에서는 CPU 가 차 evict 로 떨어지며 재계산이 생기고 406 ms. 그래도 Evict 의 절반 수준이다.
- 세션 하나만 놓고 보면 reload 시간과 재계산 시간이 같아지는 PCIe 는 약 **0.92 GB/s** (토큰당 112 KiB ÷ 대역폭 = 1 ÷ 8,000 tok/s)다. 격자의 최저값 1.5 GB/s 보다 아래라서 이번 범위에서는 역전이 나타나지 않았다.

## 2. Watermark Offload 격자 (9조건 · seed 15개 합계)

| PCIe (GB/s) | CPU (GiB) | Watermark SLO 통과 | 평균 P95 TTFT (ms) | 재계산 (천 토큰/run) | Evict 대비 |
|---|---|---|---|---|---|
| 12 | 64 | 134/135 | 272 | 0 | 유지 |
| 12 | 4 | 134/135 | 277 | 4 | 유지 |
| 12 | 2 | 132/135 | 317 | 26 | 유지 |
| 12 | 1 | 131/135 | 369 | 60 | 유지 |
| 6 | 64 | 134/135 | 277 | 0 | 유지 |
| 6 | 4 | 134/135 | 282 | 5 | 유지 |
| 6 | 2 | 131/135 | 326 | 29 | 유지 |
| 6 | 1 | 130/135 | 377 | 63 | 유지 |
| 3 | 64 | 134/135 | 293 | 0 | 유지 |
| 3 | 4 | 134/135 | 301 | 6 | 유지 |
| 3 | 2 | 132/135 | 341 | 32 | 유지 |
| 3 | 1 | 130/135 | 392 | 65 | 유지 |
| 1.5 | 64 | 134/135 | 343 | 0 | 유지 |
| 1.5 | 4 | 134/135 | 347 | 8 | 유지 |
| 1.5 | 2 | 132/135 | 373 | 37 | 유지 |
| 1.5 | 1 | 129/135 | 406 | 69 | 유지 |

Evict/Recompute 기준: SLO 98/135, 평균 TTFT 722 ms, 재계산 381 천 토큰/run.

## 3. 다른 정책

| 정책 | SLO 통과 (격자 전체 범위) |
|---|---|
| LRU Offload | 5 ~ 5 /135 |
| Duration-Aware | 10 ~ 14 /135 |
| Evict/Recompute | 98/135 (PCIe · CPU 무관) |
| Fixed TTL | 31/135 (PCIe · CPU 무관) |
| Pin-All | 5/135 (PCIe · CPU 무관) |

LRU Offload 와 Duration-Aware 는 격자 어디서도 점유율 기준에 막혀 낮은 통과 수에 머문다(10주차 결과와 같은 원인: 입장 시점 미제어).

## 4. 비용이 쌓이는 자리

| 정책 · 조건 | normal P95 TTFT (ms) | prefill 사용률 (%) | PCIe 사용률 (%) | stress 완료시간 P95 (s) |
|---|---|---|---|---|
| Evict/Recompute | 722 | 30.3 | 0.0 | 79.0 |
| Watermark · 12 GB/s · 64 GiB | 272 | 12.0 | 0.8 | 76.8 |
| Watermark · 3 GB/s · 64 GiB | 293 | 12.0 | 3.5 | 77.0 |
| Watermark · 1.5 GB/s · 64 GiB | 343 | 11.9 | 7.6 | 77.6 |
| Watermark · 1.5 GB/s · 1 GiB | 406 | 15.2 | 2.9 | 77.5 |

- Evict 는 prefill 사용률이 2.5배이고 그만큼 normal TTFT 가 길다.
- Watermark 는 PCIe 가 느려지면 PCIe 사용률이 오르지만 prefill 사용률은 그대로다. CPU 1 GiB 에서만 evict 로 떨어진 만큼 prefill 사용률이 15 % 로 오른다.
- stress 작업 완료시간 P95 는 도구 대기가 대부분이라 정책 간 차이가 작다(Watermark 77 ~ 78 s, Evict 79 s).

## 5. 해석 범위와 한계

- **이 결론은 "PCIe 전송이 prefill 계산과 경합하지 않는다"는 시뮬레이터 가정에 기댄다.** 실제 GPU 에서는 전송이 메모리 대역폭을 나눠 쓰거나 계산과 겹치지 못할 수 있다.
- **prefill 모델이 결정적이다.** 시뮬레이터는 prefill 을 한 번에 하나씩 처리한다(FIFO). 실제 vLLM 의 chunked prefill · 배치는 재계산이 일반 요청을 막는 정도를 줄이므로 Evict 의 불리함이 작아질 수 있다. prefill 속도(8,000 tok/s, 자리표시자)가 빨라져도 마찬가지다. 다음 민감도는 이 축이어야 한다.
- 역전 경계(약 0.92 GB/s 근처)는 계산값이다. 1.5 GB/s 아래를 확인하려면 별도 계획으로 격자를 추가한다.
- seed 15개 짝지은 비교, 통계 검정 없음. 기준 정책이 PCIe · CPU 와 무관함은 1.5 GB/s · 1 GiB 조합에서 기본값 결과와 바이트 비교로 확인했다.

## 6. 재현

```powershell
python -m kvbench cost-sensitivity --output results/cost_sensitivity
```

기준 정책(Evict · Fixed TTL · Pin-All)은 Python 에서 `run_w2_factorial(..., seeds=tuple(range(15)), policies=("evict_recompute", "fixed_ttl", "pin_all"))` 로 한 번 실행한다.
