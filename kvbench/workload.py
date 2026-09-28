"""Policy-independent session traces. Times use milliseconds, rates use /second."""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
import hashlib
import json
import math
from pathlib import Path
import random


SCHEMA_VERSION = 1
WORKLOADS = ("W0", "W1", "W2")
STRESS_LEVELS = (1, 2, 4, 8)


def is_number(value):
    return type(value) in (int, float) and math.isfinite(value)


@dataclass(frozen=True)
class Config:
    duration_ms: float
    warmup_ms: float
    normal_arrival_rate_per_s: float
    stress_arrival_rate_per_s: float
    normal_prompt_tokens: list
    stress_initial_prompt_tokens: list
    stress_followup_prompt_tokens: list
    output_tokens: list
    stress_turns: list
    tool_duration_ms: list
    w2_short_wait_factor: float
    w2_long_wait_factor: float
    w2_burst_period_ms: float
    w2_burst_width_ms: float

    def __post_init__(self):
        for name in ("duration_ms", "normal_arrival_rate_per_s",
                     "stress_arrival_rate_per_s", "w2_short_wait_factor",
                     "w2_long_wait_factor", "w2_burst_period_ms", "w2_burst_width_ms"):
            value = getattr(self, name)
            if not is_number(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if not is_number(self.warmup_ms) or not 0 <= self.warmup_ms < self.duration_ms:
            raise ValueError("warmup_ms must satisfy 0 <= warmup_ms < duration_ms")
        if not self.w2_short_wait_factor < 1 < self.w2_long_wait_factor:
            raise ValueError("W2 factors must satisfy short < 1 < long")
        if self.w2_burst_width_ms > self.w2_burst_period_ms:
            raise ValueError("burst width must not exceed its period")
        for name in ("normal_prompt_tokens", "stress_initial_prompt_tokens",
                     "stress_followup_prompt_tokens", "output_tokens", "stress_turns",
                     "tool_duration_ms"):
            value = getattr(self, name)
            if (not isinstance(value, list) or len(value) != 2
                    or any(type(x) is not int or x < 1 for x in value)
                    or value[0] > value[1]):
                raise ValueError(f"{name} must be [positive integer min, max]")
        if self.stress_turns[0] < 2:
            raise ValueError("stress sessions must have at least two turns")


@dataclass(frozen=True)
class Turn:
    schema_version: int
    session_id: str
    turn_id: int
    request_id: str
    request_class: str
    seed: int
    workload_id: str
    stress_level: int
    session_arrival_ms: float
    arrival_ms: float | None
    depends_on_request_id: str | None
    prompt_tokens: int
    output_tokens: int
    context_tokens: int
    expected_turns: int
    tool_duration_ms: float
    arrival_rate_per_s: float
    burst_id: int | None


def load_config(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("config must be a JSON object")
    expected = {f.name for f in fields(Config)}
    if set(data) != expected:
        raise ValueError(f"config keys: missing={sorted(expected - set(data))}, "
                         f"unknown={sorted(set(data) - expected)}")
    return Config(**data)


def _rng(seed, stream):
    # Do not use Python's process-randomized hash(). Each class has its own stream.
    digest = hashlib.sha256(f"{seed}:{stream}".encode()).digest()
    return random.Random(int.from_bytes(digest, "big"))


def _arrivals(rng, rate, duration_ms):
    now = 0.0
    while True:
        now += rng.expovariate(rate) * 1000
        if now >= duration_ms:
            return
        yield now


def generate(config, workload="W0", stress_level=1, seed=0):
    if workload not in WORKLOADS:
        raise ValueError(f"workload must be one of {WORKLOADS}")
    if type(stress_level) is not int or stress_level not in STRESS_LEVELS:
        raise ValueError(f"stress_level must be one of {STRESS_LEVELS}")
    if workload == "W0" and stress_level != 1:
        raise ValueError("W0 only supports stress_level=1")
    if type(seed) is not int or seed < 0:
        raise ValueError("seed must be a nonnegative integer")
    rows = []
    for request_class in ("normal", "stress"):
        rate = getattr(config, f"{request_class}_arrival_rate_per_s")
        arrival_rng = _rng(seed, f"{request_class}:arrivals")
        content_rng = _rng(seed, f"{request_class}:content")
        for index, arrival in enumerate(_arrivals(arrival_rng, rate, config.duration_ms)):
            session_id = f"{request_class}-{index:06d}"
            burst_id = None
            if request_class == "stress" and workload == "W2":
                burst_id = int(arrival // config.w2_burst_period_ms)
                start = burst_id * config.w2_burst_period_ms
                # Compress even a partial final period into its burst window.
                period = min(config.w2_burst_period_ms, config.duration_ms - start)
                width = min(config.w2_burst_width_ms, period)
                arrival = start + (arrival - start) * width / period
            turns = (1 if request_class == "normal"
                     else content_rng.randint(*config.stress_turns))
            context = 0
            for turn_id in range(turns):
                if request_class == "normal":
                    prompt_range = config.normal_prompt_tokens
                elif turn_id == 0:
                    prompt_range = config.stress_initial_prompt_tokens
                else:
                    prompt_range = config.stress_followup_prompt_tokens
                prompt = content_rng.randint(*prompt_range)
                output = content_rng.randint(*config.output_tokens)
                context += prompt
                wait = 0.0
                if turn_id < turns - 1:
                    wait = float(content_rng.randint(*config.tool_duration_ms))
                    if workload == "W1":
                        wait *= stress_level
                    elif workload == "W2":
                        factor = (config.w2_short_wait_factor if turn_id % 2 == 0
                                  else config.w2_long_wait_factor)
                        wait *= factor * stress_level
                rows.append(Turn(
                    SCHEMA_VERSION, session_id, turn_id, f"{session_id}:{turn_id}",
                    request_class, seed, workload, stress_level, arrival,
                    arrival if turn_id == 0 else None,
                    None if turn_id == 0 else f"{session_id}:{turn_id - 1}",
                    prompt, output, context, turns, wait, rate, burst_id,
                ))
                context += output
    rows.sort(key=lambda row: (row.session_arrival_ms, row.session_id, row.turn_id))
    validate(rows)
    return rows


def validate(rows):
    """Check the schema and complete session chains; input order is immaterial."""
    sessions = {}
    request_ids = set()
    run_identity = None
    for row in rows:
        for name in ("schema_version", "turn_id", "seed", "stress_level",
                     "prompt_tokens", "output_tokens", "context_tokens", "expected_turns"):
            value = getattr(row, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a nonnegative integer")
        for name in ("session_id", "request_id"):
            if not isinstance(getattr(row, name), str) or not getattr(row, name):
                raise ValueError(f"{name} must be a nonempty string")
        if row.schema_version != SCHEMA_VERSION:
            raise ValueError("unsupported schema_version")
        if row.workload_id not in WORKLOADS or row.request_class not in ("normal", "stress"):
            raise ValueError("invalid workload_id or request_class")
        if row.stress_level not in STRESS_LEVELS or (row.workload_id == "W0" and row.stress_level != 1):
            raise ValueError("invalid workload/stress_level combination")
        identity = (row.seed, row.workload_id, row.stress_level)
        if run_identity is not None and identity != run_identity:
            raise ValueError("one trace must contain one seed/workload/stress_level")
        run_identity = identity
        for name in ("session_arrival_ms", "tool_duration_ms", "arrival_rate_per_s"):
            if not is_number(getattr(row, name)) or getattr(row, name) < 0:
                raise ValueError(f"{name} must be finite and nonnegative")
        if row.arrival_rate_per_s <= 0 or row.expected_turns < 1 or row.output_tokens < 1:
            raise ValueError("arrival rate, expected_turns and output_tokens must be positive")
        if row.arrival_ms is not None and (not is_number(row.arrival_ms) or row.arrival_ms < 0):
            raise ValueError("arrival_ms must be null or finite and nonnegative")
        if row.depends_on_request_id is not None and not isinstance(row.depends_on_request_id, str):
            raise ValueError("depends_on_request_id must be null or string")
        if row.burst_id is not None and (type(row.burst_id) is not int or row.burst_id < 0):
            raise ValueError("burst_id must be null or nonnegative integer")
        if (row.workload_id == "W2" and row.request_class == "stress") != (row.burst_id is not None):
            raise ValueError("burst_id is required only for W2 stress sessions")
        if row.request_id in request_ids:
            raise ValueError(f"duplicate request_id: {row.request_id}")
        request_ids.add(row.request_id)
        sessions.setdefault(row.session_id, []).append(row)
    for session_id, turns in sessions.items():
        turns.sort(key=lambda row: row.turn_id)
        first = turns[0]
        if len(turns) != first.expected_turns:
            raise ValueError(f"{session_id}: missing or extra turns")
        if first.request_class == "normal" and len(turns) != 1:
            raise ValueError("normal requests must have exactly one turn")
        if first.request_class == "stress" and len(turns) < 2:
            raise ValueError("stress sessions must have at least two turns")
        context = 0
        for index, row in enumerate(turns):
            for name in ("request_class", "expected_turns", "session_arrival_ms",
                         "arrival_rate_per_s", "burst_id"):
                if getattr(row, name) != getattr(first, name):
                    raise ValueError(f"{session_id}: inconsistent {name}")
            if row.turn_id != index:
                raise ValueError(f"{session_id}: turn_id must be contiguous from 0")
            expected_dependency = None if index == 0 else turns[index - 1].request_id
            if row.depends_on_request_id != expected_dependency:
                raise ValueError(f"{session_id}: invalid turn dependency")
            if row.arrival_ms != (row.session_arrival_ms if index == 0 else None):
                raise ValueError(f"{session_id}: only the first turn has an external arrival")
            context += row.prompt_tokens
            if row.context_tokens != context:
                raise ValueError(f"{session_id}: inconsistent context_tokens")
            context += row.output_tokens
            if index == len(turns) - 1 and row.tool_duration_ms != 0:
                raise ValueError(f"{session_id}: final turn must have zero tool wait")
            if index < len(turns) - 1 and row.tool_duration_ms <= 0:
                raise ValueError(f"{session_id}: intermediate turn must have positive tool wait")
    return {"sessions": len(sessions), "turns": len(rows),
            "normal_requests": sum(r.request_class == "normal" for r in rows),
            "stress_sessions": sum(ts[0].request_class == "stress" for ts in sessions.values()),
            "tool_calls": sum(r.tool_duration_ms > 0 for r in rows)}


def read_trace(path):
    rows = []
    expected = {f.name for f in fields(Turn)}
    with Path(path).open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, 1):
            try:
                data = json.loads(line)
                if not isinstance(data, dict) or set(data) != expected:
                    raise ValueError("trace fields must exactly match schema v1")
                rows.append(Turn(**data))
            except (TypeError, ValueError) as error:
                raise ValueError(f"line {line_number}: {error}") from error
    validate(rows)
    return rows


def write_trace(path, rows, config, workload, stress_level, seed):
    summary = validate(rows)
    content = "".join(json.dumps(asdict(row), ensure_ascii=False, allow_nan=False) + "\n"
                      for row in rows).encode("utf-8")
    metadata = {
        "schema_version": SCHEMA_VERSION,
        "generator_version": "0.1.0",
        "workload_id": workload,
        "stress_level": stress_level,
        "seed": seed,
        "config": asdict(config),
        "summary": summary,
        "sha256": hashlib.sha256(content).hexdigest(),
        "time_semantics": "External arrivals stop at duration_ms; simulator drains remaining turns.",
    }
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    path.with_suffix(".meta.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return summary
