"""LRU Offload: idle KV stays on the GPU until blocks run out, then the longest-idle sessions move to
CPU memory and reload on resume. Falls back to eviction when the CPU pool is full. LMCache-style offloading."""

from __future__ import annotations

from .base import Action, Decision, Policy


class LruOffload(Policy):
    name = "lru_offload"

    def on_idle_start(self, view, ctx):
        return Decision(Action.KEEP)

    def select_victims(self, needed_blocks, candidates, ctx):
        victims, freed, cpu_left = [], 0, ctx.cpu_free_bytes
        for view in sorted(candidates, key=lambda c: c.idle_since_ms):
            nbytes = view.gpu_blocks * ctx.block_bytes
            if nbytes <= cpu_left:
                victims.append((view.session_id, Action.OFFLOAD))
                cpu_left -= nbytes
            else:
                victims.append((view.session_id, Action.EVICT))
            freed += view.gpu_blocks
            if freed >= needed_blocks:
                break
        return victims
