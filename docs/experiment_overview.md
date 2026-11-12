# 실험 조건·정책 총정리: 10주차까지 / 11주차

정리 기준: 2026-11-12, 현재 저장소의 문서·설정·보존된 실행 manifest. 실행 횟수보다 **정책 비교 / 환경 변경 / 정책 파라미터 변경 / 기존 실행 분석**을 구분해서 읽는다. 이 문서는 실험 목록이며 결과·결론은 각 보고서를 따른다.

## 1. 전체 구성

| 구분 | 정책 구성 | 주로 변경한 것 |
|---|---|---|
| 7주차 기초 비교 | Pin-All, Evict/Recompute, Fixed TTL; 별도 노트북에서 LRU offload 추가 | 기본 도착률, TTL, 도구 대기 배율 |
| 9주차 비교 | 위 4개 + Duration-Aware = 5개 | 실행 기간·stress 도착률·burst/대기 규칙 |
| 10주차 비교 | 기존 5개 + Watermark offload + Capped Pin-All = 7개 | 입장 상한, PCIe, CPU 메모리 풀 |
| 11주차 초기 평가 | 기존 7개 재실행 + block 정책 4개 = 기본 설정 11개 | 부분 KV 회수·보관 방식, 환경, 정책 파라미터 |
| 11주차 보완 평가 | block 정책 4개 중심 + GPU 메모리 비제약 Pin-All 참조 | 더 넓은 환경 축, 긴 실행, 도착률, prefill 가정 |

**11주차 핵심 4개는 새 제안 2개와 비교 기준선 2개다.** T-LRU ↔ Block LRU evict, HotPrefix admission ↔ Block LRU offload를 우선 비교한다. 기존 엔진과 block 엔진 사이에는 회수 단위·진행 중 전송 계산·CPU 교체 차이도 있으므로 정책 효과와 구분한다.

## 2. 공통으로 알아둘 조건

### 기본 시스템·입력

별도 변경을 적은 실험 외에는 다음을 사용한다.

| 항목 | 값 |
|---|---|
| GPU KV 풀 | 4GiB |
| CPU KV 풀 | 64GiB; GPU에서 옮긴 KV를 저장할 RAM 공간 |
| PCIe | 단방향 12GB/s |
| Prefill / decode | 8,000 tokens/s / 세션당 30 tokens/s |
| block / KV 크기 | 16 tokens/block / 114,688 bytes/token |
| normal 최초 도착률 | 2 sessions/s |
| warm-up / 입장 대기 제한 | 10초 / 30초 |
| normal / stress 턴 수 | normal 1턴 / stress 3–5턴 |
| prompt | normal 128–512, stress 첫 턴 2,048–4,096, 후속 턴 64–256 tokens |
| output / 기본 도구 대기 | 32–128 tokens / 1–5초 |

이는 실제 GPU 측정값이 아닌 시뮬레이터 가정이다. 같은 trace/seed에서 정책을 바꿔 재생한다. 아래의 기간은 **최초 외부 도착 생성 기간**이며, 남은 세션을 처리하는 drain 때문에 시뮬레이션 종료는 더 늦을 수 있다.

### 반복 사용한 세 설정

| 이름 | 기간 | stress 최초 도착률 | 설정 |
|---|---:|---:|---|
| 60s/0.2 | 60초 | 0.2 sessions/s | [week7.json](../configs/week7.json) |
| 180s/0.2 | 180초 | 0.2 sessions/s | [week7_180s_stress02.json](../configs/week7_180s_stress02.json) |
| 180s/0.3 | 180초 | 0.3 sessions/s | [week7_180s_stress03.json](../configs/week7_180s_stress03.json) |

### 반복 사용한 9개 워크로드 조건

| 조건 | stress 최초 도착 | 도구 대기 |
|---|---|---|
| w0_1x | 원래 포아송 도착 | 기본 1–5초 |
| base_2x / base_4x | 원래 도착 | 기본 대기 ×2 / ×4 |
| burst_2x / burst_4x | 매 10초의 도착을 첫 1초로 압축 | 기본 대기 ×2 / ×4 |
| wait_2x / wait_4x | 원래 도착 | 기본 대기에 0.25배·4배를 교대로 적용한 뒤 ×2 / ×4 |
| both_2x / both_4x | burst 적용 | wait 규칙도 적용 |

