"""Publish reproducible reference gaps, per-condition crossovers and long-run checks."""
from __future__ import annotations

import argparse
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import random
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.report_week11 import LABELS, read_rows, validate as validate_original
from scripts.run_week11 import write_json

VARIANTS = ("prefix_lru_evict", "tlru", "prefix_lru_offload", "hotprefix_admission")
METRICS = ("resume_p95_ms", "normal_p95_ttft_ms", "resume_measurement_p95_ms",
           "transfer_bytes", "recomputed_tokens", "prefill_measurement_busy_pct",
           "pcie_measurement_busy_pct", "cpu_peak_gib", "resume_early_p95_ms",
           "resume_late_p95_ms", "gpu_measurement_mean_pct", "stress_completion_pct",
           "normal_reject_rate_pct")


@lru_cache(maxsize=None)
def bootstrap_indices(n):
    import numpy as np
    rng = random.Random(20261112)
    return np.array([rng.choices(range(n), k=n) for _ in range(2000)])


def ci(values):
    """Same seeded 2,000 bootstrap draws and order statistics as the original report."""
    import numpy as np
    if not values:
        return None, None
    samples = np.sort(np.asarray(values)[bootstrap_indices(len(values))].mean(axis=1))
    return float(samples[50]), float(samples[1950])


def verify_evaluated_sources(root, expected_sources, snapshot_dir=None):
    """Verify old run inputs without treating a moved document as new evidence.

    Only the two documentation-reorganisation paths may use snapshots.
    Simulator and system/policy configuration files must still match the run.
    """
    root = Path(root)
    snapshots = Path(snapshot_dir) if snapshot_dir else root / "docs/data/week11_revision/provenance"
    index_path = snapshots / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else {"files": {}}
    allowed = {"docs/week11_revision_plan.md", "scripts/run_week11_revision.py"}
    archived = []
    for name, expected in expected_sources.items():
        current = root / name
        if current.exists() and hashlib.sha256(current.read_bytes()).hexdigest() == expected:
            continue
        entry = index["files"].get(name) if name in allowed else None
        if not entry or entry["sha256"] != expected:
            raise AssertionError(f"evaluated source changed: {name}")
        snapshot = (snapshots / entry["snapshot"]).resolve()
        if not snapshot.is_relative_to(snapshots.resolve()):
            raise AssertionError("snapshot outside provenance directory")
        if hashlib.sha256(snapshot.read_bytes()).hexdigest() != expected:
            raise AssertionError(f"evaluated snapshot changed: {name}")
        archived.append(name)
    return {"evaluated_sources_verified": True,
            "current_source_hashes_match": not archived,
            "core_simulator_and_configs_match": True,
            "archived_source_paths": sorted(archived)}


