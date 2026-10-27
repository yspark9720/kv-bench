# KV-Bench 워크로드 생성기와 정책 시뮬레이터

도구를 사용하는 LLM Agent의 KV Cache 정책 비교에 사용할 **합성 입력 트레이스**를 생성하고, 같은 트레이스를 **정책만 바꿔 재생**해 TTFT·거절률·GPU KV 점유율을 계산합니다. 세션 도착 시각, 턴별 토큰 수, 도구 대기시간을 W0·W1·W2_BURST·W2_WAIT·W2 조건에 맞춰 JSONL 파일로 저장하고, 시뮬레이터가 그 파일을 읽습니다.

현재 구현 범위는 트레이스 생성·저장·검증과 정책 6종 중 5종의 이산 사건 시뮬레이션입니다. 실제 LLM 추론이나 GPU 실행은 포함하지 않으며, 시스템 파라미터(prefill·decode 속도 등)는 실측 전 자리표시자입니다.

현재 연구 질문은 **도구 대기 중인 Agent 세션의 KV를 언제 유지·이동·삭제해야 일반 요청 SLO를 지키면서 재계산·전송 부담을 줄일 수 있는가**입니다. 9주차에는 기존 정책을 유지한 채 60초·stress 0.2/s, 180초·0.2/s, 180초·0.3/s를 설정당 225회씩 분석했습니다. 정책별 차이와 프로젝트의 다음 확인 사항은 [9주차 실험·분석](docs/week9_experiment.MD)에 정리했습니다.

| 문서 | 내용 |
|---|---|
| [9주차 실험·분석](docs/week9_experiment.MD) | 세 설정의 결과 출처, 정책별 차이, 해석 한계와 다음 실험 방향 |
| [W2 요인 분리](docs/w2_diagnostics.md) | 9조건의 정의, 실행 방법과 진단 로그 |
| [시뮬레이터](docs/simulator.md) | 정책 동작, 자원 가정, 지표의 집계 구간 |
| [트레이스 스키마](docs/trace_schema.md) | 입력 필드와 세션·턴의 시간 의미 |
| [7주차 실험](docs/week7_experiment.md) | 기존 3정책·45회 부분 비교의 설정과 재현 방법 |

## 1. 설치

Python 3.9 이상이 필요합니다. 생성기와 시뮬레이터는 표준 라이브러리만 사용하므로 `requirements.txt`에는 외부 패키지가 없습니다. GPU 없이 실행할 수 있습니다. `notebooks/`의 노트북만 pandas와 matplotlib이 필요합니다.

아래 명령은 `README.md`, `requirements.txt`, `kvbench/`가 있는 **프로젝트 루트**에서 실행합니다.

macOS / Linux:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Windows PowerShell:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

`.venv`가 이미 있으면 생성 명령은 생략합니다. 새 터미널에서는 활성화 명령을 다시 실행하고, 종료할 때는 `deactivate`를 입력합니다.

## 2. 실행 명령

### 트레이스 생성

```sh
# W0: 기준 워크로드
python -m kvbench generate --config configs/smoke.json --workload W0 --seed 0 --output outputs/w0_s1_seed0.jsonl

# W1: 도구 대기시간 4배
python -m kvbench generate --config configs/smoke.json --workload W1 --stress-level 4 --seed 0 --output outputs/w1_s4_seed0.jsonl

# W2: burst와 교대 대기에 4배 부하 적용
python -m kvbench generate --config configs/smoke.json --workload W2 --stress-level 4 --seed 0 --output outputs/w2_s4_seed0.jsonl
```

| 옵션 | 의미 | 기본값 |
|---|---|---|
| `--config` | JSON 설정 파일 경로 | 필수 |
| `--workload` | W0, W1, W2_BURST, W2_WAIT, W2 | W0 |
| `--stress-level` | 대기시간 배율 1, 2, 4, 8. W0는 1만 허용 | 1 |
| `--seed` | 0 이상의 정수 난수 seed | 0 |
| `--output` | 저장할 `.jsonl` 경로 | 필수 |

