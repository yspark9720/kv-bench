# W2 요인 분리와 정책 진단

이 실험은 기존 정책을 변경하지 않고 W2 결과의 원인을 점검한다. 실제 LLM 추론은 수행하지 않는다.

이 문서는 조건 분리와 실행 방법을 설명한다. 세 설정의 실제 수치, 정책별 차이와 프로젝트 방향은 [9주차 실험·분석](week9_experiment.md)에 정리한다.

## 비교 조건

같은 seed와 강도에서 요청 ID, 토큰 수, 기본 도구 대기를 공유한다.

| 이름 | workload ID | stress 도착 | 도구 대기 |
|---|---|---|---|
| `base_Nx` | `W1` | 원래 포아송 도착 | 기본 대기 × N |
| `burst_Nx` | `W2_BURST` | 10초 구간의 앞 1초로 압축 | 기본 대기 × N |
| `wait_Nx` | `W2_WAIT` | 원래 포아송 도착 | 짧음 0.25배·긺 4배 교대, 추가로 × N |
| `both_Nx` | `W2` | 압축 | 교대 대기, 추가로 × N |

N은 2 또는 4다. `w0_1x`는 기존 정책별 TTFT 상대 SLO의 기준이다. `burst_Nx`와 `base_Nx`의 차이는 도착 시각만, `wait_Nx`와 `base_Nx`의 차이는 대기 규칙만이다. **교대 대기 규칙은 평균 대기시간도 변경한다.** 따라서 이 대비를 평균이 같은 상황에서의 순수한 분산 효과로 해석하지 않는다. 입력의 실제 평균·중앙값·P95를 결과와 함께 확인한다.

## 실행

```powershell
python -m kvbench w2-factorial --output results/w2_factorial
```

기본값은 실행 가능한 다섯 정책 × 9조건 × 5 seed = 225 run이다. Dynamic TTL은 아직 실행 불가능하므로 제외한다. 출력 디렉터리가 이미 있으면 보호를 위해 종료한다. 다시 실행할 때는 다른 경로를 지정한다. `--no-events`로 개별 이벤트 로그를 생략할 수 있다.

| 출력 | 내용 |
|---|---|
| `experiment.json` | 실제 설정, 조건, 정책, seed, 측정 구간 |
| `traces/` | 조건별 입력 JSONL과 메타데이터 |
| `runs/` | run별 summary, requests, occupancy, events |
| `summary.csv` | 정책 × 조건 × seed 원값 |
| `comparison.csv` | 조건별 평균·표준편차와 기존 SLO 통과율 |
| `slo_by_seed.csv` | 정책별 W0 TTFT, 배율, 세 기준의 seed별 통과 여부 |

`notebooks/week9_60s_stress02_review.ipynb`는 이 출력에서 입력 대기 분포, 정책별 지표, SLO 통과 수, Peak 근처 이벤트를 한곳에 보여준다. 같은 분석 흐름을 180초 설정에 적용한 파일은 `notebooks/week9_180s_stress02_review.ipynb`와 `notebooks/week9_180s_stress03_review.ipynb`다.

세 노트북은 각각 한 설정의 결과만 읽으며, 결과 폴더가 없을 때 해당 설정으로 실행한다. 공통 흐름은 입력 대기 분포 → SLO 요약 → seed별 상대·절대 지표 → 요인별 그래프 → Duration-Aware 결정 → Peak 이벤트 → 전체 표·그래프 → 인사이트다. 60초·0.2의 원본 출력은 `results/w2_factorial`, 두 180초 노트북은 각각 `results/w2_factorial_180s_stress02_review_20261027`, `results/w2_factorial_180s_stress03_review_20261027`을 사용한다. 설정 파일·출력·노트북의 대응과 새 경로를 사용하는 재현 명령은 [9주차 문서](week9_experiment.md#2-설정과-결과-출처)를 참고한다.

Peak 근처 이벤트와 정책 결정 분석에는 `events.csv`가 필요하다. 리뷰 노트북용 실험을 생성할 때는 `--no-events`를 사용하지 않는다. 결과 폴더가 존재한다는 사실만으로 실행이 완전하다고 보장되지는 않으므로 manifest·225회 summary·이벤트 파일을 함께 확인한다.

## 지표의 측정 구간

기존 `peak_kv_occupancy_pct`는 warm-up 이후 시뮬레이션 종료까지의 순간 최댓값이다. 7주차 결과와 비교할 수 있도록 정의를 유지했다.

새 `mean_kv_occupancy_pct`, `kv_above_90_ms`, `kv_above_90_pct_of_time`은 모든 정책에 공통인 **외부 도착 측정 구간** `[warmup_ms, duration_ms)`에서 시간 가중으로 계산한다. 따라서 시뮬레이션 종료 시각이 정책마다 달라 생기는 분모 차이를 피한다. 이 세 지표는 기존 SLO 통과 판정에는 넣지 않는다.

전송 바이트·재계산 토큰·정책 동작 횟수는 warm-up과 drain을 포함한 전체 run 누계다. 요청 지표와 동일한 warm-up 필터가 적용된 것으로 해석하지 않는다. 세부 집계 범위는 [시뮬레이터 지표](simulator.md#지표)를 따른다.

`TOOL_START` 이벤트의 detail에는 의사결정 당시 GPU 블록, headroom, 예상 대기, 편도 오프로드 추정시간, prefix 재계산 추정시간, CPU 여유가 기록된다. 현재 Duration-Aware의 예상 대기는 실제 미래 대기시간을 사용하므로 Oracle 참고선이라는 점을 유지해서 표시한다.

## 해석 경계

- 동일 조건 안의 정책 비교는 동일 trace를 사용한다. 조건 간 차이는 생성 규칙의 효과를 분석하는 데 사용한다.
- W2의 2×와 W1의 2×는 실제 평균 대기시간이 같지 않다. 이름의 배율만으로 같은 강도라고 가정하지 않는다.
- SLO의 TTFT 기준은 각 정책의 자기 W0 P95에 대한 3배다. `normal_p95_ttft_ms` 절대값도 함께 읽는다.
- 90%는 평가 목표이며 엔진의 실제 할당 상한은 풀 용량 100%다. Peak 실패만으로 지연·거절이 동일하다고 해석하지 않는다.
- `peak_kv_occupancy_pct`는 한 순간의 초과에도 민감하다. `kv_above_90_ms`로 지속시간을 확인한다.
- oracle 대기 입력은 최적 정책의 보장이 아니다. idle 진입 결정과 pressure 회수의 실제 행동 횟수를 구분하고 EVICT가 실행됐는지 확인한다.
- prefill·decode·PCIe 속도는 아직 실측 전 설정값이다. 결과는 현 시뮬레이터 가정 아래의 상대 비교다.