**2x/4x는 도구 대기 배율이다. 도착률 0.2/0.3과 다른 축이다.** wait 규칙은 평균 대기시간도 바꾸므로 평균을 맞춘 순수 분산 비교가 아니다. 중간 도구 호출 뒤 복귀가 보장되며 세션 간 prefix 공유는 없다.

## 3. 10주차까지 사용한 정책

| 정책 | 코드명 | 요약 / 주 설정 |
|---|---|---|
| Pin-All | pin_all | 도구 대기 중 GPU KV 유지, 회수하지 않음 |
| Evict/Recompute | evict_recompute | 도구 대기 진입 시 삭제, 복귀 시 재계산 |
| Fixed TTL | fixed_ttl | 정한 시간 유지 후 삭제. 7주차 이후 주 평가는 3초, pressure eviction 비활성 |
| LRU offload | lru_offload | GPU 부족 시 오래 idle인 세션의 KV 전체를 CPU로 옮김. CPU에 못 넣으면 삭제 |
| Duration-Aware | duration_aware | GPU 여유·도구 대기·전송 비용으로 유지/이동/삭제. keep_headroom=30%, breakeven_factor=2 고정 |
| Watermark offload | watermark_offload | 기본 GPU 입장 상한 90%에서 LRU offload |
| Capped Pin-All | pin_all_capped | 같은 90% 입장 상한이지만 회수하지 않는 대조군 |

Duration-Aware는 실제 trace의 대기시간을 사용하는 oracle 참조이며, 30%는 idle 진입의 유지 판단 기준이다. Watermark처럼 70% 입장 상한을 강제하는 설정이 아니다. 초기 policies.json의 Fixed TTL 기본값은 30초지만, 7주차 이후 본 실험은 policies_week7.json의 3초를 쓴다.

### 실험 목록

| 실험 묶음 | 정책 | 실행 조건·변경값 | seed / 성격 | 기록 |
|---|---|---|---|---|
| 초기 W0·도착률 점검 | Pin-All, Evict/Recompute; 도착률 sweep은 Pin-All | smoke의 60초·stress 0.5/s W0, 도착률 후보 0.1/0.2/0.3/0.4/0.5 | 0–4; 초기 노트북 구성 | [W0 노트북](../notebooks/w0_baseline.ipynb) |
| 7주차 기초 정책 비교 | Pin-All, Evict, TTL; 별도 확장에 LRU 포함 | 60s/0.2, W0 1x·W1 2x·W1 4x | 0–4; 기본 45회, LRU 포함 구성 60회 | [7주차 문서](week7_experiment.md), [LRU 확장 노트북](../notebooks/week7_policy_steps.ipynb) |
| TTL 후보 비교 | Fixed TTL | 60s/0.2의 W0, TTL 1/3/5초 | 0–4; 15회 파라미터 비교 | [7주차 단계별 노트북](../notebooks/week7_step_by_step.ipynb) |
| 9주차 본 비교 | 기존 5정책 | 세 설정 × 9조건 | 0–4; 675회 | [9주차 실험](week9_experiment.md) |
| 9주차 상세 원인 분석 | 같은 5정책 | 위 675회를 같은 조건으로 재생; Peak 소유·입장/prefill 대기·복귀 분해 | 새 조건·seed 추가 없음; 재현·관측 분석 | [원인 분석](week9_root_cause_analysis.md) |
| 10주차 입장 상한 비교 | 기존 5정책 + Watermark 90% + Capped Pin-All 90% | 세 설정 × 9조건, 기본 시스템 | 주 평가 0–4, 945회; 문서에 5–14 추가 검증도 기록 | [10주차 본 실험](week10_experiment.md) |
| 10주차 PCIe·CPU 비용 민감도 | Watermark·LRU offload·Duration-Aware; 참조는 Evict·TTL·Pin-All | **180s/0.2만**, 9조건. PCIe 12/6/3/1.5GB/s × CPU 64/4/2/1GiB의 **16조합** | 0–14; offload 6,480회 + 참조 405회 | [비용 민감도](week10_sensitivity.md) |
| 10주차 Watermark 상한 민감도 | Watermark의 상한 5변형 + LRU offload | 세 설정 × 9조건, 상한 80/85/90/95/100%, 기본 시스템 | 0–14; 2,430회 | [상한 변경](week10_watermark_ceiling.md) |