기존 JSONL 또는 동명의 메타데이터 파일이 있으면 덮어쓰지 않고 종료합니다. 재실행할 때는 다른 출력 이름을 지정하거나 기존 파일을 검증·조회하면 됩니다.

### 정책 시뮬레이션

```sh
# 생성한 트레이스를 pin_all 정책으로 재생
python -m kvbench simulate --trace outputs/w0_s1_seed0.jsonl --policy pin_all --output results/w0_s1_seed0_pin_all

# 같은 트레이스를 fixed_ttl 로
python -m kvbench simulate --trace outputs/w0_s1_seed0.jsonl --policy fixed_ttl --output results/w0_s1_seed0_fixed_ttl

# 한 실험의 runs/*/summary.json을 새 CSV로 모으기
python -m kvbench collect results/w2_factorial/runs --output results/w2_factorial/summary_recollected.csv
```

| 옵션 | 의미 | 기본값 |
|---|---|---|
| `--trace` | 생성기가 만든 `.jsonl` 경로. 같은 이름의 `.meta.json`에서 `warmup_ms`를 읽음 | 필수 |
| `--policy` | pin_all, evict_recompute, fixed_ttl, lru_offload, duration_aware, dynamic_ttl(미구현) | 필수 |
| `--system` | 시스템 파라미터 JSON | `configs/system_1080ti_llama3b.json` |
| `--policies` | 정책 파라미터 JSON. 파일이 없으면 정책 기본값 | `configs/policies.json` |
| `--warmup-ms` | 메타데이터의 `warmup_ms` 대신 쓸 값 | 메타데이터 값 |
| `--no-events` | `events.csv`를 생략 (대량 실행용) | 기본은 이벤트 저장 |
| `--output` | 이 run의 결과 디렉터리 | 필수 |

결과 디렉터리가 이미 있으면 덮어쓰지 않고 종료합니다. 정책 동작, 지표 정의, 출력 파일은 [시뮬레이터 문서](docs/simulator.md)에 정리돼 있습니다.

### 7주차 비교 실험

7주차 설정은 W0의 stress 세션 도착률을 0.2 sessions/s로 보정하고 Fixed TTL을 3초로 고정합니다. W0 1x·W1 2x·W1 4x를 Pin-All·Evict/Recompute·Fixed TTL로 seed 5개씩 실행합니다.

```sh
# Fixed TTL 1초·3초·5초 비교
python -m kvbench ttl-sweep

# 정책 3개 × 부하 3개 × seed 5개 = 45 runs
python -m kvbench week7
```

실행 조건과 출력 파일은 [7주차 실험 문서](docs/week7_experiment.md)에 정리돼 있습니다.

각 단계를 직접 확인하려면 노트북 의존성을 설치하고 Jupyter Lab에서 `notebooks/week7_step_by_step.ipynb`를 위에서부터 실행합니다.

### W2 요인 분리 진단

기존 정책을 변경하지 않고 버스트 도착과 교대 대기를 분리해 비교합니다. 실행 가능한 다섯 정책 × 9조건 × 5 seed를 같은 설정으로 재생합니다.

```powershell
python -m kvbench w2-factorial --output results/w2_factorial
```

조건·지표·해석 범위는 [W2 진단 문서](docs/w2_diagnostics.md)에 있습니다. 설정별 표와 그림은 `notebooks/week9_60s_stress02_review.ipynb`, `notebooks/week9_180s_stress02_review.ipynb`, `notebooks/week9_180s_stress03_review.ipynb`에서 확인할 수 있습니다. 기존 출력 경로가 있으면 새 경로를 지정합니다.

### 9주차: 기간·stress 도착률 비교

세 노트북은 각각 해당 설정의 225회 결과를 읽습니다. 위에서부터 실행하면 저장된 결과를 검증·분석하고, 지정한 결과 폴더가 없을 때만 실험을 생성합니다. 마지막 9절에는 정책별 인사이트와 도구 대기를 제외한 작업시간 보조 진단이 있습니다.

