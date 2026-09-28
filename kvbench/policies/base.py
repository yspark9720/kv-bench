"""Policy interface. A policy decides at three points and never touches engine state directly.

- on_idle_start: a session enters tool wait -> KEEP / OFFLOAD / EVICT, optionally with a TTL
- on_ttl_expire: the TTL from on_idle_start ran out (default EVICT)
- select_victims: a new turn cannot get KV blocks -> which idle sessions to clear
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum


class Action(str, Enum):
    KEEP = "keep"
    OFFLOAD = "offload"
    EVICT = "evict"


@dataclass(frozen=True)
class IdleView:
    """Read-only view of one idle session."""
    session_id: str
    request_class: str
    gpu_blocks: int
    context_tokens: int             # tokens held in KV while idle
    idle_since_ms: float
    predicted_wait_ms: float        # tool wait estimate; currently the trace value (oracle)
    remaining_wait_ms: float        # predicted_wait_ms minus time already waited
    ttl_deadline_ms: float | None


@dataclass(frozen=True)
class Decision:
    action: Action
    ttl_ms: float | None = None     # with KEEP: call on_ttl_expire after this long


@dataclass(frozen=True)
class PolicyContext:
    """Resource state and cost parameters at decision time."""
    now_ms: float
    free_blocks: int
    total_blocks: int
    block_bytes: int
    cpu_free_bytes: int
    pcie_bytes_per_s: float
    prefill_tokens_per_s: float
    admission_queue_len: int

    @property
    def headroom(self):
        return self.free_blocks / self.total_blocks if self.total_blocks else 0.0

    def offload_ms(self, blocks):
        """One-direction transfer time for `blocks`."""
        return blocks * self.block_bytes / self.pcie_bytes_per_s * 1000

    def recompute_ms(self, tokens):
        """Prefill time to rebuild `tokens` of prefix."""
        return tokens / self.prefill_tokens_per_s * 1000


class Policy(ABC):
    name = "base"

    def __init__(self, **params):
        self.params = params

    @abstractmethod
    def on_idle_start(self, view, ctx):
        ...

    def on_ttl_expire(self, view, ctx):
        return Action.EVICT

    def select_victims(self, needed_blocks, candidates, ctx):
        """(session_id, action) pairs to clear under memory pressure. Default: nothing, so the turn waits."""
        return []

    def describe(self):
        return {"name": self.name, **self.params}
