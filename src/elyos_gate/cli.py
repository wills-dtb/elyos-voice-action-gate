"""Replay JSONL synthetic voice-agent action envelopes through the durable gate."""
import argparse
import json
from dataclasses import asdict
from pathlib import Path
from .core import ActionGate


def main() -> int:
    parser = argparse.ArgumentParser(description="Voice-agent booking action gate (synthetic demo)")
    parser.add_argument("fixture", type=Path, help="JSONL file of structured agent intents")
    parser.add_argument("--db", default=":memory:", help="SQLite persistence path")
    parser.add_argument("--output", type=Path, help="optional JSON report")
    args = parser.parse_args()
    gate = ActionGate(args.db)
    decisions = []
    for line_no, line in enumerate(args.fixture.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            decisions.append(asdict(gate.process(json.loads(line))))
        except (ValueError, json.JSONDecodeError) as exc:
            raise SystemExit(f"Invalid input line {line_no}: {exc}")
    report = {
        "decisions": decisions,
        "bookings": gate.bookings(),
        "audit": gate.audit_events(),
        "metrics": {
            "events": len(decisions),
            "committed": sum(d["status"] == "committed" for d in decisions),
            "handoffs": sum(d["status"] == "handoff" for d in decisions),
            "blocked": sum(d["status"] == "blocked" for d in decisions),
            "latency_budget_breaches": sum(d["latency_budget_breached"] for d in decisions),
        },
    }
    output = json.dumps(report, indent=2)
    if args.output:
        args.output.write_text(output + "\n")
    else:
        print(output)
    gate.close()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