초기 W0 노트북은 현재 실행 출력이 보존되어 있지 않으므로 그 구성만 기록했다. 7주차 단계별·LRU 확장 노트북은 실행 출력이 보존돼 있다. 초기 항목을 이번에 다시 실행한 것으로 서술하지 않는다.

Watermark 100%는 기존 LRU와 동일한 replay를 내는 대조점이다. 상한 5개가 서로 다른 새 정책 5개라는 뜻은 아니다. 9주차 원인 분석도 새 정책 비교 실험으로 중복 계산하지 않는다.

## 4. 11주차 정책과 초기 평가

### 새 block 엔진의 핵심 4개

| 정책 | 코드명 | 역할 |
|---|---|---|
| Block LRU evict | prefix_lru_evict | 필요한 block만 LRU 순서로 삭제하는 GPU-only 기준선 |
| T-LRU | tlru | 예상 prefill 예산에 맞춰 회수할 suffix를 우선 선택. 기본 200ms, 다음 prompt 추정 160 tokens |
| Block LRU offload | prefix_lru_offload | 같은 GPU block LRU 순서에서 CPU 보관을 시도하는 기준선 |
| HotPrefix admission | hotprefix_admission | GPU 순서는 LRU로 고정하고 CPU 보관을 빈도·hotness로 선별. 기본 F=2, clock=15, 100 prefill마다 aging |

메모리 할당은 기존에도 16-token block이었다. 11주차의 차이는 **세션 전체가 아니라 필요한 block만 회수하고 나머지를 재사용하는 엔진**을 추가한 것이다. 기존 7정책의 엔진은 보존했다.

### 초기 7,290회

| 실험 묶음 | 비교 정책 | 조건 | seed / 실행 수 |
|---|---|---|---|
| 주 평가 | 기존 7개 + block 4개 = 11개 | 기존 세 설정 × 9조건, 기본 시스템 | 0–14; 4,455회 |
| 환경 민감도 | block 4개 + 기존 LRU offload 참고값 | 세 설정 × 9조건. 기본 외 **1.5GB/s·64GiB, 12GB/s·1GiB, 1.5GB/s·1GiB** | 0–4; 추가 3환경 2,025회. 기본 환경은 주 평가의 같은 5seed를 참조 |
| 정책 파라미터·구성 비교 | T-LRU, HotPrefix의 변형 | 세 설정 × 9조건, 기본 시스템 | 0–4; 810회 |

파라미터·구성 비교의 추가 6변형은 다음과 같다. 기본값은 주 평가에서 가져오므로 810회에 다시 포함하지 않는다.

- T-LRU: prefill 예산 100ms / 400ms. 기본 200ms와 비교.
- HotPrefix: 빈도 기준 F=1 / F=3. 기본 F=2와 비교.
- HotPrefix: GPU 회수 순서를 hotness로 바꾸는 변형.
- HotPrefix: 선택적 CPU admission을 끄는 대조군.

결과: [11주차 통합 보고서](week11_experiment.md), [초기 정책 비교 노트북](../notebooks/week11_policy_review.ipynb), [원자료 manifest](data/week11/experiment.json).

## 5. 11주차 보완 평가

핵심 block 4개를 같은 엔진에서 비교하고, 필요한 곳에 **GPU KV 풀 64GiB·Pin-All 비제약 참조**를 추가했다. GPU 64GiB는 CPU 풀 64GiB와 다른 설정이다. 아래 실험 묶음의 모든 축을 한꺼번에 전부 교차시킨 것은 아니다.

| 실험 묶음 | 정책 | 실제 조건 | 실제 추가 replay 수 |
|---|---|---|---:|
| 비제약 메모리 참조 | Pin-All | 기존 세 설정 × 9조건 × seed 0–14. **GPU** 풀만 64GiB | 405 |
| PCIe·CPU 축 확장 | 핵심 block 4개 | **180초**, stress 0.2/0.3, **base_4x·both_4x만**, seed 0–4. PCIe 12/3/1.5/0.9/0.6/0.3GB/s에서 CPU 64GiB 고정. CPU 64/8/4/2/1/0.5GiB에서 PCIe 12GB/s 고정 | 480 |
| 긴 실행 재확인 | block 4개 + 비제약 참조 | **900초·warm-up 300초**, stress 0.2/0.3, base_4x·both_4x, seed 0–4. PCIe 12/1.5/0.9GB/s, CPU 64GiB | 180 |
| 도착률 격자의 추가 지점 | 같은 정책 | 긴 실행의 **both_4x**에 stress **0.1/0.4** 추가. 0.2/0.3은 위 장기 결과와 공유. 같은 3개 PCIe에서 실행하고, 용량 표는 12/0.9GB/s를 사용 | 90 |
| prefill 가정 민감도 | block 4개 + 비제약 참조 | **180s/0.2**, base_4x·both_4x, seed 0–4. prefill 8,000/4,000/2,000 tokens/s, 기본 PCIe·CPU | 150 |
| 합계 | | | **1,305** |

