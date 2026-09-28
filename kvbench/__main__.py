"""Run from code/: python3 -m kvbench --help."""

import argparse
import json
from pathlib import Path

from .workload import generate, load_config, read_trace, validate, write_trace


def main():
    parser = argparse.ArgumentParser(description="KV-Bench synthetic trace tools")
    commands = parser.add_subparsers(dest="command", required=True)
    make = commands.add_parser("generate", help="Generate a trace and its metadata")
    make.add_argument("--config", required=True, type=Path)
    make.add_argument("--workload", choices=("W0", "W1", "W2"), default="W0")
    make.add_argument("--stress-level", type=int, choices=(1, 2, 4, 8), default=1)
    make.add_argument("--seed", type=int, default=0)
    make.add_argument("--output", required=True, type=Path)
    check = commands.add_parser("validate", help="Validate a complete JSONL trace")
    check.add_argument("trace", type=Path)
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
        else:
            summary = validate(read_trace(args.trace))
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    except (OSError, ValueError) as error:
        parser.exit(2, f"error: {error}\n")


if __name__ == "__main__":
    main()
