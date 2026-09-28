"""Read a trace and inspect session dependencies without running a simulator."""

import argparse
from collections import defaultdict
import json

from kvbench.workload import read_trace, validate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace", help="Path to a generated .jsonl trace")
    args = parser.parse_args()
    try:
        rows = read_trace(args.trace)
    except (OSError, ValueError) as error:
        parser.exit(2, f"error: {error}\n")

    # The simulator can use these indexes to find a session's next turn.
    sessions = defaultdict(list)
    for row in rows:
        sessions[row.session_id].append(row)
    for turns in sessions.values():
        turns.sort(key=lambda row: row.turn_id)

    # Register only first turns as external arrivals. Later turns depend on
    # previous decode completion + tool wait, determined by the simulator.
    external_arrivals = sorted(
        (row for row in rows if row.turn_id == 0),
        key=lambda row: (row.arrival_ms, row.session_id),
    )
    print(json.dumps(validate(rows), indent=2))
    print(f"External arrivals to schedule: {len(external_arrivals)}")
    for turns in sessions.values():
        if turns[0].request_class == "stress":
            print(f"Example stress session: {turns[0].session_id}")
            for row in turns:
                print(
                    f"  turn={row.turn_id}, arrival_ms={row.arrival_ms}, "
                    f"depends_on={row.depends_on_request_id}, "
                    f"prompt={row.prompt_tokens}, context={row.context_tokens}, "
                    f"output={row.output_tokens}, tool_wait_ms={row.tool_duration_ms}"
                )
            break
    else:
        print("No stress session in this trace.")


if __name__ == "__main__":
    main()
