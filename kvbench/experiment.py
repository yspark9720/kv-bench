"""Reproducible experiment matrices used for the week-7 comparison."""

from __future__ import annotations

import csv
from dataclasses import asdict
import json
from pathlib import Path
from statistics import mean, stdev

from .simulate import collect, simulate, write_results
from .system import load_policy_params, load_system_config
from .workload import generate, load_config, write_trace


WEEK7_POLICIES = ("pin_all", "evict_recompute", "fixed_ttl")
WEEK7_CONDITIONS = (
    ("1x", "W0", 1),
    ("2x", "W1", 2),
    ("4x", "W1", 4),
)
WEEK7_SEEDS = (0, 1, 2, 3, 4)
W2_FACTORIAL_POLICIES = ("pin_all", "evict_recompute", "fixed_ttl", "lru_offload", "duration_aware")
W2_FACTORIAL_CONDITIONS = (
    ("w0_1x", "W0", 1),
    ("base_2x", "W1", 2), ("burst_2x", "W2_BURST", 2),
    ("wait_2x", "W2_WAIT", 2), ("both_2x", "W2", 2),
    ("base_4x", "W1", 4), ("burst_4x", "W2_BURST", 4),
    ("wait_4x", "W2_WAIT", 4), ("both_4x", "W2", 4),
)
PRIMARY_METRICS = (
    "normal_p95_ttft_ms",
    "normal_reject_rate_pct",
    "peak_kv_occupancy_pct",
)
DIAGNOSTIC_METRICS = (
    "eviction_count",
    "recomputed_tokens",
    "offload_count",
    "transfer_bytes",
    "mean_kv_occupancy_pct",
    "kv_above_90_ms",
    "kv_above_90_pct_of_time",
    "stress_p95_completion_ms",
)


def _prepare_output(output_dir):
    output_dir = Path(output_dir)
    if output_dir.exists():
        raise ValueError("output already exists; choose a new path")
    (output_dir / "traces").mkdir(parents=True)
    (output_dir / "runs").mkdir()
    return output_dir


