"""Block-granularity replay for T-LRU and HotPrefix retention-policy experiments.

Shares arrival, FIFO prefill/decode/PCIe timing and whole-turn reservations with
the legacy engine. Only TOOL_WAIT blocks are reclaimable. A per-session chain
represents known append-only prefixes; there is no invented cross-session reuse.
CPU/GPU placement is tracked per block, including in-flight ownership. Missing
tokens are recomputed exactly once, even with mixed GPU/CPU/missing history.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import heapq

from .engine import Simulator, SessionState, KvLoc, cached_prefix, held_tokens
from .policies.prefix import BlockView


@dataclass
class Block:
    index: int
    tokens: int = 0
    location: str = 'none'
    frequency: int = 0
    clock: int = 0
    last_access_ms: float = 0.0
    accessed_turn: int = -1


class PrefixSimulator(Simulator):
    def __init__(self, rows, system, policy, keep_events=True):
        super().__init__(rows, system, policy, keep_events)
        self.blocks = {sid: {} for sid in self.sessions}
        self.operations = {}
        self.operation_seq = 0
        self.prefill_tokens = {}
        self.processed_requests = 0
        self.resume_ready_ms = {}
        self.costs_by_request = {}
        self.prefix_stats = {'gpu_blocks_evicted': 0, 'cpu_blocks_evicted': 0,
                             'cpu_admission_rejected_blocks': 0, 'partial_restores': 0}

    def run(self):
        for s in self.sessions.values():
            self._push(s.job_arrival_ms, 'ARRIVE', s.sid, 0)
        handlers = {'ARRIVE': self._on_arrive, 'ADMIT_DEADLINE': self._on_admit_deadline,
                    'RELOAD_DONE': self._on_reload_done, 'FIRST_TOKEN': self._on_first_token,
                    'DECODE_END': self._on_decode_end, 'TOOL_END': self._on_tool_end,
                    'BLOCK_OFFLOAD_DONE': self._on_block_offload_done}
        while self._heap:
            self.now, _, kind, sid, idx = heapq.heappop(self._heap)
            handlers[kind](self.sessions[sid], idx)
        self.assert_resources()
        return self

    def assert_resources(self):
        if self.gpu.used_blocks != sum(s.gpu_blocks for s in self.sessions.values()):
            raise AssertionError('GPU accounting mismatch')
        if self.cpu.used_bytes != sum(s.cpu_bytes for s in self.sessions.values()):
            raise AssertionError('CPU accounting mismatch')
        if self.gpu.used_blocks or self.cpu.used_bytes or self.operations:
            raise AssertionError('unfinished resource ownership after drain')

    def _view(self, s):
        return replace(super()._view(s), predicted_wait_ms=0.0, remaining_wait_ms=0.0)

    def _block_view(self, s, b):
        return BlockView(s.sid, b.index, b.tokens, held_tokens(s.current),
                         s.idle_since_ms if s.idle_since_ms is not None else self.now,
                         b.last_access_ms, b.frequency, b.clock, self.system.block_size_tokens)

    def _candidates(self, location):
        return [self._block_view(s, b) for s in self.sessions.values()
                if s.state is SessionState.TOOL_WAIT
                for b in self.blocks[s.sid].values() if b.location == location]

    def _sync(self, s):
        blocks = self.blocks[s.sid].values()
        s.cpu_bytes = sum(b.location in ('cpu', 'to_cpu', 'to_gpu') for b in blocks) * self.system.block_bytes
        if s.state in (SessionState.NEW, SessionState.WAIT_ADMIT, SessionState.TOOL_WAIT):
            s.gpu_blocks = sum(b.location in ('gpu', 'to_cpu') for b in blocks)
            locations = {b.location for b in blocks}
            s.loc = (KvLoc.TO_CPU if 'to_cpu' in locations else KvLoc.GPU if 'gpu' in locations
                     else KvLoc.CPU if 'cpu' in locations else KvLoc.NONE)

    def _try_admit(self):
        while self.admission_queue:
            s = self.sessions[self.admission_queue[0]]
            if s.state is not SessionState.WAIT_ADMIT:
                self.admission_queue.popleft()
                continue
            need = self._needed_blocks(s)
            room = self._admit_room()
            if need > room:
                # All prefix policies use the same in-flight accounting. Never
                # select extra victims for blocks already scheduled to be freed.
                inflight = len(self.operations)
                shortfall = max(need - room - inflight, 0)
                if shortfall:
                    views = self.policy.select_blocks(shortfall, self._candidates('gpu'), self._ctx())
                    # CPU state cannot change through events during this batch.
                    # Policies need only their coldest eligible resident. A heap
                    # avoids rebuilding all block views for every selected page.
                    cpu_heap = [(self.policy.cpu_priority(b), b) for b in self._candidates('cpu')] if self.policy.uses_cpu else []
                    heapq.heapify(cpu_heap)
                    for view in views:
                        while cpu_heap and self.blocks[cpu_heap[0][1].session_id][cpu_heap[0][1].index].location != 'cpu':
                            heapq.heappop(cpu_heap)
                        residents = [cpu_heap[0][1]] if cpu_heap else []
                        self._reclaim(view, residents)
            if need <= self._admit_room():
                self.admission_queue.popleft()
                self._admit(s, need)
            else:
                break

    def _cost(self, s, **values):
        row = self.costs_by_request.setdefault(s.current.request_id,
                {'gpu_to_cpu_bytes': 0, 'cpu_to_gpu_bytes': 0, 'recomputed_tokens': 0})
        for key, value in values.items():
            row[key] += value

    def _reclaim(self, view, residents):
        s, b = self.sessions[view.session_id], self.blocks[view.session_id][view.index]
        if s.state is not SessionState.TOOL_WAIT or b.location != 'gpu':
            return
        admitted, victims, reason = self.policy.cpu_admission(
            view, residents, self.cpu.free_bytes // self.system.block_bytes)
        if self.policy.uses_cpu:
            self._log('CPU_ADMISSION', s, f'block={b.index} frequency={b.frequency} clock={b.clock}'
                      f' accepted={int(admitted)} reason={reason}')
        for sid, index in victims:
            self._drop_cpu(self.sessions[sid], self.blocks[sid][index])
        if admitted and self.cpu.try_alloc(self.system.block_bytes):
            b.location = 'to_cpu'
            self.operation_seq += 1
            op = self.operation_seq
            self.operations[op] = s.sid, b.index, s.current.request_id
            _, end = self.pcie.schedule(self.now, self.system.block_bytes)
            self.counters.offloads += 1
            self._sync(s)
            self._push(end, 'BLOCK_OFFLOAD_DONE', s.sid, op)
            self._log('OFFLOAD_START', s, f'block={b.index} bytes={self.system.block_bytes} done_at={end:.6f}')
        else:
            if self.policy.uses_cpu:
                self.prefix_stats['cpu_admission_rejected_blocks'] += 1
            b.location = 'none'
            self.gpu.free(1, self.now)
            self.counters.evictions += 1
            self.prefix_stats['gpu_blocks_evicted'] += 1
            self._sync(s)
            self._log('EVICT', s, f'block={b.index} tokens={b.tokens} reason=pressure')

    def _drop_cpu(self, s, b):
        if b.location != 'cpu' or s.state is not SessionState.TOOL_WAIT:
            raise AssertionError('attempt to evict a locked CPU block')
        b.location = 'none'
        self.cpu.free(self.system.block_bytes)
        self.counters.evictions += 1
        self.prefix_stats['cpu_blocks_evicted'] += 1
        self._sync(s)
        self._log('CPU_EVICT', s, f'block={b.index} tokens={b.tokens}')

    def _on_block_offload_done(self, s, op):
        sid, index, request_id = self.operations.pop(op)
        b = self.blocks[sid][index]
        if b.location != 'to_cpu':
            raise AssertionError('invalid offload completion')
        b.location = 'cpu'
        self.gpu.free(1, self.now)
        self.counters.gpu_to_cpu_bytes += self.system.block_bytes
        row = self.costs_by_request.setdefault(request_id,
                {'gpu_to_cpu_bytes': 0, 'cpu_to_gpu_bytes': 0, 'recomputed_tokens': 0})
        row['gpu_to_cpu_bytes'] += self.system.block_bytes
        self._sync(s)
        self._log('OFFLOAD_DONE', s, f'block={index}')
        if s.pending_arrival and not any(b.location == 'to_cpu' for b in self.blocks[sid].values()):
            s.pending_arrival = False
            self._on_arrive(s, s.idx)
        self._try_admit()

    def _admit(self, s, need):
        turn = s.current
        prefix = cached_prefix(turn)
        gpu_tokens = sum(b.tokens for b in self.blocks[s.sid].values() if b.location == 'gpu')
        cpu_tokens = sum(b.tokens for b in self.blocks[s.sid].values() if b.location == 'cpu')
        missing = prefix - gpu_tokens - cpu_tokens
        if missing < 0:
            raise AssertionError('cache exceeds known history')
        self.gpu.alloc(need, self.now)
        s.gpu_blocks += need
        s.record.admit_ms = self.now
        self.prefill_tokens[s.sid] = missing + turn.prompt_tokens
        self._log('ADMIT', s, f'blocks=+{need} held={s.gpu_blocks} wait_ms={self.now - s.record.arrival_ms:.6f}'
                  f' reused_gpu_tokens={gpu_tokens} reused_cpu_tokens={cpu_tokens} missing_tokens={missing}')
        if missing:
            s.record.recomputed_tokens = missing
            s.record.restore = 'recompute'
            self.counters.recomputed_tokens += missing
            self._cost(s, recomputed_tokens=missing)
            self._log('RECOMPUTE', s, f'tokens={missing}')
            if gpu_tokens or cpu_tokens:
                self.prefix_stats['partial_restores'] += 1
        if cpu_tokens:
            s.state, s.loc = SessionState.RELOADING, KvLoc.TO_GPU
            s.record.restore = 'reload_recompute' if missing else 'reload'
            for b in self.blocks[s.sid].values():
                if b.location == 'cpu':
                    b.location = 'to_gpu'
            _, end = self.pcie.schedule(self.now, s.cpu_bytes)
            self.counters.reloads += 1
            self.counters.cpu_to_gpu_bytes += s.cpu_bytes
            self._cost(s, cpu_to_gpu_bytes=s.cpu_bytes)
            self._push(end, 'RELOAD_DONE', s.sid, s.idx)
            self._log('RELOAD_START', s, f'bytes={s.cpu_bytes} done_at={end:.6f}')
        else:
            s.loc = KvLoc.GPU
            self._start_prefill(s, self.prefill_tokens[s.sid])

    def _on_reload_done(self, s, idx):
        self.cpu.free(s.cpu_bytes)
        for b in self.blocks[s.sid].values():
            if b.location == 'to_gpu':
                b.location = 'gpu'
        s.cpu_bytes, s.loc = 0, KvLoc.GPU
        self._log('RELOAD_DONE', s)
        self._start_prefill(s, self.prefill_tokens[s.sid])

    def _materialize(self, s, tokens):
        size = self.system.block_size_tokens
        for index in range(self.system.blocks_for(tokens)):
            b = self.blocks[s.sid].setdefault(index, Block(index))
            b.tokens = min(size, tokens - index * size)
            b.location = 'gpu'
            if b.accessed_turn != s.idx:
                b.frequency += 1
                b.clock = self.policy.max_age
                b.last_access_ms = self.now
                b.accessed_turn = s.idx

    def _on_first_token(self, s, idx):
        self._materialize(s, s.current.context_tokens)
        self.processed_requests += 1
        if self.processed_requests % self.policy.age_every_requests == 0:
            for blocks in self.blocks.values():
                for b in blocks.values():
                    b.clock = max(b.clock - 1, 0)
        super()._on_first_token(s, idx)

    def _on_decode_end(self, s, idx):
        self._materialize(s, held_tokens(s.current))
        super()._on_decode_end(s, idx)

    def _on_tool_end(self, s, idx):
        if s.state is SessionState.TOOL_WAIT and s.idx == idx:
            self.resume_ready_ms[s.turns[idx + 1].request_id] = self.now
        super()._on_tool_end(s, idx)

    def _release(self, s):
        if s.gpu_blocks:
            self.gpu.free(s.gpu_blocks, self.now)
        if s.cpu_bytes:
            self.cpu.free(s.cpu_bytes)
        for b in self.blocks[s.sid].values():
            b.location = 'none'
        s.gpu_blocks, s.cpu_bytes, s.loc = 0, 0, KvLoc.NONE

    def _on_admit_deadline(self, s, idx):
        if s.state is not SessionState.WAIT_ADMIT or s.idx != idx:
            return
        s.record.outcome, s.record.reject_reason = 'rejected', 'kv_pool_full'
        self._release(s)
        s.state, s.job_finished_ms = SessionState.REJECTED, self.now
        self.counters.rejected_jobs += 1
        self._log('REJECT', s, f'waited_ms={self.now - s.record.arrival_ms:.0f}')
        self._try_admit()

    def _complete(self, s):
        self._release(s)
        s.state, s.job_finished_ms = SessionState.DONE, self.now
        self.counters.completed_jobs += 1
        self._log('COMPLETE', s, f'job_ms={self.now - s.job_arrival_ms:.0f}')
        self._try_admit()