| 설정 | 입력 기간 | stress 도착률 | 리뷰 노트북 |
|---|---:|---:|---|
| `configs/week7.json` | 60초 | 0.2/s | [60초·0.2 리뷰](notebooks/week9_60s_stress02_review.ipynb) |
| `configs/week7_180s_stress02.json` | 180초 | 0.2/s | [180초·0.2 리뷰](notebooks/week9_180s_stress02_review.ipynb) |
| `configs/week7_180s_stress03.json` | 180초 | 0.3/s | [180초·0.3 리뷰](notebooks/week9_180s_stress03_review.ipynb) |

세 설정 모두 W0 1x와 기본·버스트·교대 대기·둘 다의 2x/4x를 비교합니다. 기존 60초 및 180초 결과를 보존하고, 새로 실행할 때는 비어 있는 출력 경로를 사용합니다. 정확한 결과 폴더와 재현 명령은 [9주차 문서](docs/week9_experiment.MD)에 있습니다.

관측된 차이는 지연·메모리·복구 비용의 교환 관계입니다. Duration-Aware는 LRU보다 전송을 더 쓰면서 90% 초과 시간을 줄였고, Evict/Recompute는 메모리 여유를 확보하는 대신 재계산이 늘었습니다. 180초·0.3은 W0부터 대부분 실패하므로 과부하 시험으로 해석합니다. seed 5개에서 관측된 차이이며 통계적 유의성을 검정한 결과는 아닙니다.

```sh
python -m pip install -r requirements-notebook.txt
python -m jupyter lab
```

### 검증과 내용 확인

```sh
# 필드, 중복 ID, 턴 연결, 누적 토큰 검사
python -m kvbench validate outputs/w0_s1_seed0.jsonl

# 전체 개수와 첫 stress 세션의 턴별 내용 출력
python -m examples.inspect_trace outputs/w0_s1_seed0.jsonl

# 자동 테스트 (생성기 + 시뮬레이터)
python -m unittest discover -s tests -v

# 명령 도움말
python -m kvbench --help
python -m kvbench generate --help
```

`python -m`은 지정한 모듈을 실행합니다. `kvbench`는 `kvbench/__main__.py`를, `examples.inspect_trace`는 `examples/inspect_trace.py`를 실행합니다. `No module named kvbench`가 나오면 현재 위치가 프로젝트 루트인지 확인하세요.

## 3. 워크로드 정의

**세션**은 하나의 작업 전체이고, **턴**은 모델이 입력을 받아 출력을 생성하는 한 단계입니다. 트레이스 한 행은 한 턴입니다.

- `normal`: 한 턴으로 끝나는 일반 요청. 도구 대기시간은 0입니다.
- `stress`: 여러 턴으로 구성된 도구 사용 세션. 마지막 턴을 제외한 각 턴의 출력 후 도구를 기다립니다.

| 워크로드 | 최초 세션 도착 | 도구 대기시간 | 부하 수준 |
|---|---|---|---|
| W0: 기준 운영 | normal·stress별 독립 포아송 도착 | 설정 범위에서 정수 균등 추출 | 1 |
| W1: 지속 점유 | W0와 동일 | 기본 대기시간 × 부하 배율 | 1, 2, 4, 8 |
| W2_BURST: burst만 | normal은 유지, stress 도착은 주기별 짧은 구간에 집중 | 기본 대기 × 부하 배율 | 1, 2, 4, 8 |
| W2_WAIT: 교대 대기만 | W0와 동일 | 짧음·긺을 교대 적용한 뒤 × 부하 배율 | 1, 2, 4, 8 |
| W2: 교대 대기와 burst | normal은 유지, stress 도착은 주기별 짧은 구간에 집중 | 짧음·긺을 교대 적용한 뒤 × 부하 배율 | 1, 2, 4, 8 |

W0에도 stress 세션이 포함됩니다. W1은 세션 수나 도착률을 높이지 않고 **도구 대기시간만 늘립니다**. 같은 설정·seed에서 W1의 1배는 W0와 라벨을 제외한 입력이 같습니다.

