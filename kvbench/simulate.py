"""Replay one trace under one policy, write the run's files, and merge run summaries."""

from __future__ import annotations

import csv
from dataclasses import asdict, fields
import json
from pathlib import Path

from .engine import EVENT_COLUMNS, RequestRecord, Simulator
from .metrics import summarize
from .policies import make_policy
from .workload import read_trace

SUMMARY_COLUMNS = [
    "run_id", "policy", "trace", "workload_id", "stress_level", "seed", "warmup_ms",
    "normal_submitted", "normal_completed", "normal_rejected",
    "normal_p50_ttft_ms", "normal_p95_ttft_ms", "normal_reject_rate_pct", "peak_kv_occupancy_pct",
    "completed_jobs", "rejected_jobs", "throughput_jobs_per_s", "task_completion_mean_ms", "task_completion_p95_ms",
    "eviction_count", "eviction_per_job", "offload_count", "reload_count", "transfer_bytes", "transfer_gib_per_job",
    "recomputed_tokens", "recomputed_tokens_per_job", "prefill_busy_pct", "pcie_busy_pct", "sessions", "end_ms",
]


def trace_warmup_ms(trace_path):
    """warmup_ms recorded in the trace's .meta.json; 0 when there is no metadata file."""
    meta_path = Path(trace_path).with_suffix(".meta.json")
    if not meta_path.exists():
        return 0.0
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    return float(meta["config"]["warmup_ms"])


def simulate(trace_path, policy, system, policy_params=None, warmup_ms=None, keep_events=True):
    """Return (Simulator, summary dict) for one trace and one policy name."""
    rows = read_trace(trace_path)
    if not rows:
        raise ValueError("trace is empty")
    if warmup_ms is None:
        warmup_ms = trace_warmup_ms(trace_path)
    sim = Simulator(rows, system, make_policy(policy, policy_params), keep_events).run()
    meta = {"trace": Path(trace_path).name, "workload_id": rows[0].workload_id,
            "stress_level": rows[0].stress_level, "seed": rows[0].seed, "warmup_ms": warmup_ms}
    return sim, summarize(sim, warmup_ms, meta)


def write_results(sim, summary, output_dir, overwrite=False):
    """Write summary.json, requests.csv, occupancy.csv and (if kept) events.csv into output_dir."""
    output_dir = Path(output_dir)
    if output_dir.exists() and not overwrite:
        raise ValueError("output already exists; choose a new path")
    output_dir.mkdir(parents=True, exist_ok=True)
    summary = {**summary, "run_id": output_dir.name}
    config = {"system": asdict(sim.system), "policy": sim.policy.describe()}
    (output_dir / "summary.json").write_text(
        json.dumps({**summary, "config": config}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    columns = [f.name for f in fields(RequestRecord)] + ["ttft_ms"]
    with (output_dir / "requests.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        for record in sim.all_records():
            values = asdict(record)
            writer.writerow([values[name] for name in columns[:-1]] + [record.ttft_ms])
    with (output_dir / "occupancy.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["time_ms", "used_blocks", "total_blocks"])
        for time_ms, used in sim.gpu.samples:
            writer.writerow([time_ms, used, sim.gpu.total_blocks])
    if sim.keep_events:
        with (output_dir / "events.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(EVENT_COLUMNS)
            writer.writerows(sim.events)
    return summary


def collect(results_dir, output=None):
    """Merge every <results_dir>/*/summary.json into one CSV. Returns the CSV path."""
    results_dir = Path(results_dir)
    rows = []
    for path in sorted(results_dir.glob("*/summary.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        data.pop("config", None)
        rows.append(data)
    if not rows:
        raise ValueError(f"no summary.json under {results_dir}")
    columns = SUMMARY_COLUMNS + sorted({key for row in rows for key in row} - set(SUMMARY_COLUMNS))
    output = Path(output) if output else results_dir / "summary.csv"
    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    return output
