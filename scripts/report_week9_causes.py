"""Build the detailed week-9 report from the verified diagnostics, without replaying policies."""

import argparse
from collections import Counter
import json
from pathlib import Path
from statistics import mean

from analyze_week9_causes import SOURCES, digest, load_csv, save_csv
from kvbench.metrics import peak_occupancy_pct
from kvbench.workload import read_trace


POLICIES = ("pin_all", "evict_recompute", "fixed_ttl", "lru_offload", "duration_aware")


def avg(rows, key):
    values = [row[key] for row in rows if row[key] is not None]
    return mean(values) if values else None


def table(headers, rows):
    def cell(value):
        return f"{value:.2f}" if isinstance(value, float) else str(value)
    return "\n".join(["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
                     + ["| " + " | ".join(cell(value) for value in row) + " |" for row in rows])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--diagnostics", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    if args.report.exists():
        parser.error("report already exists; choose a new path")
    rows = json.loads((args.diagnostics / "run_diagnostics.json").read_text(encoding="utf-8"))
    assert len(rows) == 675
    provenance = json.loads((args.diagnostics / "provenance.json").read_text(encoding="utf-8"))
    assert provenance["observer_sha256"] == digest(Path(__file__).with_name("analyze_week9_causes.py"))
    # This report describes the fixed week-9 dataset; fail rather than reuse stale narrative counts.
    assert Counter(r["failure_mask"] for r in rows) == {
        "pass": 203, "occupancy_pass": 363, "ttft_pass+occupancy_pass": 54,
        "ttft_pass+reject_pass+occupancy_pass": 41, "reject_pass+occupancy_pass": 14,
    }
    assert sum(not r["occupancy_pass"] and r["peak_non_idle_above90"] for r in rows) == 76
    index = {(r["setting"], r["load"], r["seed"], r["policy"]): r for r in rows}
    failures, delay, ownership, factors, reversals, window_changes = [], [], [], [], [], []
    for setting in SOURCES:
        for policy in POLICIES:
            group = [r for r in rows if r["setting"] == setting and r["policy"] == policy]
            failures.append({"setting": setting, "policy": policy, "runs": len(group),
                             "pass": sum(r["failure_mask"] == "pass" for r in group),
                             "peak_fail": sum(not r["occupancy_pass"] for r in group),
                             "ttft_fail": sum(not r["ttft_pass"] for r in group),
                             "reject_fail": sum(not r["reject_pass"] for r in group)})
            group = [r for r in group if r["load"] == "both_4x"]
            delay.append({"setting": setting, "policy": policy,
                          **{key: avg(group, key) for key in
                             ("normal_p95_ttft_ms", "p95_admission_ms", "p95_prefill_queue_ms",
                              "p95_prefill_compute_ms", "p95_blocker_stress_initial_ms",
                              "p95_blocker_stress_recompute_ms", "resume_p95_ms", "blocked_ms_pct",
                              "normal_behind_stress_ms_pct", "normal_fits_behind_stress_ms_pct")}})
            for contrast, left, right in (("burst-base", "base", "burst"), ("wait-base", "base", "wait")):
                pairs = [(index[setting, f"{left}_{level}x", seed, policy],
                          index[setting, f"{right}_{level}x", seed, policy])
                         for level in (2, 4) for seed in range(5)]
                diffs = [b["normal_p95_ttft_ms"] - a["normal_p95_ttft_ms"] for a, b in pairs]
                factors.append({"setting": setting, "policy": policy, "contrast": contrast,
                                "paired_comparisons": len(pairs), "p95_delta_mean_ms": mean(diffs),
                                "p95_delta_min_ms": min(diffs), "p95_delta_max_ms": max(diffs),
                                "p95_increased": sum(d > 1e-7 for d in diffs),
                                "above90_delta_pp": mean(b["kv_above_90_pct_of_time"] -
                                                         a["kv_above_90_pct_of_time"] for a, b in pairs)})
    for policy in POLICIES:
        group = [r for r in rows if r["policy"] == policy and not r["occupancy_pass"]]
        ownership.append({"policy": policy, "peak_failed_runs": len(group),
                          "non_idle_above90": sum(r["peak_non_idle_above90"] for r in group),
                          "idle_alone_covers": sum(r["peak_idle_alone_could_cover"] for r in group),
                          "inflight_alone_covers": sum(r["peak_inflight_alone_could_cover"] for r in group),
                          "normal_owner_pct": mean(r["peak_normal_owner"] / r["peak_used"] * 100 for r in group),
                          "stress_owner_pct": mean(r["peak_stress_owner"] / r["peak_used"] * 100 for r in group)})
    for r in rows:
        if r["policy"] in ("lru_offload", "duration_aware") and not r["ttft_pass"]:
            evict = index[r["setting"], r["load"], r["seed"], "evict_recompute"]
            if evict["ttft_pass"] and r["normal_p95_ttft_ms"] < evict["normal_p95_ttft_ms"]:
                reversals.append({"setting": r["setting"], "load": r["load"], "seed": r["seed"],
                                  "policy": r["policy"], "p95_ms": r["normal_p95_ttft_ms"],
                                  "ratio": r["ttft_ratio"], "evict_p95_ms": evict["normal_p95_ttft_ms"],
                                  "evict_ratio": evict["ttft_ratio"]})
        source = SOURCES[r["setting"]]
        config = json.loads((source / "experiment.json").read_text(encoding="utf-8"))["workload_config"]
        samples = load_csv(source / "runs" / r["run_id"] / "occupancy.csv")
        window_peak = round(peak_occupancy_pct(
            [(float(s["time_ms"]), int(s["used_blocks"])) for s in samples
             if float(s["time_ms"]) < config["duration_ms"]], 2340, config["warmup_ms"]), 2)
        new_pass = window_peak <= 90 and r["ttft_pass"] and r["reject_pass"]
        if new_pass != (r["failure_mask"] == "pass"):
            window_changes.append({"setting": r["setting"], "policy": r["policy"], "load": r["load"],
                                   "seed": r["seed"], "old_peak_pct": r["peak_kv_occupancy_pct"],
                                   "input_window_peak_pct": window_peak, "new_pass": new_pass})
    for filename, data in (("failure_counts.csv", failures), ("peak_ownership.csv", ownership),
                           ("p95_decomposition.csv", delay), ("factor_contrasts.csv", factors),
                           ("relative_slo_reversals.csv", reversals), ("window_changes.csv", window_changes)):
        save_csv(args.diagnostics / filename, data)
    fail_table = table(["설정", "정책", "전체 통과 /45", "Peak 실패", "TTFT 실패", "거절률 실패"],
                       [[r[k] for k in ("setting", "policy", "pass", "peak_fail", "ttft_fail", "reject_fail")]
                        for r in failures])
    owner_table = table(["정책", "Peak 실패", "회수 불가 예약만으로 90% 초과", "idle만 즉시 비우면 90% 이하",
                         "전송 중 블록만 반환되면 90% 이하", "Peak의 stress 소유 비중(%)"],
                        [[r[k] for k in ("policy", "peak_failed_runs", "non_idle_above90", "idle_alone_covers",
                                        "inflight_alone_covers", "stress_owner_pct")] for r in ownership])
    delay_table = table(["설정", "정책", "P95 TTFT(ms)", "입장 대기", "prefill 대기", "자기 prefill 계산"],
                        [[r[k] for k in ("setting", "policy", "normal_p95_ttft_ms", "p95_admission_ms",
                                        "p95_prefill_queue_ms", "p95_prefill_compute_ms")] for r in delay])
    blocker_table = table(["설정", "정책", "prefill 대기(ms)", "stress 첫 턴이 점유한 시간",
                           "stress 재계산 턴이 점유한 시간"],
                          [[r[k] for k in ("setting", "policy", "p95_prefill_queue_ms",
                                          "p95_blocker_stress_initial_ms", "p95_blocker_stress_recompute_ms")]
                           for r in delay if r["policy"] in ("evict_recompute", "lru_offload", "duration_aware")])
    queue_table = table(["설정", "정책", "입장 대기열 막힘(%)", "normal이 stress 뒤에서 대기(%)",
                         "그중 normal 자체 공간은 충분(%)"],
                        [[r[k] for k in ("setting", "policy", "blocked_ms_pct", "normal_behind_stress_ms_pct",
                                        "normal_fits_behind_stress_ms_pct")] for r in delay])
    factor_table = table(["정책", "대비", "P95 변화 평균(ms)", "최소", "최대", "증가한 쌍 /10",
                          "90% 초과 시간 변화(pp)"],
                         [[r[k] for k in ("policy", "contrast", "p95_delta_mean_ms", "p95_delta_min_ms",
                                         "p95_delta_max_ms", "p95_increased", "above90_delta_pp")]
                          for r in factors if r["setting"] == "180s_02"])
    example_reversal = next(r for r in reversals if r["setting"] == "60s_02" and
                            r["load"] == "burst_2x" and r["policy"] == "lru_offload")
    active_examples = [r for r in rows if r["policy"] == "duration_aware" and r["peak_non_idle_above90"]]
    active_table = table(["설정", "조건", "seed", "Peak 시각(s)", "회수 불가 예약(blocks)", "idle", "전송 중"],
                         [[r["setting"], r["load"], r["seed"], r["peak_time_ms"] / 1000,
                           r["peak_non_idle_blocks"], r["peak_idle"], r["peak_offloading"]] for r in active_examples])
    resume_table = table(["설정", "정책", "복귀 P95 평균(ms)"],
                         [[r["setting"], r["policy"], r["resume_p95_ms"]] for r in delay])
    report = f"""# 9주차 상세 원인 분석: 전체 675회 재생 검증

작성: 2026-11-02. 분석 대상은 9주차의 기존 5정책 × 9조건 × 5 seed × 3설정이다. Watermark와 10주차 신규 정책은 분석 대상에 넣지 않았다. 정책·스케줄링·평가 기준을 변경하지 않고 관찰용 서브클래스로 동일 실행을 재생했다.

## 1. 핵심 결론

1. 675회 중 전체 SLO 통과는 **203회**, 실패는 **472회**다. 실패 472회 모두 Peak 기준을 넘었다. **363회는 Peak만 실패**, 109회는 지연 또는 거절률도 함께 실패했다. 지연·거절이 중요하지 않다는 뜻이 아니라, 이번 자료의 실패 분류에서 Peak가 공통 조건이었다는 뜻이다.
2. Peak 실패를 모두 idle 회수 지연으로 설명할 수 없다. 실패 **76회**는 해당 Peak 순간에 idle과 offload 전송 중 KV를 모두 제거해도 나머지 GPU 예약 블록이 90%를 넘는다. 그중 Evict는 48회, LRU는 3회, Duration-Aware는 7회다. 활성·대기 요청의 예약량과 입장 제어를 함께 봐야 한다.
3. 대표 `both_4x`에서 Pin-All의 긴 normal TTFT는 대부분 **메모리 입장 대기**, Evict·LRU·Duration-Aware의 P95는 대부분 **공유 FIFO prefill 대기**다. 같은 TTFT 악화라도 원인이 다르다.
4. LRU와 Duration-Aware의 짧은 지연과 높은 Peak는 동시에 성립한다. Duration-Aware는 과밀 지속시간을 줄였지만, 30% 여유 기준은 idle 시점 판단이며 입장 상한이 아니어서 순간 Peak를 보장하지 않는다.
5. `wait` 조건은 대기 분산뿐 아니라 평균 대기시간도 늘린다. `burst`는 긴 stress 첫 prompt가 prefill에 몰리는 효과도 있다. 따라서 모든 차이를 메모리 정책이나 대기 예측만으로 설명하면 안 된다.

## 2. 출처와 검증 방법

| 설정 표기 | 입력 기간 / stress 도착률 | 원본 결과 |
|---|---|---|
| 60s_02 | 60초 / 0.2 sessions/s | `results/w2_factorial` |
| 180s_02 | 180초 / 0.2 sessions/s | `results/w2_factorial_180s_stress02_review_20261027` |
| 180s_03 | 180초 / 0.3 sessions/s | `results/w2_factorial_180s_stress03_review_20261027` |

세 폴더에서 각각 manifest·225개 run·45개 trace를 확인했다. 총 135개 trace의 메타데이터 SHA-256과 실제 내용이 일치한다. 675회 모두 원본 `summary.csv`와 run별 `summary.json`이 일치하고, 관찰용 재생의 **기존 summary 전체 값, requests 전체 행, occupancy 전체 행, events 전체 행**이 원본과 정확히 일치했다. 원본을 덮어쓰지 않았다. 이는 새로운 seed 실험이나 원인 제거 실험이 아니라, 기존 실행의 상태를 추가 관찰한 재현이다.

추가 관찰은 Peak 때 소유 상태, prefill의 예약 시작·종료, FIFO 입장 대기열 상태다. 원래 이벤트에는 소수 첫째 자리로 반올림된 prefill 시작·종료가 있지만, 재생에서는 같은 스케줄러 계산값을 반올림 없이 사용했다. 각 완료 normal 요청에서 `TTFT = 입장 대기 + prefill 대기 + 자기 prefill 계산`의 합을 검사했다.

warm-up은 10초다. Peak는 기존 정의대로 warm-up 이후 drain까지, 점유율 시간 비율·대기열 시간 비율은 `[warmup, 입력 종료)`에서 측정한다. 전송·재계산과 정책 행동 횟수는 전체 run 누계다. seed별 지표를 평균하며, 전체 요청을 합친 P95나 675개의 독립 표본으로 해석하지 않는다.

## 3. 전체 SLO 실패 분해

기존 기준: 같은 정책·같은 seed의 W0 P95 대비 3배 이하, normal 거절률 10% 이하, Peak 90% 이하. 아래 실패 항목은 중복된다.

{fail_table}

총 TTFT 기준 실패는 **95회**, 거절률 기준 실패는 **55회**다. 거절률 기준 실패 55회는 모두 Pin-All이다. 다만 거절이 한 번이라도 발생한 run은 Pin-All 82회, Fixed TTL 2회이므로 ‘거절률 기준 통과’를 ‘거절 없음’과 혼동하면 안 된다. Fixed TTL의 두 run은 180s_03의 `w0_1x` seed 3(7.38%), `both_2x` seed 3(9.85%)다.

W0도 180초 설정에서 실패한다. 따라서 180s_03을 안전한 정상 부하로 놓고 W2만 문제라고 설명할 수 없다. 입력 기간 증가는 관찰 시간과 총 입력 수를 함께 늘리므로, 이 세 설정만으로 정상 상태나 장기 안정성을 증명하지 않는다.

## 4. Peak의 상세 원인: 누가 GPU 블록을 잡고 있었나

GPU 풀은 2,340 blocks이며 90%는 2,106 blocks다. 엔진은 턴 입장 때 `context_tokens + output_tokens`의 블록을 한 번에 예약한다. 따라서 prefill을 기다리는 요청도 이미 GPU 블록을 잡고 있다. 이 분석에서 점유량은 실제 생성된 토큰 수가 아니라 **엔진이 예약한 KV 블록**이다.

- **idle**: `TOOL_WAIT`이고 KV가 GPU에 있어 회수 대상이 될 수 있는 블록.
- **전송 중**: CPU로 offload 중이며, 완료 전까지 GPU에 남는 블록.
- **회수 불가 예약**: 그 밖의 prefill 대기·계산, decode, reload, 복귀 후 입장 대기 등의 블록. 현재 정책은 이 요청들을 회수 대상으로 선택하지 않는다. ‘계산이 진행 중인 요청만’이라는 뜻은 아니다.

각 run의 기존 정의에 따른 **최초 최대 Peak 순간**을 조사했다. 아래의 ‘즉시 비우면’은 그 순간의 산술적 공간 계산이며, 해당 정책으로 실제 재실험한 결과가 아니다. idle과 전송 중 열은 서로 중복될 수 있다.

{owner_table}

이 표에서 도출되는 원인은 두 갈래다.

1. **회수 가능한 idle KV가 남아 있는 경우:** LRU는 공간이 실제로 모자랄 때만 회수하며, Pin-All은 회수하지 않고, Fixed TTL은 만료까지 기다린다. 이 규칙들은 90% 여유 목표를 직접 강제하지 않는다. Duration-Aware도 idle 시작 시 한 세션의 행동을 정할 뿐 전체 점유율을 특정 값 아래로 내릴 때까지 계속 회수하지 않는다.
2. **회수 불가 예약만으로 초과한 경우:** idle KV만 더 빨리 옮기는 것으로는 이미 발생한 Peak를 없앨 수 없다. 입장을 늦추거나 예약·스케줄링 방식을 바꾸는 등 활성 요청 측 제어를 별도로 검토해야 한다. 단, 이 관찰만으로 특정 제어 방식의 최적성이나 모든 부하에서의 필요성을 증명하지는 않는다.

Duration-Aware의 구체적인 회수 불가 예약 초과 사례는 다음과 같다. 모두 기존 9주차 조건이며, oracle 입력을 쓰더라도 발생했다.

{active_table}

### Peak를 올린 마지막 요청과 메모리 소유자는 다르다

실패 472회에서 최초 최대 Peak를 만든 입장은 normal **257회**, stress 첫 턴 **87회**, stress 복귀 턴 **128회**다. 따라서 기존 대표 사례 세 개처럼 모든 Peak가 stress ADMIT에서 발생했다고 일반화하면 안 된다. 작은 normal 입장이 이미 높은 예약량에 마지막 블록을 더할 수도 있다. 위 표의 stress 소유 비중과 마지막 입장 주체를 구분해야 한다.

### drain 때문에 실패가 과장됐는가

최대 Peak가 입력 종료 이후였던 run은 10회다. 그러나 Peak를 입력 측정 구간으로 제한했을 때 전체 SLO 판정이 달라지는 run은 **1회**뿐이다: 180s_03 / `wait_2x` / Fixed TTL / seed 0, 기존 93.80% → 입력 구간 82.22%, 실패 → 통과. 따라서 대부분의 실패를 drain 집계만으로 설명할 수 없다. 기존 SLO를 변경하지 않았다.

## 5. normal TTFT의 원인은 정책별로 다르다

아래는 `both_4x`의 seed 5개 평균이다. **각 run의 P95에 해당하는 실제 normal 요청 한 개**를 고르고 그 요청의 구간을 분해한 뒤 평균했다. 구성 요소 자체의 P95를 각각 계산해 더한 것이 아니다.

{delay_table}

### Pin-All: 긴 입장 대기가 주원인

180s_02·180s_03의 P95는 거의 전부 입장 대기다. idle KV가 남아 있어도 Pin-All은 회수하지 않고, 입장 기회가 생기지 않으면 30초에 거절한다. 30초 근처의 완료 요청 TTFT가 반복된다고 안정적인 처리 능력으로 해석할 수 없다. 거절된 요청은 TTFT 분포에서 빠지고, 거절된 stress 세션의 나머지 턴도 사라진다.

입장 대기열은 FIFO이므로 앞 stress 요청이 들어갈 공간이 부족하면, 뒤 normal 요청이 자기 공간을 확보할 수 있어도 기다릴 수 있다. 다음 표는 측정 구간 시간 비율의 seed 평균이며, 요청 비율이 아니다. 마지막 열은 단순히 stress 뒤에 섰다는 관찰을 넘어 normal 자체에 필요한 free blocks가 충분한 상태를 확인한 것이다.

{queue_table}

### Evict: 재계산이 공유 prefill의 대기를 늘린다

Evict는 도구 대기마다 KV를 삭제하므로 idle 점유를 없앤다. 하지만 복귀 시 이전 문맥을 prefill에서 다시 처리한다. 대표 조건에서 P95 normal 요청의 메모리 입장 대기는 0ms인데 prefill 대기가 길다. 이는 해당 P95 요청의 지연을 단순히 GPU 메모리 부족으로 설명하면 틀린다는 뜻이다.

### LRU·Duration-Aware: 재계산이 없어도 stress 첫 prompt가 FIFO를 점유한다

두 정책의 대표 조건 P95 normal 요청도 대부분 prefill 대기다. stress 첫 prompt는 2,048~4,096 tokens이고 normal prompt는 128~512 tokens다. 현재 prefill은 한 번에 하나의 턴을 처리하므로 긴 stress prompt 뒤에 normal이 서면 영향을 받는다. 실제 P95 요청의 대기 구간이 어떤 prefill 작업과 겹쳤는지 시간 교집합으로 분해했다.

{blocker_table}

‘재계산 턴’은 이전 문맥과 새 prompt를 한 작업으로 처리한 전체 prefill 구간이다. 표의 값을 순수 재계산 토큰의 계산 시간이라고 부르지는 않는다. 남은 대기는 normal 또는 cached stress 복귀 prompt의 처리 구간이다. 이 표는 관측한 지연 경로이지, 해당 작업을 제거했을 때 시스템 전체가 얼마나 빨라질지 예측하는 실험은 아니다.

60s_02·seed 0의 LRU와 Duration-Aware에서는 `burst_2x`, `burst_4x`, `both_2x`, `both_4x` 모두 같은 normal 요청 `normal-000017:0`이 P95이고 값도 613.31ms다. 이 사례는 도구 대기 배율보다 공통 stress 첫 턴의 도착 압축과 prefill 경합이 해당 꼬리 지연을 지배했음을 보여준다.

## 6. 버스트·대기 규칙·재계산 타이밍을 분리해서 읽기

`burst-base`는 stress 최초 도착 시각을 압축한 차이이고, `wait-base`는 도구 대기 규칙을 변경한 차이다. wait 규칙은 0.25배·4배 교대이므로 평균 대기시간도 증가한다. 이를 평균이 같은 분포의 순수 변동성 효과라고 해석하지 않는다.

다음은 180s_02에서 강도 2·4 각각 seed 5개, 총 10개의 짝지은 차이다. 같은 seed가 두 강도에서 반복되므로 독립 표본 10개라는 뜻은 아니다. 지연 변화는 오른쪽 조건에서 왼쪽 조건을 뺀 값이다.

{factor_table}

방향이 seed마다 달라질 수 있는 이유는 대기가 복귀 시각과 동시 활성 세션 수를 함께 바꾸기 때문이다. 도구 대기가 길어지면 메모리 점유 기간은 늘 수 있지만, 복귀 prefill이 서로 떨어져 오히려 일부 지연이 줄 수도 있다. 집계 평균과 최소·최대를 함께 보고 모든 seed에 같은 효과가 있다고 주장하지 않는다.

Evict는 **각 설정·seed에서 9조건의 재계산 토큰 총량이 모두 같다**. 입력의 토큰·턴 수와 즉시 삭제 규칙이 같기 때문이다. 그럼에도 P95는 달라진다. 따라서 지연 차이를 재계산 총량 하나로 설명할 수 없고, 공유 prefill에 재계산이 몰리는 시각을 봐야 한다.

Fixed TTL 3초는 대기와 직접 상호작용한다. `base_4x`는 도구 대기가 4~20초라 복귀 전에 항상 TTL이 만료된다. 반면 `wait_2x`의 짧은 대기는 0.5~2.5초라 해당 턴에서는 KV를 재사용할 수 있고, 긴 대기는 8~40초여서 만료된다. 이러한 행동 차이와 입장 타이밍 때문에 4x가 2x보다 무조건 더 나쁘다고 기대할 수 없다.

## 7. 상대 TTFT SLO가 절대 성능 순위와 다른 이유

TTFT 기준 실패 95회는 Pin-All 77회, Fixed TTL 1회, LRU 9회, Duration-Aware 8회다. LRU·Duration-Aware의 실패 17회 중 **15회**는 같은 입력의 Evict보다 절대 P95가 짧은데도, Evict는 자기 W0 대비 TTFT 기준을 통과했다.

예를 들어 60s_02 / `burst_2x` / seed 0에서 LRU는 **{example_reversal['p95_ms']:.1f}ms, 자기 W0 대비 {example_reversal['ratio']:.2f}배**로 TTFT 실패다. Evict는 **{example_reversal['evict_p95_ms']:.1f}ms, 자기 W0 대비 {example_reversal['evict_ratio']:.2f}배**로 TTFT 통과다. Evict의 기준 W0 자체가 재계산 등으로 더 느리기 때문이다. 여기서 ‘통과’는 TTFT 세부 기준이며 전체 SLO와 구분한다.

이는 기존 판정을 바꿀 이유라기보다 상대 기준과 절대 지연을 함께 보고할 이유다. SLO 판정은 그대로 유지했다. 또한 이 17회는 모두 Peak도 실패했으므로, 이번 9주차 자료에는 ‘상대 TTFT만 실패했지만 나머지는 통과한’ run이 없다.

## 8. offload·복귀 비용과 현재 구현의 해석 범위

LRU와 Duration-Aware는 전체 135회씩에서 모두 재계산 0·EVICT 0이었다. Duration-Aware의 전체 idle 결정은 KEEP 8,315회, OFFLOAD 4,762회, EVICT 0회다. CPU 최대 사용은 전체 분석에서 **{max(r['cpu_peak_gib'] for r in rows):.2f}GiB**로 풀 64GiB보다 작았다. 따라서 이 자료로 CPU 부족 시 삭제 선택이나 현실 대기 예측 오차의 효과를 평가했다고 할 수 없다.

LRU는 회수 호출 3,728회 중 758회, Duration-Aware는 731회 중 92회에서 이미 진행 중인 offload 블록이 부족분을 덮는데 추가 victim을 선택했다. 이는 후보 선택 시 전송 중 블록을 부족분에서 빼지 않는 현재 엔진 동작과 일치한다. 호출 횟수 기준이고, 이 수치만으로 불필요한 전송 바이트나 수정 효과를 단정하지 않는다. 정책 수정 실험은 이번 분석에서 하지 않았다.

### 도구 종료부터 첫 토큰까지의 복귀 지연

원래 요청 기록은 도구 종료 시 offload 중이면 전송 완료 뒤에 생성된다. 따라서 `first_token - arrival`은 전송 잔여 대기를 놓칠 수 있다. 관찰한 실제 TOOL_END 시각과 다음 턴 첫 토큰을 연결해 보조 지표를 계산했다. 첫 턴은 제외하고 실제 도구 종료가 warm-up 이후인 완료 stress 복귀 턴의 run별 P95를 사용했다.

{resume_table}

전체 자료에서 도구가 offload보다 먼저 끝난 복귀는 **{sum(r['resume_tail_wait_positive'] for r in rows)}건**, 누락될 수 있던 잔여 대기의 최댓값은 **{max(r['resume_tail_wait_max_ms'] for r in rows):.2f}ms**였다. 이는 모든 정책·조건의 실행을 합친 이벤트 수이며 독립 세션 수가 아니다. 복귀 표본은 완료 턴만 포함하므로 `run_diagnostics.csv`의 복귀 턴 수·완료 수·거절 수와 원본 stress 세션 완료 수를 함께 읽어야 한다. 도구 대기가 긴 전체 완료시간과 복귀 TTFT는 다른 지표다.

## 9. 확정한 관측과 아직 필요한 검증

| 이번 분석에서 확인한 것 | 여기서 확정하지 않은 것 |
|---|---|
| 전체 675회의 실패 기준과 Peak 당시 예약 소유 상태 | 모든 부하에서 통하는 단일 실패 원인 |
| P95 normal 요청의 입장·prefill 대기 분해 | 실제 GPU의 prefill·decode 경합과 전송 비용 |
| 일부 Peak는 idle을 모두 비워도 90%를 넘는 상태 | 특정 입장 상한이나 정책의 최적성 |
| 현재 자료에서 LRU·Duration-Aware는 EVICT 분기를 사용하지 않음 | 작은 CPU 풀·느린 PCIe에서의 9주차 정책 순위 |
| 상대 TTFT와 절대 지연 순위가 어긋나는 실제 사례 | 절대 TTFT SLO를 어떤 값으로 정해야 하는가 |
| 별도 관찰로 복귀 잔여 대기를 포함한 지연 계산 가능 | 예측기 정확도와 임계값 튜닝의 우위 |

우선순위는 ① 메모리 여유 목표를 요청 입장과 연결해 평가, ② 같은 목표를 지킬 때 전송·재계산·복귀 지연 비용 비교, ③ 새 환경이나 파라미터 선택은 별도 계획·평가 seed로 검증하는 순서다. 이는 원인 분석에서 얻은 후속 질문이며, 이번 보고서에서 실행한 새 정책 실험은 아니다. 10주차 결과와 혼합해 9주차에 이미 검증했다고 서술하지 않는다.

## 10. 재현과 원자료

관찰용 코드는 [`analyze_week9_causes.py`](../scripts/analyze_week9_causes.py), 보고서 생성은 [`report_week9_causes.py`](../scripts/report_week9_causes.py)다. 표준 라이브러리만 사용하며, 위 원본 결과 폴더가 필요하다. 출력과 보고서는 기존 경로를 덮어쓰지 않는다.

```powershell
python scripts/analyze_week9_causes.py --output results/week9_causes_replay
python scripts/report_week9_causes.py --diagnostics results/week9_causes_replay --report docs/week9_root_cause_analysis_replay.md
```

이번 분석 출력: [`{args.diagnostics.as_posix()}`](../{args.diagnostics.as_posix()}/run_diagnostics.csv).

- `run_diagnostics.csv` / `.json`: 675회 원값, 실패 기준·Peak 소유·지연 분해·복귀 지표.
- `failure_counts.csv`: 설정·정책별 실패 기준 집계.
- `peak_ownership.csv`: Peak 실패 시점의 회수 가능량과 소유 비중.
- `p95_decomposition.csv`: 대표 조건의 P95 요청 경로와 prefill 점유 작업.
- `factor_contrasts.csv`: burst·wait의 seed별 대비를 집계한 평균·범위.
- `relative_slo_reversals.csv`: 절대 지연과 상대 TTFT 판정이 어긋난 15개 정책 비교.
- `window_changes.csv`: 입력 구간 Peak를 쓸 때 전체 SLO가 바뀌는 1회.
- `provenance.json`: 원본 manifest·통합 CSV·135개 trace 해시와 관찰 코드 해시.

`results/`는 Git 추적 제외 대상이다. 다른 환경에서 이 보고서의 수치를 재검증하려면 원본 결과를 확보하거나 9주차 문서의 재현 명령으로 먼저 생성해야 한다.
"""
    args.report.write_text(report, encoding="utf-8")
    print(f"Wrote {args.report} and six summary CSVs", flush=True)


if __name__ == "__main__":
    main()
