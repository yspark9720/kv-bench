"""Run paired week-11 policies on the unchanged week-9/10 workloads.

Example: python scripts/run_week11.py --output results/week11_20261112
Raw runs are ignored by git; publish compact tables using report_week11.py.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from kvbench.engine import Simulator
from kvbench.experiment import W2_FACTORIAL_CONDITIONS
from kvbench.metrics import summarize
from kvbench.policies import make_policy
from kvbench.simulate import write_results
from kvbench.system import load_system_config, load_policy_params
from kvbench.week11 import diagnostics, digest
from kvbench.workload import generate, load_config, write_trace

SETTINGS = {'60s_02': 'week7.json', '180s_02': 'week7_180s_stress02.json',
            '180s_03': 'week7_180s_stress03.json'}
LEGACY = ('pin_all', 'evict_recompute', 'fixed_ttl', 'lru_offload', 'duration_aware',
          'watermark_offload', 'pin_all_capped')
NEW = ('prefix_lru_evict', 'prefix_lru_offload', 'tlru', 'hotprefix_admission')
MAIN = [(p, p, {}) for p in LEGACY + NEW]
HARDWARE = [(p, p, {}) for p in ('lru_offload',) + NEW]
ABLATIONS = [('tlru_100ms', 'tlru', {'threshold_ms': 100}),
             ('tlru_400ms', 'tlru', {'threshold_ms': 400}),
             ('hotprefix_f1', 'hotprefix_admission', {'frequency_threshold': 1}),
             ('hotprefix_f3', 'hotprefix_admission', {'frequency_threshold': 3}),
             ('hotprefix_gpu_hotness', 'hotprefix_admission', {'gpu_eviction': 'hotness'}),
             ('hotprefix_selective_off', 'hotprefix_admission', {'selective': False})]
ENVIRONMENTS = {'default': (12e9, 64 * 2**30), 'slow_pcie': (1.5e9, 64 * 2**30),
                'small_cpu': (12e9, 2**30), 'slow_small': (1.5e9, 2**30)}


def write_csv(path, rows):
    import csv
    columns = list(dict.fromkeys(k for row in rows for k in row))
    with Path(path).open('w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')


def worker(job):
    output, setting, condition, wid, level, seed, environment, variants, suite, save_raw = job
    cfg = load_config(ROOT / 'configs' / SETTINGS[setting])
    system = load_system_config(ROOT / 'configs/system_1080ti_llama3b.json')
    bandwidth, cpu = ENVIRONMENTS[environment]
    system = replace(system, pcie_bytes_per_s=bandwidth, cpu_kv_pool_bytes=cpu)
    trace = generate(cfg, wid, level, seed)
    trace_id = f'{setting}__{condition}__seed{seed}'
    # A trace is written once per suite/environment, never by competing workers.
    trace_path = Path(output) / 'traces' / f'{suite}__{environment}__{trace_id}.jsonl'
    write_trace(trace_path, trace, cfg, wid, level, seed)
    trace_hash = hashlib.sha256(trace_path.read_bytes()).hexdigest()
    rows = []
    for variant, policy, override in variants:
        params = load_policy_params(ROOT / 'configs/policies_week7.json', policy) | override
        sim = Simulator(trace, system, make_policy(policy, params), keep_events=True).run()
        if sim.gpu.used_blocks or sim.cpu.used_bytes:
            raise AssertionError('resources leaked')
        meta = {'suite': suite, 'setting': setting, 'environment': environment,
                'condition': condition, 'workload_id': wid, 'stress_level': level,
                'seed': seed, 'variant': variant, 'pcie_gbps': bandwidth / 1e9,
                'cpu_gib': cpu / 2**30, 'trace_sha256': trace_hash,
                'admission_ceiling': getattr(sim.policy, 'admission_ceiling', 1.0)}
        summary = summarize(sim, cfg.warmup_ms, meta, cfg.duration_ms)
        summary.update(diagnostics(sim, cfg.warmup_ms, cfg.duration_ms))
        if save_raw:
            # Keep complete event logs for seed 0 and ordinary raw outputs for all seeds.
            sim.keep_events = seed == 0
            run_id = f'{suite}__{environment}__{trace_id}__{variant}'
            write_results(sim, summary, Path(output) / 'runs' / run_id)
        rows.append(summary)
    return rows


def run(output, seeds=range(15), secondary_seeds=range(5), workers=4,
        settings=SETTINGS, conditions=W2_FACTORIAL_CONDITIONS, secondary=True, save_raw=True):
    output = Path(output)
    if output.exists():
        raise ValueError('output exists; choose a new directory')
    (output / 'traces').mkdir(parents=True)
    (output / 'runs').mkdir()
    jobs = []
    for setting in settings:
        for label, wid, level in conditions:
            for seed in seeds:
                jobs.append((str(output), setting, label, wid, level, seed, 'default', MAIN, 'main', save_raw))
            if secondary:
                for seed in secondary_seeds:
                    for env in ENVIRONMENTS:
                        if env != 'default':
                            jobs.append((str(output), setting, label, wid, level, seed, env, HARDWARE, 'hardware', save_raw))
                    jobs.append((str(output), setting, label, wid, level, seed, 'default', ABLATIONS, 'ablation', save_raw))
    sources = sorted((ROOT / 'kvbench').rglob('*.py')) + [Path(__file__)]
    manifest = {'experiment': 'week11_prefix_retention', 'status': 'running',
                'base_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
                'source_sha256': {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources},
                'settings': {k: json.loads((ROOT / 'configs' / SETTINGS[k]).read_text()) for k in settings},
                'system': asdict(load_system_config(ROOT / 'configs/system_1080ti_llama3b.json')),
                'policy_defaults': json.loads((ROOT / 'configs/policies_week7.json').read_text()),
                'main_variants': MAIN, 'hardware_variants': HARDWARE, 'ablation_variants': ABLATIONS,
                'environments': ENVIRONMENTS, 'conditions': list(conditions), 'seeds': list(seeds),
                'secondary_seeds': list(secondary_seeds), 'expected_runs': sum(len(j[-3]) for j in jobs),
                'scope': 'T-LRU Algorithm 1; HotPrefix sections 3.2-3.3 in session-isolated block chains; no promotion or CUDA performance claims'}
    write_json(output / 'experiment.json', manifest)
    began, rows = time.monotonic(), []
    if workers == 1:
        for index, job in enumerate(jobs, 1):
            rows.extend(worker(job))
            if index % 10 == 0:
                print(f'{index}/{len(jobs)} paired traces; {len(rows)} runs; {time.monotonic()-began:.1f}s', flush=True)
    else:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            for index, result in enumerate(pool.map(worker, jobs, chunksize=1), 1):
                rows.extend(result)
                if index % 10 == 0:
                    print(f'{index}/{len(jobs)} paired traces; {len(rows)} runs; {time.monotonic()-began:.1f}s', flush=True)
    rows.sort(key=lambda r: (r['suite'], r['setting'], r['environment'], r['condition'], r['seed'], r['variant']))
    w0 = {(r['suite'], r['setting'], r['environment'], r['seed'], r['variant']): r['normal_p95_ttft_ms']
          for r in rows if r['condition'] == 'w0_1x'}
    for row in rows:
        baseline = w0.get((row['suite'], row['setting'], row['environment'], row['seed'], row['variant']))
        row['ttft_ratio_vs_policy_w0'] = row['normal_p95_ttft_ms'] / baseline if baseline else None
        row['legacy_all_slo_pass'] = (row['ttft_ratio_vs_policy_w0'] <= 3 and
                                    row['normal_reject_rate_pct'] <= 10 and row['peak_kv_occupancy_pct'] <= 90) if baseline else None
    write_csv(output / 'summary.csv', rows)
    manifest.update(status='complete', completed_runs=len(rows), elapsed_seconds=time.monotonic()-began,
                    trace_files=len(list((output / 'traces').glob('*.jsonl'))), summary_sha256=hashlib.sha256((output/'summary.csv').read_bytes()).hexdigest())
    if len(rows) != manifest['expected_runs']:
        raise AssertionError('incomplete experiment')
    write_json(output / 'experiment.json', manifest)
    print(f"Complete: {len(rows)} runs, {manifest['elapsed_seconds']:.1f}s, {output}", flush=True)
    return output


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--pilot', action='store_true', help='one seed and one setting, all nine conditions')
    args = parser.parse_args()
    if args.workers < 1:
        parser.error('--workers must be positive')
    run(args.output, seeds=(0,) if args.pilot else range(15),
        settings=('180s_02',) if args.pilot else SETTINGS, secondary=not args.pilot, workers=args.workers)
