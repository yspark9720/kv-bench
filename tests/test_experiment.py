import csv
import json
from pathlib import Path
import tempfile
import unittest

from kvbench.experiment import W2_FACTORIAL_CONDITIONS, run_cost_sensitivity, W2_FACTORIAL_CONDITIONS, run_ttl_sweep, run_w2_factorial, run_week7
from kvbench.system import load_policy_params
from kvbench.workload import load_config


ROOT = Path(__file__).resolve().parents[1]


class ExperimentTests(unittest.TestCase):
    def test_week7_configs(self):
        workload = load_config(ROOT / "configs/week7.json")
        self.assertEqual(workload.stress_arrival_rate_per_s, 0.2)
        self.assertEqual(
            load_policy_params(ROOT / "configs/policies_week7.json", "fixed_ttl")["ttl_ms"],
            3000,
        )

    def test_reduced_week7_matrix_and_aggregation(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "week7"
            raw, comparison = run_week7(
                ROOT / "configs/week7.json",
                ROOT / "configs/system_1080ti_llama3b.json",
                ROOT / "configs/policies_week7.json",
                output,
                seeds=(0,),
                keep_events=False,
            )
            with raw.open(newline="", encoding="utf-8") as handle:
                raw_rows = list(csv.DictReader(handle))
            with comparison.open(newline="", encoding="utf-8") as handle:
                comparison_rows = list(csv.DictReader(handle))
            self.assertEqual(len(raw_rows), 9)
            self.assertEqual(len(comparison_rows), 9)
            self.assertEqual({row["load"] for row in comparison_rows}, {"1x", "2x", "4x"})
            self.assertTrue(all(row["seeds"] == "1" for row in comparison_rows))
            self.assertFalse(any((output / "runs").glob("*/events.csv")))
            manifest = json.loads((output / "experiment.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["workload_config"]["stress_arrival_rate_per_s"], 0.2)
            self.assertEqual(manifest["policy_configs"]["fixed_ttl"]["ttl_ms"], 3000)
            with self.assertRaises(ValueError):
                run_week7(
                    ROOT / "configs/week7.json",
                    ROOT / "configs/system_1080ti_llama3b.json",
                    ROOT / "configs/policies_week7.json",
                    output,
                    seeds=(0,),
                )

    def test_extra_policy_is_judged_with_the_same_slos(self):
        with tempfile.TemporaryDirectory() as directory:
            _, comparison = run_week7(
                ROOT / "configs/week7.json",
                ROOT / "configs/system_1080ti_llama3b.json",
                ROOT / "configs/policies_week7.json",
                Path(directory) / "week7",
                seeds=(0,),
                policies=("pin_all", "evict_recompute", "fixed_ttl", "lru_offload"),
                keep_events=False,
            )
            with comparison.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 12)
            self.assertEqual({row["load"] for row in rows if row["policy"] == "lru_offload"}, {"1x", "2x", "4x"})

    def test_reduced_cost_sensitivity(self):
        with tempfile.TemporaryDirectory() as directory:
            output = run_cost_sensitivity(
                ROOT / "configs/week7.json", ROOT / "configs/system_1080ti_llama3b.json",
                ROOT / "configs/policies_week7.json", Path(directory) / "sens",
                pcie_gbps=(12, 1.5), cpu_gib=(64, 1), seeds=(0,),
                conditions=W2_FACTORIAL_CONDITIONS[:1], policies=("watermark_offload",),
            )
            with output.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 4)
            self.assertEqual({(r["pcie_gbps"], r["cpu_gib"]) for r in rows},
                             {("12", "64"), ("12", "1"), ("1.5", "64"), ("1.5", "1")})
            system = json.loads((Path(directory) / "sens/systems/pcie1.5_cpu1.json").read_text(encoding="utf-8"))
            self.assertEqual(system["pcie_bytes_per_s"], 1.5e9)
            self.assertEqual(system["cpu_kv_pool_bytes"], 2**30)

    def test_reduced_ttl_sweep(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "ttl"
            raw, comparison = run_ttl_sweep(
                ROOT / "configs/week7.json",
                ROOT / "configs/system_1080ti_llama3b.json",
                output,
                ttl_candidates_ms=(1000, 3000),
                seeds=(0,),
            )
            with raw.open(newline="", encoding="utf-8") as handle:
                self.assertEqual(len(list(csv.DictReader(handle))), 2)
            with comparison.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual([row["ttl_ms"] for row in rows], ["1000", "3000"])
            self.assertTrue(all(float(row["eviction_count_mean"]) > 0 for row in rows))

    def test_reduced_w2_factorial_outputs_paired_conditions(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "factorial"
            conditions = W2_FACTORIAL_CONDITIONS[:5]
            raw, comparison = run_w2_factorial(
                ROOT / "configs/week7.json",
                ROOT / "configs/system_1080ti_llama3b.json",
                ROOT / "configs/policies_week7.json",
                output,
                seeds=(0,), conditions=conditions,
                policies=("fixed_ttl", "duration_aware"), keep_events=True,
            )
            with raw.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            with comparison.open(newline="", encoding="utf-8") as handle:
                comparisons = list(csv.DictReader(handle))
            with (output / "slo_by_seed.csv").open(newline="", encoding="utf-8") as handle:
                judgements = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 10)
            self.assertEqual(len(comparisons), 10)
            self.assertEqual(len(judgements), 10)
            self.assertEqual({row["load"] for row in comparisons}, {item[0] for item in conditions})
            self.assertTrue(all(float(row["measurement_end_ms"]) == 60000 for row in rows))
            self.assertTrue((output / "runs" / "w2_s2_seed0__duration_aware" / "events.csv").exists())


if __name__ == "__main__":
    unittest.main()
