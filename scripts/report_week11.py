"""Validate, aggregate, publish, and plot the week-11 paired experiment."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
import hashlib
import json
from pathlib import Path
import random
import shutil
from statistics import mean, stdev
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.run_week11 import write_csv, write_json

METRICS = ('normal_p95_ttft_ms', 'resume_p95_ms', 'stress_p95_completion_ms',
           'normal_reject_rate_pct', 'stress_completion_pct', 'transfer_bytes', 'recomputed_tokens',
           'cohort_transfer_bytes', 'cohort_recomputed_tokens', 'resume_tel_per_completed_turn_ms',
           'mean_kv_occupancy_pct', 'peak_kv_occupancy_pct')
LABELS = {'prefix_lru_evict': 'Block LRU evict', 'prefix_lru_offload': 'Block LRU offload',
          'tlru': 'T-LRU', 'hotprefix_admission': 'HotPrefix admission', 'lru_offload': 'Legacy LRU offload'}


def read_rows(path):
    with Path(path).open(newline='', encoding='utf-8') as handle:
        rows = list(csv.DictReader(handle))
    for row in rows:
        for key, value in row.items():
            if value == '':
                row[key] = None
            elif key not in ('requests_sha256', 'occupancy_sha256', 'trace_sha256'):
                try:
                    row[key] = float(value)
                except ValueError:
                    pass
    return rows


def ci(values, count=2000):
    if not values:
        return None, None
    rng = random.Random(20261112)
    samples = sorted(mean(rng.choices(values, k=len(values))) for _ in range(count))
    return samples[int(.025 * count)], samples[min(int(.975 * count), count-1)]


def aggregate(rows):
    grouped = defaultdict(list)
    for r in rows:
        grouped[(r['suite'], r['setting'], r['environment'], r['condition'], r['variant'])].append(r)
    out = []
    for keys, values in sorted(grouped.items()):
        row = dict(zip(('suite', 'setting', 'environment', 'condition', 'variant'), keys))
        row['seeds'] = len(values)
        for metric in METRICS:
            numbers = [r[metric] for r in values if r.get(metric) is not None]
            row[metric + '_mean'] = mean(numbers) if numbers else None
            row[metric + '_std'] = stdev(numbers) if len(numbers) > 1 else 0
        out.append(row)
    return out


def paired(rows):
    by = {(r['suite'], r['setting'], r['environment'], r['condition'], r['seed'], r['variant']): r for r in rows}
    candidates = [('main', 'tlru', 'prefix_lru_evict'),
                  ('main', 'hotprefix_admission', 'prefix_lru_offload'),
                  ('main', 'prefix_lru_offload', 'lru_offload'),
                  ('hardware', 'tlru', 'prefix_lru_evict'),
                  ('hardware', 'hotprefix_admission', 'prefix_lru_offload'),
                  ('ablation', 'tlru_100ms', 'tlru'), ('ablation', 'tlru_400ms', 'tlru'),
                  ('ablation', 'hotprefix_f1', 'hotprefix_admission'),
                  ('ablation', 'hotprefix_f3', 'hotprefix_admission'),
                  ('ablation', 'hotprefix_gpu_hotness', 'hotprefix_admission'),
                  ('ablation', 'hotprefix_selective_off', 'prefix_lru_offload')]
    grouped = defaultdict(list)
    for suite, candidate, baseline in candidates:
        for r in rows:
            if r['suite'] != suite or r['variant'] != candidate:
                continue
            baseline_suite = 'main' if suite == 'ablation' else suite
            key = (baseline_suite, r['setting'], r['environment'], r['condition'], r['seed'], baseline)
            if key not in by:
                raise AssertionError(f'missing paired baseline {key}')
            b = by[key]
            if b['trace_sha256'] != r['trace_sha256']:
                raise AssertionError('unpaired traces')
            for condition in (r['condition'], 'ALL9'):
                grouped[(suite, r['setting'], r['environment'], condition, candidate, baseline)].append((r, b))
    output = []
    for keys, pairs in sorted(grouped.items()):
        item = dict(zip(('suite', 'setting', 'environment', 'condition', 'candidate', 'baseline'), keys))
        item['paired_runs'] = len(pairs)
        item['identical_requests'] = sum(r['requests_sha256'] == b['requests_sha256'] for r, b in pairs)
        for metric in METRICS:
            values = [(r, b) for r, b in pairs if r.get(metric) is not None and b.get(metric) is not None]
            # ALL9 averages conditions WITHIN each seed first. Resampling treats
            # the seed as the unit, not nine correlated workload rows.
            per_seed = defaultdict(list)
            for r, b in values:
                per_seed[r['seed']].append((r[metric], b[metric]))
            differences = [mean(a-b for a, b in v) for v in per_seed.values()]
            lo, hi = ci(differences)
            base = mean(b[metric] for _, b in values) if values else None
            cand = mean(r[metric] for r, _ in values) if values else None
            delta = mean(differences) if differences else None
            item.update({metric + '_baseline': base, metric + '_candidate': cand,
                         metric + '_delta': delta, metric + '_ci_low': lo, metric + '_ci_high': hi,
                         metric + '_delta_pct': 100 * delta / base if base else None,
                         metric + '_seeds': len(differences)})
        output.append(item)
    return output


def validate(output, rows):
    manifest = json.loads((output/'experiment.json').read_text(encoding='utf-8'))
    if manifest['status'] != 'complete' or manifest['completed_runs'] != len(rows):
        raise AssertionError('experiment is incomplete')
    if hashlib.sha256((output/'summary.csv').read_bytes()).hexdigest() != manifest['summary_sha256']:
        raise AssertionError('summary hash mismatch')
    for name, expected in manifest['source_sha256'].items():
        if hashlib.sha256((ROOT/name).read_bytes()).hexdigest() != expected:
            raise AssertionError(f'evaluated source changed: {name}')
    trace_hashes, seen = {}, set()
    for r in rows:
        key = tuple(r[k] for k in ('suite', 'setting', 'environment', 'condition', 'seed', 'variant'))
        if key in seen:
            raise AssertionError('duplicate run')
        seen.add(key)
        trace_key = tuple(r[k] for k in ('setting', 'condition', 'seed'))
        if trace_key in trace_hashes and trace_hashes[trace_key] != r['trace_sha256']:
            raise AssertionError('policy/environment changed input trace')
        trace_hashes[trace_key] = r['trace_sha256']
        if not 0 <= r['peak_kv_occupancy_pct'] <= 100 or not 0 <= r['stress_completion_pct'] <= 100:
            raise AssertionError('invalid resource/completion metric')
    main = {(r['setting'], r['condition'], r['seed'], r['variant']): r for r in rows if r['suite']=='main'}
    disabled_matches = 0
    cpu_free_matches = 0
    for r in rows:
        if r['suite']=='ablation' and r['variant']=='hotprefix_selective_off':
            b = main[r['setting'], r['condition'], r['seed'], 'prefix_lru_offload']
            if (r['requests_sha256'], r['occupancy_sha256']) != (b['requests_sha256'], b['occupancy_sha256']):
                raise AssertionError('selective-off control changed replay')
            disabled_matches += 1
        if r['suite']=='hardware' and r['variant'] in ('tlru', 'prefix_lru_evict'):
            b = main[r['setting'], r['condition'], r['seed'], r['variant']]
            if r['requests_sha256'] != b['requests_sha256']:
                raise AssertionError('CPU-free policy affected by PCIe/CPU setting')
            cpu_free_matches += 1
    return {'runs': len(rows), 'suites': dict(Counter(r['suite'] for r in rows)),
            'unique_input_traces': len(trace_hashes), 'all_paired_inputs_identical': True,
            'all_source_hashes_match': True, 'selective_off_exact_matches': disabled_matches,
            'cpu_free_hardware_exact_matches': cpu_free_matches}


def plot(rows, directory):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    directory.mkdir(parents=True, exist_ok=True)
    variants = ('prefix_lru_evict', 'tlru', 'prefix_lru_offload', 'hotprefix_admission')
    metrics = [('normal_p95_ttft_ms', 'Normal P95 TTFT (ms)', 1),
               ('resume_p95_ms', 'Resume P95 latency (ms)', 1),
               ('transfer_bytes', 'Transfer (GiB/run)', 2**30),
               ('recomputed_tokens', 'Recomputed tokens/run', 1)]
    envs = ('default', 'slow_pcie', 'small_cpu', 'slow_small')
    settings = ('60s_02', '180s_02', '180s_03')
    grouped = defaultdict(list)
    for row in rows:
        if (row['variant'] in variants and row['seed'] < 5
                and row['suite'] == ('main' if row['environment'] == 'default' else 'hardware')):
            grouped[row['setting'], row['environment'], row['variant']].append(row)
    means = {}
    for setting in settings:
        for env in envs:
            for variant in variants:
                values = grouped[setting, env, variant]
                # Every environment must contain the same nine conditions and
                # five seeds; missing runs must not silently bias a figure.
                keys = {(r['condition'], r['seed']) for r in values}
                if (len(values) != 45 or len(keys) != 45
                        or {r['seed'] for r in values} != set(range(5))
                        or len({r['condition'] for r in values}) != 9):
                    raise AssertionError(f'incomplete plot cohort: {setting}, {env}, {variant}')
                for metric, _, scale in metrics:
                    means[setting, env, variant, metric] = mean(r[metric] / scale for r in values)
    groups = (
        ('week11_policy_comparison_60s_02', '60s / stress 0.2', ('60s_02',)),
        ('week11_policy_comparison', '180s / stress 0.2', ('180s_02',)),
        ('week11_policy_comparison_180s_03', '180s / stress 0.3', ('180s_03',)),
        ('week11_policy_comparison_average', 'Equal-weight mean of the three settings', settings),
    )
    paths = []
    for stem, heading, included in groups:
        fig, axes = plt.subplots(2, 2, figsize=(13, 8))
        for ax, (metric, title, _) in zip(axes.flat, metrics):
            for i, variant in enumerate(variants):
                values = [mean(means[setting, env, variant, metric] for setting in included) for env in envs]
                ax.bar([j + (i-1.5)*.2 for j in range(4)], values, width=.2, label=LABELS[variant])
            ax.set_xticks(range(4), ['12GB/s\n64GiB', '1.5GB/s\n64GiB', '12GB/s\n1GiB', '1.5GB/s\n1GiB'])
            ax.set_title(title)
            ax.set_ylim(bottom=0)
            ax.grid(axis='y', alpha=.2)
        handles, labels = axes[0,0].get_legend_handles_labels()
        fig.legend(handles, labels, loc='lower center', ncol=4, frameon=False, fontsize=10)
        fig.suptitle(f'Week 11: {heading}\nMean of run-level metrics across nine conditions; the SAME five seeds in every environment')
        if len(included) > 1:
            fig.text(.5, .055, 'Average of run-level P95s, not pooled P95; costs are per run, not per second.',
                     ha='center', fontsize=9)
        fig.tight_layout(rect=(0, .085 if len(included) > 1 else .055, 1, .94))
        png, svg = directory/f'{stem}.png', directory/f'{stem}.svg'
        fig.savefig(png, dpi=160)
        fig.savefig(svg)
        plt.close(fig)
        svg.write_text('\n'.join(line.rstrip() for line in svg.read_text(encoding='utf-8').splitlines()) + '\n', encoding='utf-8')
        paths.append(png)
    return paths


def report(output, publish=True):
    rows = read_rows(output/'summary.csv')
    checks = validate(output, rows)
    comparisons, pairs = aggregate(rows), paired(rows)
    hardware_pairs = paired([r for r in rows if r['suite'] in ('main', 'hardware') and r['seed'] < 5])
    write_csv(output/'comparison.csv', comparisons)
    write_csv(output/'paired.csv', pairs)
    write_csv(output/'paired_hardware.csv', hardware_pairs)
    write_json(output/'validation.json', checks)
    if publish:
        dest = ROOT/'docs/data/week11'
        dest.mkdir(parents=True, exist_ok=True)
        for name in ('summary.csv', 'comparison.csv', 'paired.csv', 'paired_hardware.csv', 'experiment.json', 'validation.json'):
            shutil.copyfile(output/name, dest/name)
        plot(rows, ROOT/'docs/figures')
    print(json.dumps(checks, indent=2))
    for r in pairs:
        if r['setting']=='180s_02' and r['condition']=='ALL9' and r['suite'] in ('main','hardware'):
            print(r['environment'], r['candidate'], 'vs', r['baseline'],
                  {k: round(r[k], 3) if r[k] is not None else None for k in
                   ('normal_p95_ttft_ms_delta', 'resume_p95_ms_delta', 'transfer_bytes_delta_pct', 'recomputed_tokens_delta')})
    return checks


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    report(args.output)