def validate(output, raw, original):
    manifest = json.loads((output / "experiment.json").read_text(encoding="utf-8"))
    if manifest["status"] != "complete" or manifest["analysis_rows"] != len(raw):
        raise AssertionError("revision experiment incomplete")
    if not manifest.get("pilot") and (len(raw) != manifest["planned_analysis_rows"]
                                      or manifest["physical_replays"] != manifest["planned_physical_replays"]):
        raise AssertionError("planned grid is incomplete")
    if hashlib.sha256((output / "summary.csv").read_bytes()).hexdigest() != manifest["summary_sha256"]:
        raise AssertionError("revision table hash mismatch")
    source_checks = verify_evaluated_sources(ROOT, manifest["source_sha256"])
    original_path = ROOT / "docs/data/week11"
    original_checks = validate_original(original_path, read_rows(original_path / "summary.csv"))
    if hashlib.sha256((original_path / "summary.csv").read_bytes()).hexdigest() != manifest["original_summary_sha256"]:
        raise AssertionError("original published data changed")
    keys = ["suite", "case_id", "prefill_rate", "pcie_gbps", "cpu_gib", "variant"]
    if raw.duplicated(keys).any():
        raise AssertionError("duplicate revision rows")
    identity = ["duration_ms", "warmup_ms", "stress_rate", "condition", "seed"]
    if raw.groupby(identity).trace_sha256.nunique().max() != 1:
        raise AssertionError("policy/environment changed trace")
    # Same workload, source and machine constants must reproduce published replays.
    original_lookup = {
        (r.setting, r.condition, r.seed, r.variant, r.pcie_gbps, r.cpu_gib): r
        for r in original[original.variant.isin(VARIANTS) & original.suite.isin(["main", "hardware"])].itertuples()
    }
    matched = 0
    for r in raw[(raw.duration_ms == 180000) & (raw.warmup_ms == 10000)
                 & (raw.prefill_rate == 8000) & raw.variant.isin(VARIANTS)].itertuples():
        key = (r.setting, r.condition, r.seed, r.variant, r.pcie_gbps, r.cpu_gib)
        if key in original_lookup:
            old = original_lookup[key]
            if (r.requests_sha256, r.occupancy_sha256) != (old.requests_sha256, old.occupancy_sha256):
                raise AssertionError(f"observer altered original replay: {key}")
            matched += 1
    refs = raw[raw.variant == "pin_all"]
    if (refs.peak_kv_occupancy_pct >= 100).any() or (refs.rejected_jobs != 0).any():
        raise AssertionError("reference memory is constrained")
    if not manifest.get("pilot") and len(raw[raw.suite == "reference"]) != 405:
        raise AssertionError("missing original-grid reference")
    if raw[raw.variant.isin(["tlru", "prefix_lru_evict"])].groupby(
            ["case_id", "prefill_rate", "variant"]).requests_sha256.nunique().max() != 1:
        raise AssertionError("CPU-free replay varies across hardware")
    for resource in ("prefill", "pcie"):
        for window in ("measurement", "early", "late"):
            if not raw[f"{resource}_{window}_busy_pct"].between(0, 100.000001).all():
                raise AssertionError("invalid clipped utilization")
    physical = raw.physical_replay.astype(str).str.lower().eq("true").sum()
    if physical != manifest["physical_replays"]:
        raise AssertionError("physical replay count mismatch")
    return {"analysis_rows": len(raw), "physical_replays": int(physical),
            "identical_original_replays": matched, "original_validation": original_checks,
            "reference_unconstrained": True, "paired_input_hashes_match": True,
            **source_checks}


def paired_effects(raw):
    import pandas as pd
    kv_bytes_per_token = json.loads((ROOT / "configs/system_1080ti_llama3b.json").read_text())["kv_bytes_per_token"]
    output = []
    group = ["suite", "setting", "duration_ms", "warmup_ms", "stress_rate",
             "condition", "prefill_rate", "pcie_gbps", "cpu_gib"]
    comparisons = (("tlru", "prefix_lru_evict"), ("tlru", "prefix_lru_offload"),
                   ("hotprefix_admission", "prefix_lru_offload"),
                   ("hotprefix_admission", "tlru"), ("hotprefix_admission", "prefix_lru_evict"),
                   ("prefix_lru_offload", "prefix_lru_evict"))
    for keys, frame in raw[raw.variant.isin(VARIANTS)].groupby(group):
        for candidate, baseline in comparisons:
            c = frame[frame.variant == candidate].set_index("seed")
            b = frame[frame.variant == baseline].set_index("seed")
            if set(c.index) != set(b.index) or c.empty:
                raise AssertionError("incomplete paired policy grid")
            c, b = c.sort_index(), b.sort_index()
            row = dict(zip(group, keys)) | {"candidate": candidate, "baseline": baseline, "seeds": len(c)}
            row["reload_to_recompute_ratio"] = kv_bytes_per_token * keys[6] / (keys[7] * 1e9)
            for metric in METRICS:
                valid = c[metric].notna() & b[metric].notna()
                differences = (c.loc[valid, metric] - b.loc[valid, metric]).tolist()
                lo, hi = ci(differences)
                row.update({metric + "_delta": sum(differences) / len(differences) if differences else None,
                            metric + "_ci_low": lo, metric + "_ci_high": hi,
                            metric + "_winning_seeds": sum(v < 0 for v in differences)})
            output.append(row)
    return pd.DataFrame(output)