W2는 기본 설정에서 매 10초 구간의 stress 도착을 첫 1초 안으로 압축합니다. 예를 들어 15초의 도착은 10.5초로 바뀝니다. 마지막 구간이 10초보다 짧으면 남은 길이를 기준으로 압축합니다. 세션 수는 유지됩니다.

W2의 대기는 세션마다 짧은 대기부터 시작해 기본 대기의 0.25배·4배를 번갈아 적용합니다. 기본 대기가 1–5초이고 부하 수준이 1이면 짧은 대기는 0.25–1.25초, 긴 대기는 4–20초입니다. 마지막 턴은 대기하지 않습니다. W2의 1배에도 이 변환이 적용되므로 W0와 다릅니다.

W2_BURST는 W2의 도착 시각만, W2_WAIT는 W2의 도구 대기 규칙만 적용합니다. 교대 규칙은 평균 대기시간도 바꾸므로 조건별 실제 대기 분포를 확인해야 합니다.

동일 설정·seed에서 워크로드 간 요청 ID와 토큰 수는 유지됩니다. W2의 stress 도착 시각 변경으로 파일 내 행 순서는 달라질 수 있습니다. 정책별 비교에는 생성한 동일 파일을 사용합니다.

## 4. 설정값

`configs/smoke.json`의 값은 **5주차 동작 확인용 합성 설정**이며 실측값이 아닙니다. `configs/week7.json`은 다른 값을 유지하고 `stress_arrival_rate_per_s`만 0.2로 낮춘 7주차 설정입니다. `[최솟값, 최댓값]` 형태의 값은 양 끝을 포함한 정수 균등 분포로 추출합니다.

`configs/week7_180s_stress02.json`은 `week7.json`에서 `duration_ms`만 180000으로 늘립니다. `configs/week7_180s_stress03.json`은 여기에 `stress_arrival_rate_per_s`를 0.3으로 높입니다. 세 파일의 warm-up은 모두 10초, normal 도착률은 모두 2.0/s입니다. 아래 표는 9주차 설정 전체의 공통 기본값이 아니라 **`smoke.json`의 값**입니다.

| 설정 | 기본값 | 의미 |
|---|---|---|
| `duration_ms` | 60000 | 새 세션 도착을 생성할 60초 구간 |
| `warmup_ms` | 10000 | 메타데이터에 저장하는 준비 구간. 생성 데이터는 제외하지 않음 |
| `normal_arrival_rate_per_s` | 2.0 | 일반 요청의 초당 평균 도착 수 |
| `stress_arrival_rate_per_s` | 0.5 | 도구 사용 세션의 초당 평균 도착 수 |
| `normal_prompt_tokens` | [128, 512] | 일반 요청 입력 토큰 |
| `stress_initial_prompt_tokens` | [2048, 4096] | stress 첫 턴 입력 토큰 |
| `stress_followup_prompt_tokens` | [64, 256] | stress 후속 턴에 추가되는 입력 토큰 |
| `output_tokens` | [32, 128] | 턴별 출력 토큰 |
| `stress_turns` | [3, 5] | stress 세션의 전체 턴 수 |
| `tool_duration_ms` | [1000, 5000] | 배율 적용 전 기본 도구 대기시간 |
| `w2_short_wait_factor` | 0.25 | W2 짧은 대기 배율 |
| `w2_long_wait_factor` | 4.0 | W2 긴 대기 배율 |
| `w2_burst_period_ms` | 10000 | W2 도착을 묶는 주기 |
| `w2_burst_width_ms` | 1000 | 해당 주기에서 도착을 집중시킬 구간 길이 |

도착 수는 확정 개수가 아니라 확률적으로 정해집니다. 세션의 후속 턴은 `duration_ms` 이후까지 이어질 수 있습니다.

### 시스템 파라미터

`configs/system_1080ti_llama3b.json`은 배정 서버(GTX 1080 Ti 11 GiB)와 Llama-3.2-3B FP16을 대상으로 둔 시스템 가정입니다. 처리·전송 속도와 메모리 예산은 실측 보정이 필요합니다. 보정한 설정으로는 새 출력 폴더에 실험을 실행하고, 기존 결과의 `experiment.json`은 보존합니다.

