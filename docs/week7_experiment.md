# 7주차 부분 비교 매트릭스

7주차 실험은 보정된 기준 워크로드에서 세 정책의 도구 대기 부하 내성을 비교합니다.

## 고정한 조건

| 항목 | 값 |
|---|---|
| 워크로드 설정 | `configs/week7.json` |
| stress 세션 도착률 | 0.2 sessions/s |
| 정책 | Pin-All, Evict/Recompute, Fixed TTL |
| 부하 | W0 1x, W1 2x, W1 4x |
| seed | 0, 1, 2, 3, 4 |
| Fixed TTL | 3,000 ms |
| 전체 run | 3 정책 × 3 부하 × 5 seed = 45 |

5주차의 도착률 sweep에서 0.2 sessions/s는 Pin-All 기준 평균 Peak KV 점유율 66.3%, 최대 76.1%, 거절률 0%였습니다. 기존 0.5 sessions/s는 W0부터 포화됐으므로 7주차 설정에서 낮췄습니다. 다른 워크로드 설정은 `smoke.json`과 같습니다.

W1 1x는 현재 생성기에서 W0와 라벨 외 입력이 같습니다. 최종 매트릭스는 중복을 빼고 W0를 1x로, W1 2x·4x를 지속 점유 부하로 사용합니다.

Fixed TTL 3초는 W0의 1–5초 균등 도구 대기에서 중앙값에 가까운 값입니다. 5초는 거의 만료되지 않아 Pin-All과 같아지고, 1초는 대부분을 삭제해 Evict/Recompute에 가까워집니다. 아래 TTL sweep으로 1·3·5초를 같은 seed에서 다시 비교할 수 있습니다.

## 실행

프로젝트 루트에서 가상환경을 활성화한 뒤 실행합니다.

각 단계를 직접 확인하려면 다음 명령으로 Jupyter Lab을 열고 `notebooks/week7_step_by_step.ipynb`를 위에서부터 실행합니다.

```sh
python -m pip install -r requirements-notebook.txt
python -m jupyter lab
```

터미널에서 전체 실험만 실행하려면 아래 명령을 사용합니다.

```sh
# 선택 근거 확인: Fixed TTL 1초·3초·5초 × seed 5개
python -m kvbench ttl-sweep

# 본 실험: 정책 3개 × 부하 3개 × seed 5개
python -m kvbench week7
```

기본 출력 디렉터리가 이미 있으면 기존 결과를 보호하기 위해 종료합니다. 새 경로를 지정하려면 다음처럼 실행합니다.

```sh
python -m kvbench ttl-sweep --output results/week7_ttl_sweep_v2
python -m kvbench week7 --output results/week7_v2
```

45개 모든 run의 `events.csv`가 필요하지 않으면 다음 옵션으로 생략합니다.

```sh
python -m kvbench week7 --no-events
```

## 결과 파일

```text
results/week7/
├── experiment.json       실제 사용한 워크로드·시스템·정책 설정과 조건
├── traces/               조건별 JSONL과 메타데이터, 총 15개
├── runs/                 run별 summary·requests·occupancy·events, 총 45개
├── summary.csv           seed별 원본 지표 45행
└── comparison.csv        정책·부하별 평균, 표준편차, SLO 통과율 9행
```

`comparison.csv`는 각 정책의 동일 seed W0를 TTFT 기준값으로 사용합니다. seed별로 다음 조건을 판정한 뒤 통과율을 기록합니다.

- P95 TTFT: 해당 정책의 동일 seed W0 대비 3배 이하
- 일반 요청 거절률: 10% 이하
- Peak KV 점유율: 90% 이하

`all_seeds_pass`는 세 기준을 모든 seed가 통과했을 때만 `True`입니다. 평균값과 표준편차도 함께 보존하므로 통과 여부만으로 결과를 해석하지 않습니다.

`results/week7_ttl_sweep/ttl_comparison.csv`에는 TTL별 평균·표준편차, eviction과 재계산 토큰 수가 저장됩니다. 선택한 TTL은 모든 부하에서 고정해야 합니다.

## 해석 범위

이 실험은 이산 사건 시뮬레이션입니다. `prefill_tokens_per_s`와 `decode_tokens_per_s`는 아직 자리표시자이므로 실제 GTX 1080 Ti의 절대 성능으로 해석하지 않습니다. 동일한 가정에서 정책별 변화와 부하 증가 양상을 비교하는 용도입니다.
