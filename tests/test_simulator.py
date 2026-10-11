from dataclasses import asdict, replace
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from kvbench.engine import Simulator
from kvbench.metrics import summarize
from kvbench.policies import REGISTRY, make_policy
from kvbench.simulate import collect, simulate, write_results
from kvbench.system import SystemConfig, load_policy_params, load_system_config
from kvbench.workload import generate, load_config, write_trace


ROOT = Path(__file__).resolve().parents[1]
POLICIES = ["pin_all", "evict_recompute", "fixed_ttl", "lru_offload", "duration_aware"]


class SimulatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workload = load_config(ROOT / "configs/smoke.json")
        cls.system = load_system_config(ROOT / "configs/system_1080ti_llama3b.json")
        # A small pool so that W1 at 4x runs out of blocks and every policy has to act.
        cls.small = replace(cls.system, gpu_kv_pool_bytes=2**30, admission_max_wait_ms=10000)

    def run_policy(self, policy, workload="W1", level=4, seed=0, system=None, **params):
        rows = generate(self.workload, workload, level, seed)
        sim = Simulator(rows, system or self.small, make_policy(policy, params)).run()
        return sim, summarize(sim, self.workload.warmup_ms)

    def test_every_policy_runs_to_completion(self):
        for policy in POLICIES:
            with self.subTest(policy=policy):
                sim, summary = self.run_policy(policy)
                self.assertTrue(all(s.state.value in ("done", "rejected") for s in sim.sessions.values()))
                self.assertEqual(sim.gpu.used_blocks, 0)
                self.assertEqual(sim.cpu.used_bytes, 0)
                for record in sim.all_records():
                    if record.outcome == "completed":
                        self.assertLessEqual(record.arrival_ms, record.admit_ms)
                        self.assertLessEqual(record.admit_ms, record.first_token_ms)
                        self.assertLessEqual(record.first_token_ms, record.complete_ms)
                self.assertGreater(summary["normal_submitted"], 0)
                self.assertTrue(0 <= summary["peak_kv_occupancy_pct"] <= 100)

    def test_pin_all_never_evicts_and_evict_recomputes(self):
        _, pinned = self.run_policy("pin_all")
        sim, evicted = self.run_policy("evict_recompute")
        self.assertEqual(pinned["eviction_count"], 0)
        self.assertEqual(pinned["recomputed_tokens"], 0)
        self.assertGreater(evicted["recomputed_tokens"], 0)
        self.assertTrue(all(r.restore in ("none", "recompute") for r in sim.all_records()))
        # Every idle entry is followed by an eviction and the recomputed prefix is the cached context.
        idle = sum(e[1] == "TOOL_START" for e in sim.events)
        self.assertEqual(idle, sum(e[1] == "EVICT" for e in sim.events))

    def test_fixed_ttl_evicts_exactly_after_ttl(self):
        sim, summary = self.run_policy("fixed_ttl", ttl_ms=1000)
        self.assertGreater(summary["eviction_count"], 0)
        starts = {(e[2], e[3]): e[0] for e in sim.events if e[1] == "TOOL_START"}
        for time_ms, event, session_id, turn_id, _, detail in sim.events:
            if event == "EVICT":
                self.assertIn("reason=ttl", detail)
                self.assertAlmostEqual(time_ms - starts[(session_id, turn_id)], 1000)

    def test_lru_offload_moves_bytes(self):
        _, summary = self.run_policy("lru_offload")
        self.assertGreaterEqual(summary["offload_count"], 1)
        self.assertGreater(summary["transfer_bytes"], 0)

    def test_dynamic_ttl_is_a_stub(self):
        with self.assertRaises(NotImplementedError):
            self.run_policy("dynamic_ttl")

    def test_longer_waits_raise_pressure_for_pin_all(self):
        _, low = self.run_policy("pin_all", "W1", 1)
        _, high = self.run_policy("pin_all", "W1", 8)
        self.assertGreaterEqual(high["peak_kv_occupancy_pct"], low["peak_kv_occupancy_pct"])

    def test_same_trace_same_result(self):
        _, first = self.run_policy("lru_offload", seed=3)
        _, second = self.run_policy("lru_offload", seed=3)
        self.assertEqual(first, second)

    def test_system_config_validation(self):
        for change in ({"block_size_tokens": 0}, {"decode_tokens_per_s": 0}, {"gpu_kv_pool_bytes": 1},
                       {"kv_bytes_per_token": 2.5}, {"admission_max_wait_ms": float("inf")}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                SystemConfig(**(asdict(self.system) | change))
        # configs/policies.json names exactly the registered policies
        listed = json.loads((ROOT / "configs/policies.json").read_text(encoding="utf-8"))
        self.assertEqual(set(listed), set(REGISTRY))
        self.assertEqual(load_policy_params(ROOT / "configs/policies.json", "fixed_ttl")["ttl_ms"], 30000)

    def test_results_and_collect(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            trace = directory / "w0.jsonl"
            write_trace(trace, generate(self.workload), self.workload, "W0", 1, 0)
            for policy in ("pin_all", "fixed_ttl"):
                sim, summary = simulate(trace, policy, self.system, keep_events=False)
                self.assertEqual(summary["warmup_ms"], self.workload.warmup_ms)
                write_results(sim, summary, directory / policy)
                self.assertTrue((directory / policy / "occupancy.csv").exists())
                self.assertFalse((directory / policy / "events.csv").exists())
                with self.assertRaises(ValueError):
                    write_results(sim, summary, directory / policy)
            merged = collect(directory)
            lines = merged.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 3)
            self.assertTrue(lines[0].startswith("run_id,policy,trace"))
            saved = json.loads((directory / "pin_all" / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["config"]["policy"], {"name": "pin_all"})

    def test_cli_simulate_and_overwrite_protection(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            trace = directory / "w0.jsonl"
            generated = subprocess.run([sys.executable, "-m", "kvbench", "generate", "--config",
                                        str(ROOT / "configs/smoke.json"), "--output", str(trace)],
                                       cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(generated.returncode, 0, generated.stderr)
            command = [sys.executable, "-m", "kvbench", "simulate", "--trace", str(trace),
                       "--policy", "pin_all", "--output", str(directory / "run")]
            run = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertIn('"normal_p95_ttft_ms"', run.stdout)
            run = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(run.returncode, 2)
            run = subprocess.run([sys.executable, "-m", "kvbench", "collect", str(directory)],
                                 cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertTrue((directory / "summary.csv").exists())


if __name__ == "__main__":
    unittest.main()