def capacity_tables(raw):
    import pandas as pd
    source = raw[(raw.suite == "long") & (raw.condition == "both_4x")
                 & raw.pcie_gbps.isin([12, .9]) & raw.variant.isin(VARIANTS)].copy()
    all_rows, limits = [], []
    for budget in (500, 1000):
        frame = source.copy()
        frame["passes"] = (frame.normal_p95_ttft_ms.le(budget)
                           & frame.resume_measurement_p95_ms.le(budget)
                           & frame.stress_completion_pct.ge(99)
                           & frame.normal_reject_rate_pct.le(1))
        for keys, values in frame.groupby(["pcie_gbps", "variant", "stress_rate"]):
            all_rows.append(dict(zip(["pcie_gbps", "variant", "stress_rate"], keys))
                            | {"budget_ms": budget, "passed_seeds": int(values.passes.sum()),
                               "seeds": len(values), "all_seeds_pass": bool(values.passes.all()),
                               "normal_p95_ms_mean": values.normal_p95_ttft_ms.mean(),
                               "resume_window_p95_ms_mean": values.resume_measurement_p95_ms.mean(),
                               "completion_pct_min": values.stress_completion_pct.min(),
                               "normal_reject_pct_max": values.normal_reject_rate_pct.max()})
    grid = pd.DataFrame(all_rows)
    for keys, values in grid.groupby(["budget_ms", "pcie_gbps", "variant"]):
        values = values.sort_values("stress_rate")
        passing = values[values.all_seeds_pass]
        highest = passing.stress_rate.max() if not passing.empty else None
        failed_seen, nonmonotone = False, False
        for r in values.itertuples():
            if not r.all_seeds_pass:
                failed_seen = True
            elif failed_seen:
                nonmonotone = True
        limits.append(dict(zip(["budget_ms", "pcie_gbps", "variant"], keys))
                      | {"max_tested_passing_rate": highest,
                         "highest_tested_rate": values.stress_rate.max(),
                         "upper_boundary_censored": highest == values.stress_rate.max(),
                         "lower_boundary_censored": passing.empty,
                         "non_monotone": nonmonotone,
                         "interpretation": "maximum passing TESTED point; not continuous capacity"})
    return grid, pd.DataFrame(limits)


def crossing_brackets(paired):
    import pandas as pd
    rows = []
    frame = paired[(paired.suite == "environment") & (paired.cpu_gib == 64)]
    for keys, values in frame.groupby(["setting", "condition", "candidate", "baseline"]):
        values = values.sort_values("pcie_gbps")
        for left, right in zip(values.to_dict("records"), values.to_dict("records")[1:]):
            a, b = left["resume_p95_ms_delta"], right["resume_p95_ms_delta"]
            if a * b < 0:
                rows.append(dict(zip(["setting", "condition", "candidate", "baseline"], keys))
                            | {"pcie_left_gbps": left["pcie_gbps"], "pcie_right_gbps": right["pcie_gbps"],
                               "left_delta_ms": a, "right_delta_ms": b,
                               "left_ci_low": left["resume_p95_ms_ci_low"],
                               "left_ci_high": left["resume_p95_ms_ci_high"],
                               "right_ci_low": right["resume_p95_ms_ci_low"],
                               "right_ci_high": right["resume_p95_ms_ci_high"]})
    return pd.DataFrame(rows)


