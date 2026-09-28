"""Dynamic TTL (Continuum): a TTL computed from per-tool wait distributions, reuse value and queue state.

Not implemented yet. Needs a tool identifier per turn in the trace and the TTL rule written down."""

from __future__ import annotations

from .base import Policy


class DynamicTTL(Policy):
    name = "dynamic_ttl"

    def on_idle_start(self, view, ctx):
        raise NotImplementedError("dynamic_ttl is not implemented; the trace has no tool identifier yet")
