"""System parameters the simulator replays a trace against. Bytes, tokens and ms."""

from __future__ import annotations

from dataclasses import dataclass, fields
import json
import math
from pathlib import Path

from .workload import is_number


@dataclass(frozen=True)
class SystemConfig:
    gpu_kv_pool_bytes: int          # GPU memory reserved for KV blocks
    cpu_kv_pool_bytes: int          # host memory that offloaded KV may occupy
    kv_bytes_per_token: int         # 2 * layers * kv_heads * head_dim * dtype bytes
    block_size_tokens: int          # tokens per KV block
    pcie_bytes_per_s: float         # one-direction GPU<->CPU transfer rate
    prefill_tokens_per_s: float     # prefill throughput of one shared FIFO server
    decode_tokens_per_s: float      # per-session decode rate, no contention
    admission_max_wait_ms: float    # a turn waiting longer than this for KV blocks is rejected

    def __post_init__(self):
        for name in ("gpu_kv_pool_bytes", "cpu_kv_pool_bytes", "kv_bytes_per_token", "block_size_tokens"):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        for name in ("pcie_bytes_per_s", "prefill_tokens_per_s", "decode_tokens_per_s",
                     "admission_max_wait_ms"):
            value = getattr(self, name)
            if not is_number(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if self.total_blocks < 1:
            raise ValueError("gpu_kv_pool_bytes must hold at least one block")

    @property
    def block_bytes(self):
        return self.block_size_tokens * self.kv_bytes_per_token

    @property
    def total_blocks(self):
        return self.gpu_kv_pool_bytes // self.block_bytes

    def blocks_for(self, tokens):
        return math.ceil(tokens / self.block_size_tokens)

    def bytes_for_blocks(self, blocks):
        return blocks * self.block_bytes


def _load_object(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("config must be a JSON object")
    return data


def load_system_config(path):
    data = _load_object(path)
    expected = {f.name for f in fields(SystemConfig)}
    if set(data) != expected:
        raise ValueError(f"system config keys: missing={sorted(expected - set(data))}, "
                         f"unknown={sorted(set(data) - expected)}")
    return SystemConfig(**data)


def load_policy_params(path, policy):
    """Parameters for one policy from a JSON object keyed by policy name. Missing means defaults."""
    params = _load_object(path).get(policy, {})
    if not isinstance(params, dict):
        raise ValueError(f"parameters for {policy} must be a JSON object")
    return params
