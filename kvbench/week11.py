"""Cohort-aligned diagnostics for week-11 replay comparisons."""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import math
import re

from .metrics import percentile


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def diagnostics(sim, warmup_ms, end_ms, tel_threshold_ms=200.0):
    if not sim.keep_events:
        raise ValueError('diagnostics require events during replay')
    prefill = {}
    cost = {'cohort_gpu_to_cpu_bytes': 0, 'cohort_cpu_to_gpu_bytes': 0, 'cohort_recomputed_tokens': 0}
    cohort = {s.sid for s in sim.sessions.values() if warmup_ms <= s.job_arrival_ms < end_ms}
    for time, event, sid, _, rid, detail in sim.events:
        if event == 'PREFILL_QUEUED':
            start = float(re.search(r'start=([\d.eE+-]+)', detail)[1])
            prefill[rid] = time, start
        elif sid in cohort and event in ('OFFLOAD_START', 'RELOAD_START', 'RECOMPUTE'):
            if event == 'RECOMPUTE':
                cost['cohort_recomputed_tokens'] += int(re.search(r'tokens=(\d+)', detail)[1])
            else:
                key = 'cohort_gpu_to_cpu_bytes' if event == 'OFFLOAD_START' else 'cohort_cpu_to_gpu_bytes'
                cost[key] += int(re.search(r'bytes=(\d+)', detail)[1])
    resume, normal = [], []
    stress_jobs = [s for s in sim.sessions.values() if s.sid in cohort and s.request_class == 'stress']
    for s in sim.sessions.values():
        records = {r.turn_id: r for r in s.records}
        for r in s.records:
            if r.outcome != 'completed':
                continue
            ready = r.arrival_ms
            if r.turn_id:
                previous = records[r.turn_id - 1]
                ready = previous.complete_ms + s.turns[r.turn_id - 1].tool_duration_ms
            queued, start = prefill[r.request_id]
            parts = {'request_id': r.request_id, 'latency_ms': r.first_token_ms - ready,
                     'offload_tail_ms': r.arrival_ms - ready,
                     'admission_ms': r.admit_ms - r.arrival_ms,
                     'restore_ms': queued - r.admit_ms,
                     'prefill_queue_ms': max(start - queued, 0),
                     'self_prefill_ms': max(r.first_token_ms - start, 0)}
            if abs(sum(v for k, v in parts.items() if k.endswith('_ms') and k != 'latency_ms') - parts['latency_ms']) > 0.11:
                raise AssertionError('latency decomposition does not add up')
            if r.turn_id and s.sid in cohort:
                resume.append(parts)
            if r.request_class == 'normal' and warmup_ms <= r.arrival_ms < end_ms:
                normal.append(parts)

    def tail_parts(items, prefix):
        if not items:
            return {f'{prefix}_p95_{key}': None for key in ('request_id', 'latency_ms', 'offload_tail_ms',
                    'admission_ms', 'restore_ms', 'prefill_queue_ms', 'self_prefill_ms')}
        ordered = sorted(items, key=lambda r: (r['latency_ms'], r['request_id']))
        selected = ordered[max(math.ceil(.95 * len(ordered)) - 1, 0)]
        return {f'{prefix}_p95_{key}': value for key, value in selected.items()}

    stress_done = sum(s.state.value == 'done' for s in stress_jobs)
    return {**cost, 'cohort_transfer_bytes': cost['cohort_gpu_to_cpu_bytes'] + cost['cohort_cpu_to_gpu_bytes'],
            'cohort_stress_jobs': len(stress_jobs), 'cohort_stress_completed': stress_done,
            'stress_completion_pct': stress_done / len(stress_jobs) * 100 if stress_jobs else None,
            'resume_planned_turns': sum(len(s.turns) - 1 for s in stress_jobs),
            'resume_completed_turns': len(resume),
            'resume_p50_ms': percentile([r['latency_ms'] for r in resume], 50),
            'resume_p95_ms': percentile([r['latency_ms'] for r in resume], 95),
            'resume_tel_ms': sum(max(r['latency_ms'] - tel_threshold_ms, 0) for r in resume),
            'resume_tel_per_completed_turn_ms': sum(max(r['latency_ms'] - tel_threshold_ms, 0) for r in resume) / len(resume) if resume else None,
            'normal_tel_ms': sum(max(r['latency_ms'] - tel_threshold_ms, 0) for r in normal),
            'resume_offload_tail_count': sum(r['offload_tail_ms'] > 1e-7 for r in resume),
            **tail_parts(resume, 'resume'), **tail_parts(normal, 'normal'),
            'requests_sha256': digest([asdict(r) for r in sim.all_records()]),
            'occupancy_sha256': digest(sim.gpu.samples),
            **{k: getattr(sim, 'prefix_stats', {}).get(k, 0) for k in
               ('gpu_blocks_evicted', 'cpu_blocks_evicted', 'cpu_admission_rejected_blocks', 'partial_restores')}}
