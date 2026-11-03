"""Audit saved week-9 runs and observe identical replays without changing policy decisions.

Run from the repository root: python scripts/analyze_week9_causes.py --output results/week9_causes
The saved inputs and outputs are read only. All diagnostics go into a new output directory.
"""

from __future__ import annotations

import argparse
from bisect import bisect_right
from collections import Counter
import csv
from dataclasses import asdict
import hashlib
from itertools import zip_longest
import json
import math
from pathlib import Path
from statistics import mean
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kvbench.engine import KvLoc, SessionState, Simulator
from kvbench.metrics import occupancy_exposure, percentile, summarize
from kvbench.policies import make_policy
from kvbench.system import SystemConfig
from kvbench.workload import read_trace


SOURCES = {
    "60s_02": ROOT / "results/w2_factorial",
    "180s_02": ROOT / "results/w2_factorial_180s_stress02_review_20261027",
    "180s_03": ROOT / "results/w2_factorial_180s_stress03_review_20261027",
}


def load_csv(path):
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def save_csv(path, rows):
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def csv_cell(value):
    return "" if value is None else str(value)


class PolicyObserver:
    def __init__(self, policy, sim):
        self.policy, self.sim = policy, sim
        self.idle_actions = Counter()
        self.pressure = Counter()

    def __getattr__(self, name):
        return getattr(self.policy, name)

    def on_idle_start(self, view, ctx):
        decision = self.policy.on_idle_start(view, ctx)
        self.idle_actions[decision.action.value] += 1
        return decision

    def select_victims(self, needed, candidates, ctx):
        victims = self.policy.select_victims(needed, candidates, ctx)
        self.pressure["calls"] += 1
        self.pressure["empty_candidates"] += not candidates
        self.pressure["insufficient_idle"] += sum(v.gpu_blocks for v in candidates) < needed
        pending = sum(s.gpu_blocks for s in self.sim.sessions.values() if s.loc is KvLoc.TO_CPU)
        self.pressure["pending_covers_shortfall"] += pending >= needed
        self.pressure["extra_reclaim_while_pending_covers"] += bool(victims) and pending >= needed
        self.pressure["victims"] += len(victims)
        return victims


