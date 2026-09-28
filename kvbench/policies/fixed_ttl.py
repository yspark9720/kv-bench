"""Fixed TTL: keep idle KV on the GPU for ttl_ms, then evict. The fixed-TTL ablation from Continuum.

Parameters
- ttl_ms: how long idle KV stays (default 30 s)
- evict_on_pressure: if true, evict the longest-idle sessions before their TTL when blocks run out
"""

from __future__ import annotations

from .base import Action, Decision, Policy


class FixedTTL(Policy):
    name = "fixed_ttl"

    def __init__(self, ttl_ms=30000.0, evict_on_pressure=False, **params):
        super().__init__(ttl_ms=ttl_ms, evict_on_pressure=evict_on_pressure, **params)
        self.ttl_ms = float(ttl_ms)
        self.evict_on_pressure = bool(evict_on_pressure)

    def on_idle_start(self, view, ctx):
        return Decision(Action.KEEP, ttl_ms=self.ttl_ms)

    def on_ttl_expire(self, view, ctx):
        return Action.EVICT

    def select_victims(self, needed_blocks, candidates, ctx):
        if not self.evict_on_pressure:
            return []
        victims, freed = [], 0
        for view in sorted(candidates, key=lambda c: c.idle_since_ms):
            victims.append((view.session_id, Action.EVICT))
            freed += view.gpu_blocks
            if freed >= needed_blocks:
                break
        return victims
