"""Duration-Aware: choose KEEP / OFFLOAD / EVICT from the predicted tool wait. A simplified reference
policy after Continuum and MORI, not a proposal of this project.

- headroom >= keep_headroom                                          -> KEEP
- predicted wait > offload round trip * breakeven_factor and CPU room -> OFFLOAD
- otherwise                                                          -> EVICT
Under pressure the sessions with the longest remaining wait are cleared first.
The prediction is the trace's real wait (oracle) until a predictor exists."""

from __future__ import annotations

from .base import Action, Decision, Policy


class DurationAware(Policy):
    name = "duration_aware"

    def __init__(self, keep_headroom=0.3, breakeven_factor=2.0, **params):
        super().__init__(keep_headroom=keep_headroom, breakeven_factor=breakeven_factor, **params)
        self.keep_headroom = float(keep_headroom)
        self.breakeven_factor = float(breakeven_factor)

    def _choose(self, view, ctx, cpu_left):
        roundtrip_ms = 2 * ctx.offload_ms(view.gpu_blocks)
        nbytes = view.gpu_blocks * ctx.block_bytes
        if view.predicted_wait_ms > roundtrip_ms * self.breakeven_factor and nbytes <= cpu_left:
            return Action.OFFLOAD
        return Action.EVICT

    def on_idle_start(self, view, ctx):
        if ctx.headroom >= self.keep_headroom:
            return Decision(Action.KEEP)
        return Decision(self._choose(view, ctx, ctx.cpu_free_bytes))

    def select_victims(self, needed_blocks, candidates, ctx):
        victims, freed, cpu_left = [], 0, ctx.cpu_free_bytes
        for view in sorted(candidates, key=lambda c: -c.remaining_wait_ms):
            action = self._choose(view, ctx, cpu_left)
            if action is Action.OFFLOAD:
                cpu_left -= view.gpu_blocks * ctx.block_bytes
            victims.append((view.session_id, action))
            freed += view.gpu_blocks
            if freed >= needed_blocks:
                break
        return victims
