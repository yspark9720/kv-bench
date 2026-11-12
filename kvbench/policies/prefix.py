"""Online block policies. No future tool duration or next prompt is exposed here.

T-LRU: Zhang et al., NeurIPS 2025, Algorithm 1 (block-rounded).
HotPrefix: Li et al., PACMMOD 3(4), 2025, sections 3.2--3.3.
HotPrefix promotion/pipelining and cross-session prefix sharing are not modeled.
"""
from __future__ import annotations

from dataclasses import dataclass
import math

from .base import Action, Decision, Policy


@dataclass(frozen=True)
class BlockView:
    session_id: str
    index: int
    tokens: int
    history_tokens: int
    idle_since_ms: float
    last_access_ms: float
    frequency: int
    clock: int
    block_size_tokens: int

    @property
    def key(self):
        return self.session_id, self.index


def _positive(value, name, integer=False, zero=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f'{name} must be finite')
    if value < 0 or (not zero and value == 0) or (integer and int(value) != value):
        raise ValueError(f'invalid {name}')
    return int(value) if integer else float(value)


class PrefixPolicy(Policy):
    uses_prefix_cache = True
    uses_cpu = False
    max_age = 15
    age_every_requests = 100

    def on_idle_start(self, view, ctx):
        return Decision(Action.KEEP)

    def select_blocks(self, needed, candidates, ctx):
        raise NotImplementedError

    def cpu_admission(self, block, residents, free_blocks):
        """Return (admit, CPU victim keys, reason). In-flight/active blocks are excluded."""
        return False, [], 'cpu_disabled'

    def cpu_priority(self, block):
        return block.last_access_ms, block.session_id, -block.index

    def _lru(self, needed, candidates):
        # A branch is reclaimed from its deepest GPU block toward its root.
        return sorted(candidates, key=lambda b: (b.idle_since_ms, b.session_id, -b.index))[:needed]


class PrefixLruEvict(PrefixPolicy):
    name = 'prefix_lru_evict'

    def select_blocks(self, needed, candidates, ctx):
        return self._lru(needed, candidates)


class TailLru(PrefixLruEvict):
    name = 'tlru'

    def __init__(self, threshold_ms=200.0, next_prompt_estimate=160, **params):
        self.threshold_ms = _positive(threshold_ms, 'threshold_ms', zero=True)
        self.next_prompt_estimate = _positive(next_prompt_estimate, 'next_prompt_estimate', integer=True, zero=True)
        super().__init__(threshold_ms=self.threshold_ms,
                         next_prompt_estimate=self.next_prompt_estimate, **params)

    def select_blocks(self, needed, candidates, ctx):
        if needed <= 0:
            return []
        xi = math.floor(self.threshold_ms * ctx.prefill_tokens_per_s / 1000)
        ordered = self._lru(len(candidates), candidates)
        safe = []
        for block in ordered:
            keep_tokens = max(block.history_tokens + self.next_prompt_estimate - xi, 0)
            # Round retention UP: never cross the TEL-safe boundary within a page.
            keep_blocks = math.ceil(keep_tokens / block.block_size_tokens)
            if block.index >= keep_blocks:
                safe.append(block)
        chosen = safe[:needed]
        chosen_keys = {b.key for b in chosen}
        if len(chosen) < needed:
            chosen.extend(b for b in ordered if b.key not in chosen_keys)
        return chosen[:needed]


class PrefixLruOffload(PrefixLruEvict):
    name = 'prefix_lru_offload'
    uses_cpu = True

    def cpu_admission(self, block, residents, free_blocks):
        if free_blocks:
            return True, [], 'space'
        if residents:
            victim = min(residents, key=lambda b: (b.last_access_ms, b.session_id, -b.index))
            return True, [victim.key], 'replace_cpu_lru'
        return False, [], 'cpu_locked_or_too_small'


class HotPrefixAdmission(PrefixLruOffload):
    name = 'hotprefix_admission'

    def __init__(self, frequency_threshold=2, max_age=15, age_every_requests=100,
                 gpu_eviction='lru', selective=True, **params):
        self.frequency_threshold = _positive(frequency_threshold, 'frequency_threshold', integer=True)
        self.max_age = _positive(max_age, 'max_age', integer=True)
        self.age_every_requests = _positive(age_every_requests, 'age_every_requests', integer=True)
        if gpu_eviction not in ('lru', 'hotness'):
            raise ValueError('gpu_eviction must be lru or hotness')
        if type(selective) is not bool:
            raise ValueError('selective must be bool')
        self.gpu_eviction, self.selective = gpu_eviction, selective
        super().__init__(frequency_threshold=self.frequency_threshold, max_age=self.max_age,
                         age_every_requests=self.age_every_requests, gpu_eviction=gpu_eviction,
                         selective=selective, **params)

    def select_blocks(self, needed, candidates, ctx):
        if self.gpu_eviction == 'lru':
            return self._lru(needed, candidates)
        # Algorithm 1: expose a parent only after its last GPU child is selected.
        branches = {}
        for block in candidates:
            branches.setdefault(block.session_id, []).append(block)
        for branch in branches.values():
            branch.sort(key=lambda b: b.index)
        chosen = []
        while branches and len(chosen) < needed:
            leaf = min((b[-1] for b in branches.values()), key=lambda b: (
                b.frequency + b.clock / b.tokens, b.idle_since_ms, b.session_id))
            chosen.append(leaf)
            branches[leaf.session_id].pop()
            if not branches[leaf.session_id]:
                del branches[leaf.session_id]
        return chosen

    def cpu_admission(self, block, residents, free_blocks):
        if not self.selective:
            return super().cpu_admission(block, residents, free_blocks)
        if block.frequency < self.frequency_threshold:
            return False, [], 'frequency'
        coldest = min(residents, key=lambda b: (b.frequency * b.clock, b.last_access_ms,
                                                b.session_id, -b.index), default=None)
        # Eq. 6. clock is the bounded, periodically aged counter from section 3.2.
        # The prose near Eq. 5 calls it elapsed time; using seconds there would
        # contradict the explicitly specified reset-to-max/decay algorithm.
        if coldest is not None and block.frequency * block.clock < coldest.frequency * coldest.clock:
            return False, [], 'hotness'
        if free_blocks:
            return True, [], 'frequency_and_hotness'
        if coldest is not None:
            return True, [coldest.key], 'replace_cpu_coldest'
        return False, [], 'cpu_locked_or_too_small'

    def cpu_priority(self, block):
        if not self.selective:
            return super().cpu_priority(block)
        return block.frequency * block.clock, block.last_access_ms, block.session_id, -block.index
