"""KV cache policies. Add a module here and register the class in REGISTRY."""

from __future__ import annotations

from .base import Action, Decision, IdleView, Policy, PolicyContext
from .duration_aware import DurationAware
from .dynamic_ttl import DynamicTTL
from .evict_recompute import EvictRecompute
from .fixed_ttl import FixedTTL
from .lru_offload import LruOffload
from .pin_all import PinAll
from .prefix import PrefixLruEvict, PrefixLruOffload, TailLru, HotPrefixAdmission
from .watermark_offload import CappedPinAll, WatermarkOffload

REGISTRY = {cls.name: cls for cls in (PinAll, EvictRecompute, FixedTTL, LruOffload, DynamicTTL, DurationAware,
                                      WatermarkOffload, CappedPinAll, PrefixLruEvict,
                                      PrefixLruOffload, TailLru, HotPrefixAdmission)}


def make_policy(name, params=None):
    if name not in REGISTRY:
        raise ValueError(f"unknown policy {name}; choose from {sorted(REGISTRY)}")
    return REGISTRY[name](**(params or {}))


__all__ = ["Action", "Decision", "IdleView", "Policy", "PolicyContext", "REGISTRY", "make_policy"]