def plot(raw, original, directory):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import LogLocator, NullFormatter, ScalarFormatter
    from matplotlib.lines import Line2D
    directory.mkdir(parents=True, exist_ok=True)
    colors = dict(zip(VARIANTS, ("#1f77b4", "#ff7f0e", "#2ca02c", "#d62728")))
    refs = raw[(raw.suite == "reference") & (raw.variant == "pin_all")][
        ["setting", "condition", "seed", "resume_p95_ms"]].rename(columns={"resume_p95_ms": "reference"})
    main = original[(original.suite == "main") & original.variant.isin(VARIANTS)].merge(
        refs, on=["setting", "condition", "seed"], validate="many_to_one")
    main["gap"] = main.resume_p95_ms - main.reference
    fig, ax = plt.subplots(figsize=(12, 5))
    settings = ("60s_02", "180s_02", "180s_03")
    for i, variant in enumerate(VARIANTS):
        values = [main[(main.setting == setting) & (main.variant == variant)].gap.mean() for setting in settings]
        ax.bar([j + (i-1.5)*.2 for j in range(3)], values, .2, label=LABELS[variant], color=colors[variant])
    ax.set_xticks(range(3), ["60s / 0.2", "180s / 0.2", "180s / 0.3"])
    ax.set_ylabel("Resume P95 gap to memory-unconstrained reference (ms)")
    ax.set_title("Original default environment: remaining latency gap\nNine conditions, 15 paired seeds; means of run-level P95 differences")
    ax.grid(axis="y", alpha=.2)
    ax.legend(loc="upper left")
    fig.tight_layout()
    fig.savefig(directory / "week11_revision_reference.png", dpi=150)
    plt.close(fig)

    def axis_figure(source, xkey, metric, stem, heading, groups, ref_source, xlabel):
        fig, axes = plt.subplots(2, 2, figsize=(13, 8))
        any_rejection = False
        for ax, (condition, rate) in zip(axes.flat, groups):
            selected = source[(source.condition == condition) & (source.stress_rate == rate)]
            if selected.empty:
                ax.set_axis_off()
                continue
            for variant in VARIANTS:
                xs, ys, lows, highs = [], [], [], []
                for x, values in selected[selected.variant == variant].groupby(xkey):
                    numbers = values[metric].dropna().tolist()
                    if not numbers:
                        continue
                    lo, hi = ci(numbers)
                    xs.append(x); ys.append(sum(numbers) / len(numbers)); lows.append(lo); highs.append(hi)
                    if values.stress_completion_pct.lt(100 - 1e-8).any() or values.normal_reject_rate_pct.gt(0).any():
                        any_rejection = True
                        ax.scatter(x, ys[-1], marker="x", color="black", s=65, zorder=5)
                ax.plot(xs, ys, marker="o", label=LABELS[variant], color=colors[variant])
                ax.fill_between(xs, lows, highs, alpha=.1, color=colors[variant])
            ref = ref_source[(ref_source.condition == condition) & (ref_source.stress_rate == rate)]
            demand = ""
            if not ref.empty:
                ax.axhline(ref[metric].mean(), linestyle="--", color="black", linewidth=1,
                           label="Memory-unconstrained reference")
                ratio = (ref.gpu_measurement_mean_pct / 100 * ref.gpu_gib / 4).mean()
                demand = f"\nReference demand {ratio:.2f}x GPU / prefill {ref.prefill_measurement_busy_pct.mean():.0f}%"
            ax.set_title(f"{condition}, stress {rate:g}/s" + demand, fontsize=10)
            ax.set_xscale("log")
            ax.set_yscale("log")
            ax.yaxis.set_major_locator(LogLocator(base=10, subs=(1, 2, 5)))
            ax.yaxis.set_major_formatter(ScalarFormatter())
            ax.yaxis.set_minor_formatter(NullFormatter())
            ticks = sorted(selected[xkey].unique())
            ax.set_xticks(ticks, [f"{x:g}" for x in ticks])
            ax.set_xlabel(xlabel)
            ax.set_ylabel("Resume P95 (ms, log scale)")
            ax.grid(alpha=.2)
        handles, labels = axes[0, 0].get_legend_handles_labels()
        if any_rejection:
            handles.append(Line2D([0], [0], color="black", marker="x", linestyle="None"))
            labels.append("Rejection in at least one seed")
        fig.legend(handles, labels, loc="lower center", ncol=3, frameon=False, fontsize=9)
        fig.suptitle(heading + "\nFive paired seeds; bands: seed-bootstrap 95% intervals; no averaging across conditions")
        fig.tight_layout(rect=(0, .09, 1, .92))
        fig.savefig(directory / (stem + ".png"), dpi=150)
        plt.close(fig)

    groups = (("base_4x", .2), ("both_4x", .2), ("base_4x", .3), ("both_4x", .3))
    env = raw[raw.suite == "environment"]
    reference = raw[(raw.suite == "reference") & raw.condition.isin(["base_4x", "both_4x"])
                    & raw.setting.isin(["180s_02", "180s_03"]) & raw.seed.lt(5)]
    axis_figure(env[env.cpu_gib == 64], "pcie_gbps", "resume_p95_ms",
                "week11_revision_pcie", "180s / warm-up 10s: PCIe axis (CPU 64GiB)",
                groups, reference, "PCIe (GB/s)")
    axis_figure(env[env.pcie_gbps == 12], "cpu_gib", "resume_p95_ms",
                "week11_revision_cpu", "180s / warm-up 10s: CPU axis (PCIe 12GB/s)",
                groups, reference, "CPU KV pool (GiB)")
    long = raw[(raw.suite == "long") & raw.variant.isin(VARIANTS)]
    axis_figure(long, "pcie_gbps", "resume_measurement_p95_ms", "week11_revision_long",
                "900s / warm-up 300s: returns READY inside [300, 900)s",
                groups, raw[(raw.suite == "long") & (raw.variant == "pin_all")], "PCIe (GB/s)")

    fig, axes = plt.subplots(1, 2, figsize=(13, 5))
    for ax, bandwidth in zip(axes, (12, .9)):
        source = long[(long.condition == "both_4x") & (long.pcie_gbps == bandwidth)]
        for variant in VARIANTS:
            values = source[source.variant == variant].groupby("stress_rate").resume_measurement_p95_ms.mean()
            ax.plot(values.index, values, marker="o", label=LABELS[variant], color=colors[variant])
        reference = raw[(raw.suite == "long") & (raw.condition == "both_4x")
                        & (raw.variant == "pin_all")].groupby("stress_rate").resume_measurement_p95_ms.mean()
        ax.plot(reference.index, reference, color="black", linestyle="--",
                label="Memory-unconstrained reference")
        for threshold in (500, 1000):
            ax.axhline(threshold, color="grey", linestyle=":", linewidth=1)
        ax.set_title(f"both_4x, PCIe {bandwidth:g}GB/s / CPU 64GiB")
        ax.set_xlabel("Stress sessions / second (normal arrival fixed at 2/s)")
        ax.set_ylabel("Resume window P95 (ms)")
        ax.grid(alpha=.2)
    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="lower center", ncol=3, frameon=False, fontsize=9)
    fig.suptitle("Tested load grid, 900s / warm-up 300s\nMeans of five run-level P95s; pass counts additionally check normal latency and completion")
    fig.tight_layout(rect=(0, .13, 1, .91))
    fig.savefig(directory / "week11_revision_capacity.png", dpi=150)
    plt.close(fig)

    fig, axes = plt.subplots(1, 2, figsize=(13, 5))
    for ax, condition in zip(axes, ("base_4x", "both_4x")):
        frame = raw[(raw.suite == "prefill") & (raw.condition == condition)]
        for variant in (*VARIANTS, "pin_all"):
            values = frame[frame.variant == variant].groupby("prefill_rate").resume_p95_ms.mean()
            ax.plot(values.index, values, marker="o",
                    label=LABELS.get(variant, "Memory-unconstrained reference"),
                    color=colors.get(variant, "black"), linestyle="--" if variant == "pin_all" else "-")
        ax.set_title(condition)
        ax.set_xlabel("Assumed prefill throughput (tokens/s), not hardware measurements")
        ax.set_ylabel("Resume cohort P95 (ms)")
        ax.set_yscale("log")
        ax.yaxis.set_major_locator(LogLocator(base=10, subs=(1, 2, 5)))
        ax.yaxis.set_major_formatter(ScalarFormatter())
        ax.yaxis.set_minor_formatter(NullFormatter())
        ax.set_ylabel("Resume cohort P95 (ms, log scale)")
        ax.set_xticks([2000, 4000, 8000])
        ax.grid(alpha=.2)
    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="lower center", ncol=3, frameon=False, fontsize=9)
    fig.suptitle("Prefill-constant sensitivity, 180s / stress 0.2 / five paired seeds")
    fig.tight_layout(rect=(0, .13, 1, .91))
    fig.savefig(directory / "week11_revision_prefill.png", dpi=150)
    plt.close(fig)






