"""Discrete-event replay of a trace under one KV cache policy.

Session states
  WAIT_ADMIT -(blocks granted)-> [RELOADING] -> PREFILLING -> DECODING -> TOOL_WAIT -(tool done)-> WAIT_ADMIT ... -> DONE
       \\-(admission_max_wait_ms exceeded)-> REJECTED (the rest of the session is dropped)

KV location
  NONE -(admit: prefill or recompute)-> GPU -(offload)-> TO_CPU -> CPU -(admit: reload)-> TO_GPU -> GPU
  GPU -(evict)-> NONE

Assumptions
- A turn reserves blocks for context_tokens + output_tokens when it is admitted.
- Prefill is one FIFO server, decode runs per session at a fixed rate, the PCIe link is one FIFO server.
- Tool waits are taken from the trace and do not depend on the policy.
- Only turn 0 has an external arrival; later turns arrive when the previous tool wait ends (see docs/trace_schema.md).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from enum import Enum
import heapq

from .policies import Action, IdleView, PolicyContext
from .resources import CpuPool, FifoServer, GpuPool


def is_last_turn(turn):
    return turn.turn_id == turn.expected_turns - 1


def held_tokens(turn):
    """Tokens in KV once this turn's output exists: the prefix, this prompt and this output."""
    return turn.context_tokens + turn.output_tokens


def cached_prefix(turn):
    """Tokens that must already be in KV when this turn starts."""
    return turn.context_tokens - turn.prompt_tokens


class SessionState(str, Enum):
    NEW = "new"
    WAIT_ADMIT = "wait_admit"
    RELOADING = "reloading"
    PREFILLING = "prefilling"
    DECODING = "decoding"
    TOOL_WAIT = "tool_wait"
    DONE = "done"
    REJECTED = "rejected"


class KvLoc(str, Enum):
    NONE = "none"
    GPU = "gpu"
    CPU = "cpu"
    TO_CPU = "to_cpu"
    TO_GPU = "to_gpu"


@dataclass
class RequestRecord:
    request_id: str
    session_id: str
    turn_id: int
    request_class: str
    arrival_ms: float
    admit_ms: float | None = None
    first_token_ms: float | None = None
    complete_ms: float | None = None
    outcome: str = "pending"            # completed | rejected | pending
    reject_reason: str = ""
    restore: str = "none"               # none | reload | recompute
    recomputed_tokens: int = 0

    @property
    def ttft_ms(self):
        return None if self.first_token_ms is None else self.first_token_ms - self.arrival_ms


@dataclass
class Session:
    turns: list
    idx: int = 0
    state: SessionState = SessionState.NEW
    loc: KvLoc = KvLoc.NONE
    gpu_blocks: int = 0
    cpu_bytes: int = 0
    idle_since_ms: float | None = None
    ttl_deadline_ms: float | None = None
    job_arrival_ms: float = 0.0
    job_finished_ms: float | None = None
    pending_arrival: bool = False
    records: list = field(default_factory=list)

    @property
    def sid(self):
        return self.turns[0].session_id

    @property
    def request_class(self):
        return self.turns[0].request_class

    @property
    def current(self):
        return self.turns[self.idx]

    @property
    def record(self):
        return self.records[-1]


@dataclass
class Counters:
    evictions: int = 0
    offloads: int = 0
    reloads: int = 0
    gpu_to_cpu_bytes: int = 0
    cpu_to_gpu_bytes: int = 0
    recomputed_tokens: int = 0
    completed_jobs: int = 0
    rejected_jobs: int = 0


EVENT_COLUMNS = ["time_ms", "event", "session_id", "turn_id", "request_id", "detail"]


