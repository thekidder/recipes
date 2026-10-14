# Server API implementation verification

October 13, 2026 · Contract `1.0.0` · Python 3.12.14

The integrated server implements the application and dashboard contract using the existing processing library. Text and still JPEG/PNG imports are enabled. URL acquisition and source refresh are deliberately unavailable and advertised as such. See [setup and migration](../docs/SERVER_API.md) and [the authoritative contract](../docs/openapi.yaml).

## Delivered

- One launcher owns the reader LAN listener, loopback Tailscale Serve upstream, shared SQLite/device store, and processing worker. A process lock prevents duplicate launchers on one data directory.
- Dashboard user creation, single-use ten-minute QR enrollment, per-device credentials, credential revocation, and user disabling. Dashboard authorization relies on tailnet reachability, with Host/Origin protections and no Tailscale identity login requirement.
- Application bearer authentication, bounded text/image imports, durable idempotency receipts, optimistic edit/accept/archive checks, immutable revisions, pending candidates, stage jobs, retry/cancel, and authenticated source/render/provenance assets.
- Exact render/page publication with atomic selection/history, legacy firmware compatibility, one-time JSON device-state migration, and persistent server identity.
- Processing-owned `server/recipes/` files were not changed by this implementation. API-specific transactions and migrations live in `server/api/`.

## Recorded checks

| Check | Result |
| --- | --- |
| `python -m pytest server/tests -q` | **84 passed** in 6.30 seconds, including 19 new API integration tests |
| Contract validator | **35 paths, 37 operations, 62 schemas; 37 exchanges and 8 rejection checks passed** |
| `python -m server.api_smoke` | **Passed** actual two-listener HTTP, QR exchange, Origin checks, route separation, process locking, and restart authentication/idempotency/pairing |
| `python -m server.smoke` | **Passed** setup, authenticated polling, stable image selection, PNG fetch, dashboard, logging, and fresh publication |
| Headless Chrome | **Passed** user creation, QR display, redemption polling, credential revocation, and no JavaScript errors; desktop 1100px and phone 390px layouts visually checked |
| JavaScript syntax / Python compilation / dependency consistency | **Passed** `node --check`, `compileall`, and `pip check` |

The full suite emitted one upstream Starlette warning about its deprecated httpx TestClient integration. There were no test failures.

The API tests use temporary libraries and mocked OpenAI responses. They cover extraction through PNG rendering and explicit publication, enrollment races/replacement/expiry, revoked tokens, per-user retries, concurrent edits, rollback of failed receipt/publication commits, signed pagination, image multipart validation, archive/cancellation fences, corrupted render repair, authenticated conditional asset requests, and migration/restart behavior.

The firmware smoke's 100 loopback polls measured median **1.226 ms**, nearest-rank p95 **1.570 ms**, and maximum **1.764 ms**. These are software-only response measurements, not physical display latency. Raw results are in [api-firmware-smoke.json](api-firmware-smoke.json).

## Verification boundaries

Both real HTTP smoke tests disabled the processing worker. Browser requests were intercepted into an isolated test server; the browser used the real dashboard HTML, JavaScript, and QR SVG. Tests made no live provider calls, changed no active reader selection, and migrated no active user data.

Actual Tailscale HTTPS deployment, the iOS URL handler/Keychain flow, and physical reader display remain integration checks. Automatic render pagination and URL acquisition remain future processing work. Pre-API historical acceptances without surviving pointer/base evidence cannot be reconstructed with certainty; future acceptance changes are recorded atomically.
