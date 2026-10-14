"""Repeatable randomized-offset physical trial assistant; does not simulate the X."""
import argparse
import json
import random
import time
from datetime import datetime, timezone
import urllib.request

def call(base, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + "/control/" + path, data=data,
        headers={"Content-Type": "application/json", "X-eRecipe-Control": "1"})
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://localhost:8000")
    p.add_argument("--count", type=int, default=30)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--cached", action="store_true", help="Alternate cached A/B instead of fresh versions")
    args = p.parse_args()
    rng = random.Random(args.seed)
    s = call(args.url, "status")
    print(f'Run {s["run_id"]}; {args.count} trials; seed {args.seed}. Film screen and control together.')
    print("Press Enter ONLY when the published version is fully readable. Enter 'miss' to record no observation.")
    first_b = s["current"]["kind"] == "A"
    for i in range(args.count):
        delay = rng.uniform(0, s["poll_seconds"])
        print(f"Trial {i+1}: waiting {delay:.1f}s before publication...", flush=True)
        time.sleep(delay)
        pub = call(args.url, "publish", {"fixture": "B" if (i + int(first_b)) % 2 else "A", "fresh": not args.cached})
        answer = input(f'Published {pub["label"]} at {pub["at"]}. Fully readable? [Enter / miss] ')
        if answer.strip().lower() == "miss":
            print("Left pending; this is not a passed trial.")
            continue
        stamp = datetime.now(timezone.utc).isoformat()
        result = call(args.url, "confirm", {"publication_id": pub["id"], "observed_at": stamp,
                      "note": f"manual keyboard observation; seed={args.seed}; trial={i+1}"})
        print(f'Observed latency: {result["latency_seconds"]:.3f}s (includes human reaction time).')

if __name__ == "__main__":
    main()
