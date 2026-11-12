# kv-bench 평가 방식 점검 (2026-11-12)

질문: 시뮬레이터 기반 정책 평가가 제대로 이루어지고 있는가, week9~11에서 뚜렷한 인사이트가 안 보이는 이유는 무엇인가, 무엇을 고쳐야 하는가.

## 판단

시뮬레이터와 실험 절차(같은 trace의 짝비교, 사전 계획, 불변식·회귀 검증, 한계 기록)는 믿을 만하다. 그러나 평가 설계가 정책을 변별하지 못한다. 정책 차이가 생길 여지가 거의 없는 환경에서, 정책을 구분하지 못하는 지표로 측정하고 있다. 인사이트가 안 보이는 것은 분석 부족이 아니라 이 설계의 결과다.

## 근거

출처는 두 가지다. (A) `docs/data/week11/summary.csv`(7,290 runs) 재분석. (B) 저장소 밖 임시 복제본(week11-experiment, 9c00787)에서 같은 코드로 돌린 추가 시뮬레이션. (B)의 실행기는 같은 설정·seed에서 발표된 값(normal P95, 복귀 P95, 전송, 재계산, Peak)을 그대로 재현했다.

### 1. 기본 환경(PCIe 12GB/s, CPU 64GiB)에서는 단순한 정책이 이미 한계에 닿아 있다

기준선: `pin_all` + GPU KV 풀 64GiB(메모리 제약이 없을 때의 값). 주 평가와 같은 9조건 × 15seed 평균.

| 설정 | 지표 | 기준선 | LRU offload | block LRU offload | HotPrefix | T-LRU | block LRU evict | Evict/Recompute |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| 60s/0.2 | normal P95 (ms) | 305.8 | 308.1 | 308.0 | 314.1 | 314.8 | 329.5 | 606.9 |
| 60s/0.2 | 복귀 P95 (ms) | 113.4 | 131.5 | 125.9 | 170.7 | 209.3 | 272.5 | 882.7 |
| 180s/0.2 | normal P95 (ms) | 265.4 | 270.1 | 270.5 | 292.5 | 342.6 | 385.2 | 722.1 |
| 180s/0.2 | 복귀 P95 (ms) | 166.2 | 196.4 | 184.1 | 280.2 | 450.2 | 547.3 | 1097.7 |
| 180s/0.3 | normal P95 (ms) | 435.3 | 447.5 | 448.5 | 497.2 | 715.2 | 759.5 | 1153.4 |
| 180s/0.3 | 복귀 P95 (ms) | 279.6 | 330.0 | 311.4 | 470.9 | 895.3 | 974.3 | 1470.1 |

- LRU offload는 Evict/Recompute와 기준선 사이 격차의 98~99%(normal), 96~98%(복귀)를 이미 메운다.
- CPU 풀을 64GiB에서 8GiB로 줄여도 모든 offload 정책의 결과가 완전히 같다(180s/0.2, 9조건 × 5seed). 0.2/s에서 CPU 사용 최대치는 180초 실행 5.6GiB, 1시간 실행 9.8GiB.
- PCIe 사용률 0.5~1.0%, prefill 사용률 12%(offload 계열).
- 기준선의 normal P95는 도구 대기 배율과 무관하다(180s/0.2: W0·base·wait 조건 모두 약 217ms, burst가 들어간 조건만 약 325ms).

### 2. 주 지표가 정책을 보지 못한다

normal P95 TTFT (180s/0.2 주 평가):
- P95 요청의 지연 구성: LRU offload 270.1ms 중 prefill 대기 229.6ms, 입장 대기 0.9ms, 자기 prefill 39.5ms.
- 405개 입력 중 LRU offload와 Duration-Aware의 값이 완전히 같은 비율 67.4%, LRU offload와 Watermark 59.3%, LRU offload와 block LRU offload 68.9%.
- LRU offload의 seed 간 표준편차 78.0ms(조건별 평균), 네 offload 정책 평균의 최대-최소 6.3ms.
- 표본 수: 60초 실행은 run당 normal 약 103개, stress 세션 약 11개, 복귀 턴 약 33개. 180초는 343개, 35~49개, 105~148개.

