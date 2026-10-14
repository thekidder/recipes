# eRecipe server

The server now includes the shared recipe API, QR-based phone enrollment, processing worker, and explicit publication to the reader. Start with [application server and Tailscale Serve setup](docs/SERVER_API.md). The [authoritative API contract](docs/openapi.yaml) covers iOS requests; [processing jobs and OpenAI setup](docs/PROCESSING.md) describes extraction and rendering.

The existing reader experiment remains available: a single TRMNL X polls the LAN server, initially every **20 seconds**. Use the browser to publish A/B fixtures, measure delivery, and run a battery soak. Stock firmware cannot be woken by a server publication. Requested sleep plus wake/network/display time determines the actual interval.

## Start

Requires Python **3.12+** on macOS or Linux. From the project root:

```sh
python3 -m venv server/.venv
server/.venv/bin/python -m pip install -r server/requirements.txt
server/.venv/bin/python -m server --base-url http://192.168.1.20:8000
```

Replace the example IP with this computer's LAN address. On macOS, find it in System Settings → Wi-Fi → Details → TCP/IP (or `ipconfig getifaddr en0` if Wi-Fi uses en0). Dependencies are already installed in this workspace's `server/.venv`.

The **last command starts the server**. Open **http://localhost:8000** on the server computer. Use Publish A / Publish B to switch images. The “fresh version” checkbox changes the visible version label and cache key; unchecked buttons reuse the original fixture. The long fixture is an initial readability test.

**Force new image** below Selected image publishes a new cache identifier. Recipe pages preserve their exact pixels; diagnostic images are rendered again. It preserves the current mode and polling interval and logs an extra image change. The X receives it on its next check-in; the button cannot wake a sleeping device.

Allow incoming TCP 8000 for this Python process in the macOS firewall. Use the same reachable LAN as the X; guest/client isolation can block it. Reserve the server's DHCP address. Keep the computer awake, plugged in, and its lid open for the soak. On macOS, prefix the start command with `caffeinate -i` to inhibit idle system sleep while it runs.

The reader listener binds to `0.0.0.0` by default. `--base-url` is the origin advertised to the device, not a bind address; localhost and wildcard URLs are rejected. Direct legacy controls retain loopback Host/Origin checks. For remote dashboard access and iOS, configure the separate Tailscale Serve listener with `--app-origin` as described in [application setup](docs/SERVER_API.md). No public hosting or port forwarding is needed.

Optional flags: `--host`, `--port`, `--data-dir`, `--device-id AA:BB:CC:DD:EE:FF`, `--poll-seconds 20`, `--app-origin`, `--serve-port 8001`, and `--no-worker`. The polling flag initializes **new state only**; later dashboard changes persist across restarts. One process owns both listeners and the device store; the launcher enforces a process lock. Do not use multiple Uvicorn workers. Tailscale Serve proxies only to the dedicated loopback application listener.

## Connect the X

1. Record the installed firmware, old custom-server value, battery configuration, current charge, Wi-Fi band, and physical location.
2. Hold both ends of the X touch bar to enter pairing mode. Join its TRMNL Wi-Fi network and open the portal.
3. Under **Advanced → Custom Server → Yes**, enter your LAN origin, e.g. `http://192.168.1.20:8000`, **without a trailing slash**.
4. Return to Wi-Fi, select the home SSID, enter its password, and connect.
5. The first valid setup MAC becomes the single paired device (or use `--device-id` to preselect it). The server generates and persists its API key. Check the dashboard for actual firmware and telemetry; verify A and B display correctly.
6. Confirm installed firmware accepts the included **1872 × 1404, one-bit grayscale PNG** files. Actual hardware compatibility is pending.