def _write_manifest(output_dir, workload, system, policy_configs, **extra):
    manifest = {
        "workload_config": asdict(workload),
        "system_config": asdict(system),
        "policy_configs": policy_configs,
        **extra,
    }
    (Path(output_dir) / "experiment.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _sample_std(values):
    return stdev(values) if len(values) > 1 else 0.0


def aggregate_week7(summary_csv, output_csv=None, policies=WEEK7_POLICIES,
                    conditions=WEEK7_CONDITIONS, baseline_label="1x"):
    """Aggregate seed-level summaries and apply the original week-7 SLOs."""
    summary_csv = Path(summary_csv)
    with summary_csv.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise ValueError("summary CSV is empty")

    condition_by_key = {(workload, str(level)): label
                        for label, workload, level in conditions}
    groups = {}
    for row in rows:
        key = (row["workload_id"], row["stress_level"])
        label = condition_by_key.get(key)
        if label is None:
            # CSV may serialize an integer level as "1.0".
            key = (row["workload_id"], str(int(float(row["stress_level"]))))
            label = condition_by_key.get(key)
        if label is None:
            raise ValueError(f"unexpected workload condition: {key}")
        groups.setdefault((row["policy"], label), []).append(row)

    baseline = {}
    for policy in policies:
        values = groups.get((policy, baseline_label))
        if not values:
            raise ValueError(f"missing {baseline_label} baseline for {policy}")
        for row in values:
            baseline[(policy, row["seed"])] = float(row["normal_p95_ttft_ms"])

    output_rows = []
    seed_rows = []
    for policy in policies:
        for label, _, _ in conditions:
            values = groups.get((policy, label))
            if not values:
                raise ValueError(f"missing {label} results for {policy}")
            item = {
                "policy": policy,
                "load": label,
                "seeds": len(values),
            }
            for metric in PRIMARY_METRICS + DIAGNOSTIC_METRICS:
                metric_values = [float(row[metric]) for row in values if row.get(metric) not in (None, "")]
                item[f"{metric}_mean"] = mean(metric_values) if metric_values else None
                item[f"{metric}_std"] = _sample_std(metric_values) if metric_values else None
            ratios = []
            ttft_passes, reject_passes, occupancy_passes, all_passes = [], [], [], []
            for row in values:
                base = baseline.get((policy, row["seed"]))
                if base is None:
                    raise ValueError(f"missing paired 1x seed {row['seed']} for {policy}")
                ratio = float(row["normal_p95_ttft_ms"]) / base if base > 0 else float("inf")
                ttft_ok = ratio <= 3
                reject_ok = float(row["normal_reject_rate_pct"]) <= 10
                occupancy_ok = float(row["peak_kv_occupancy_pct"]) <= 90
                ratios.append(ratio)
                ttft_passes.append(ttft_ok)
                reject_passes.append(reject_ok)
                occupancy_passes.append(occupancy_ok)
                all_passes.append(ttft_ok and reject_ok and occupancy_ok)
                seed_rows.append({
                    "policy": policy, "load": label, "seed": row["seed"],
                    "w0_p95_ttft_ms": base,
                    "normal_p95_ttft_ms": float(row["normal_p95_ttft_ms"]),
                    "ttft_ratio_vs_policy_w0": ratio,
                    "normal_reject_rate_pct": float(row["normal_reject_rate_pct"]),
                    "peak_kv_occupancy_pct": float(row["peak_kv_occupancy_pct"]),
                    "ttft_pass": ttft_ok, "reject_pass": reject_ok,
                    "occupancy_pass": occupancy_ok,
                    "all_pass": ttft_ok and reject_ok and occupancy_ok,
                })
            item["ttft_ratio_vs_policy_1x_mean"] = mean(ratios)
            item["ttft_ratio_vs_policy_1x_std"] = _sample_std(ratios)
            item["ttft_slo_pass_rate_pct"] = 100 * sum(ttft_passes) / len(ttft_passes)
            item["reject_slo_pass_rate_pct"] = 100 * sum(reject_passes) / len(reject_passes)
            item["occupancy_slo_pass_rate_pct"] = 100 * sum(occupancy_passes) / len(occupancy_passes)
            item["all_slo_pass_rate_pct"] = 100 * sum(all_passes) / len(all_passes)
            item["all_seeds_pass"] = all(all_passes)
            output_rows.append(item)

    output_csv = Path(output_csv) if output_csv else summary_csv.with_name("comparison.csv")
    with output_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(output_rows[0]))
        writer.writeheader()
        writer.writerows(output_rows)
    with output_csv.with_name("slo_by_seed.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(seed_rows[0]))
        writer.writeheader()
        writer.writerows(seed_rows)
    return output_csv


def run_week7(config_path, system_path, policies_path, output_dir,
              seeds=WEEK7_SEEDS, conditions=WEEK7_CONDITIONS,
              policies=WEEK7_POLICIES, keep_events=True):
    """Run W0 1x and W1 2x/4x for three policies and return output paths."""
    output_dir = _prepare_output(output_dir)
    workload = load_config(config_path)
    system = load_system_config(system_path)
    policy_configs = {policy: load_policy_params(policies_path, policy) for policy in policies}
    _write_manifest(
        output_dir, workload, system, policy_configs,
        experiment="week7_partial_matrix",
        conditions=[{"load": label, "workload": wid, "stress_level": level}
                    for label, wid, level in conditions],
        seeds=list(seeds), policies=list(policies), keep_events=keep_events,
        slo={"ttft_ratio_max": 3, "reject_rate_pct_max": 10,
             "peak_kv_occupancy_pct_max": 90},
    )

    for label, workload_id, level in conditions:
        for seed in seeds:
            trace = output_dir / "traces" / f"{workload_id.lower()}_s{level}_seed{seed}.jsonl"
            rows = generate(workload, workload_id, level, seed)
            write_trace(trace, rows, workload, workload_id, level, seed)
            for policy in policies:
                sim, summary = simulate(
                    trace, policy, system, policy_configs[policy], keep_events=keep_events
                )
                summary["load"] = label
                run_id = f"{workload_id.lower()}_s{level}_seed{seed}__{policy}"
                write_results(sim, summary, output_dir / "runs" / run_id)

    raw_summary = collect(output_dir / "runs", output_dir / "summary.csv")
    comparison = aggregate_week7(raw_summary, output_dir / "comparison.csv", policies)
    return raw_summary, comparison


def run_w2_factorial(config_path, system_path, policies_path, output_dir,
                     seeds=WEEK7_SEEDS, conditions=W2_FACTORIAL_CONDITIONS,
                     policies=W2_FACTORIAL_POLICIES, keep_events=True):
    """Compare burst and alternating waits independently on paired traces."""
    output_dir = _prepare_output(output_dir)
    workload = load_config(config_path)
    system = load_system_config(system_path)
    policy_configs = {policy: load_policy_params(policies_path, policy) for policy in policies}
    _write_manifest(
        output_dir, workload, system, policy_configs,
        experiment="w2_factorial_diagnostic",
        conditions=[{"load": label, "workload": wid, "stress_level": level}
                    for label, wid, level in conditions],
        seeds=list(seeds), policies=list(policies), keep_events=keep_events,
        measurement_window={"start_ms": workload.warmup_ms, "end_ms": workload.duration_ms},
        slo={"ttft_ratio_max": 3, "reject_rate_pct_max": 10,
             "peak_kv_occupancy_pct_max": 90},
    )

    for label, workload_id, level in conditions:
        for seed in seeds:
            trace = output_dir / "traces" / f"{workload_id.lower()}_s{level}_seed{seed}.jsonl"
            rows = generate(workload, workload_id, level, seed)
            write_trace(trace, rows, workload, workload_id, level, seed)
            for policy in policies:
                sim, summary = simulate(
                    trace, policy, system, policy_configs[policy], keep_events=keep_events
                )
                summary["load"] = label
                run_id = f"{workload_id.lower()}_s{level}_seed{seed}__{policy}"
                write_results(sim, summary, output_dir / "runs" / run_id)

    raw_summary = collect(output_dir / "runs", output_dir / "summary.csv")
    comparison = aggregate_week7(
        raw_summary, output_dir / "comparison.csv", policies,
        conditions=conditions, baseline_label="w0_1x",
    )
    return raw_summary, comparison


def run_ttl_sweep(config_path, system_path, output_dir,
                  ttl_candidates_ms=(1000, 3000, 5000), seeds=WEEK7_SEEDS,
                  keep_events=False):
    """Compare fixed-TTL candidates on W0 without changing the final policy config."""
    output_dir = _prepare_output(output_dir)
    workload = load_config(config_path)
    system = load_system_config(system_path)
    policy_configs = {
        str(ttl): {"ttl_ms": ttl, "evict_on_pressure": False}
        for ttl in ttl_candidates_ms
    }
    _write_manifest(
        output_dir, workload, system, policy_configs,
        experiment="fixed_ttl_calibration", workload_id="W0", stress_level=1,
        ttl_candidates_ms=list(ttl_candidates_ms), seeds=list(seeds), keep_events=keep_events,
    )

    for seed in seeds:
        trace = output_dir / "traces" / f"w0_s1_seed{seed}.jsonl"
        rows = generate(workload, "W0", 1, seed)
        write_trace(trace, rows, workload, "W0", 1, seed)
        for ttl in ttl_candidates_ms:
            params = policy_configs[str(ttl)]
            sim, summary = simulate(trace, "fixed_ttl", system, params, keep_events=keep_events)
            summary["ttl_ms"] = ttl
            run_id = f"w0_s1_seed{seed}__fixed_ttl_{ttl}ms"
            write_results(sim, summary, output_dir / "runs" / run_id)

    raw_summary = collect(output_dir / "runs", output_dir / "summary.csv")
    with raw_summary.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    output_rows = []
    for ttl in ttl_candidates_ms:
        selected = [row for row in rows if int(float(row["ttl_ms"])) == ttl]
        item = {"ttl_ms": ttl, "seeds": len(selected)}
        for metric in PRIMARY_METRICS + DIAGNOSTIC_METRICS:
            values = [float(row[metric]) for row in selected]
            item[f"{metric}_mean"] = mean(values)
            item[f"{metric}_std"] = _sample_std(values)
        output_rows.append(item)
    comparison = output_dir / "ttl_comparison.csv"
    with comparison.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(output_rows[0]))
        writer.writeheader()
        writer.writerows(output_rows)
    return raw_summary, comparison
