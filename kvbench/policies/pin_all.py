"""Pin-All: idle KV stays on the GPU; new turns wait or get rejected. Matches vLLM's default behaviour."""

from __future__ import annotations

from .base import Action, Decision, Policy


class PinAll(Policy):
    name = "pin_all"

    def on_idle_start(self, view, ctx):
        return Decision(Action.KEEP)
