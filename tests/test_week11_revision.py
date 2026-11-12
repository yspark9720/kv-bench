from dataclasses import replace
from importlib.util import find_spec
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from kvbench.engine import Simulator
from kvbench.policies import make_policy
from kvbench.week11 import diagnostics
from scripts.run_week11_revision import Observer, ObservedCpu, ObservedServer
from scripts.report_week11_revision import capacity_tables, crossing_brackets, ci, verify_evaluated_sources
from scripts.report_week11 import ci as original_ci
from test_prefix_policies import system, trace


class RevisionObservationTests(unittest.TestCase):
    def test_observer_preserves_replay_and_original_diagnostics(self):
        rows = trace([("a", 0, [60, 10, 10], [100, 200]),
                      ("b", 100, [60, 10], [2000])])
        for policy in ("prefix_lru_evict", "tlru", "prefix_lru_offload", "hotprefix_admission"):
            with self.subTest(policy=policy):
                params = {"threshold_ms": 20} if policy == "tlru" else {}
                plain = Simulator(rows, system(pcie=50), make_policy(policy, params)).run()
                observed = Simulator(rows, system(pcie=50), make_policy(policy, params))
                observer = Observer(observed, 0, 5000)
                observed.run()
                observer.finish()
                self.assertEqual(diagnostics(plain, 0, 5000), diagnostics(observed, 0, 5000))
                self.assertEqual(plain.now, observed.now)
                self.assertEqual(plain.prefill.busy_ms_total, observed.prefill.busy_ms_total)
                self.assertEqual(plain.pcie.busy_ms_total, observed.pcie.busy_ms_total)
                metrics = observer.metrics()
                self.assertLessEqual(metrics["prefill_measurement_busy_pct"], 100)
                self.assertLessEqual(metrics["pcie_measurement_busy_pct"], 100)
                self.assertLessEqual(metrics["cpu_peak_gib"] * 2**30, system().cpu_kv_pool_bytes)

    def test_window_utilization_clips_work_spanning_warmup_and_drain(self):
        observed = ObservedServer(1000, {"measure": (10, 20)})
        self.assertEqual(observed.schedule(0, 15), (0, 15))
        self.assertEqual(observed.schedule(15, 100), (15, 115))
        self.assertEqual(observed.busy_ms_total, 115)
        self.assertEqual(observed.busy_by_window["measure"], 10)

    def test_cpu_peak_counts_reserved_space_without_counting_failed_allocation(self):
        cpu = ObservedCpu(100)
        self.assertTrue(cpu.try_alloc(60))
        self.assertFalse(cpu.try_alloc(50))
        cpu.free(60)
        self.assertTrue(cpu.try_alloc(40))
        self.assertEqual(cpu.peak_bytes, 60)

    def test_measurement_returns_include_session_arriving_before_warmup(self):
        rows = trace([("a", 0, [10, 10], [2000])])
        sim = Simulator(rows, replace(system(), gpu_kv_pool_bytes=1000000),
                        make_policy("prefix_lru_evict"))
        observer = Observer(sim, 1000, 5000)
        sim.run()
        observer.finish()
        self.assertEqual(diagnostics(sim, 1000, 5000)["cohort_stress_jobs"], 0)
        self.assertEqual(observer.metrics()["resume_measurement_count"], 1)