환경 축 확장은 6×6 전체 교차가 아니라 **각각 한 축씩 변경**한 설계다. CPU를 쓰지 않는 T-LRU·block evict는 같은 trace·prefill에서 한 번 실행하고 환경별 분석 행에 재사용했다. 따라서 실제 실행은 1,305회, 분석 표는 1,825행이다.

도착률 평가의 예시 기준은 normal·복귀 P95 각각 500ms 이하 또는 각각 1,000ms 이하, stress 완료율 ≥99%, normal 거절률 ≤1%다. 모든 seed가 통과한 최대 **시험 격자점**을 보고하며, 확정된 실제 서비스 SLO나 연속적 최대 용량으로 해석하지 않는다.

결과: [11주차 통합 보고서](week11_experiment.md), [평가 재검토 노트북](../notebooks/week11_evaluation_review.ipynb), [추가 manifest](data/week11_revision/experiment.json).

## 6. 실험과 구분할 분석·검증

| 항목 | 성격 |
|---|---|
| 9주차 Peak 소유·입장/prefill 대기 분석 | 기존 675회 재생에서 추가 관찰 |
| 11주차 복귀 지연 분해 | 실제 P95 요청의 offload 잔여·입장·복원·prefill 대기·자기 계산 성분 분석 |
| HotPrefix 빈도/hotness 거절·CPU 교체 집계 | 위 평가 중 발생한 결정의 메커니즘 분석 |
| 기존 SLO 실패·비제약 참조 격차 재분석 | 기존/추가 데이터의 지표 해석 |
| 테스트, 기존 945회 fingerprint 비교, 선택적 admission OFF 대조 | 정합성·회귀 검증. 별도 정책 성능 기여나 독립 표본 추가로 세지 않음 |
| 11주차의 세 설정 평균 그래프 | 기존 설정별 값의 같은 가중치 평균. 새로운 네 번째 워크로드 실험이 아님 |

seed 0–4는 5번 반복, 0–14는 15번 반복을 뜻한다. 같은 seed·같은 조건의 재실행을 독립적인 새 표본으로 합산하지 않는다. 여러 주차의 실행 횟수도 겹치는 정책·입력·검증 재생이 있어 독립 실험 수처럼 단순 합계하지 않는다.

## 7. 주차에 따라 달라진 평가의 중심

| 구분 | 주로 본 지표 |
|---|---|
| 7–10주차 및 초기 기록 | 정책별 W0 대비 normal P95 3배, normal 거절률 10%, GPU Peak 90%의 기존 SLO; 전송·재계산·지연도 함께 기록 |
| 11주차 보완 | 실제 복귀 P95, 비제약 참조와의 격차, 완료율·거절률, 예시 절대 지연 도착률 격자. normal TTFT·기존 상대 SLO·Peak는 보조 |

## 8. 완료된 실험과 섞지 않을 항목

- Dynamic TTL·InferCept 정책은 도입·평가하지 않았다. Dynamic TTL은 미구현 stub이다.
- Duration-Aware의 30% headroom을 변경하는 별도 민감도 실험은 현재 목록에 없다.
- 실제 GPU 처리량 보정·실제 서빙 엔진 검증, 미복귀 생명주기, 공유 prefix는 후속 작업이다.
- 외부 평가 문서의 임시 복제본에서 실행한 600/1,800/3,600초 등의 결과는 이번 저장소의 보존된 week11 보완 실행 목록에 포함하지 않는다.
- HotPrefix는 정책 일부를 세션별 block 체인에 적용한 것이며 전체 논문 시스템의 성능 재현으로 부르지 않는다.
