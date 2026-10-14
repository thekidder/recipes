# Stage 1 result report

Date: 2026-10-13

Implementation: local single-device FastAPI BYOS server, requested 20-second baseline, stable immutable 1872 × 1404 one-bit PNG fixtures, persisted pairing/current image, local dashboard, explicit snapshots, background stress rendering, UTC JSONL measurements, randomized trial helper, and per-run report generator.

Software verification: 21 focused protocol/persistence/measurement tests passed on Python 3.12.14. One dependency deprecation warning: Starlette currently supports but deprecates the httpx test-client adapter. Fixtures and dashboard were visually inspected. A real Uvicorn loopback smoke run passed setup, authenticated display, stable filename, image delivery, fresh publication, log acceptance, and dashboard HTTP. Across 100 display requests, median was 1.17 ms, p95 2.35 ms, and maximum 4.67 ms. These are server-only timings; no physical device was involved. Raw timings and check list: [software-smoke.json](software-smoke.json).

Physical device evidence: none collected. The installed X firmware and battery configuration are not known.

| Gate | Result |
|---|---|
| X image decoding and A/B switching | Pending hardware |
| Actual check-in cadence at requested 20 seconds | Pending hardware |
| 30 publication trials: median, p95, maximum | Pending hardware |
| <=60 second complete workflow | Pending; stage 2/3 not implemented |
| Two hours unchanged without unnecessary redraw | Pending hardware |
| 72 hours unplugged and operational | Pending hardware |
| Restart persistence on physical display | Pending hardware |
| Wi-Fi/server recovery and low battery | Pending hardware |
| Short/long recipe readability | Pending hardware |

Raw hardware measurements will be written to server/data/events.jsonl after connection. No fabricated telemetry or battery result is included. Retain stock firmware/BYOS as the experiment; defer architectural conclusions until physical gates are measured.
