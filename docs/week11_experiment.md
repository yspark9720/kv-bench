# 11주차: 정책 구현부터 평가 재검토까지

2026-11-12 기준. 정책 설명·계획·초기 결과·보완 평가·후속 검증을 이 문서로 통합했다. 읽는 순서는 **비교 목적 → 정책과 조건 → 초기 관측 → 평가 피드백 → 추가 검증 → 조건별 판단과 다음 작업**이다.

| 읽을 파일 | 역할 |
|---|---|
| [정책 비교 노트북](../notebooks/week11_policy_review.ipynb) | 초기 7,290회의 정책 비교·환경 민감도·파라미터 비교 |
| [평가 재검토 노트북](../notebooks/week11_evaluation_review.ipynb) | 피드백에 따른 기존 결과 재분석과 추가 1,305회 평가 |
| [전체 실험 목록](experiment_overview.md) | 10주차까지와 11주차의 조건·정책을 한 번에 확인 |

## 1. 무엇을 확인하려는가?

도구를 기다리는 Agent의 KV를 **GPU에 얼마나 남길지**, GPU에서 회수한 KV를 **CPU에 보관할지 삭제할지**를 비교한다. 주어진 환경과 부하에서 복귀 지연·전송·재계산 비용의 유불리를 확인하는 것이 목적이다.

초기 비교는 T-LRU와 HotPrefix의 정책 일부를 기존 워크로드에 적용하는 단계였다. 이후 평가 점검을 반영해 “새 정책이 좋은가?”에서 **“어떤 조건에서 어떤 보관 방식이 유리하며, 그 판단을 어떤 지표로 검증할 것인가?”**로 질문을 구체화했다.

모든 결과는 고정 처리량·FIFO를 사용하는 시뮬레이션이다. 실제 GPU 처리량·생성 품질 또는 원 논문 전체 시스템의 성능 재현으로 해석하지 않는다.

## 2. 비교 정책과 구현 범위

| 같은 block 엔진의 정책 | GPU 공간이 부족할 때 | CPU와 복귀 처리 |
|---|---|---|
| **Block LRU evict** | 오래 idle인 세션의 suffix부터 필요한 block만 삭제 | CPU를 사용하지 않고 누락 토큰 재계산 |
| **T-LRU** | 예상 prefill 예산에 맞는 suffix 우선 회수, 부족하면 block LRU | 남은 prefix 재사용 + 누락 토큰 재계산 |
| **Block LRU offload** | 같은 GPU block LRU 순서로 회수 | CPU 보관 시도; 부족하면 회수 가능한 CPU LRU block 교체 |
| **HotPrefix admission** | 기본 GPU 순서는 같은 block LRU | 빈도·hotness로 CPU 보관/교체 여부를 선별; 거절한 KV는 삭제 |

핵심 짝비교는 **T-LRU ↔ Block LRU evict**, **HotPrefix admission ↔ Block LRU offload**다. 여기서 CPU admission 거절은 KV를 CPU에 넣지 않는 결정이며 사용자 요청 거절과 다르다.

기존 **Pin-All, Evict/Recompute, Fixed TTL, LRU offload, Duration-Aware, Watermark offload, Capped Pin-All**도 초기 주 평가에 포함했다. 기존 정책은 세션 전체 회수 엔진을 유지한다. 기존 엔진과 새 엔진에는 회수 단위·진행 중 전송의 부족량 계산·CPU 포화 시 교체 방식 차이가 있어, 엔진 간 차이를 새 정책 하나의 효과로 합치지 않는다. Duration-Aware는 실제 도구 대기시간을 쓰는 oracle 참조다.

### 기본값과 중요한 구현 규칙

- **T-LRU:** 자기 prefill 예산 200ms, 다음 prompt 추정 160 tokens. 실제 미래 prompt를 읽지 않는다. 계산식은 아래와 같으며 block 경계에서 보관량을 올림한다.

  ~~~text
  xi = floor(budget_ms × prefill_tokens_per_s / 1000)
  keep_blocks = ceil(max(history_tokens + estimated_prompt - xi, 0) / block_size)
  ~~~

  해당 경계 뒤의 GPU suffix를 먼저 회수한다. 예산은 입장·전송·대기열을 포함한 전체 TTFT 보장이 아니다.