| 설정 | 기본값 | 의미 |
|---|---|---|
| `gpu_kv_pool_bytes` | 4294967296 | KV Cache 용 GPU 메모리 (11 GiB − 가중치 약 6 GiB − runtime ≈ 4 GiB) |
| `cpu_kv_pool_bytes` | 68719476736 | 오프로드 목적지 호스트 메모리 |
| `kv_bytes_per_token` | 114688 | 2 × 28 layers × 8 KV heads × 128 dim × 2 B = 112 KiB |
| `block_size_tokens` | 16 | KV 예약·회수의 block 단위 토큰 수 |
| `pcie_bytes_per_s` | 12000000000 | GPU↔CPU 한 방향 전송 속도 가정, 실측 보정 필요 |
| `prefill_tokens_per_s` | 8000 | prefill 처리 속도, 하나의 FIFO 서버 |
| `decode_tokens_per_s` | 30 | 세션당 decode 속도 |
| `admission_max_wait_ms` | 30000 | 이 시간 안에 KV block을 받지 못한 턴은 거절 |

`configs/policies.json`은 기존 정책 파라미터입니다. `configs/policies_week7.json`은 Fixed TTL을 3,000ms로 바꾼 7주차 설정입니다. 두 파일 모두 `fixed_ttl.evict_on_pressure`는 false이며, 나머지 정책 설정은 같습니다.

## 5. 파이썬 파일별 역할

| 파일 | 역할 |
|---|---|
| `kvbench/__init__.py` | 패키지 설명과 버전 `0.1.0` |
| `kvbench/__main__.py` | generate·validate·simulate·collect·week7·ttl-sweep·w2-factorial CLI와 기존 출력 보호 |
| `kvbench/workload.py` | 설정·턴 자료형, 난수·도착 생성, W0–W2 변환, 검증, 파일 입출력 |
| `kvbench/system.py` | 시스템 파라미터 자료형과 JSON 로드, 정책 파라미터 로드 |
| `kvbench/resources.py` | GPU block 풀, CPU 풀, PCIe·prefill FIFO 서버 |
| `kvbench/policies/` | 정책 인터페이스(`base.py`)와 정책 6종, `REGISTRY` |
| `kvbench/engine.py` | 세션 상태기계와 이벤트 루프 |
| `kvbench/metrics.py` | run 지표 집계 |
| `kvbench/simulate.py` | 트레이스 재생, 결과 파일 쓰기, summary 모으기 |
| `kvbench/experiment.py` | 7주차 45회·W2 요인 분리 225회 매트릭스, TTL sweep, 평균·표준편차·seed별 SLO 집계 |
| `examples/__init__.py` | 실행 예제 패키지 설명 |
| `examples/inspect_trace.py` | 파일을 읽어 세션별로 묶고, 최초 도착 수와 첫 stress 세션의 내용을 출력 |
| `tests/test_workload.py` | 재현성, 워크로드 변환, 문맥 계산, 오류 검출, 파일 입출력, CLI 테스트 |
| `tests/test_simulator.py` | 정책별 완주·자원 반환, 정책 동작(무삭제·즉시 삭제·TTL), 결정성, 설정 검증, 결과 파일, CLI 테스트 |
| `tests/test_experiment.py` | 실험 매트릭스, 출력 보호, seed별 기준값과 SLO 집계 검증 |
| `notebooks/w0_baseline.ipynb` | 5주차: W0 × pin_all·evict_recompute × seed 5 실행, 지표 표, 점유율 곡선, events.csv 로 동작 확인 |
| `notebooks/week7_step_by_step.ipynb` | 7주차: 설정·트레이스·정책·TTL을 단계별로 확인한 뒤 45회 비교 실행 |
| `notebooks/week7_policy_steps.ipynb` | 7주차: 같은 설정 · 같은 SLO 기준에 LRU Offload를 더해(`run_week7(..., policies=...)`) 기준선 → Evict → Fixed TTL → LRU를 한 단계씩 비교 (60회) |
| `notebooks/week9_60s_stress02_review.ipynb` | 9주차: 60초·stress 0.2/s, 225회 요인 분리 및 정책 분석 |
| `notebooks/week9_180s_stress02_review.ipynb` | 9주차: 180초·stress 0.2/s, 장시간 부하와 Peak 진단 |
| `notebooks/week9_180s_stress03_review.ipynb` | 9주차: 180초·stress 0.3/s, 과부하에서 지연·거절·비용 분석 |

