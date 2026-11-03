import csv
import json
from pathlib import Path
import tempfile
import unittest

from scripts.run_watermark_ceiling import ROOT, run_ceiling_sweep, supplementary_metrics
from kvbench.experiment import W2_FACTORIAL_CONDITIONS


class WatermarkCeilingTests(unittest.TestCase):
    def test_paired_sweep_ceiling_invariant_and_lru_equivalence(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "sweep"
            raw = run_ceiling_sweep(
                output, settings={"small": ROOT / "configs/week7.json"},
                ceilings=(0.8, 0.9, 1.0), seeds=(0,),
                conditions=W2_FACTORIAL_CONDITIONS[:2], reference_dirs={}, progress=None,
            )
            manifest = json.loads((output / "experiment.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "complete")
            self.assertEqual(manifest["actual_runs"], 8)
            self.assertEqual(manifest["watermark_100_lru_exact_matches"], 2)
            with raw.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 8)
            self.assertTrue(all(int(row["used_blocks_max"]) <= int(row["admission_limit_blocks"]) for row in rows))
            self.assertTrue(all(int(row["stress_resume_turns"]) >= int(row["stress_resume_completed_turns"]) for row in rows))
            with (output / "ceiling_slo_by_seed.csv").open(newline="", encoding="utf-8") as handle:
                slo = list(csv.DictReader(handle))
            for variant in {row["variant"] for row in slo}:
                own = [row for row in slo if row["variant"] == variant]
                w0 = next(row for row in own if row["load"] == "w0_1x")
                self.assertEqual(float(w0["ttft_ratio_vs_policy_w0"]), 1)
                self.assertTrue(all(row["w0_p95_ttft_ms"] == w0["normal_p95_ttft_ms"] for row in own))
            with self.assertRaises(ValueError):
                run_ceiling_sweep(output)

    def test_invalid_values_leave_no_output(self):
        with tempfile.TemporaryDirectory() as directory:
            for index, ceilings in enumerate(((0,), (0.9, 0.9), (float("nan"),), (1.1,), ())):
                output = Path(directory) / str(index)
                with self.subTest(ceilings=ceilings), self.assertRaises(ValueError):
                    run_ceiling_sweep(output, ceilings=ceilings, progress=None)
                self.assertFalse(output.exists())

    def test_resume_latency_includes_offload_tail_and_reports_rejections(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            files = {
                "requests.csv": (
                    ["request_id", "request_class", "arrival_ms", "admit_ms", "first_token_ms", "outcome"],
                    [["normal-0:0", "normal", 100, 120, 180, "completed"],
                     ["stress-0:1", "stress", 160, 170, 210, "completed"],
                     ["stress-0:2", "stress", 220, "", "", "rejected"]]),
                "events.csv": (
                    ["time_ms", "event", "session_id", "turn_id"],
                    [[100, "TOOL_END", "stress-0", 0], [220, "TOOL_END", "stress-0", 1]]),
                "occupancy.csv": (["time_ms", "used_blocks", "total_blocks"], [[0, 0, 10], [100, 8, 10]]),
            }
            for filename, (header, rows) in files.items():
                with (run / filename).open("w", newline="", encoding="utf-8") as handle:
                    writer = csv.writer(handle)
                    writer.writerow(header)
                    writer.writerows(rows)
            metrics = supplementary_metrics(run, 100, 0.8)
            self.assertEqual(metrics["stress_p95_resume_ms"], 110)
            self.assertEqual(metrics["stress_p95_offload_tail_wait_ms"], 60)
            self.assertEqual(metrics["normal_p95_admission_wait_ms"], 20)
            self.assertEqual(metrics["stress_resume_turns"], 2)
            self.assertEqual(metrics["stress_resume_completed_turns"], 1)
            self.assertEqual(metrics["stress_resume_rejected_turns"], 1)


if __name__ == "__main__":
    unittest.main()