- **HotPrefix:** 기본 빈도 F=2. block을 한 턴에서 한 번 접근한 것으로 기록한다. 최근성 clock은 사용 시 15로 reset, 100 prefill 완료마다 1 감소한다. 15는 논문 예시, 100회는 사전에 정한 재현 설정이다. CPU 후보는 빈도 기준과 resident 최소 frequency×clock 비교를 통과해야 하며 공간이 있어도 거절될 수 있다. GPU hotness 순서는 별도 변형이다.
- **공통:** 입장 때 턴의 context+output 공간을 예약하고, TOOL_WAIT의 KV만 회수한다. 실행·입장 완료 요청·전송 중 자원은 보호한다. offload 완료 시 GPU를 반환하고, 새 block 엔진은 이미 전송 중인 반환 예정 block을 부족량에서 제외한다.
- **복귀:** GPU·CPU·삭제 상태가 섞여도 남은 KV를 재사용하고 없는 토큰만 재계산한다. copy 중 도구가 끝났을 때 요청 등록 전에 발생한 잔여 offload 대기도 복귀 지연에 포함한다.

코드: [정책](../kvbench/policies/prefix.py), [block 엔진](../kvbench/prefix_engine.py), [복귀 지표](../kvbench/week11.py). 근거: [T-LRU Algorithm 1](https://arxiv.org/html/2510.15152v1#S4), [HotPrefix 저자 PDF](https://cs.nju.edu.cn/tianchen/lunwen/2026/sigmod26-liyuhang.pdf). HotPrefix의 선제 promotion·CUDA pipeline·cuckoo filter 성능·세션 간 prefix 공유는 구현 범위 밖이다.

## 3. 실험 조건과 평가 방법

기본은 GPU KV 4GiB·CPU KV 64GiB·PCIe 12GB/s·prefill 8,000 tokens/s·세션당 decode 30 tokens/s다. normal 도착률 2/s, warm-up 10초, 입장 대기 제한 30초다. stress는 3–5턴이며 중간 도구 호출 뒤 반드시 복귀한다.

반복 사용한 세 설정은 **60초·0.2/s, 180초·0.2/s, 180초·0.3/s**다. 9조건은 W0와 base/burst/wait/both 각각 2x/4x다. 2x/4x는 도구 대기 배율이다. 모든 설정을 모든 확장 축과 교차한 실험은 아니다.

| 단계 | 조건 | 실행 |
|---|---|---:|
| 초기 주 평가 | 세 설정 × 9조건 × 15seed × 기존 7개+block 4개 | 4,455 |
| 초기 환경 민감도 | 세 설정 × 9조건 × 5seed; PCIe 1.5GB/s·CPU 64GiB, 12GB/s·1GiB, 1.5GB/s·1GiB; block 4개+기존 LRU 참고값 | 2,025 |
| 초기 파라미터 비교 | 세 설정 × 9조건 × 5seed; T-LRU 100/400ms, HotPrefix F=1/3·GPU hotness·CPU selective OFF | 810 |
| 비제약 참조 | 기존 세 설정 × 9조건 × 15seed; GPU 64GiB·Pin-All | 405 |
| 환경 축 확장 | 180초·0.2/0.3/s, base_4x·both_4x, 5seed. PCIe 12/3/1.5/0.9/0.6/0.3GB/s 또는 CPU 64/8/4/2/1/0.5GiB를 각각 변경 | 480 |
| 긴 실행과 도착률 격자 | 900초·warm-up 300초, base_4x·both_4x의 0.2/0.3/s; both_4x에 0.1/0.4 추가. 5seed, PCIe 12/1.5/0.9GB/s·CPU 64GiB | 270 |
| prefill 가정 민감도 | 180초·0.2/s, base_4x·both_4x, 5seed. 8,000/4,000/2,000 tokens/s | 150 |

초기 합계 **7,290회**, 추가 합계 **1,305회**다. CPU를 쓰지 않는 정책은 같은 입력에서 한 번 실행해 환경 행에 재사용하므로 추가 분석 표는 1,825행이다. 재사용 행을 독립 실행으로 세지 않는다.

### 지표를 읽는 기준

- **복귀 지연:** 실제 도구 종료→다음 첫 토큰. 초기 지표는 warm-up 이후 최초 도착한 세션 cohort, 장기 window 지표는 도구 종료가 [300,900)초인 복귀 요청이다. 두 분모를 구분한다.
- **비제약 참조 격차:** 같은 계산/FIFO에서 GPU KV 풀만 충분히 늘린 Pin-All과 비교한다. 이론적 최저 지연을 보장하는 하한은 아니다.
- **완료율·거절률:** P95는 완료 요청만의 값이므로 함께 읽는다. 계획된 후속 턴과 완료 턴 수도 보존한다.
- **비용·사용률:** 전송 bytes·재계산 tokens를 보고한다. 추가 사용률은 서비스 예약 구간을 측정 창에 잘라 계산한다. 비용 누계·세션 cohort 귀속 비용·drain 포함 지표를 섞지 않는다.
- 평균은 run별 P95의 평균이며 pooled P95가 아니다. 짝차이 CI는 seed 단위 bootstrap 2,000회다. 5seed와 다중 비교의 불확실성이 남는다.

## 4. 초기 평가에서 관측한 것

아래는 기본 환경·9조건×15seed의 복귀 P95 평균이다. 단위 ms.

| 설정 | Block evict → T-LRU | Block offload → HotPrefix |
|---|---:|---:|
| 60초·0.2/s | 272.5 → 209.3 | 125.9 → 170.7 |
| 180초·0.2/s | 547.3 → 450.2 | 184.1 → 280.2 |
| 180초·0.3/s | 974.3 → 895.3 | 311.4 → 470.9 |

180초·0.2/s에서 T-LRU는 같은 block evict 대비 복귀 지연 17.7%·재계산량 7.8%를 줄였다. HotPrefix는 같은 block offload 대비 전송량 20.5%를 줄였지만 복귀 지연은 52.2% 늘었다. 두 새 기본 정책은 초기 주 평가·초기 hardware 평가에서 stress 완료율 100%, normal 거절률 0%였다.

이 관측만으로 빠른 offload보다 GPU-only가 낫다거나 선택적 CPU 보관이 항상 유리하다고 할 수 없다. 초기 180초·0.2/s의 5seed 파라미터 비교에서도 HotPrefix F=1/2/3은 전송 13.59/10.74/2.91GiB, 복귀 182.6/272.2/460.8ms였다. 기본 T-LRU 200ms의 좋은 평균을 전역 최적값으로 선정한 실험도 아니다.

## 5. 평가 피드백으로 바꾼 질문

[검토 당시 피드백 원문](week11_feedback.md)을 보존했다. 아래는 해당 문제 제기에 대해 이번 평가에서 반영한 내용과 남은 검증을 구분한 것이다.

| 점검 내용 | 반영한 것 | 남은 경계 |
|---|---|---|
| 기본 offload가 이미 좋은 환경인가? | 비제약 참조 대비 격차 | 다른 계산 모델·실측 환경까지의 하한은 아님 |
| 평균이 정책 역전을 가리는가? | 조건·부하별 PCIe/CPU 곡선과 짝차이 | 모든 9조건을 확장 격자에서 재실행한 것은 아님 |
| normal TTFT·Peak가 정책을 구분하는가? | 복귀·참조 격차 중심, 기존 SLO 재분석 | 실제 서비스의 절대 SLO 확정 필요 |
| 짧은 실행의 초기 상태 영향인가? | 900초·300초 warm-up, 전반/후반 비교 | 정상 상태 완전 입증은 아님 |
| 정책의 판단이 실제 작동했는가? | 빈도/hotness 거절·CPU 교체·구간 사용률 | 미복귀·공유 prefix 미도입 |
| 상수·엔진이 현실을 반영하는가? | 처리량 가정 민감도 | 하드웨어 실측·실제 엔진 검증 미수행 |

기존 7정책도 비제약 격차·SLO 실패 재분석에 포함했다. 확장 환경·장기·prefill 추가 실행의 중심은 block 4개와 비제약 Pin-All 참조다. 기존 Watermark·Duration-Aware까지 확장 격자에서 재평가했다고 주장하지 않는다.

## 6. 추가 평가에서 확인한 결과

### 6.1 기본 offload에 남은 개선 여유

| 기존 설정 | 비제약 참조 복귀 P95 | Block offload 복귀 P95 | 격차 |
|---|---:|---:|---:|
| 60초·0.2/s | 113.4 | 125.9 | 12.4 |
| 180초·0.2/s | 166.2 | 184.1 | 17.9 |
| 180초·0.3/s | 279.6 | 311.4 | 31.7 |

기본 환경은 단순 offload에 남은 개선 여유가 작았다. 기존 평가의 실패 중 Peak를 포함한 비율은 88.3%, Peak만 실패한 비율은 73.8%였다. 기존 LRU의 180초·0.2/s normal P95 270.1ms 중 prefill 대기는 229.6ms였다. 이런 이유로 normal TTFT·기존 상대 SLO를 단독 주 지표로 해석하지 않는다.

### 6.2 전송 환경·부하에 따라 유리한 정책이 바뀐다

예시로 **900초·warm-up 300초, base_4x·0.3/s, CPU 64GiB**의 복귀 window P95를 비교한다. 단위 ms, 5seed 평균이다.

| PCIe | Block evict | T-LRU | Block offload | HotPrefix |
|---|---:|---:|---:|---:|
| 12GB/s | 1,532.9 | 1,406.0 | **277.9** | 626.0 |
| 1.5GB/s | 1,532.9 | 1,406.0 | 1,396.2 | **1,076.0** |
| 0.9GB/s | 1,532.9 | **1,406.0** | 5,734.4 | 2,305.7 |

이 사례는 모든 seed에서 stress 완료율 100%·normal 거절률 0%였다. 1.5GB/s의 HotPrefix−T-LRU 차이는 −330.0ms [95% CI −489.7, −176.4]로 5seed 모두 HotPrefix가 낮았다. 다른 조건에서도 같은 순위라고 일반화하지 않는다.

PCIe/CPU별 전체 곡선은 평가 재검토 노트북에 있다. 교차점은 평균 짝차이 부호가 바뀐 **인접 시험 구간**으로 보고하며 정확한 경계값을 보간하지 않는다. 극단적인 0.3GB/s의 일부 block offload 실행에서는 요청 거절이 발생했고, 해당 점은 그래프에 ×로 표시한다.

### 6.3 T-LRU의 장기 효과도 조건별로 봐야 한다

900초·warm-up 300초, 같은 block evict 대비 T-LRU의 복귀 window P95 차이다. 음수가 개선이며 단위 ms다.

| 조건 | 도착률 | 평균 차이 | 95% CI |
|---|---:|---:|---|
| base_4x | 0.2/s | −109.8 | [−196.8, +44.0] |
| base_4x | 0.3/s | −126.9 | [−187.3, −46.9] |
| both_4x | 0.2/s | −36.3 | [−51.7, −21.7] |
| both_4x | 0.3/s | −86.9 | [−191.1, −25.4] |

초기 9조건 평균의 17.7%를 장기 조건의 공통 개선율로 쓰지 않는다. 최초 도착 cohort와 도구 종료 window 지표를 모두 보존했다. [300,600)·[600,900) 차이도 보고하며, 900초 도착 종료 직전에는 drain 경계 영향이 남을 수 있다.

### 6.4 큰 CPU에서는 HotPrefix의 빈도 필터가 지배했다

확장 평가의 기본 환경에서 두 조건·두 도착률×5seed를 합치면 CPU admission 221,854건 중 거절은 56,108건이고 전부 frequency 사유였다. hotness 거절·CPU 교체는 0건, 빈도 거절의 약 76.8%는 첫 도구 대기에서 나왔다. 이는 해당 20개 replay의 진단이며 기존 9조건 전체의 수치가 아니다.

현재 입력은 중간 도구 호출 뒤 반드시 돌아온다. 첫 접근이라 빈도가 낮은 KV도 곧 필요할 수 있으므로, 과거 빈도와 미래 가치가 잘 맞는 환경인지 별도 확인해야 한다. CPU가 작을 때의 선별·교체 동작은 추가 표로 구분한다.

### 6.5 도착률·처리량은 아직 모형 안의 조건부 평가다

both_4x의 900초 실행에서 도착률 0.1/0.2/0.3/0.4를 시험했다. 예시 기준은 normal·복귀 P95 각각 500ms 또는 1,000ms 이하, stress 완료율 ≥99%, normal 거절률 ≤1%다.

1,000ms 기준에서 **5seed 모두 통과한 최대 시험 도착률**은 다음과 같다.

| PCIe | Block evict | T-LRU | Block offload | HotPrefix |
|---|---:|---:|---:|---:|
| 12GB/s | 0.2/s | 0.2/s | 0.3/s | 0.3/s |
| 0.9GB/s | 0.2/s | 0.2/s | 0.1/s | 0.1/s |

이는 시험 격자점이며 실제 서비스의 확정 SLO나 연속적 최대 용량이 아니다. 통과점이 없는 경우도 용량 0을 뜻하지 않는다.

Prefill 8,000/4,000/2,000 tokens/s는 비제약 참조까지 같은 속도로 재실행했다. 예를 들어 both_4x·180초·0.2/s에서 비제약 복귀 P95는 47.8→810.8→4,903.0ms였다. 기본 계산 가정의 영향이 크므로 2,000도 실측값으로 채택하지 않는다.

## 7. 정책별 판단과 다음 검증

| 정책 | 현재 근거에서 가능한 판단 | 다음에 필요한 검증 |
|---|---|---|
| Block LRU evict | CPU를 쓰지 않는 단순 부분 회수 기준선 | workload·계산 부하별 재계산과 대기열 비용 |
| T-LRU | 같은 GPU-only 기준선 대비 조건별 개선, 느린 전송에서 유리한 영역 | 세션 길이·재사용 여부·예산 가정에 대한 일반화 |
| Block LRU offload | 빠른 PCIe·충분한 CPU에서는 참조값에 가까움 | 실측 전송 속도·copy overlap·작은 CPU에서의 비용 |
| HotPrefix admission | 기본 환경에서는 지연 손해, 일부 중간 전송·높은 부하에서 이득 | 미복귀·공유 prefix·재사용 편중에서의 선별 가치 |
| 기존 7정책 | 초기 결과와 참조 격차·SLO 재해석까지 | 확장 조건에서 결론이 필요하면 기존 엔진 안에서 별도 비교 |

다음 우선순위는 **실측 보정 → 대표 역전 구간 재확인 → 재사용 불확실성 → 실제 엔진 한 지점 검증**이다.

- **보정:** GPU·모델 revision·정밀도·batch·문맥 길이를 기록하고 prefill/decode·양방향 복사를 측정한다. warm-up·동기화 후 길이별 service 시간과 반복 분포를 남긴다. 전송 GB/s와 메모리 GiB를 구분한다. 부하는 참조 prefill 사용률과 평균 GPU 수요/4GiB도 함께 맞춘다.
- **입력:** 미복귀·도구별 긴 꼬리·공유 prefix를 검증하려면 생명주기·공유 참조 수까지 정의해야 한다. 지금처럼 마지막 턴에서 즉시 KV를 해제하는 모델에서 후속 턴을 단순 삭제하는 것은 미복귀 KV 잔존 실험의 대체가 아니다.
- **현실 검증:** 지원 가능한 실제 엔진에서 같은 길이·도착·대기 조건 한 지점을 측정하고 예약·batching·전송 overlap 차이를 설명한다. 아직 수행하지 않았다.

현재 입력에도 도착·대기·길이의 무작위성은 있다. 부족한 것은 특히 재사용 여부와 세션 간 공유의 불확실성이다. normal TTFT도 재계산이 공유 FIFO에 미치는 간접 영향을 반영하므로 보조 지표로 유지한다. Dynamic TTL·InferCept는 구현·평가하지 않았다.

## 8. 재현과 기록

- [초기 원자료](data/week11): summary·paired·hardware·실행 manifest. [추가 원자료](data/week11_revision): 참조 격차·조건별 짝차이·기제·안정성·용량 격자·실행 manifest.
- 실험 당시 계획과 문서 정리 전 runner는 [provenance/index.json](data/week11_revision/provenance/index.json)에 hash와 함께 보존했다. 실험 manifest·원자료는 수정하지 않았다. 현재 simulator/config는 직접 hash를 검증하고, 이동한 계획·runner의 당시 내용은 보존본으로 검증한다.
- 기존 7정책 945회 fingerprint 동등성, 무작위 자원 보존, 미래 정보 비사용, 선택적 보관 OFF 대조를 초기 평가에서 확인했다. 추가 관측기의 replay 동등성과 구간 사용률도 검증했다. [초기 검증](data/week11/regression.json), [추가 검증](data/week11_revision/validation.json), [테스트 기록](data/week11_revision/regression.json).
- 원시 trace·요청·이벤트는 로컬 results에 있고 Git에서는 제외한다. 재실행은 새 출력 디렉터리를 사용한다. 노트북 재생성은 저장된 CSV·그림을 읽으며 시뮬레이션을 다시 돌리지 않는다.

~~~powershell
# 저장된 결과로 두 노트북 생성
python scripts/build_week11_notebook.py
python scripts/build_week11_evaluation_notebook.py

# 전체 실험을 새로 실행할 때만
python scripts/run_week11.py --output results/week11_new --workers 4
python scripts/report_week11.py results/week11_new
python scripts/run_week11_revision.py --output results/week11_revision_new --workers 4
python scripts/report_week11_revision.py results/week11_revision_new

python -m unittest discover -s tests -v
~~~
