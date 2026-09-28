"""Resource models shared by every policy: KV block pools and single-server FIFO links."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class GpuPool:
    """KV block allocator. Appends (time_ms, used_blocks) after every change for occupancy curves."""
    total_blocks: int
    used_blocks: int = 0
    samples: list = field(default_factory=list)

    @property
    def free_blocks(self):
        return self.total_blocks - self.used_blocks

    def can_alloc(self, blocks):
        return blocks <= self.free_blocks

    def alloc(self, blocks, now):
        if blocks > self.free_blocks:
            raise RuntimeError(f"GPU pool: requested {blocks} blocks, {self.free_blocks} free")
        self.used_blocks += blocks
        self.samples.append((now, self.used_blocks))

    def free(self, blocks, now):
        if blocks > self.used_blocks:
            raise RuntimeError("GPU pool: freeing more than allocated")
        self.used_blocks -= blocks
        self.samples.append((now, self.used_blocks))


@dataclass
class CpuPool:
    """Offload destination, tracked in bytes."""
    total_bytes: int
    used_bytes: int = 0

    @property
    def free_bytes(self):
        return self.total_bytes - self.used_bytes

    def try_alloc(self, nbytes):
        if nbytes > self.free_bytes:
            return False
        self.used_bytes += nbytes
        return True

    def free(self, nbytes):
        self.used_bytes -= nbytes
        if self.used_bytes < 0:
            raise RuntimeError("CPU pool: freeing more than allocated")


@dataclass
class FifoServer:
    """One server that handles one job at a time. Used for the PCIe link and the prefill engine."""
    rate_per_s: float               # bytes/s or tokens/s
    busy_until_ms: float = 0.0
    busy_ms_total: float = 0.0

    def schedule(self, now, work):
        """Reserve the server for `work` units starting at `now` or later; returns (start, end)."""
        start = max(now, self.busy_until_ms)
        duration_ms = work / self.rate_per_s * 1000
        end = start + duration_ms
        self.busy_until_ms = end
        self.busy_ms_total += duration_ms
        return start, end
