"""Run from the project root: python3 -m kvbench --help."""

import argparse
import json
from pathlib import Path

from .policies import REGISTRY
from .simulate import collect, simulate, write_results
from .system import load_policy_params, load_system_config
from .workload import generate, load_config, read_trace, validate, write_trace


def main():
    parser = argparse.ArgumentParser(description="KV-Bench synthetic trace and policy simulation tools")
    commands = parser.add_subparsers(dest="command", required=True)
    make = commands.add_parser("generate", help="Generate a trace and its metadata")
    make.add_argument("--config", required=True, type=Path)
    make.add_argument("--workload", choices=("W0", "W1", "W2"), default="W0")
    make.add_argument("--stress-level", type=int, choices=(1, 2, 4, 8), default=1)
    make.add_argument("--seed", type=int, default=0)
    make.add_argument("--output", required=True, type=Path)
    check = commands.add_parser("validate", help="Validate a complete JSONL trace")
    check.add_argument("trace", type=Path)
    replay = commands.add_parser("simulate", help="Replay a trace under one KV cache policy")
    replay.add_argument("--trace", required=True, type=Path)
    replay.add_argument("--policy", required=True, choices=sorted(REGISTRY))
    replay.add_argument("--system", type=Path, default=Path("configs/system_1080ti_llama3b.json"))
    replay.add_argument("--policies", type=Path, default=Path("configs/policies.json"),
                        help="Policy parameters JSON; a missing file means defaults")
    replay.add_argument("--warmup-ms", type=float, default=None,
                        help="Override warmup_ms from the trace metadata")
    replay.add_argument("--no-events", action="store_true", help="Do not write events.csv")
    replay.add_argument("--output", required=True, type=Path, help="Directory for this run")
    merge = commands.add_parser("collect", help="Merge results/*/summary.json into one CSV")
    merge.add_argument("results", type=Path)
    merge.add_argument("--output", type=Path, default=None, help="CSV path, default <results>/summary.csv")
    args = parser.parse_args()
    try:
        if args.command == "generate":
            if args.output.suffix != ".jsonl":
                raise ValueError("output must have a .jsonl extension")
            metadata_path = args.output.with_suffix(".meta.json")
            if args.output.exists() or metadata_path.exists():
                raise ValueError("output already exists; choose a new path")
            config = load_config(args.config)
            rows = generate(config, args.workload, args.stress_level, args.seed)
            summary = write_trace(args.output, rows, config, args.workload, args.stress_level, args.seed)
            print(f"Trace: {args.output}\nMetadata: {metadata_path}")
        elif args.command == "validate":
            summary = validate(read_trace(args.trace))
        elif args.command == "simulate":
            if args.output.exists():
                raise ValueError("output already exists; choose a new path")
            system = load_system_config(args.system)
            params = load_policy_params(args.policies, args.policy) if args.policies.exists() else {}
            sim, summary = simulate(args.trace, args.policy, system, params, args.warmup_ms, not args.no_events)
            write_results(sim, summary, args.output)
            print(f"Results: {args.output}")
        else:
            print(f"Summary: {collect(args.results, args.output)}")
            return
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    except (OSError, ValueError, NotImplementedError) as error:
        parser.exit(2, f"error: {error}\n")


if __name__ == "__main__":
    main()