SLO 통과 수(주 평가 11정책):
- 전체 SLO 실패 중 Peak 90% 실패를 포함한 비율 88.3%, Peak만 실패한 비율 73.8%.
- pin_all, prefix_lru_evict, tlru, hotprefix_admission, prefix_lru_offload, lru_offload의 통과 수가 60s/180s·0.2/180s·0.3에서 모두 32/5/2(각 135회 중)로 같다.
- 메모리 무제한 실행과 요청 기록 전체가 완전히 같은 run: 60s/0.2에서 63회 중 31회, 180s/0.2에서 15회 중 10회가 SLO 실패로 판정된다. 180s/0.2의 W0만 보면 15회 중 11회가 무제한 실행과 같고 그중 6회가 실패다.
- Peak는 최댓값 통계라 실행 길이에 좌우된다. 같은 도착률(0.2/s)에서 길이만 바꾼 W0의 Peak ≤ 90% 통과 seed(10개 중): Evict/Recompute 9 → 7 → 5 → 1 → 0, LRU offload 9 → 4 → 1 → 0 → 0 (60, 180, 600, 1800, 3600초).

### 3. 워크로드에 최신 정책이 활용할 불확실성이 없다

- 생성기는 stress 세션의 모든 중간 턴 뒤에 반드시 다음 턴을 만든다. 버린 KV는 (세션이 거절되지 않는 한) 전부 다시 필요하다.
- 세션 간 prefix 공유가 없고 도구 대기는 균등분포 [1,5]초 × 배율이며 도구 종류가 없다. 세션은 3~5턴이다.
- HotPrefix admission, 180s/0.2 기본 환경(9조건 × 5seed, 결정 182,001건): 허용 77.7%, 거절 22.3%. 거절 사유는 전부 `frequency`이고 `hotness` 거절과 CPU 교체는 0건이다. frequency 거절의 70.5%가 세션의 첫 도구 대기에서 나온다. CPU 1GiB에서만 교체(28.3%)와 hotness 거절(6.7%)이 동작한다.
- T-LRU 논문(Zhang et al., NeurIPS 2025)은 대화가 종료될 수 있고 다음 턴 도착이 불확실하며 저장 계층이 하나(CPU/SSD offload는 향후 과제)인 환경을 다룬다. 기준선은 LRU·Threshold-LRU이고 오프라인 상한(Tail-Optimized Belady)도 쓴다. HotPrefix(Li et al., SIGMOD 2026)는 여러 요청이 공유하는 prefix의 재사용 편중을 대상으로 한다.

### 4. 실행이 짧고 상수가 보정되지 않았다

- 메모리 무제한일 때의 KV 수요(풀 4GiB 대비, both_4x, 0.2/s, [10초, 길이) 평균): 60초 76%, 180초 129%, 600초 142%, 1800초 152%, 3600초 154%. base_4x는 63% → 86% → 88% → 91% → 92%.
- 기준선 normal P95의 seed 간 표준편차(W0): 60초 96ms, 180초 69ms, 600초 41ms, 1800초 12ms.
- prefill 속도 민감도(180s/0.2, 9조건 × 5seed, normal P95): 8,000 tok/s에서 기준선 271 / LRU offload 274 / T-LRU 328 / Evict 703ms. 4,000에서 1,101 / 1,104 / 1,832 / 4,398ms. 2,000에서 5,002 / 4,996 / 29,006 / 38,306ms.
- 어림 계산: GTX 1080 Ti의 FP32 연산 성능 약 11 TFLOPS, Llama-3.2-3B의 토큰당 연산량 약 6 GFLOP이면 prefill 이론 상한이 2,000 tok/s 안팎이다. 실측이 필요한 추정이다.
- `prefill_busy_pct`는 분모에 drain이 들어가 부하 구간의 사용률을 낮게 보여 준다(180s/0.2 기본: 11.7% 표시, 도착 구간 기준 약 17%).
- vLLM 문서(v0.7.2)는 GPU compute capability 7.0 이상을 요구한다. GTX 1080 Ti는 6.1이다.

## 환경을 축으로 놓으면 순위가 뒤집힌다

180s/0.2, 9조건 × 5seed 평균, CPU 64GiB. 같은 block 엔진의 정책만 비교. PCIe 12와 1.5 행은 발표된 hardware suite 값과 같다. r은 토큰당 reload 시간 ÷ 재계산 시간.

| PCIe (GB/s) | r | block LRU offload 복귀 P95 | HotPrefix 복귀 P95 | block LRU offload normal P95 | HotPrefix normal P95 |
|---:|---:|---:|---:|---:|---:|
| 12 | 0.08 | 182.6 | 272.2 | 275.0 | 289.7 |
| 3 | 0.31 | 287.6 | 342.4 | 297.5 | 310.3 |
| 1.5 | 0.61 | 517.8 | 498.4 | 366.9 | 357.8 |
| 0.9 | 1.02 | 875.7 | 739.2 | 534.6 | 445.8 |
| 0.6 | 1.53 | 1547.3 | 1111.7 | 909.9 | 633.0 |
| 0.3 | 3.06 | 5587.5 | 3273.7 | 4295.6 | 2160.6 |

