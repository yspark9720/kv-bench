"""Replay the fixed week-10 matrix at several Watermark admission ceilings.

Run from the repository root. Existing policy defaults and simulator behavior are unchanged.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kvbench.experiment import W2_FACTORIAL_CONDITIONS, run_w2_factorial
from kvbench.metrics import percentile
from kvbench.system import load_system_config
from kvbench.workload import load_config


SETTINGS = {"60s_02": ROOT / "configs/week7.json",
            "180s_02": ROOT / "configs/week7_180s_stress02.json",
            "180s_03": ROOT / "configs/week7_180s_stress03.json"}
REFERENCES = {"60s_02": ROOT / "results/w2_factorial",
              "180s_02": ROOT / "results/w2_factorial_180s_stress02_review_20261027",
              "180s_03": ROOT / "results/w2_factorial_180s_stress03_review_20261027"}
CEILINGS = (0.8, 0.85, 0.9, 0.95, 1.0)


def read_csv(path):
    with Path(path).open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def write_csv(path, rows):
    with Path(path).open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def supplementary_metrics(run, warmup_ms, ceiling):
    requests = read_csv(run / "requests.csv")
    tool_ends = {}
    with (run / "events.csv").open(newline="", encoding="utf-8") as handle:
        for event in csv.DictReader(handle):
            if event["event"] == "TOOL_END":
                request_id = f"{event['session_id']}:{int(event['turn_id']) + 1}"
                tool_ends[request_id] = float(event["time_ms"])
    normal = [r for r in requests if r["request_class"] == "normal"
              and float(r["arrival_ms"]) >= warmup_ms and r["outcome"] == "completed"]
    admission_wait = [float(r["admit_ms"]) - float(r["arrival_ms"]) for r in normal]
    resumes = [r for r in requests if r["request_class"] == "stress" and r["request_id"] in tool_ends
               and tool_ends[r["request_id"]] >= warmup_ms]
    completed = [r for r in resumes if r["outcome"] == "completed"]
    resume_ms = [float(r["first_token_ms"]) - tool_ends[r["request_id"]] for r in completed]
    resume_tail = [float(r["arrival_ms"]) - tool_ends[r["request_id"]] for r in completed]
    with (run / "occupancy.csv").open(newline="", encoding="utf-8") as handle:
        samples = csv.DictReader(handle)
        peak, violations, limit = 0, 0, None
        for sample in samples:
            limit = math.floor(int(sample["total_blocks"]) * ceiling)
            used = int(sample["used_blocks"])
            peak = max(peak, used)
            violations += used > limit
    if limit is None:
        raise ValueError(f"no occupancy samples: {run}")
    if violations:
        raise AssertionError(f"admission ceiling violated: {run}")
    return {"admission_limit_blocks": limit, "used_blocks_max": peak, "ceiling_violations": violations,
            "normal_p95_admission_wait_ms": percentile(admission_wait, 95),
            "normal_completed_with_admission_wait": sum(v > 1e-7 for v in admission_wait),
            "stress_resume_turns": len(resumes), "stress_resume_completed_turns": len(completed),
            "stress_resume_rejected_turns": sum(r["outcome"] == "rejected" for r in resumes),
            "stress_p95_resume_ms": percentile(resume_ms, 95),
            "stress_p95_offload_tail_wait_ms": percentile(resume_tail, 95)}


def assert_same_run(first, second):
    left = json.loads((first / "summary.json").read_text(encoding="utf-8"))
    right = json.loads((second / "summary.json").read_text(encoding="utf-8"))
    if left["config"]["system"] != right["config"]["system"]:
        raise AssertionError(f"system configuration mismatch: {first} / {second}")
    for data in (left, right):
        data.pop("policy")
        data.pop("config")
        data.pop("run_id")
    if left != right:
        raise AssertionError(f"summary mismatch: {first} / {second}")
    for filename in ("requests.csv", "occupancy.csv", "events.csv"):
        if sha256(first / filename) != sha256(second / filename):
            raise AssertionError(f"{filename} mismatch: {first} / {second}")


def run_ceiling_sweep(output, *, settings=None, system_path=None, policies_path=None,
                      ceilings=CEILINGS, seeds=tuple(range(15)), conditions=W2_FACTORIAL_CONDITIONS,
                      reference_dirs=None, progress=print):
    settings = dict(SETTINGS if settings is None else settings)
    references = REFERENCES if reference_dirs is None else reference_dirs
    system_path = Path(system_path or ROOT / "configs/system_1080ti_llama3b.json")
    policies_path = Path(policies_path or ROOT / "configs/policies_week7.json")
    output, ceilings = Path(output), tuple(float(c) for c in ceilings)
    seeds, conditions = tuple(seeds), tuple(conditions)
    if not ceilings or len(set(ceilings)) != len(ceilings) or any(
        not math.isfinite(c) or not 0 < c <= 1 for c in ceilings
    ):
        raise ValueError("ceilings must be distinct finite values in (0, 1]")
    if not seeds or len(set(seeds)) != len(seeds) or any(type(s) is not int or s < 0 for s in seeds):
        raise ValueError("seeds must be distinct nonnegative integers")
    if not settings or not any(c[0] == "w0_1x" for c in conditions):
        raise ValueError("settings must be nonempty and conditions must include w0_1x")
    configs = {name: load_config(path) for name, path in settings.items()}
    load_system_config(system_path)
    base_params = json.loads(policies_path.read_text(encoding="utf-8"))
    if output.exists():
        raise ValueError("output already exists; choose a new path")
    (output / "policies").mkdir(parents=True)
    variants = [("lru_offload", "lru_offload", 1.0)] + [
        (f"watermark_c{c:g}", "watermark_offload", c) for c in ceilings
    ]
    source_files = [Path(__file__)] + sorted((ROOT / "kvbench").glob("*.py")) + sorted(
        (ROOT / "kvbench/policies").glob("*.py"))
    manifest = {"experiment": "watermark_ceiling_sensitivity", "status": "running",
                "base_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
                "settings": {k: str(Path(v).relative_to(ROOT)) for k, v in settings.items()},
                "ceilings": ceilings, "seeds": seeds, "conditions": conditions,
                "system_sha256": sha256(system_path), "policies_sha256": sha256(policies_path),
                "source_sha256": {str(p.relative_to(ROOT)): sha256(p) for p in source_files},
                "expected_runs": len(settings) * len(variants) * len(seeds) * len(conditions)}
    write_json(output / "experiment.json", manifest)
    for variant, policy, ceiling in variants:
        params = dict(base_params.get(policy, {}))
        if policy == "watermark_offload":
            params["admission_ceiling"] = ceiling
        write_json(output / "policies" / f"{variant}.json", {policy: params})
    raw_rows, comparison_rows, slo_rows, trace_hashes = [], [], [], {}
    equivalence_count, legacy_count = 0, 0
    for setting, config_path in settings.items():
        hashes = None
        for variant, policy, ceiling in variants:
            if progress:
                progress(f"{setting}: {variant} ({len(conditions) * len(seeds)} runs)")
            folder = output / setting / variant
            raw_file, comparison_file = run_w2_factorial(
                config_path, system_path, output / "policies" / f"{variant}.json", folder,
                seeds=seeds, conditions=conditions, policies=(policy,), keep_events=True,
            )
            current_hashes = {p.name: sha256(p) for p in sorted((folder / "traces").glob("*.jsonl"))}
            if hashes is not None and hashes != current_hashes:
                raise AssertionError("variants did not use identical traces")
            hashes = current_hashes
            metadata = {"setting": setting, "variant": variant, "admission_ceiling": ceiling}
            for row in read_csv(raw_file):
                run = folder / "runs" / row["run_id"]
                extra = supplementary_metrics(run, configs[setting].warmup_ms, ceiling)
                raw_rows.append({**metadata, "relative_run_dir": run.relative_to(output).as_posix(), **row, **extra})
                if policy == "watermark_offload" and ceiling == 1:
                    baseline = output / setting / "lru_offload/runs" / row["run_id"].replace(
                        "__watermark_offload", "__lru_offload")
                    assert_same_run(run, baseline)
                    equivalence_count += 1
                reference = references.get(setting)
                if policy == "lru_offload" and reference is not None and int(row["seed"]) < 5:
                    original = Path(reference) / "runs" / row["run_id"]
                    if original.exists():
                        assert_same_run(run, original)
                        legacy_count += 1
            comparison_rows += [{**metadata, **row} for row in read_csv(comparison_file)]
            slo_rows += [{**metadata, **row} for row in read_csv(folder / "slo_by_seed.csv")]
        trace_hashes[setting] = hashes
    if len(raw_rows) != manifest["expected_runs"]:
        raise AssertionError("run count does not match the experiment plan")
    write_csv(output / "ceiling_runs.csv", raw_rows)
    write_csv(output / "ceiling_comparison.csv", comparison_rows)
    write_csv(output / "ceiling_slo_by_seed.csv", slo_rows)
    manifest.update(status="complete", actual_runs=len(raw_rows), trace_sha256=trace_hashes,
                    watermark_100_lru_exact_matches=equivalence_count,
                    legacy_lru_exact_matches=legacy_count,
                    occupancy_ceiling_violations=sum(r["ceiling_violations"] for r in raw_rows))
    write_json(output / "experiment.json", manifest)
    return output / "ceiling_runs.csv"


def parse_seeds(value):
    if "-" in value:
        low, high = (int(v) for v in value.split("-"))
        return tuple(range(low, high + 1))
    return tuple(int(v) for v in value.split(","))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ceilings", default="0.8,0.85,0.9,0.95,1.0")
    parser.add_argument("--seeds", default="0-14")
    parser.add_argument("--settings", default=",".join(SETTINGS))
    args = parser.parse_args()
    names = args.settings.split(",")
    if len(set(names)) != len(names) or set(names) - set(SETTINGS):
        parser.error(f"settings must be a distinct subset of {list(SETTINGS)}")
    try:
        path = run_ceiling_sweep(args.output, settings={k: SETTINGS[k] for k in names},
                                 ceilings=tuple(float(c) for c in args.ceilings.split(",")),
                                 seeds=parse_seeds(args.seeds), progress=lambda s: print(s, flush=True))
    except (OSError, ValueError) as error:
        parser.exit(2, f"error: {error}\n")
    print(f"Results: {path}", flush=True)


if __name__ == "__main__":
    main()