class RevisionProvenanceTests(unittest.TestCase):
    def test_moved_plan_uses_verified_original_snapshot_and_reports_current_difference(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snapshots = root / "provenance"
            snapshots.mkdir()
            original = b"prespecified plan\n"
            expected = hashlib.sha256(original).hexdigest()
            (snapshots / "plan.txt").write_bytes(original)
            (snapshots / "index.json").write_text(json.dumps({"files": {
                "docs/week11_revision_plan.md": {"snapshot": "plan.txt", "sha256": expected}}}))
            result = verify_evaluated_sources(root, {"docs/week11_revision_plan.md": expected}, snapshots)
            self.assertTrue(result["evaluated_sources_verified"])
            self.assertFalse(result["current_source_hashes_match"])
            self.assertEqual(result["archived_source_paths"], ["docs/week11_revision_plan.md"])
            (snapshots / "plan.txt").write_bytes(b"edited after results")
            with self.assertRaisesRegex(AssertionError, "snapshot changed"):
                verify_evaluated_sources(root, {"docs/week11_revision_plan.md": expected}, snapshots)

    def test_snapshot_cannot_hide_changed_simulator_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "kvbench").mkdir()
            (root / "kvbench/engine.py").write_bytes(b"changed simulator")
            snapshots = root / "provenance"
            snapshots.mkdir()
            original = b"original simulator"
            expected = hashlib.sha256(original).hexdigest()
            (snapshots / "engine.txt").write_bytes(original)
            (snapshots / "index.json").write_text(json.dumps({"files": {
                "kvbench/engine.py": {"snapshot": "engine.txt", "sha256": expected}}}))
            with self.assertRaisesRegex(AssertionError, "evaluated source changed"):
                verify_evaluated_sources(root, {"kvbench/engine.py": expected}, snapshots)


@unittest.skipUnless(find_spec("pandas") and find_spec("numpy"), "analysis dependencies not installed")
class RevisionAnalysisTests(unittest.TestCase):
    def test_bootstrap_preserves_original_seeded_draws_and_percentiles(self):
        for values in ([1., -2., 10., 9., 100.], [0.] * 5, [1.25, 12.8, -9.3]):
            for actual, expected in zip(ci(values), original_ci(values)):
                self.assertAlmostEqual(actual, expected, places=10)

    def test_capacity_does_not_turn_a_rejection_or_missing_latency_into_a_pass(self):
        import pandas as pd
        rows = []
        for rate, latency, completion in ((.1, 100, 100), (.2, 100, 98), (.3, None, 100), (.4, 100, 100)):
            for seed in (0, 1):
                rows.append(dict(suite="long", condition="both_4x", pcie_gbps=12,
                                 variant="tlru", stress_rate=rate, seed=seed,
                                 normal_p95_ttft_ms=100, resume_measurement_p95_ms=latency,
                                 stress_completion_pct=completion, normal_reject_rate_pct=0))
        grid, limits = capacity_tables(pd.DataFrame(rows))
        tight = grid[grid.budget_ms == 500].set_index("stress_rate")
        self.assertFalse(tight.loc[.2, "all_seeds_pass"])
        self.assertFalse(tight.loc[.3, "all_seeds_pass"])
        limit = limits[limits.budget_ms == 500].iloc[0]
        self.assertTrue(limit.non_monotone)
        self.assertTrue(limit.upper_boundary_censored)
        self.assertEqual(limit.max_tested_passing_rate, .4)
        self.assertFalse(limit.lower_boundary_censored)
        failed = pd.DataFrame(rows).assign(normal_reject_rate_pct=2)
        _, failed_limits = capacity_tables(failed)
        self.assertTrue(failed_limits.lower_boundary_censored.all())
        self.assertTrue(failed_limits.max_tested_passing_rate.isna().all())

    def test_crossing_reports_only_sampled_bracket_and_keeps_uncertainty(self):
        import pandas as pd
        rows = []
        for bandwidth, delta in ((.6, -20), (.9, -10), (1.5, 30), (3, 40)):
            rows.append(dict(suite="environment", cpu_gib=64, setting="180s_02",
                             condition="both_4x", candidate="tlru", baseline="prefix_lru_offload",
                             pcie_gbps=bandwidth, resume_p95_ms_delta=delta,
                             resume_p95_ms_ci_low=delta-25, resume_p95_ms_ci_high=delta+25))
        brackets = crossing_brackets(pd.DataFrame(rows))
        self.assertEqual(len(brackets), 1)
        self.assertEqual((brackets.iloc[0].pcie_left_gbps, brackets.iloc[0].pcie_right_gbps), (.9, 1.5))
        self.assertGreater(brackets.iloc[0].left_ci_high, 0)


if __name__ == "__main__":
    unittest.main()