GPU 전용 정책은 PCIe와 무관하다: T-LRU 복귀 432.2 / normal 328.4ms, block LRU evict 524.4 / 376.4ms, Evict/Recompute 1056.5 / 702.9ms. 기준선 169.4 / 270.7ms.

- 전송이 빠르면 항상 CPU에 보관하는 쪽이 이기고 HotPrefix의 선별은 손해다. PCIe 약 1~2GB/s 아래에서는 GPU 전용 T-LRU가 CPU 보관 정책을 앞선다. HotPrefix의 선별은 전송이 비쌀 때만 block LRU offload보다 낫다.
- 발표된 5seed 데이터에서도 1.5GB/s·180s/0.2에서 T-LRU가 block LRU offload보다 낮다(normal 5/5 seed, 복귀 4/5 seed). 180s/0.3에서는 다시 offload가 앞선다. 교차점은 부하에 따라 움직인다.
- 900초 실행(warm-up 300초, 5seed) 확인: base_4x는 1.5GB/s에서 T-LRU가 앞서고(복귀 −104ms, 4/5 seed), both_4x는 1.5GB/s에서 비슷하고 0.9GB/s에서 T-LRU가 앞선다(5/5 seed). 같은 실행에서 T-LRU와 block LRU evict의 차이는 both_4x에서 거의 사라진다(복귀 739 vs 762ms).
- CPU 축(PCIe 12GB/s): 4GiB부터 재계산이 생기고, 1GiB에서 block LRU offload 복귀 394.6 / HotPrefix 403.8ms로 T-LRU 432.2ms와 비슷해진다.
- 기존 엔진의 정책은 전송이 느려질수록 더 빨리 나빠진다(1.5GB/s 복귀 P95: LRU offload 809.7, Watermark 981.4ms). 회수 단위와 in-flight 처리가 다른 엔진이라 block 엔진 정책과의 직접 비교에는 엔진 차이가 섞인다.
- 10주차 비용 민감도는 격자가 1.5GB/s에서 끝나고(손익분기 계산값 약 0.92GB/s) normal TTFT와 통과 수만 비교해서 역전이 보이지 않았다.

## 개선할 점 (우선순위 순)

1. 환경을 축으로 놓고 같은 엔진의 정책을 한 그래프에서 비교한다. PCIe는 손익분기 아래까지, CPU 풀은 실제 초과 수요(수 GiB) 아래까지 내리고, 9조건 평균 대신 조건·부하별 교차점을 보고한다.
2. 주 지표를 바꾼다. Peak 90%, 상대 TTFT, 통과 수는 보조로 내린다. 메모리 무제한 기준선 대비 격차, 복귀 지연, 절대 SLO를 지키는 최대 세션 도착률(용량)을 주 지표로 쓴다. 일반 요청의 TTFT는 prefill 대기열이 지배하므로 KV 정책의 평가 지표로는 보조에 둔다.
3. 상수를 실측한다. 배정 서버에서 prefill·decode·GPU↔CPU 복사 속도를 재고, 부하를 절대 도착률 대신 prefill 사용률과 메모리 수요 비율로 정의한다.
4. 워크로드에 불확실성을 넣는다. 턴마다 종료 확률(미복귀), 도구 종류별 긴 꼬리 대기, 세션 간 공유 prefix.
5. 실행을 정상 상태로 만든다. 600~1,800초 이상, warm-up은 세션 수명의 몇 배, seed 간 짝비교와 구간 추정을 유지한다.
6. 한 지점은 실제 엔진으로 검증한다.

## 추가 실행의 한계

같은 시뮬레이터와 같은 자리표시자 상수를 썼다. 환경 축 표는 180s/0.2 한 설정의 5seed 평균이라 교차점 위치는 근사다. 추가 실행 스크립트와 결과는 세션의 임시 폴더에만 있고 저장소에는 넣지 않았다.

## 참고

- T-LRU: https://arxiv.org/html/2510.15152
- HotPrefix: https://rmarcus.info/dbscholar/papers/hdef9b04805eb320a (DOI 10.1145/3749168)
- vLLM GPU 요구사항: https://docs.vllm.ai/en/v0.7.2/getting_started/installation/gpu/index.html