`workload.py`의 주요 구성은 다음과 같습니다.

| 구성 | 동작 |
|---|---|
| `Config` | 설정값 보관 및 양수·범위·시간 조건 검사 |
| `Turn` | 트레이스 한 행의 필드 정의 |
| `is_number()` | 유한한 정수·실수인지 확인 |
| `load_config()` | JSON 키를 확인하고 Config 생성 |
| `_rng()` | seed와 용도별로 난수 생성기를 분리 |
| `_arrivals()` | 지수분포 간격을 누적해 최초 세션 도착 시각 생성 |
| `generate()` | normal·stress 세션을 만들고 워크로드 변환 후 정렬·검증 |
| `validate()` | 행 형식, 고유 ID, 연속 턴, 이전 턴 참조, 누적 토큰 검사 및 개수 반환 |
| `read_trace()` | JSONL을 Turn 목록으로 읽고 검증 |
| `write_trace()` | Turn 목록을 JSONL과 메타데이터로 저장 |

생성 명령은 `load_config()` → `generate()` → `write_trace()` 순서로 실행됩니다. 난수는 normal/stress와 도착/내용을 분리해 사용합니다.

시뮬레이션 명령은 `read_trace()` → `Simulator(...).run()` → `summarize()` → `write_results()` 순서입니다. 세션 상태와 정책 결정 지점은 [docs/simulator.md](docs/simulator.md)에 있습니다.

## 6. 출력 형식

| 파일 | 내용 |
|---|---|
| `이름.jsonl` | 한 줄에 한 턴의 JSON 객체 |
| `이름.meta.json` | 설정 전체, 버전, 워크로드, 배율, seed, 개수 요약, JSONL의 SHA-256 |

생성·검증 시 출력하는 `sessions`, `turns`, `normal_requests`, `stress_sessions`, `tool_calls`는 입력 데이터의 개수입니다.

첫 턴에는 외부 도착 시각 `arrival_ms`가 있고, 후속 턴은 `arrival_ms=null`과 직전 턴 ID를 가집니다. 실제 후속 턴의 시작 시각은 입력에서 고정하지 않습니다.

`prompt_tokens`는 이번 턴에 새로 추가되는 입력이고, `context_tokens`는 이전 입력·출력과 이번 입력을 합친 값입니다. 예를 들어 첫 입력 2,000, 첫 출력 100, 다음 입력 200이면 다음 턴의 `context_tokens`는 2,300입니다.

전체 필드와 시간·토큰 정의는 [트레이스 스키마](docs/trace_schema.md)에 정리돼 있습니다. `validate`는 데이터 구조를 검사하며 메타데이터 hash 대조나 성능 측정은 수행하지 않습니다.

시뮬레이션 결과는 `--output` 디렉터리에 `summary.json`, `requests.csv`, `occupancy.csv`, `events.csv`로 저장됩니다. `collect`는 지정한 폴더의 바로 아래 run별 `summary.json`을 모으므로, 매트릭스 결과는 `results/<실험>/runs`를 입력으로 지정합니다. 필드는 [시뮬레이터 문서](docs/simulator.md)의 출력 절을 참고합니다.

`week7`과 `w2-factorial` 명령은 각각의 출력 폴더에 `summary.csv`, 정책·조건별 `comparison.csv`, seed별 `slo_by_seed.csv`를 만듭니다. 실행에 사용한 설정 전체는 `experiment.json`에 저장합니다. `comparison.csv`의 P95 평균은 run별 P95의 seed 평균이며 전체 요청을 합친 P95가 아닙니다.

`.venv/`, `outputs/`, `results/`, Python 캐시, `.env` 파일은 Git 추적에서 제외됩니다.
