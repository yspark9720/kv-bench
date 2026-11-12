from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from kvbench.engine import Simulator
from kvbench.policies import make_policy
from kvbench.week11 import diagnostics
from test_prefix_policies import system, trace
from scripts.report_week11 import paired, read_rows, validate
from scripts.run_week11 import run


class Week11Tests(unittest.TestCase):
    def test_resume_decomposition_includes_copy_tail(self):
        sim = Simulator(trace([('a', 0, [60, 10], [100]), ('b', 100, [60, 10], [2000])]),
                        system(pcie=50), make_policy('prefix_lru_offload')).run()
        out = diagnostics(sim, 0, 5000)
        self.assertGreater(out['resume_offload_tail_count'], 0)
        self.assertEqual(out['resume_completed_turns'], 2)
        self.assertEqual(out['resume_planned_turns'], 2)
        parts = ('offload_tail_ms','admission_ms','restore_ms','prefill_queue_ms','self_prefill_ms')
        self.assertAlmostEqual(sum(out['resume_p95_'+p] for p in parts), out['resume_p95_ms'], places=1)
        self.assertEqual(out['cohort_transfer_bytes'], sim.counters.gpu_to_cpu_bytes + sim.counters.cpu_to_gpu_bytes)
        self.assertEqual(out['cohort_recomputed_tokens'], sim.counters.recomputed_tokens)

    def test_cost_cohort_excludes_early_session(self):
        sim = Simulator(trace([('a', 0, [60, 10], [1000]), ('b', 100, [60, 10], [2000])]),
                        system(), make_policy('prefix_lru_evict')).run()
        out = diagnostics(sim, 50, 5000)
        self.assertEqual(out['cohort_stress_jobs'], 1)
        self.assertEqual(out['cohort_recomputed_tokens'], sim.sessions['b'].records[1].recomputed_tokens)
        self.assertEqual(out['resume_planned_turns'], 1)

    def test_rejected_and_unsubmitted_followups_not_hidden(self):
        sim = Simulator(trace([('a', 0, [60, 10], [1000]), ('b', 100, [600, 10], [1000])]),
                        system(wait=100), make_policy('prefix_lru_evict')).run()
        out = diagnostics(sim, 0, 5000)
        self.assertEqual(out['stress_completion_pct'], 50)
        self.assertEqual(out['resume_planned_turns'], 2)
        self.assertEqual(out['resume_completed_turns'], 1)

    def test_reduced_matrix_is_paired_and_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)/'run'
            run(output, seeds=(0,), settings=('60s_02',),
                conditions=(('w0_1x','W0',1), ('both_4x','W2',4)),
                workers=1, secondary=False, save_raw=False)
            rows = read_rows(output/'summary.csv')
            self.assertEqual(len(rows), 22)
            self.assertTrue(validate(output, rows)['all_paired_inputs_identical'])
            pairs = paired(rows)
            self.assertEqual(len(pairs), 9)
            self.assertTrue(all(r['paired_runs'] == (2 if r['condition']=='ALL9' else 1) for r in pairs))
            with self.assertRaises(ValueError):
                run(output, seeds=(0,), workers=1, secondary=False)


if __name__ == '__main__':
    unittest.main()
