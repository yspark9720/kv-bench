from dataclasses import replace
import heapq
import random
import unittest

from kvbench.engine import Simulator
from kvbench.metrics import summarize
from kvbench.policies import make_policy
from kvbench.policies.base import PolicyContext
from kvbench.policies.prefix import BlockView, HotPrefixAdmission, TailLru
from kvbench.system import SystemConfig
from kvbench.workload import Turn, generate, load_config


def context():
    return PolicyContext(1000, 0, 20, 10, 1000, 1000, 1000, 1)


def block(sid='a', index=0, frequency=1, clock=15, idle=0):
    return BlockView(sid, index, 10, 70, idle, idle, frequency, clock, 10)


def trace(specs):
    rows = []
    for sid, arrival, prompts, waits in specs:
        tokens = 0
        for i, prompt in enumerate(prompts):
            tokens += prompt
            rows.append(Turn(1, sid, i, f'{sid}:{i}', 'stress', 0, 'W0', 1,
                             arrival, arrival if not i else None, f'{sid}:{i-1}' if i else None,
                             prompt, 10, tokens, len(prompts), waits[i] if i < len(waits) else 0, 1, None))
            tokens += 10
    return rows


def system(cpu=1000, pcie=1000, wait=10000):
    return SystemConfig(120, cpu, 1, 10, pcie, 1000, 10000, wait)


