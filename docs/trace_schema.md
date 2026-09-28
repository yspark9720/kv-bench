# 트레이스 스키마 v1

## 데이터 단위와 시간

JSONL 한 행은 한 세션의 한 턴입니다. 시간 단위는 ms, 토큰 수는 정수, 도착률은 초당 세션 수입니다. 턴 번호는 0부터 시작합니다. 파일은 `(session_arrival_ms, session_id, turn_id)` 순으로 저장하지만 **실제 실행 이벤트의 시간순 로그가 아닙니다**.

도구 호출은 해당 턴의 output 생성이 끝난 직후 한 번 발생한다고 가정합니다. 마지막 턴은 도구를 호출하지 않습니다. 중간 턴 하나당 하나의 도구 호출만 모델링하며 병렬 도구 호출, 취소, 재시도, 문맥 잘라내기는 범위 밖입니다.

| 필드 | 형식 | 의미 |
|---|---|---|
| schema_version | int | 현재 1 |
| session_id | str | 파일 내 세션 식별자 |
| turn_id | int | 세션 내 0부터 연속하는 턴 번호 |
| request_id | str | 파일 내 고유한 턴 요청 ID |
| request_class | str | `normal` 또는 `stress` |
| seed | int | 0 이상의 난수 seed |
| workload_id | str | W0, W1, W2 |
| stress_level | int | 1, 2, 4, 8. W0는 1 |
| session_arrival_ms | number | 최초 외부 도착 시각. 같은 세션의 모든 행에 반복 |
| arrival_ms | number / null | 첫 턴은 최초 도착 시각, 후속 턴은 null |
| depends_on_request_id | str / null | 후속 턴은 직전 턴의 request_id, 첫 턴은 null |
| prompt_tokens | int | 이번 턴에서 새로 추가되는 입력 토큰. 도구 결과·템플릿 토큰을 포함한 총량으로 간주 |
| output_tokens | int | 이번 턴에서 생성할 출력 토큰, 1 이상 |
| context_tokens | int | 이번 턴의 입력을 추가한 후, 출력을 생성하기 전 누적 문맥 길이 |
| expected_turns | int | 해당 세션 전체 턴 수 |
| tool_duration_ms | number | 이번 턴 출력 완료 뒤 외부 도구의 실행·대기시간. 마지막 턴은 0 |
| arrival_rate_per_s | number | 해당 클래스의 원래 세션 도착률. W2 burst 순간 도착률이 아님 |
| burst_id | int / null | W2 stress의 0부터 시작하는 주기 번호. 그 외 null |

첫 턴은 `context_tokens = prompt_tokens`입니다. 이후는 아래와 같습니다.

```text
context_tokens[t] = context_tokens[t-1] + output_tokens[t-1] + prompt_tokens[t]
```

도구 대기 중 보존할 토큰 수는 `context_tokens + output_tokens`입니다. KV가 남아 있다면 복귀 시 새 `prompt_tokens`만 prefill하고, 전부 삭제됐다면 해당 턴의 전체 `context_tokens`를 재계산합니다. GPU block 반올림과 실제 메모리 바이트 계산은 자원 모델이 담당합니다.

## 턴의 시간 해석

`turn_id == 0`인 첫 턴만 외부 도착 시각을 갖습니다. 후속 턴은 직전 턴의 출력 완료와 도구 대기가 끝나야 실행 가능하므로 `arrival_ms`가 null입니다. 각 행의 `session_arrival_ms`는 최초 도착 정보를 반복한 값이며 해당 턴의 실행 시각이 아닙니다.

실제 도구 시작·완료·복귀 시각은 이 입력 파일에 포함하지 않습니다. 각 시각의 의미는 다음과 같습니다.

```text
tool_start_ms = 이번 턴의 실제 decode 완료 시각
tool_ready_ms = tool_start_ms + tool_duration_ms
resume_ms = 필요한 전송·재계산·자원 대기를 반영한 실제 복귀 시각
```

이 구조는 후속 요청이 직전 응답과 도구 완료에 종속되는 세션 모델입니다. 외부 도착과 도구 소요시간이 같아도 정책별 실제 후속 턴 시작 시각은 달라질 수 있습니다. 생성기는 이러한 실행 시각을 계산하지 않습니다.

## 메타데이터와 측정 구간

동반 `.meta.json`에 생성기 버전, 설정 전체, workload·배율·seed, 개수 요약과 JSONL 바이트의 SHA-256을 저장합니다. `validate` 명령은 JSONL 구조와 세션 연결을 검사하며, 메타데이터의 hash 일치나 실험 성능을 검사하는 명령은 아닙니다.

`duration_ms`는 외부 도착을 생성하는 구간 `[0, duration_ms)`의 끝입니다. 세션의 모든 턴은 파일에 포함하므로 후속 턴의 실행은 이 시각 이후까지 이어질 수 있습니다.

`warmup_ms`는 설정과 메타데이터에 보존됩니다. 생성기는 warm-up 구간의 데이터를 삭제하거나 측정 지표를 집계하지 않습니다.

포아송 도착이므로 매우 짧은 구간이나 낮은 도착률에서는 요청이 0개일 수 있습니다. 빈 트레이스도 형식상 허용합니다.