class ObservedSimulator(Simulator):
    def __init__(self, rows, system, policy, warmup_ms, duration_ms):
        super().__init__(rows, system, policy, keep_events=True)
        self.policy = PolicyObserver(policy, self)
        self.warmup_ms, self.duration_ms = warmup_ms, duration_ms
        self.prefill_jobs = []
        self.prefill_by_request = {}
        self.tool_ends = {}
        self.queue_samples = []
        self.peak_snapshot = None
        self.cpu_peak_bytes = 0
        self.rejections = []

    def ownership(self):
        parts = Counter()
        owners = Counter()
        for s in self.sessions.values():
            if not s.gpu_blocks:
                continue
            if s.loc is KvLoc.TO_CPU:
                group = "offloading"
            elif s.state is SessionState.TOOL_WAIT and s.loc is KvLoc.GPU:
                group = "idle"
            elif s.state is SessionState.PREFILLING:
                job = self.prefill_by_request[s.current.request_id]
                group = "prefill_queue" if self.now < job["start"] else "prefill_compute"
            elif s.state is SessionState.DECODING:
                group = "decode"
            elif s.state is SessionState.RELOADING:
                group = "reload"
            elif s.state is SessionState.WAIT_ADMIT:
                group = "wait_admit"
            else:
                group = "other"
            parts[group] += s.gpu_blocks
            owners[s.request_class] += s.gpu_blocks
        assert sum(parts.values()) == self.gpu.used_blocks
        return {**{key: parts[key] for key in
                   ("idle", "offloading", "prefill_queue", "prefill_compute", "decode", "reload", "wait_admit", "other")},
                "normal_owner": owners["normal"], "stress_owner": owners["stress"]}

    def _admit(self, s, need):
        before = self.gpu.used_blocks
        super()._admit(s, need)
        if (self.now >= self.warmup_ms and
                (self.peak_snapshot is None or self.gpu.used_blocks > self.peak_snapshot["used"])):
            self.peak_snapshot = {
                "time_ms": self.now, "used": self.gpu.used_blocks, "before": before,
                "added": need, "request_id": s.current.request_id, "request_class": s.request_class,
                "turn_id": s.current.turn_id, **self.ownership(),
            }

    def _start_prefill(self, s, tokens):
        start = max(self.now, self.prefill.busy_until_ms)
        if s.request_class == "normal":
            category = "normal"
        elif s.current.turn_id == 0:
            category = "stress_initial"
        elif s.record.restore == "recompute":
            category = "stress_recompute"
        else:
            category = "stress_cached_resume"
        job = {"request_id": s.current.request_id, "queued": self.now, "start": start,
               "end": start + tokens / self.system.prefill_tokens_per_s * 1000,
               "tokens": tokens, "category": category}
        self.prefill_jobs.append(job)
        self.prefill_by_request[job["request_id"]] = job
        super()._start_prefill(s, tokens)

    def _try_admit(self):
        super()._try_admit()
        queued = [self.sessions[sid] for sid in self.admission_queue
                  if self.sessions[sid].state is SessionState.WAIT_ADMIT]
        normals = sum(s.request_class == "normal" for s in queued)
        normal_fits_behind_stress = bool(queued) and queued[0].request_class == "stress" and any(
            s.request_class == "normal" and self._needed_blocks(s) <= self.gpu.free_blocks for s in queued[1:]
        )
        self.queue_samples.append((self.now, len(queued), normals,
                                   int(bool(queued) and queued[0].request_class == "stress"),
                                   int(normal_fits_behind_stress)))

    def _start_offload(self, s, reason):
        super()._start_offload(s, reason)
        self.cpu_peak_bytes = max(self.cpu_peak_bytes, self.cpu.used_bytes)

    def _on_tool_end(self, s, idx):
        if s.state is SessionState.TOOL_WAIT and s.idx == idx:
            self.tool_ends[f"{s.sid}:{s.current.turn_id + 1}"] = self.now
        super()._on_tool_end(s, idx)

    def _on_admit_deadline(self, s, idx):
        if s.state is SessionState.WAIT_ADMIT and s.idx == idx:
            self.rejections.append({"time_ms": self.now, "request_id": s.current.request_id,
                                    "normal": s.request_class == "normal",
                                    "own_need_fits": self._needed_blocks(s) <= self.gpu.free_blocks,
                                    **self.ownership()})
        super()._on_admit_deadline(s, idx)


def verify_csv_rows(path, replay_rows):
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.reader(handle)
        next(reader)
        for number, (saved, replay) in enumerate(zip_longest(reader, replay_rows), start=2):
            if saved != replay:
                raise AssertionError(f"Replay differs: {path}:{number}\n{saved}\n{replay}")


def queue_exposure(sim):
    totals = Counter()
    for index, (time_ms, count, normal, stress_head, normal_fits) in enumerate(sim.queue_samples):
        end = sim.queue_samples[index + 1][0] if index + 1 < len(sim.queue_samples) else sim.now
        elapsed = max(0, min(end, sim.duration_ms) - max(time_ms, sim.warmup_ms))
        totals["blocked_ms"] += elapsed * bool(count)
        totals["normal_waiting_ms"] += elapsed * bool(normal)
        totals["normal_behind_stress_ms"] += elapsed * bool(normal and stress_head)
        totals["normal_fits_behind_stress_ms"] += elapsed * normal_fits
    denominator = sim.duration_ms - sim.warmup_ms
    return {key + "_pct": 100 * totals[key] / denominator for key in
            ("blocked_ms", "normal_waiting_ms", "normal_behind_stress_ms", "normal_fits_behind_stress_ms")}


