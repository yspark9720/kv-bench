# KV-Bench 워크로드 생성기와 정책 시뮬레이터

도구를 사용하는 LLM Agent의 KV Cache 정책 비교에 사용할 **합성 입력 트레이스**를 생성하고, 같은 트레이스를 **정책만 바꿔 재생**해 TTFT·거절률·GPU KV 점유율을 계산합니다. 세션 도착 시각, 턴별 토큰 수, 도구 대기시간을 W0·W1·W2 조건에 맞춰 JSONL 파일로 저장하고, 시뮬레이터가 그 파일을 읽습니다.

현재 구현 범위는 트레이스 생성·저장·검증과 정책 6종 중 5종의 이산 사건 시뮬레이션입니다. 실제 LLM 추론이나 GPU 실행은 포함하지 않으며, 시스템 파라미터(prefill·decode 속도 등)는 실측 전 자리표시자입니다.

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
| `--workload` | W0, W1, W2 | W0 |
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

# results/*/summary.json 을 한 표로
python -m kvbench collect results
```

| 옵션 | 의미 | 기본값 |
|---|---|---|
| `--trace` | 생성기가 만든 `.jsonl` 경로. 같은 이름의 `.meta.json`에서 `warmup_ms`를 읽음 | 필수 |
| `--policy` | pin_all, evict_recompute, fixed_ttl, lru_offload, duration_aware, dynamic_ttl(미구현) | 필수 |
| `--system` | 시스템 파라미터 JSON | `configs/system_1080ti_llama3b.json` |
| `--policies` | 정책 파라미터 JSON. 파일이 없으면 정책 기본값 | `configs/policies.json` |
| `--warmup-ms` | 메타데이터의 `warmup_ms` 대신 쓸 값 | 메타데이터 값 |
| `--no-events` | `events.csv`를 쓰지 않음 (대량 실행용) | 쓰지 않음 |
| `--output` | 이 run의 결과 디렉터리 | 필수 |

결과 디렉터리가 이미 있으면 덮어쓰지 않고 종료합니다. 정책 동작, 지표 정의, 출력 파일은 [시뮬레이터 문서](docs/simulator.md)에 정리돼 있습니다.

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
| W2: 교대 대기와 burst | normal은 유지, stress 도착은 주기별 짧은 구간에 집중 | 짧음·긺을 교대 적용한 뒤 × 부하 배율 | 1, 2, 4, 8 |

W0에도 stress 세션이 포함됩니다. W1은 세션 수나 도착률을 높이지 않고 **도구 대기시간만 늘립니다**. 같은 설정·seed에서 W1의 1배는 W0와 라벨을 제외한 입력이 같습니다.

W2는 기본 설정에서 매 10초 구간의 stress 도착을 첫 1초 안으로 압축합니다. 예를 들어 15초의 도착은 10.5초로 바뀝니다. 마지막 구간이 10초보다 짧으면 남은 길이를 기준으로 압축합니다. 세션 수는 유지됩니다.

W2의 대기는 세션마다 짧은 대기부터 시작해 기본 대기의 0.25배·4배를 번갈아 적용합니다. 기본 대기가 1–5초이고 부하 수준이 1이면 짧은 대기는 0.25–1.25초, 긴 대기는 4–20초입니다. 마지막 턴은 대기하지 않습니다. W2의 1배에도 이 변환이 적용되므로 W0와 다릅니다.

동일 설정·seed에서 워크로드 간 요청 ID와 토큰 수는 유지됩니다. W2의 stress 도착 시각 변경으로 파일 내 행 순서는 달라질 수 있습니다. 정책별 비교에는 생성한 동일 파일을 사용합니다.

## 4. 설정값

`configs/smoke.json`의 값은 **동작 확인용 합성 설정**이며 실측값이 아닙니다. `[최솟값, 최댓값]` 형태의 값은 양 끝을 포함한 정수 균등 분포로 추출합니다.

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

`configs/system_1080ti_llama3b.json`은 배정 서버(GTX 1080 Ti 11 GiB)와 Llama-3.2-3B FP16 기준입니다. `prefill_tokens_per_s`와 `decode_tokens_per_s`는 **실측 전 자리표시자**이며, 실측값이 나오면 이 파일만 바꿉니다.

| 설정 | 기본값 | 의미 |
|---|---|---|
| `gpu_kv_pool_bytes` | 4294967296 | KV Cache 용 GPU 메모리 (11 GiB − 가중치 약 6 GiB − runtime ≈ 4 GiB) |
| `cpu_kv_pool_bytes` | 68719476736 | 오프로드 목적지 호스트 메모리 |
| `kv_bytes_per_token` | 114688 | 2 × 28 layers × 8 KV heads × 128 dim × 2 B = 112 KiB |
| `block_size_tokens` | 16 | KV block 하나의 토큰 수 (vLLM 기본값) |
| `pcie_bytes_per_s` | 12000000000 | GPU↔CPU 한 방향 전송 속도 (PCIe 3.0 x16 실측 약 12 GB/s) |
| `prefill_tokens_per_s` | 8000 | prefill 처리 속도, 하나의 FIFO 서버 |
| `decode_tokens_per_s` | 30 | 세션당 decode 속도 |
| `admission_max_wait_ms` | 30000 | 이 시간 안에 KV block을 받지 못한 턴은 거절 |

`configs/policies.json`은 정책별 파라미터입니다. `fixed_ttl.ttl_ms`(30000), `fixed_ttl.evict_on_pressure`(false), `duration_aware.keep_headroom`(0.3), `duration_aware.breakeven_factor`(2.0). 나머지 정책은 파라미터가 없습니다.

## 5. 파이썬 파일별 역할

| 파일 | 역할 |
|---|---|
| `kvbench/__init__.py` | 패키지 설명과 버전 `0.1.0` |
| `kvbench/__main__.py` | CLI 인자 해석, generate·validate 실행, 기존 출력 보호 및 오류 출력 |
| `kvbench/workload.py` | 설정·턴 자료형, 난수·도착 생성, W0–W2 변환, 검증, 파일 입출력 |
| `kvbench/system.py` | 시스템 파라미터 자료형과 JSON 로드, 정책 파라미터 로드 |
| `kvbench/resources.py` | GPU block 풀, CPU 풀, PCIe·prefill FIFO 서버 |
| `kvbench/policies/` | 정책 인터페이스(`base.py`)와 정책 6종, `REGISTRY` |
| `kvbench/engine.py` | 세션 상태기계와 이벤트 루프 |
| `kvbench/metrics.py` | run 지표 집계 |
| `kvbench/simulate.py` | 트레이스 재생, 결과 파일 쓰기, summary 모으기 |
| `examples/__init__.py` | 실행 예제 패키지 설명 |
| `examples/inspect_trace.py` | 파일을 읽어 세션별로 묶고, 최초 도착 수와 첫 stress 세션의 내용을 출력 |
| `tests/test_workload.py` | 재현성, 워크로드 변환, 문맥 계산, 오류 검출, 파일 입출력, CLI 테스트 |
| `tests/test_simulator.py` | 정책별 완주·자원 반환, 정책 동작(무삭제·즉시 삭제·TTL), 결정성, 설정 검증, 결과 파일, CLI 테스트 |
| `notebooks/w0_baseline.ipynb` | 5주차: W0 × pin_all·evict_recompute × seed 5 실행, 지표 표, 점유율 곡선, events.csv 로 동작 확인 |

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

시뮬레이션 결과는 `--output` 디렉터리에 `summary.json`, `requests.csv`, `occupancy.csv`, `events.csv`로 저장되고, `collect`가 `results/summary.csv`로 모읍니다. 필드는 [시뮬레이터 문서](docs/simulator.md)의 출력 절을 참고합니다.

`.venv/`, `outputs/`, `results/`, Python 캐시, `.env` 파일은 Git 추적에서 제외됩니다.
