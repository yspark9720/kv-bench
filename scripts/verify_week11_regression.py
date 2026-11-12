"""Capture/verify legacy replay fingerprints before extending cache granularity."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from kvbench.engine import Simulator
from kvbench.experiment import W2_FACTORIAL_CONDITIONS
from kvbench.metrics import summarize
from kvbench.policies import make_policy
from kvbench.system import load_policy_params, load_system_config
from kvbench.workload import generate, load_config

SETTINGS = ('week7.json', 'week7_180s_stress02.json', 'week7_180s_stress03.json')
POLICIES = ('pin_all', 'evict_recompute', 'fixed_ttl', 'lru_offload',
            'duration_aware', 'watermark_offload', 'pin_all_capped')


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def run(path, capture=False, seeds=range(5)):
    path = Path(path)
    reference = None if capture else json.loads(path.read_text(encoding='utf-8'))
    rows = {}
    system = load_system_config(ROOT / 'configs/system_1080ti_llama3b.json')
    for setting in SETTINGS:
        cfg = load_config(ROOT / 'configs' / setting)
        for seed in seeds:
            for label, workload, level in W2_FACTORIAL_CONDITIONS:
                trace = generate(cfg, workload, level, seed)
                for name in POLICIES:
                    params = load_policy_params(ROOT / 'configs/policies_week7.json', name)
                    sim = Simulator(trace, system, make_policy(name, params)).run()
                    summary = summarize(sim, cfg.warmup_ms, measurement_end_ms=cfg.duration_ms)
                    key = f'{setting}/{seed}/{label}/{name}'
                    if reference is not None:
                        summary = {k: summary[k] for k in reference[key]['summary']}
                    row = {'summary': summary,
                           'records': fingerprint([asdict(r) for r in sim.all_records()]),
                           'occupancy': fingerprint(sim.gpu.samples), 'events': fingerprint(sim.events)}
                    if reference is not None and row != reference[key]:
                        raise AssertionError(f'legacy replay changed: {key}')
                    rows[key] = row
            print(f'{setting}: seed {seed} verified ({len(rows)} runs)', flush=True)
    if capture:
        if path.exists():
            raise ValueError('refusing to overwrite baseline')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(rows, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    print(f'{len(rows)} legacy fingerprints: ' + ('captured' if capture else 'identical'), flush=True)
    return len(rows)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('path', type=Path)
    parser.add_argument('--capture', action='store_true')
    args = parser.parse_args()
    run(args.path, args.capture)