def analyze_run(sim, saved, judgement):
    result = {key: value for key, value in saved.items() if key != "config"}
    flags = {key: judgement[key] == "True" for key in ("ttft_pass", "reject_pass", "occupancy_pass")}
    mask = "+".join(name for name, flag in flags.items() if not flag) or "pass"
    result.update(flags, failure_mask=mask, ttft_ratio=float(judgement["ttft_ratio_vs_policy_w0"]),
                  w0_p95_ms=float(judgement["w0_p95_ttft_ms"]))
    peak = sim.peak_snapshot
    assert round(100 * peak["used"] / sim.gpu.total_blocks, 2) == saved["peak_kv_occupancy_pct"], saved["run_id"]
    result.update({f"peak_{key}": value for key, value in peak.items()})
    limit = math.floor(sim.gpu.total_blocks * 0.9)
    active = peak["used"] - peak["idle"] - peak["offloading"]
    result.update(
        peak_non_idle_blocks=active,
        peak_non_idle_above90=active > limit,
        peak_idle_alone_could_cover=peak["used"] - peak["idle"] <= limit,
        peak_inflight_alone_could_cover=peak["used"] - peak["offloading"] <= limit,
        peak_in_drain=peak["time_ms"] >= sim.duration_ms,
        cpu_peak_gib=sim.cpu_peak_bytes / 2**30,
    )
    result.update(queue_exposure(sim))
    result.update({"idle_" + action: sim.policy.idle_actions[action] for action in ("keep", "offload", "evict")})
    result.update({"pressure_" + key: sim.policy.pressure[key] for key in
                   ("calls", "empty_candidates", "insufficient_idle", "pending_covers_shortfall",
                    "extra_reclaim_while_pending_covers", "victims")})
    normals = [r for r in sim.all_records() if r.request_class == "normal"
               and r.arrival_ms >= sim.warmup_ms and r.outcome == "completed"]
    p95_record = sorted(normals, key=lambda r: r.ttft_ms)[math.ceil(len(normals) * 0.95) - 1]
    job_ends = [job["end"] for job in sim.prefill_jobs]
    normal_parts = []
    for record in normals:
        job = sim.prefill_by_request[record.request_id]
        parts = {"admission_ms": record.admit_ms - record.arrival_ms,
                 "prefill_queue_ms": job["start"] - record.admit_ms,
                 "prefill_compute_ms": job["end"] - job["start"]}
        assert math.isclose(sum(parts.values()), record.ttft_ms, abs_tol=1e-7)
        normal_parts.append(parts)
        if record is p95_record:
            result["p95_request_id"] = record.request_id
            result.update({"p95_" + key: value for key, value in parts.items()})
            blockers = Counter()
            for blocker in sim.prefill_jobs[bisect_right(job_ends, record.admit_ms):]:
                if blocker["start"] >= job["start"]:
                    break
                blockers[blocker["category"]] += max(
                    0, min(blocker["end"], job["start"]) - max(blocker["start"], record.admit_ms)
                )
            assert math.isclose(sum(blockers.values()), parts["prefill_queue_ms"], abs_tol=1e-7)
            result.update({"p95_blocker_" + category + "_ms": blockers[category] for category in
                           ("normal", "stress_initial", "stress_recompute", "stress_cached_resume")})
    for part in normal_parts[0]:
        result["normal_mean_" + part] = mean(item[part] for item in normal_parts)
    resumes = [r for r in sim.all_records() if r.request_id in sim.tool_ends
               and sim.tool_ends[r.request_id] >= sim.warmup_ms]
    done = [r for r in resumes if r.outcome == "completed"]
    result.update(
        resume_turns=len(resumes), resume_done=len(done),
        resume_rejected=sum(r.outcome == "rejected" for r in resumes),
        resume_p95_ms=percentile([r.first_token_ms - sim.tool_ends[r.request_id] for r in done], 95),
        resume_tail_wait_p95_ms=percentile([r.arrival_ms - sim.tool_ends[r.request_id] for r in done], 95),
        resume_tail_wait_positive=sum(r.arrival_ms - sim.tool_ends[r.request_id] > 1e-7 for r in resumes),
        resume_tail_wait_max_ms=max((r.arrival_ms - sim.tool_ends[r.request_id] for r in resumes), default=0),
        normal_reject_idle_mean_blocks=mean([r["idle"] for r in sim.rejections if r["normal"]])
        if any(r["normal"] for r in sim.rejections) else None,
        normal_rejected_despite_own_need_fits=sum(r["normal"] and r["own_need_fits"] for r in sim.rejections),
    )
    input_peak, _, _ = occupancy_exposure(sim.gpu.samples, sim.gpu.total_blocks,
                                          sim.warmup_ms, sim.duration_ms)
    result["mean_occupancy_rechecked"] = input_peak
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=None, help="Only for development checks")
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output already exists; choose a new directory")
    all_results, provenance = [], {}
    for setting, folder in SOURCES.items():
        manifest = json.loads((folder / "experiment.json").read_text(encoding="utf-8"))
        system = SystemConfig(**manifest["system_config"])
        warmup, duration = manifest["workload_config"]["warmup_ms"], manifest["workload_config"]["duration_ms"]
        judgements = {(row["policy"], row["load"], int(row["seed"])): row
                      for row in load_csv(folder / "slo_by_seed.csv")}
        run_dirs = sorted((folder / "runs").iterdir())
        assert len(run_dirs) == len(judgements) == 225
        merged_rows = {row["run_id"]: row for row in load_csv(folder / "summary.csv")}
        trace_hashes = {}
        for trace in sorted((folder / "traces").glob("*.jsonl")):
            trace_meta = json.loads(trace.with_suffix(".meta.json").read_text(encoding="utf-8"))
            assert digest(trace) == trace_meta["sha256"], trace
            assert trace_meta["config"] == manifest["workload_config"], trace
            trace_hashes[trace.name] = digest(trace)
        assert len(trace_hashes) == 45
        provenance[setting] = {"directory": str(folder.relative_to(ROOT)), "runs": 225,
                               "trace_sha256": trace_hashes,
                               "sha256": {name: digest(folder / name) for name in
                                          ("experiment.json", "summary.csv", "comparison.csv", "slo_by_seed.csv")}}
        for run_index, run_dir in enumerate(run_dirs):
            saved = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
            assert saved["config"]["system"] == manifest["system_config"]
            for key, value in merged_rows[run_dir.name].items():
                assert value == csv_cell(saved[key]), (run_dir, key)
            rows = read_trace(folder / "traces" / saved["trace"])
            policy = make_policy(saved["policy"], manifest["policy_configs"][saved["policy"]])
            sim = ObservedSimulator(rows, system, policy, warmup, duration).run()
            summary = summarize(sim, warmup, measurement_end_ms=duration)
            for key, value in summary.items():
                if saved[key] != value:
                    raise AssertionError(f"Summary mismatch {run_dir.name}: {key}: {saved[key]} != {value}")
            verify_csv_rows(run_dir / "events.csv", ([csv_cell(v) for v in event] for event in sim.events))
            verify_csv_rows(run_dir / "occupancy.csv", ([csv_cell(v) for v in (t, used, system.total_blocks)]
                                                        for t, used in sim.gpu.samples))
            verify_csv_rows(run_dir / "requests.csv", ([csv_cell(v) for v in (*asdict(r).values(), r.ttft_ms)]
                                                       for r in sim.all_records()))
            judgement = judgements[saved["policy"], saved["load"], saved["seed"]]
            all_results.append({"setting": setting, **analyze_run(sim, saved, judgement)})
            if (run_index + 1) % 25 == 0:
                print(f"{setting}: {run_index + 1}/225 audited", flush=True)
            if args.limit and len(all_results) >= args.limit:
                break
        if args.limit and len(all_results) >= args.limit:
            break
    args.output.mkdir(parents=True)
    save_csv(args.output / "run_diagnostics.csv", all_results)
    (args.output / "run_diagnostics.json").write_text(json.dumps(all_results, ensure_ascii=False, indent=2)
                                                     + "\n", encoding="utf-8")
    (args.output / "provenance.json").write_text(json.dumps({"sources": provenance,
        "audited_runs": len(all_results), "verification": "all legacy summary values, requests, occupancy, events",
        "observer_sha256": digest(Path(__file__))}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(all_results)} audited run diagnostics to {args.output}", flush=True)


if __name__ == "__main__":
    main()
