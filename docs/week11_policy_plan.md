# 11주차 정책 구현·평가 계획

## 범위와 재현 수준

- T-LRU: 논문 Algorithm 1의 TEL-safe 우선 회수와 LRU fallback을 block 단위로 구현한다. 현재 엔진의 active 요청은 회수하지 않는 제약을 모든 새 기준선에 동일하게 적용한다.
- HotPrefix: §3.2의 접근 빈도·aging clock과 §3.3의 GPU 회수·선택적 CPU admission을 구현한다. 기본 비교는 GPU 회수 순서를 LRU로 고정해 CPU admission 효과를 분리한다. GPU hotness 순서도 별도 ablation으로 제공한다.
- HotPrefix §3.4의 선제 승격·CUDA pipeline, 서로 다른 세션 사이의 prefix 공유 및 실제 GPU kernel은 이번 범위가 아니다. 전체 HotPrefix 시스템 성능 재현이라고 부르지 않는다.
- exact dictionary와 세션 내부 append-only block chain을 사용한다. Cuckoo-filter의 확률적 metadata 오차를 흉내 내지 않는다.

## 공통 실행 모델

기존 7개 정책의 엔진을 보존하고 새 prefix 엔진을 별도로 둔다. 새 정책끼리는 같은 block 크기, FIFO 입장, GPU 예약, PCIe 서버, active 보호, CPU 용량/전송 비용을 쓴다. 세션별 suffix를 회수하며 남은 prefix와 CPU copy만 복원하고 없는 토큰만 재계산한다. 메모리는 offload 완료 시 해제한다. 전송 중 복귀 및 CPU 포화도 검증한다.

T-LRU와 비교할 block LRU evict, HotPrefix와 비교할 block LRU offload를 함께 구현한다. 기존 whole-session LRU는 추가 참조이며 회수 단위가 다른 결과를 정책 효과로 혼동하지 않는다. 기존 정책의 in-flight 회수 동작도 바꾸지 않는다.

## 사전 고정 설정

- 주 실험: 기존 60s/0.2, 180s/0.2, 180s/0.3 설정 × 기존 9개 W0/W1/W2 조건 × seed 0–14.
- 기존 정책 7개, block LRU evict/offload, T-LRU, HotPrefix admission을 평가한다.
- T-LRU prefill budget 200ms, 다음 prompt 추정 160 tokens(설정 범위 64–256의 중간값). 100/400ms를 민감도로 추가한다. 현재 trace의 미래 prompt를 읽지 않는다.
- HotPrefix frequency threshold 2, max_age 15. 논문의 aging 주기는 구체적인 요청 수가 명시되지 않아 100 completed-prefill requests를 명시적 재현 설정으로 사용한다. threshold 1/3과 GPU hotness 순서를 ablation으로 확인한다.
- 환경 민감도: 기본 12GB/s·64GiB 외 1.5GB/s·64GiB, 12GB/s·1GiB, 1.5GB/s·1GiB. 주 실험과 구분하고 seed 0–4로 평가한다.
- 모든 새 정책의 GPU 입장 상한은 100%. Watermark의 90% SLO 통과 수로 우수성을 주장하지 않는다.

## 지표와 판정

normal P95 TTFT, 실제 TOOL_END→FIRST_TOKEN 복귀 P95, stress 작업 P95·완료율·거절률, 전송 bytes, 재계산 tokens, 평균·최대 GPU 점유를 보고한다. 복귀 지연은 전송 중 복귀의 잔여 offload 시간도 포함한다. 지연 성분은 같은 요청을 기준으로 분해한다. 완료한 요청의 지연과 거절/완료 수를 함께 제시한다. 비용은 전체 run 누계와 측정 cohort 귀속 비용을 구분한다.

효과는 같은 trace/seed에 대한 차이로 평가하고 seed 단위 CI를 제공한다. threshold 민감도에서 좋은 값을 찾았다는 이유로 기본값을 사후 변경하지 않는다. 현재 trace는 중간 tool call 뒤 반드시 복귀하므로 '다시는 쓰지 않을 KV 검출'은 검증할 수 없다. 모델의 예약·FIFO 대기가 policy 목표를 가릴 가능성도 그대로 보고한다.

## 검증

수작업 예제의 부분 회수·복원 토큰/전송량, TEL-safe 및 LRU fallback, aging·CPU threshold/hotness 비교, CPU 포화·진행 중 복귀·요청 timeout, 자원 반환·결정성·미래 입력 비사용, CLI/출력·기존 정책 fingerprint 회귀 검증 후 본 실험을 실행한다.

## 근거

- T-LRU: https://arxiv.org/html/2510.15152v1#S4
- SGLang 구현: https://github.com/sgl-project/sglang/blob/main/python/sglang/srt/mem_cache/evict_policy.py
- HotPrefix: https://doi.org/10.1145/3749168 ; 저자 PDF https://cs.nju.edu.cn/tianchen/lunwen/2026/sigmod26-liyuhang.pdf