def report(output, publish=True):
    import pandas as pd
    output = Path(output)
    raw = pd.read_csv(output / "summary.csv")
    original = pd.read_csv(ROOT / "docs/data/week11/summary.csv")
    checks = validate(output, raw, original)
    checks["analysis_source_sha256"] = {
        p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (Path(__file__), ROOT / "scripts/build_week11_notebook.py",
                  ROOT / "scripts/build_week11_evaluation_notebook.py",
                  ROOT / "scripts/week11_revision_notebook.py", ROOT / "scripts/report_week11.py")}
    checks["unique_input_traces"] = int(raw.trace_sha256.nunique())
    effects = paired_effects(raw)
    grid, limits = capacity_tables(raw)
    refs = raw[raw.suite == "reference"][["setting", "condition", "seed", "resume_p95_ms",
                                        "normal_p95_ttft_ms", "requests_sha256",
                                        "gpu_measurement_mean_pct"]].rename(
        columns={k: "reference_" + k for k in
                 ("resume_p95_ms", "normal_p95_ttft_ms", "requests_sha256", "gpu_measurement_mean_pct")})
    headroom = original[original.suite == "main"].merge(refs, on=["setting", "condition", "seed"],
                                                       validate="many_to_one")
    for metric in ("resume_p95_ms", "normal_p95_ttft_ms"):
        headroom[metric + "_gap_ms"] = headroom[metric] - headroom["reference_" + metric]
    headroom["identical_reference_requests"] = headroom.requests_sha256 == headroom.reference_requests_sha256
    grouped = headroom.groupby(["setting", "variant"])
    headroom_summary = grouped[["resume_p95_ms", "normal_p95_ttft_ms", "reference_resume_p95_ms",
                               "reference_normal_p95_ttft_ms", "resume_p95_ms_gap_ms",
                               "normal_p95_ttft_ms_gap_ms", "reference_gpu_measurement_mean_pct"]].mean().reset_index()
    normal_parts = ["normal_p95_admission_ms", "normal_p95_restore_ms",
                    "normal_p95_prefill_queue_ms", "normal_p95_self_prefill_ms"]
    normal_components = original[(original.suite == "main") & (original.setting == "180s_02")
                                 & original.variant.isin((*VARIANTS, "lru_offload"))].groupby("variant")[
        ["normal_p95_ttft_ms"] + normal_parts].mean().reset_index()
    if (normal_components[normal_parts].sum(axis=1) - normal_components.normal_p95_ttft_ms).abs().max() > .11:
        raise AssertionError("normal latency components do not add up")
    gap_metrics = ["resume_p95_ms", "resume_measurement_p95_ms", "normal_p95_ttft_ms"]
    ref_keys = ["trace_sha256", "prefill_rate", "warmup_ms"]
    reference_lookup = raw[raw.variant == "pin_all"].drop_duplicates(ref_keys)[
        ref_keys + gap_metrics].rename(columns={m: "reference_" + m for m in gap_metrics})
    reference_gaps = raw[raw.variant.isin(VARIANTS)][
        ["suite", "setting", "condition", "seed", "variant", "stress_rate", "duration_ms",
         "warmup_ms", "prefill_rate", "pcie_gbps", "cpu_gib", "trace_sha256"] + gap_metrics].merge(
             reference_lookup, on=ref_keys, validate="many_to_one", how="left", indicator=True)
    if not reference_gaps["_merge"].eq("both").all():
        raise AssertionError("missing matched unconstrained reference")
    reference_gaps = reference_gaps.drop(columns="_merge")
    for metric in gap_metrics:
        reference_gaps[metric + "_gap_ms"] = reference_gaps[metric] - reference_gaps["reference_" + metric]
    cpu_rows = raw[(raw.suite == "environment") & (raw.pcie_gbps == 12)
                   & raw.variant.isin(["prefix_lru_offload", "hotprefix_admission"])]
    cpu_base = cpu_rows[cpu_rows.cpu_gib == 64][
        ["setting", "condition", "seed", "variant", "requests_sha256"]].rename(
            columns={"requests_sha256": "default_requests_sha256"})
    cpu_pairs = cpu_rows.merge(cpu_base, on=["setting", "condition", "seed", "variant"],
                              validate="many_to_one")
    cpu_pairs["identical_reference_cpu"] = cpu_pairs.requests_sha256 == cpu_pairs.default_requests_sha256
    cpu_equivalence = cpu_pairs.groupby(["setting", "condition", "variant", "cpu_gib"]).agg(
        identical_default_seeds=("identical_reference_cpu", "sum"), seeds=("seed", "size"),
        cpu_peak_gib_max=("cpu_peak_gib", "max")).reset_index()
    environment_outcomes = raw[raw.suite == "environment"].groupby(
        ["setting", "condition", "variant", "pcie_gbps", "cpu_gib"]).agg(
        stress_completion_pct_mean=("stress_completion_pct", "mean"),
        stress_completion_pct_min=("stress_completion_pct", "min"),
        normal_reject_rate_pct_max=("normal_reject_rate_pct", "max")).reset_index()
    regime_summary = raw[(raw.suite == "long") & (raw.condition == "base_4x")
                         & raw.stress_rate.eq(.3) & raw.variant.isin(VARIANTS)].pivot_table(
        index="pcie_gbps", columns="variant", values="resume_measurement_p95_ms", aggfunc="mean")
    regime_summary = regime_summary.reindex(columns=VARIANTS).sort_index(ascending=False).reset_index()
    flags = []
    for (setting, variant), frame in grouped:
        flags.append({"setting": setting, "variant": variant, "runs": len(frame),
                      "legacy_slo_passes": int(frame.legacy_all_slo_pass.sum()),
                      "identical_reference_requests": int(frame.identical_reference_requests.sum()),
                      "identical_reference_but_slo_fails": int((frame.identical_reference_requests & ~frame.legacy_all_slo_pass).sum())})
    stability = []
    for keys, frame in raw[(raw.suite == "long") & raw.stress_rate.isin([.2, .3])].groupby(
            ["condition", "stress_rate", "pcie_gbps", "variant"]):
        delta = (frame.resume_late_p95_ms - frame.resume_early_p95_ms).dropna().tolist()
        lo, hi = ci(delta)
        stability.append(dict(zip(["condition", "stress_rate", "pcie_gbps", "variant"], keys))
                         | {"early_window_p95_mean_ms": frame.resume_early_p95_ms.mean(),
                            "late_window_p95_mean_ms": frame.resume_late_p95_ms.mean(),
                            "late_minus_early_ms": sum(delta) / len(delta) if delta else None,
                            "ci_low": lo, "ci_high": hi, "seeds": len(delta)})
    mechanism_cols = [c for c in raw.columns if c.startswith("cpu_admission_")]
    mechanism_rows = raw[raw.suite == "environment"].copy()
    # An absent event reason is a zero count, not an unobserved measurement.
    mechanism_rows[mechanism_cols] = mechanism_rows[mechanism_cols].fillna(0)
    mechanisms = mechanism_rows.groupby(
        ["setting", "condition", "pcie_gbps", "cpu_gib", "variant"])[
            mechanism_cols + ["cpu_blocks_evicted", "cpu_peak_gib", "prefill_measurement_busy_pct",
                              "pcie_measurement_busy_pct", "prefill_busy_pct", "pcie_busy_pct"]].mean().reset_index()
    reference_rows = raw[raw.variant == "pin_all"].copy()
    reference_rows["reference_memory_demand_ratio"] = (
        reference_rows.gpu_measurement_mean_pct / 100 * reference_rows.gpu_gib / 4)
    resource_demand = reference_rows.groupby(
        ["suite", "setting", "duration_ms", "warmup_ms", "stress_rate", "condition", "prefill_rate"])[
            ["reference_memory_demand_ratio", "prefill_measurement_busy_pct",
             "gpu_measurement_mean_pct", "resume_measurement_p95_ms"]].mean().reset_index()
    tables = {"paired.csv": effects, "capacity_grid.csv": grid, "capacity_limits.csv": limits,
              "crossing_brackets.csv": crossing_brackets(effects), "headroom.csv": headroom,
              "headroom_summary.csv": headroom_summary, "slo_reassessment.csv": pd.DataFrame(flags),
              "stability.csv": pd.DataFrame(stability), "mechanisms.csv": mechanisms,
              "resource_demand.csv": resource_demand, "reference_gaps.csv": reference_gaps,
              "cpu_equivalence.csv": cpu_equivalence, "normal_tail_components.csv": normal_components}
    tables["environment_outcomes.csv"] = environment_outcomes
    tables["regime_summary.csv"] = regime_summary
    for name, frame in tables.items():
        frame.to_csv(output / name, index=False)
    write_json(output / "validation.json", checks)
    if publish:
        dest = ROOT / "docs/data/week11_revision"
        dest.mkdir(parents=True, exist_ok=True)
        for name in ("summary.csv", "experiment.json", "validation.json", *tables):
            shutil.copyfile(output / name, dest / name)
        plot(raw, original, ROOT / "docs/figures")
        # The curated narrative is maintained in docs/week11_experiment.md.
        # Publishing data must not recreate the former split reports.
    print(json.dumps(checks, indent=2), flush=True)
    return checks


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    report(args.output)