class PrefixPolicyTests(unittest.TestCase):
    def test_tel_safe_trims_multiple_branches_before_lru_fallback(self):
        candidates = [block(sid, i, idle=idle) for sid, idle in [('a', 0), ('b', 100)] for i in range(7)]
        policy = TailLru(threshold_ms=30, next_prompt_estimate=10)
        self.assertEqual([b.key for b in policy.select_blocks(3, candidates, context())],
                         [('a', 6), ('a', 5), ('b', 6)])
        self.assertEqual([b.key for b in policy.select_blocks(5, candidates, context())],
                         [('a', 6), ('a', 5), ('b', 6), ('b', 5), ('a', 4)])

    def test_tel_boundary_rounds_retained_tokens_up(self):
        candidates = [replace(block(index=i), history_tokens=65) for i in range(7)]
        policy = TailLru(threshold_ms=20, next_prompt_estimate=10)
        # keep >=55 tokens -> six pages; only page index 6 is safe.
        self.assertEqual([b.index for b in policy.select_blocks(1, candidates, context())], [6])
        self.assertEqual(policy.select_blocks(0, candidates, context()), [])

    def test_hotprefix_frequency_and_hotness_both_gate_cpu_admission(self):
        p = HotPrefixAdmission(frequency_threshold=2)
        self.assertEqual(p.cpu_admission(block(frequency=1), [], 100)[2], 'frequency')
        resident = block('other', frequency=3, clock=15)
        self.assertEqual(p.cpu_admission(block(frequency=2), [resident], 100)[2], 'hotness')
        admit, victims, _ = p.cpu_admission(block(frequency=4), [resident], 0)
        self.assertTrue(admit)
        self.assertEqual(victims, [('other', 0)])
        self.assertFalse(p.cpu_admission(block(frequency=4), [], 0)[0])

    def test_gpu_hotness_eviction_exposes_parents_after_children(self):
        p = HotPrefixAdmission(gpu_eviction='hotness')
        views = [block('a', 0, frequency=1), block('a', 1, frequency=4), block('b', 0, frequency=2)]
        self.assertEqual([b.key for b in p.select_blocks(3, views, context())],
                         [('b', 0), ('a', 1), ('a', 0)])

    def test_partial_restore_recomputes_only_missing_tokens(self):
        rows = trace([('a', 0, [60, 10], [1000]), ('b', 100, [60, 10], [2000])])
        sim = Simulator(rows, system(), make_policy('prefix_lru_evict')).run()
        a, b = sim.sessions['a'].records[1], sim.sessions['b'].records[1]
        self.assertEqual(a.recomputed_tokens, 20)
        self.assertEqual(b.recomputed_tokens, 40)
        self.assertAlmostEqual(a.first_token_ms, 1091)
        self.assertEqual(sim.counters.recomputed_tokens, 60)
        self.assertEqual(sim.gpu.used_blocks, 0)

    def test_partial_last_page_preserves_valid_token_count(self):
        rows = trace([('a', 0, [63, 11], [1000]), ('b', 100, [60, 10], [2000])])
        sim = Simulator(rows, system(), make_policy('prefix_lru_evict')).run()
        # A holds 73 valid tokens in eight pages. Three tail pages are reclaimed.
        self.assertEqual(sim.sessions['a'].records[1].recomputed_tokens, 23)

    def test_reload_and_recompute_can_coexist_without_double_counting(self):
        rows = trace([('a', 0, [60, 10, 10], [1000, 1000]),
                      ('b', 100, [60, 10, 10], [2000, 1000])])
        sim = Simulator(rows, system(cpu=20), make_policy('hotprefix_admission',
                        {'frequency_threshold': 1})).run()
        self.assertTrue(any(r.restore == 'reload_recompute' for r in sim.all_records()))
        self.assertEqual(sum(r.recomputed_tokens for r in sim.all_records()), sim.counters.recomputed_tokens)
        self.assertEqual(sum(v['recomputed_tokens'] for v in sim.costs_by_request.values()), sim.counters.recomputed_tokens)
        self.assertEqual(sum(v['gpu_to_cpu_bytes'] for v in sim.costs_by_request.values()), sim.counters.gpu_to_cpu_bytes)
        self.assertEqual(sum(v['cpu_to_gpu_bytes'] for v in sim.costs_by_request.values()), sim.counters.cpu_to_gpu_bytes)

    def test_tool_return_during_offload_keeps_true_ready_time(self):
        rows = trace([('a', 0, [60, 10], [100]), ('b', 100, [60, 10], [2000])])
        sim = Simulator(rows, system(pcie=50), make_policy('prefix_lru_offload')).run()
        record = sim.sessions['a'].records[1]
        self.assertAlmostEqual(sim.resume_ready_ms['a:1'], 161)
        self.assertGreater(record.arrival_ms, sim.resume_ready_ms['a:1'])
        self.assertGreater(record.first_token_ms - sim.resume_ready_ms['a:1'], record.ttft_ms)

    def test_disabled_selective_admission_matches_same_engine_lru(self):
        rows = trace([('a', 0, [60, 10, 10], [1000, 1000]), ('b', 100, [60, 10], [2000])])
        a = Simulator(rows, system(cpu=20), make_policy('prefix_lru_offload')).run()
        b = Simulator(rows, system(cpu=20), make_policy('hotprefix_admission', {'selective': False})).run()
        x, y = summarize(a, 0), summarize(b, 0)
        x.pop('policy'); y.pop('policy')
        self.assertEqual(x, y)
        self.assertEqual(a.all_records(), b.all_records())
        self.assertEqual(a.gpu.samples, b.gpu.samples)

    def test_zero_tlru_budget_matches_block_lru(self):
        rows = trace([('a', 0, [60, 10], [1000]), ('b', 100, [60, 10], [2000])])
        a = Simulator(rows, system(), make_policy('prefix_lru_evict')).run()
        b = Simulator(rows, system(), make_policy('tlru', {'threshold_ms': 0})).run()
        self.assertEqual(a.all_records(), b.all_records())
        self.assertEqual(a.gpu.samples, b.gpu.samples)

    def test_access_frequency_and_request_count_aging_are_causal(self):
        rows = trace([('a', 0, [20, 10, 10], [10, 10])])
        sim = Simulator(rows, replace(system(), gpu_kv_pool_bytes=2000), make_policy('hotprefix_admission',
                        {'max_age': 3, 'age_every_requests': 1})).run()
        self.assertEqual(sim.blocks['a'][0].frequency, 3)
        self.assertEqual(sim.blocks['a'][0].clock, 2)
        self.assertEqual(sim.processed_requests, 3)

    def test_future_information_not_in_block_policy_view(self):
        names = set(BlockView.__dataclass_fields__)
        self.assertFalse(names & {'tool_duration_ms', 'remaining_wait_ms', 'expected_turns', 'next_prompt_tokens'})

    def test_unobserved_future_does_not_change_earlier_decisions(self):
        original = trace([('a', 0, [60, 10], [1000]), ('b', 100, [60, 10], [2000]),
                          ('c', 200, [80], [])])
        alternate = [replace(r, tool_duration_ms=5000) if r.request_id == 'a:0' else
                     replace(r, prompt_tokens=90, context_tokens=160) if r.request_id == 'a:1' else r
                     for r in original]
        events = {'EVICT', 'CPU_EVICT', 'CPU_ADMISSION', 'OFFLOAD_START', 'ADMIT'}
        for name in ('prefix_lru_evict', 'tlru', 'prefix_lru_offload', 'hotprefix_admission'):
            a = Simulator(original, system(), make_policy(name)).run()
            b = Simulator(alternate, system(), make_policy(name)).run()
            decisions = lambda sim: [e for e in sim.events if e[0] < 800 and e[1] in events]
            self.assertEqual(decisions(a), decisions(b), name)

    def test_all_policies_drain_with_cpu_full_slow_link_and_timeouts(self):
        cfg = load_config('configs/smoke.json')
        rows = generate(cfg, 'W2', 4, 3)
        sys = SystemConfig(2**30, 1, 114688, 16, 1.5e9, 8000, 30, 1000)
        for name in ('prefix_lru_evict', 'prefix_lru_offload', 'tlru', 'hotprefix_admission'):
            with self.subTest(policy=name):
                sim = Simulator(rows, sys, make_policy(name), keep_events=False).run()
                self.assertTrue(all(s.state.value in ('done', 'rejected') for s in sim.sessions.values()))
                self.assertEqual(sim.gpu.used_blocks, 0)
                self.assertEqual(sim.cpu.used_bytes, 0)
                self.assertTrue(all(0 <= used <= sim.gpu.total_blocks for _, used in sim.gpu.samples))

    def test_bad_policy_parameters(self):
        for name, params in [('tlru', {'threshold_ms': float('nan')}), ('tlru', {'next_prompt_estimate': -1}),
                             ('hotprefix_admission', {'frequency_threshold': 0}),
                             ('hotprefix_admission', {'age_every_requests': 0}),
                             ('hotprefix_admission', {'gpu_eviction': 'oracle'})]:
            with self.subTest(name=name, params=params), self.assertRaises(ValueError):
                make_policy(name, params)

    def test_resource_conservation_after_every_event_in_randomized_replays(self):
        rng = random.Random(20261112)
        for case in range(30):
            specs = []
            for i in range(rng.randint(2, 5)):
                turns = rng.randint(2, 4)
                specs.append((f's{i}', rng.randrange(100),
                              [rng.randint(5, 90) for _ in range(turns)],
                              [rng.randint(1, 500) for _ in range(turns-1)]))
            rows = trace(specs)
            sys = SystemConfig(rng.randint(6, 30)*10, rng.randint(1, 10)*10, 1, 10,
                               rng.choice([20, 100, 1000]), 1000, 10000, rng.choice([50, 1000]))
            for name in ('prefix_lru_evict', 'prefix_lru_offload', 'tlru', 'hotprefix_admission'):
                sim = Simulator(rows, sys, make_policy(name), keep_events=False)
                for s in sim.sessions.values():
                    sim._push(s.job_arrival_ms, 'ARRIVE', s.sid, 0)
                while sim._heap:
                    sim.now, _, event, sid, idx = heapq.heappop(sim._heap)
                    getattr(sim, '_on_' + event.lower())(sim.sessions[sid], idx)
                    self.assertEqual(sim.gpu.used_blocks, sum(s.gpu_blocks for s in sim.sessions.values()), (case, name, event))
                    self.assertEqual(sim.cpu.used_bytes, sum(s.cpu_bytes for s in sim.sessions.values()), (case, name, event))
                    self.assertTrue(0 <= sim.gpu.used_blocks <= sim.gpu.total_blocks)
                    self.assertTrue(0 <= sim.cpu.used_bytes <= sim.cpu.total_bytes)
                    for s in sim.sessions.values():
                        blocks = sim.blocks[s.sid].values()
                        self.assertEqual(s.cpu_bytes, sum(b.location in ('cpu','to_cpu','to_gpu') for b in blocks)*10)
                sim.assert_resources()


if __name__ == '__main__':
    unittest.main()
