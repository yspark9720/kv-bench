"""Evict/Recompute: drop KV as soon as a session goes idle and prefill the whole prefix again on resume.
Same behaviour as vLLM preemption in recompute mode."""

from __future__ import annotations

from .base import Action, Decision, Policy


class EvictRecompute(Policy):
    name = "evict_recompute"

    def on_idle_start(self, view, ctx):
        return Decision(Action.EVICT)
