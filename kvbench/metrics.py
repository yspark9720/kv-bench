"""Run summary. Primary metrics decide SLO pass/fail; secondary metrics explain why.

Primary
- normal_p95_ttft_ms: P95 of first_token_ms - arrival_ms over completed normal requests
- normal_reject_rate_pct: rejected normal / submitted normal * 100
- peak_kv_occupancy_pct: max used_blocks / total_blocks * 100 after warm-up

Requests that arrive before warmup_ms are excluded from every metric.
"""

from __future__ import annotations

import math


def percentile(values, p):
    """Nearest-rank percentile; None for an empty list."""
    if not values:
        return None
    ordered = sorted(values)
    index = max(math.ceil(p / 100 * len(ordered)) - 1, 0)
    return ordered[index]


def peak_occupancy_pct(samples, total_blocks, warmup_ms):
    """Maximum of the step function after warm-up, including the value in force when warm-up ends."""
    if total_blocks <= 0:
        return 0.0
    carried, peak = 0, 0
    for time_ms, used in samples:
        if time_ms < warmup_ms:
            carried = used
        else:
            peak = max(peak, used)
    return max(peak, carried) / total_blocks * 100


def summarize(sim, warmup_ms, meta=None):
    counters = sim.counters
    records = sim.all_records()
    measured = [r for r in records if r.arrival_ms >= warmup_ms]
    normal = [r for r in measured if r.request_class == "normal"]
    normal_done = [r for r in normal if r.outcome == "completed" and r.ttft_ms is not None]
    normal_rejected = [r for r in normal if r.outcome == "rejected"]
    all_done = [r for r in measured if r.outcome == "completed" and r.ttft_ms is not None]

    jobs_done = [s for s in sim.sessions.values()
                 if s.job_finished_ms is not None and s.state.value == "done" and s.job_arrival_ms >= warmup_ms]
    window_s = max(sim.now - warmup_ms, 1.0) / 1000
    n_jobs = max(len(jobs_done), 1)
    completion = [s.job_finished_ms - s.job_arrival_ms for s in jobs_done]
    transfer_bytes = counters.gpu_to_cpu_bytes + counters.cpu_to_gpu_bytes

    return {
        **(meta or {}),
        "policy": sim.policy.name,
        "sessions": len(sim.sessions),
        "end_ms": round(sim.now, 1),
        "normal_submitted": len(normal),
        "normal_completed": len(normal_done),
        "normal_rejected": len(normal_rejected),
        "normal_p50_ttft_ms": percentile([r.ttft_ms for r in normal_done], 50),
        "normal_p95_ttft_ms": percentile([r.ttft_ms for r in normal_done], 95),
        "normal_reject_rate_pct": len(normal_rejected) / len(normal) * 100 if normal else None,
        "peak_kv_occupancy_pct": round(peak_occupancy_pct(sim.gpu.samples, sim.gpu.total_blocks, warmup_ms), 2),
        "all_p95_ttft_ms": percentile([r.ttft_ms for r in all_done], 95),
        "completed_jobs": len(jobs_done),
        "rejected_jobs": counters.rejected_jobs,
        "throughput_jobs_per_s": round(len(jobs_done) / window_s, 4),
        "task_completion_mean_ms": sum(completion) / len(completion) if completion else None,
        "task_completion_p95_ms": percentile(completion, 95),
        "eviction_count": counters.evictions,
        "eviction_per_job": counters.evictions / n_jobs,
        "offload_count": counters.offloads,
        "reload_count": counters.reloads,
        "transfer_bytes": transfer_bytes,
        "transfer_gib_per_job": transfer_bytes / n_jobs / 2**30,
        "recomputed_tokens": counters.recomputed_tokens,
        "recomputed_tokens_per_job": counters.recomputed_tokens / n_jobs,
        "prefill_busy_pct": round(sim.prefill.busy_ms_total / max(sim.now, 1.0) * 100, 2),
        "pcie_busy_pct": round(sim.pcie.busy_ms_total / max(sim.now, 1.0) * 100, 2),
    }
