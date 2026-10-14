"""Read sanitized JSONL into per-run, per-mode measurements. Never claims hardware success."""
import argparse
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path

def summary(events):
    groups = defaultdict(list)
    for e in events:
        groups[(e["run_id"], e["mode"], e["poll_seconds"])].append(e)
    output = []
    for (run, mode, poll), rows in groups.items():
        pubs = {e["publication_id"]: e for e in rows if e["event"] == "publish"}
        confirmations = {e["confirmed_publication_id"]: e for e in rows if e["event"] == "visual_confirmation"}
        times = sorted(e["latency_seconds"] for e in confirmations.values())
        checks = [e for e in rows if e["event"] == "checkin"]
        intervals = [e["observed_interval_seconds"] for e in checks if e.get("observed_interval_seconds") is not None]
        output.append({"run_id": run, "mode": mode, "poll_seconds": poll, "publications": len(pubs), "observations": len(times),
            "pending_observations": [k for k in pubs if k not in confirmations],
            "latency_seconds": {"median": statistics.median(times) if times else None,
                "p95_nearest_rank": times[math.ceil(.95*len(times))-1] if times else None, "max": max(times) if times else None},
            "misses_over_30s": [e for e in confirmations.values() if e["latency_seconds"] > 30],
            "requirement_failures_over_60s": [e for e in confirmations.values() if e["latency_seconds"] > 60],
            "checkins": len(checks), "observed_interval_median": statistics.median(intervals) if intervals else None,
            "image_requests": sum(e["event"] == "image_request" for e in rows),
            "errors": [e for e in rows if e["event"] == "error"],
            "hardware_battery_gate": "Pending: verify 72 hours unplugged, workload, gaps, and operational status manually"})
    return output

def main():
    p = argparse.ArgumentParser()
    p.add_argument("events", type=Path)
    args = p.parse_args()
    events = []
    for n, line in enumerate(args.events.read_text().splitlines(), 1):
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            raise SystemExit(f"Invalid JSONL at line {n}; preserve original and repair a copy before reporting")
    print(json.dumps(summary(events), indent=2))

if __name__ == "__main__":
    main()
