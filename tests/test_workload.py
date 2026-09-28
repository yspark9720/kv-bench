from dataclasses import asdict, replace
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from kvbench.workload import Config, generate, load_config, read_trace, validate, write_trace


ROOT = Path(__file__).resolve().parents[1]


class WorkloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = load_config(ROOT / "configs/smoke.json")

    def test_reproducibility_and_seed_variation(self):
        for workload in ("W0", "W1", "W2"):
            with self.subTest(workload=workload):
                self.assertEqual(generate(self.config, workload, seed=7),
                                 generate(self.config, workload, seed=7))
                self.assertNotEqual(generate(self.config, workload, seed=7),
                                    generate(self.config, workload, seed=8))

    def test_w1_changes_only_wait_and_labels(self):
        baseline = generate(self.config, "W0", seed=5)
        for level in (1, 2, 4, 8):
            changed = generate(self.config, "W1", level, seed=5)
            self.assertEqual(len(baseline), len(changed))
            for before, after in zip(baseline, changed):
                expected = replace(before, workload_id="W1", stress_level=level,
                                   tool_duration_ms=before.tool_duration_ms * level)
                self.assertEqual(expected, after)

    def test_w2_preserves_normal_and_session_content(self):
        baseline = {r.request_id: r for r in generate(self.config, seed=5)}
        for after in generate(self.config, "W2", 4, seed=5):
            before = baseline[after.request_id]
            if after.request_class == "normal":
                self.assertEqual(replace(before, workload_id="W2", stress_level=4), after)
                continue
            self.assertEqual(before.prompt_tokens, after.prompt_tokens)
            self.assertEqual(before.output_tokens, after.output_tokens)
            self.assertEqual(before.context_tokens, after.context_tokens)
            factor = (self.config.w2_short_wait_factor if after.turn_id % 2 == 0
                      else self.config.w2_long_wait_factor)
            self.assertEqual(after.tool_duration_ms, before.tool_duration_ms * factor * 4)
            start = after.burst_id * self.config.w2_burst_period_ms
            self.assertTrue(start <= after.session_arrival_ms < start + self.config.w2_burst_width_ms)

    def test_context_and_dependencies(self):
        rows = generate(self.config)
        sessions = {}
        for row in rows:
            sessions.setdefault(row.session_id, []).append(row)
        for turns in sessions.values():
            cumulative = 0
            for i, row in enumerate(turns):
                self.assertEqual(row.context_tokens, cumulative + row.prompt_tokens)
                cumulative += row.prompt_tokens + row.output_tokens
                if i:
                    self.assertIsNone(row.arrival_ms)
                    self.assertEqual(row.depends_on_request_id, turns[i - 1].request_id)
            self.assertEqual(turns[-1].tool_duration_ms, 0)

    def test_validation_rejects_corrupt_traces(self):
        rows = generate(self.config)
        index = next(i for i, row in enumerate(rows) if row.turn_id == 1)
        bad = rows[index]
        mutations = [replace(bad, turn_id=0), replace(bad, context_tokens=0),
                     replace(bad, depends_on_request_id="missing"),
                     replace(bad, arrival_ms=100), replace(bad, tool_duration_ms=-1),
                     replace(bad, seed=True), replace(bad, session_arrival_ms=float("nan")),
                     replace(bad, expected_turns=999), replace(bad, workload_id="W1"),
                     replace(bad, burst_id=1)]
        for mutation in mutations:
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                validate(rows[:index] + [mutation] + rows[index + 1:])
        with self.assertRaises(ValueError):
            validate(rows + [rows[0]])
        with self.assertRaises(ValueError):
            validate(rows[:index] + rows[index + 1:])

    def test_bad_config_and_arguments(self):
        for change in ({"warmup_ms": -1}, {"normal_arrival_rate_per_s": 0},
                       {"tool_duration_ms": [5, 1]}, {"stress_turns": [1, 3]},
                       {"duration_ms": float("inf")}, {"output_tokens": [True, 3]},
                       {"w2_burst_width_ms": 100000}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                Config(**(asdict(self.config) | change))
        for kwargs in ({"workload": "W3"}, {"stress_level": 2}, {"seed": -1},
                       {"stress_level": True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                generate(self.config, **kwargs)

    def test_io_round_trip_and_content_hash(self):
        import hashlib
        rows = generate(self.config)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.jsonl"
            write_trace(path, rows, self.config, "W0", 1, 0)
            self.assertEqual(rows, read_trace(path))
            meta = json.loads(path.with_suffix(".meta.json").read_text())
            self.assertEqual(meta["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
            path.write_text('{"unexpected": 1}\n')
            with self.assertRaisesRegex(ValueError, "line 1"):
                read_trace(path)

    def test_cli_and_overwrite_protection(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.jsonl"
            command = [sys.executable, "-m", "kvbench", "generate", "--config",
                       str(ROOT / "configs/smoke.json"), "--output", str(path)]
            run = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            contents = path.read_bytes()
            run = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(run.returncode, 2)
            self.assertEqual(contents, path.read_bytes())
            run = subprocess.run([sys.executable, "-m", "kvbench", "validate", str(path)],
                                 cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)

    def test_full_proposed_workload_grid(self):
        for workload in ("W0", "W1", "W2"):
            for level in ((1,) if workload == "W0" else (1, 2, 4, 8)):
                for seed in range(5):
                    with self.subTest(workload=workload, level=level, seed=seed):
                        rows = generate(self.config, workload, level, seed)
                        summary = validate(rows)
                        self.assertGreater(summary["normal_requests"], 0)
                        self.assertGreater(summary["stress_sessions"], 0)
                        self.assertTrue(all(0 <= r.session_arrival_ms < self.config.duration_ms
                                            for r in rows))


if __name__ == "__main__":
    unittest.main()
