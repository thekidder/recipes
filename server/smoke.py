"""Real HTTP software smoke/latency check in an isolated temporary data directory."""
import argparse
import json
import math
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from datetime import datetime, timezone
import httpx

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    project = Path(__file__).resolve().parent.parent
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        with socket.socket() as upstream:
            upstream.bind(("127.0.0.1", 0))
            serve_port = upstream.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    # Documentation-only address: images are deliberately fetched through loopback.
    advertised = f"http://192.0.2.1:{port}"
    with tempfile.TemporaryDirectory(prefix="erecipe-smoke-") as directory:
        process = subprocess.Popen([sys.executable, "-m", "server", "--base-url", advertised,
            "--host", "127.0.0.1", "--port", str(port), "--serve-port", str(serve_port),
            "--no-worker", "--data-dir", directory],
            cwd=project, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            with httpx.Client(base_url=base, timeout=5) as client:
                for _ in range(100):
                    if process.poll() is not None:
                        raise RuntimeError(process.stderr.read().decode())
                    try:
                        if client.get("/health").status_code == 200:
                            break
                    except httpx.ConnectError:
                        pass
                    time.sleep(.05)
                else:
                    raise RuntimeError("Server did not start")
                setup = client.get("/api/setup", headers={"ID": "02:00:00:00:00:01", "FW-Version": "software-simulator"})
                setup.raise_for_status()
                auth = {"Access-Token": setup.json()["api_key"], "FW-Version": "software-simulator"}
                elapsed, versions = [], set()
                for _ in range(100):
                    start = time.perf_counter()
                    response = client.get("/api/display", headers=auth)
                    elapsed.append((time.perf_counter()-start)*1000)
                    response.raise_for_status()
                    payload = response.json()
                    assert payload["refresh_rate"] == 20 and payload["status"] == 0
                    versions.add(payload["filename"])
                assert len(versions) == 1
                image = client.get("/images/" + payload["filename"])
                image.raise_for_status()
                assert image.headers["content-type"] == "image/png"
                assert image.content.startswith(b"\x89PNG")
                assert client.get("/").status_code == 200
                assert client.post("/api/log", headers=auth, json={"logs": []}).status_code == 204
                changed = client.post("/control/publish", headers={"X-eRecipe-Control": "1"},
                                      json={"fixture": "B", "fresh": True})
                changed.raise_for_status()
                assert client.get("/api/display", headers=auth).json()["filename"] not in versions
                result = {"at": datetime.now(timezone.utc).isoformat(), "kind": "software-only-loopback",
                    "python": sys.version.split()[0], "requests": len(elapsed), "poll_response_ms": {
                        "median": statistics.median(elapsed), "p95_nearest_rank": sorted(elapsed)[math.ceil(.95*len(elapsed))-1],
                        "max": max(elapsed)}, "raw_poll_response_ms": elapsed,
                    "checks": ["setup", "authenticated display", "20-second requested sleep", "stable cached filename",
                               "PNG fetch and Content-Type", "dashboard HTTP", "log 204", "fresh publication"],
                    "hardware_latency": "UNMEASURED", "battery_life": "UNMEASURED"}
                output = json.dumps(result, indent=2)
                if args.output:
                    args.output.write_text(output+"\n")
                print(json.dumps({k:v for k,v in result.items() if k != "raw_poll_response_ms"}, indent=2))
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

if __name__ == "__main__":
    main()
