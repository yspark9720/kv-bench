"""Watermark Offload: keep idle KV like LRU Offload, but admit new turns only up to an occupancy ceiling
(default 90 %, the SLO headroom target). A turn that would cross the ceiling first offloads the longest-idle
sessions to CPU; it is admitted once their blocks are freed. Proposed in docs/week10_plan.md.

pin_all_capped is the control: the same ceiling without any reclaim, so turns wait or get rejected."""

from __future__ import annotations

from .lru_offload import LruOffload
from .pin_all import PinAll


class WatermarkOffload(LruOffload):
    name = "watermark_offload"

    def __init__(self, admission_ceiling=0.9, **params):
        super().__init__(admission_ceiling=admission_ceiling, **params)
        if not 0 < float(admission_ceiling) <= 1:
            raise ValueError("admission_ceiling must be in (0, 1]")
        self.admission_ceiling = float(admission_ceiling)


class CappedPinAll(PinAll):
    name = "pin_all_capped"

    def __init__(self, admission_ceiling=0.9, **params):
        super().__init__(admission_ceiling=admission_ceiling, **params)
        if not 0 < float(admission_ceiling) <= 1:
            raise ValueError("admission_ceiling must be in (0, 1]")
        self.admission_ceiling = float(admission_ceiling)