Setup is idempotent for the paired MAC. Restarts preserve pairing, polling settings, run ID, and the last image. The integrated server stores credentials and device state in `recipes.sqlite3`. Existing `state.json` is backed up and imported once; see [migration details](docs/SERVER_API.md#persistence-and-migration). Preserve the complete data directory and do not share credential-bearing files.

**Restore:** hold both touch-bar ends, reconnect to the TRMNL portal, and restore the previous custom origin. For TRMNL's hosted service, follow the official rollback: Advanced → Soft Reset, then normal Wi-Fi setup with the API Server URL completely blank. This resets device configuration; record settings first. The server never requests firmware reset or OTA.

## Modes and measurements

- **Static / soak (default):** check-ins update logs/dashboard without changing image bytes or filename. Normal polls never render an image.
- **Diagnostic snapshot:** explicit button publishes an image with the preceding sample and its age. It costs an extra redraw. An on-image timestamp is generation time, not screen completion.
- **Refresh stress:** after each display response, a background task prepares and publishes diagnostic content for the next wake. Analyze this workload separately. A very fast overlapping poll can receive the preceding version while rendering finishes.

The dashboard shows reported charge, sample age, charging/USB raw values, requested and observed intervals, image publication and fetch times, firmware, run ID, changes, errors, and charge trend. Missing/invalid charge is unavailable; voltage is never converted into invented percent. “Mark unplugged” is an **operator annotation** after disconnecting power. Reported USB power or charging ends that window. Changing polling/mode ends it too; start a new run for clean comparisons. Charge estimates require at least two hours of samples and a positive drop, use first-to-last charge, and do not prove runtime. No wake-current-to-runtime extrapolation is used.

`WAKE_TIME` stays raw: official firmware source currently sends **milliseconds**, although Terminus documentation describes seconds. Confirm the installed version before interpreting. Other unverified units and sentinels remain raw rather than guessed. Hyphenated and underscored header names are accepted case-insensitively.

Image requests are **fetch attempts**, not physical display acknowledgments. Preview requests use a separate route so browsing the dashboard does not inflate device fetch counts. A user or script fetching `/images/...` still counts as a transport request.

## Repeatable checks

```sh
server/.venv/bin/python -m pytest server/tests -q
server/.venv/bin/python -m server.smoke --output server/results/software-smoke.json
server/.venv/bin/python -m server.trials --count 30 --seed 42
server/.venv/bin/python -m server.report server/data/events.jsonl
```

The smoke script starts its own isolated server and reports **loopback software latency only**; it does not contact or pair your X. Its temporary device state is removed afterward.

The trial helper waits reproducible random offsets, publishes fresh A/B versions, and records Enter when the screen is fully readable. Use `--cached` for the separate cached-reselection experiment. Film the device and publication control together for more precise timing; keyboard measurements include reaction time. A missed observation remains pending, never a pass.

Raw UTC events live in ignored `server/data/events.jsonl`. They include run/mode/publication/version, check-ins, telemetry, settings, image requests, firmware logs, request latency, and manual observations. Known credentials and credential-shaped fields are redacted. Firmware message text is otherwise untrusted diagnostic material; inspect before sharing. Device state and publication history commit in SQLite after immutable assets exist. A disk failure stops the affected operation; state is never silently replaced by a default recipe.

Assets are immutable, content-addressed and retained for reproducibility. Stress runs produce many assets (roughly 50–150 MB/day depending on image complexity and interval); monitor disk usage and archive the complete data directory after each experiment. A crash can leave an orphan asset or truncate the last log line, but should not publish a partially written image. Preserve originals before repairing a log copy.

Fixtures are checked in under `server/fixtures/`. Regenerate with `python -m server.generate_fixtures`; the renderer uses Arial on macOS, DejaVu Sans on Linux, or Pillow's bundled fallback. `ERECIPE_FONT` can select a fixed font for reproducibility across machines. Rendered content detects body overflow.

See [hardware runbook](docs/HARDWARE_RUNBOOK.md), [protocol evidence](docs/PROTOCOL.md), and [stage report](results/STAGE1.md). Hardware gates remain pending.