class Simulator:
    def __init__(self, rows, system, policy, keep_events=True):
        self.system = system
        self.policy = policy
        self.keep_events = keep_events
        self.gpu = GpuPool(system.total_blocks)
        self.cpu = CpuPool(system.cpu_kv_pool_bytes)
        self.pcie = FifoServer(system.pcie_bytes_per_s)
        self.prefill = FifoServer(system.prefill_tokens_per_s)
        self.counters = Counters()
        self.events = []
        self.now = 0.0
        self._heap = []
        self._seq = 0
        self.admission_queue = deque()

        by_session = {}
        for row in rows:
            by_session.setdefault(row.session_id, []).append(row)
        self.sessions = {}
        for session_id, turns in by_session.items():
            turns.sort(key=lambda t: t.turn_id)
            self.sessions[session_id] = Session(turns=turns, job_arrival_ms=float(turns[0].arrival_ms))

    # event queue
    def _push(self, time_ms, kind, sid, idx=-1):
        self._seq += 1
        heapq.heappush(self._heap, (time_ms, self._seq, kind, sid, idx))

    def _log(self, event, s=None, detail=""):
        if not self.keep_events:
            return
        if s is None:
            self.events.append((self.now, event, "", -1, "", detail))
        else:
            turn = s.turns[min(s.idx, len(s.turns) - 1)]
            self.events.append((self.now, event, s.sid, turn.turn_id, turn.request_id, detail))

    def run(self):
        for s in self.sessions.values():
            self._push(s.job_arrival_ms, "ARRIVE", s.sid, 0)
        handlers = {
            "ARRIVE": self._on_arrive, "ADMIT_DEADLINE": self._on_admit_deadline,
            "RELOAD_DONE": self._on_reload_done, "FIRST_TOKEN": self._on_first_token,
            "DECODE_END": self._on_decode_end, "TOOL_END": self._on_tool_end,
            "TTL_EXPIRE": self._on_ttl_expire, "OFFLOAD_DONE": self._on_offload_done,
        }
        while self._heap:
            time_ms, _, kind, sid, idx = heapq.heappop(self._heap)
            self.now = time_ms
            handlers[kind](self.sessions[sid], idx)
        return self

    # views handed to the policy
    def _ctx(self):
        return PolicyContext(
            now_ms=self.now, free_blocks=self.gpu.free_blocks, total_blocks=self.gpu.total_blocks,
            block_bytes=self.system.block_bytes, cpu_free_bytes=self.cpu.free_bytes,
            pcie_bytes_per_s=self.system.pcie_bytes_per_s, prefill_tokens_per_s=self.system.prefill_tokens_per_s,
            admission_queue_len=len(self.admission_queue),
        )

    def _view(self, s):
        turn = s.current
        idle_since = s.idle_since_ms if s.idle_since_ms is not None else self.now
        predicted = max(float(turn.tool_duration_ms), 0.0)
        return IdleView(
            session_id=s.sid, request_class=s.request_class, gpu_blocks=s.gpu_blocks,
            context_tokens=held_tokens(turn), idle_since_ms=idle_since, predicted_wait_ms=predicted,
            remaining_wait_ms=max(idle_since + predicted - self.now, 0.0), ttl_deadline_ms=s.ttl_deadline_ms,
        )

    def _idle_candidates(self, exclude):
        return [self._view(s) for s in self.sessions.values()
                if s.state is SessionState.TOOL_WAIT and s.loc is KvLoc.GPU and s.gpu_blocks > 0 and s.sid != exclude]

    # arrival and admission
    def _on_arrive(self, s, idx):
        if s.loc is KvLoc.TO_CPU:          # came back while an offload is in flight; retry when it lands
            s.pending_arrival = True
            return
        turn = s.current
        s.records.append(RequestRecord(turn.request_id, s.sid, turn.turn_id, s.request_class, self.now))
        s.state = SessionState.WAIT_ADMIT
        self.admission_queue.append(s.sid)
        self._push(self.now + self.system.admission_max_wait_ms, "ADMIT_DEADLINE", s.sid, s.idx)
        self._log("ARRIVE", s, f"loc={s.loc.value}")
        self._try_admit()

    def _needed_blocks(self, s):
        total = self.system.blocks_for(held_tokens(s.current))
        held = s.gpu_blocks if s.loc is KvLoc.GPU else 0
        return max(total - held, 0)

    def _try_admit(self):
        while self.admission_queue:
            s = self.sessions[self.admission_queue[0]]
            if s.state is not SessionState.WAIT_ADMIT:
                self.admission_queue.popleft()
                continue
            need = self._needed_blocks(s)
            if not self.gpu.can_alloc(need):
                shortfall = need - self.gpu.free_blocks
                victims = self.policy.select_victims(shortfall, self._idle_candidates(s.sid), self._ctx())
                self._apply_victims(victims)
            if self.gpu.can_alloc(need):
                self.admission_queue.popleft()
                self._admit(s, need)
            else:
                break   # FIFO: the head waits, so does everyone behind it

    def _apply_victims(self, victims):
        for sid, action in victims:
            victim = self.sessions.get(sid)
            if victim is None or victim.state is not SessionState.TOOL_WAIT or victim.loc is not KvLoc.GPU:
                continue
            if action is Action.EVICT:
                self._evict(victim, reason="pressure")
            elif action is Action.OFFLOAD:
                self._start_offload(victim, reason="pressure")

    def _admit(self, s, need):
        turn = s.current
        self.gpu.alloc(need, self.now)
        s.gpu_blocks += need
        s.record.admit_ms = self.now
        self._log("ADMIT", s, f"blocks=+{need} held={s.gpu_blocks} wait_ms={self.now - s.record.arrival_ms:.1f}")
        if s.loc is KvLoc.CPU:                       # reload
            s.loc, s.state = KvLoc.TO_GPU, SessionState.RELOADING
            s.record.restore = "reload"
            _, end = self.pcie.schedule(self.now, s.cpu_bytes)
            self.counters.reloads += 1
            self.counters.cpu_to_gpu_bytes += s.cpu_bytes
            self._push(end, "RELOAD_DONE", s.sid, s.idx)
            self._log("RELOAD_START", s, f"bytes={s.cpu_bytes} done_at={end:.1f}")
            return
        if s.loc is KvLoc.NONE and turn.turn_id > 0:  # recompute the evicted prefix
            prefix = cached_prefix(turn)
            s.record.restore, s.record.recomputed_tokens = "recompute", prefix
            self.counters.recomputed_tokens += prefix
            self._log("RECOMPUTE", s, f"tokens={prefix}")
            s.loc = KvLoc.GPU
            self._start_prefill(s, prefix + turn.prompt_tokens)
            return
        s.loc = KvLoc.GPU
        self._start_prefill(s, turn.prompt_tokens)

    def _start_prefill(self, s, tokens):
        s.state = SessionState.PREFILLING
        start, end = self.prefill.schedule(self.now, tokens)
        self._push(end, "FIRST_TOKEN", s.sid, s.idx)
        self._log("PREFILL_QUEUED", s, f"tokens={tokens} start={start:.1f} end={end:.1f}")

    def _on_reload_done(self, s, idx):
        self.cpu.free(s.cpu_bytes)
        s.cpu_bytes, s.loc = 0, KvLoc.GPU
        self._log("RELOAD_DONE", s)
        self._start_prefill(s, s.current.prompt_tokens)

    # generation
    def _on_first_token(self, s, idx):
        s.record.first_token_ms = self.now
        s.state = SessionState.DECODING
        decode_ms = s.current.output_tokens / self.system.decode_tokens_per_s * 1000
        self._push(self.now + decode_ms, "DECODE_END", s.sid, s.idx)
        self._log("FIRST_TOKEN", s, f"ttft_ms={s.record.ttft_ms:.1f}")

    def _on_decode_end(self, s, idx):
        turn = s.current
        s.record.complete_ms, s.record.outcome = self.now, "completed"
        self._log("DECODE_END", s)
        if is_last_turn(turn):
            self._complete(s)
            return
        s.state, s.idle_since_ms, s.ttl_deadline_ms = SessionState.TOOL_WAIT, self.now, None
        self._push(self.now + turn.tool_duration_ms, "TOOL_END", s.sid, s.idx)
        decision = self.policy.on_idle_start(self._view(s), self._ctx())
        self._log("TOOL_START", s, f"wait_ms={turn.tool_duration_ms:.0f} decision={decision.action.value}"
                                    + (f" ttl_ms={decision.ttl_ms:.0f}" if decision.ttl_ms else ""))
        self._apply_decision(s, decision)
        self._try_admit()

    def _apply_decision(self, s, decision):
        if decision.action is Action.KEEP:
            if decision.ttl_ms is not None:
                s.ttl_deadline_ms = self.now + decision.ttl_ms
                self._push(s.ttl_deadline_ms, "TTL_EXPIRE", s.sid, s.idx)
        elif decision.action is Action.OFFLOAD:
            self._start_offload(s, reason="idle")
        elif decision.action is Action.EVICT:
            self._evict(s, reason="idle")

    # moving and dropping KV
    def _start_offload(self, s, reason):
        nbytes = self.system.bytes_for_blocks(s.gpu_blocks)
        if not self.cpu.try_alloc(nbytes):
            self._log("OFFLOAD_SKIPPED", s, "cpu_pool_full -> evict")
            self._evict(s, reason=reason)
            return
        s.loc, s.cpu_bytes = KvLoc.TO_CPU, nbytes
        _, end = self.pcie.schedule(self.now, nbytes)
        self.counters.offloads += 1
        self._push(end, "OFFLOAD_DONE", s.sid, s.idx)
        self._log("OFFLOAD_START", s, f"bytes={nbytes} reason={reason} done_at={end:.1f}")

    def _on_offload_done(self, s, idx):
        self.gpu.free(s.gpu_blocks, self.now)
        self.counters.gpu_to_cpu_bytes += s.cpu_bytes
        s.gpu_blocks, s.loc = 0, KvLoc.CPU
        self._log("OFFLOAD_DONE", s)
        if s.pending_arrival:
            s.pending_arrival = False
            self._on_arrive(s, s.idx)
        self._try_admit()

    def _evict(self, s, reason):
        if s.gpu_blocks:
            self.gpu.free(s.gpu_blocks, self.now)
        self.counters.evictions += 1
        self._log("EVICT", s, f"blocks={s.gpu_blocks} reason={reason}")
        s.gpu_blocks, s.loc = 0, KvLoc.NONE

    def _on_ttl_expire(self, s, idx):
        if s.state is not SessionState.TOOL_WAIT or s.idx != idx or s.loc is not KvLoc.GPU:
            return
        action = self.policy.on_ttl_expire(self._view(s), self._ctx())
        self._log("TTL_EXPIRE", s, f"action={action.value}")
        if action is Action.EVICT:
            self._evict(s, reason="ttl")
        elif action is Action.OFFLOAD:
            self._start_offload(s, reason="ttl")
        self._try_admit()

    # resume and finish
    def _on_tool_end(self, s, idx):
        if s.state is not SessionState.TOOL_WAIT or s.idx != idx:
            return
        self._log("TOOL_END", s)
        s.idx += 1
        s.state, s.idle_since_ms, s.ttl_deadline_ms = SessionState.NEW, None, None
        self._on_arrive(s, s.idx)

    def _on_admit_deadline(self, s, idx):
        if s.state is not SessionState.WAIT_ADMIT or s.idx != idx:
            return
        s.record.outcome, s.record.reject_reason = "rejected", "kv_pool_full"
        if s.gpu_blocks:
            self.gpu.free(s.gpu_blocks, self.now)
        if s.cpu_bytes:
            self.cpu.free(s.cpu_bytes)
        s.gpu_blocks, s.cpu_bytes, s.loc = 0, 0, KvLoc.NONE
        s.state, s.job_finished_ms = SessionState.REJECTED, self.now
        self.counters.rejected_jobs += 1
        self._log("REJECT", s, f"waited_ms={self.now - s.record.arrival_ms:.0f}")
        self._try_admit()

    def _complete(self, s):
        if s.gpu_blocks:
            self.gpu.free(s.gpu_blocks, self.now)
        if s.cpu_bytes:
            self.cpu.free(s.cpu_bytes)
        s.gpu_blocks, s.cpu_bytes, s.loc = 0, 0, KvLoc.NONE
        s.state, s.job_finished_ms = SessionState.DONE, self.now
        self.counters.completed_jobs += 1
        self._log("COMPLETE", s, f"job_ms={self.now - s.job_arrival_ms:.0f}")
        self._try_admit()

    # results
    def all_records(self):
        return [r for s in self.sessions.values() for r in s.records]
