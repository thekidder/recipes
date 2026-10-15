# eRecipe server

The server now includes the shared recipe API, QR-based phone enrollment, processing worker, and explicit publication to the reader. Start with [application server and Tailscale Serve setup](docs/SERVER_API.md). The [authoritative API contract](docs/openapi.yaml) covers iOS requests; [processing jobs and OpenAI/OpenRouter setup](docs/PROCESSING.md) describes extraction and rendering.

Calendar fallback and center-tap dismissal are available with `ERECIPE_TRMNL_API_KEY` configured in `server/.env`; see [calendar setup and behavior](docs/SERVER_API.md#calendar-fallback-and-center-tap-dismissal).

A single TRMNL X polls the LAN server, initially every **20 seconds**. The dashboard lists the shared recipe library and lets you send a rendered recipe page to the reader, see the selected screen, and return to the calendar. Stock firmware cannot be woken by a server publication; requested sleep plus wake/network/display time determines the actual interval.

## Start

For automatic startup at macOS login on **port 10000**, including Tailscale HTTPS
on the same port, follow [the login service setup](docs/MACOS_SERVICE.md). It uses
the installer in `server/scripts/install_launch_agent.py` and preserves the existing
credentials and recipe library. The manual development commands below use port 8000.

Requires Python **3.12+** on macOS or Linux. From the project root:

```sh
python3 -m venv server/.venv
server/.venv/bin/python -m pip install -r server/requirements.txt
server/.venv/bin/python -m server --base-url http://192.168.1.20:8000
```

Replace the example IP with this computer's LAN address. On macOS, find it in System Settings → Wi-Fi → Details → TCP/IP (or `ipconfig getifaddr en0` if Wi-Fi uses en0). Dependencies are already installed in this workspace's `server/.venv`.

The **last command starts the server**. Open **http://localhost:8000** on the server computer.

- **Recipes** (`/`): search current, unarchived recipes and send a ready page to the device. Longer recipes have a page selector. Processing recipes stay visible with their status until ready. The current-screen preview shows the selection and whether the device fetched it; fetching does not confirm a physical screen update. **Return to calendar** selects the cached calendar for the next check-in.
- **Settings** (`/settings`): change the polling interval and mode. Normal mode keeps the selected screen. Snapshot mode keeps the existing screen; stress mode replaces it with diagnostics on subsequent check-ins. Sending a recipe or selecting the calendar returns to Normal mode without changing the interval. Phone enrollment is also linked here on the Tailscale Serve dashboard.
- **Logs & errors** (`/logs`): recent errors, device details, raw telemetry, publication history, and the latest 100 logged events. Activity reads are bounded to the last 256 KiB of the log and redact known credentials.

**Nighttime polling** is configured in **Settings** with an enable switch, start/end times, time zone, and interval in seconds. It is off by default, with 22:00–07:00 and 900 seconds prefilled. The time zone defaults to the server's named local zone (UTC if unavailable) and is saved explicitly. Overnight and same-day windows work; start is inclusive, end is exclusive. Nighttime polling must be at least as slow as normal polling, with the same 3,600-second maximum. Settings persist across restarts, recipe sends, and calendar selection.

The server evaluates the schedule on every device check-in. Near a schedule boundary, it requests a shorter sleep so the reader can switch intervals promptly (minimum five seconds; device wake/network time still adds delay). Daylight-saving changes use local wall-clock hours: skipped times take effect when the clock passes them, and repeated hours follow the same schedule on both occurrences. A sleeping device receives a changed schedule on its next check-in; the server cannot wake it. The dashboard shows the currently requested interval, and Logs & errors also shows the last interval sent to the device.

The dashboard uses the existing local/Serve control restrictions. Recipe sends validate the selected render against the recipe's current revision and use the same publication flow as the phone app. No phone bearer token is needed for dashboard controls. The old fixture, tap-trial, battery-run, snapshot-publish, and manual-observation controls are no longer part of the dashboard.

Allow incoming TCP 8000 for this Python process in the macOS firewall. Use the same reachable LAN as the X; guest/client isolation can block it. Reserve the server's DHCP address. Keep the computer awake, plugged in, and its lid open while using the reader. On macOS, prefix the start command with `caffeinate -i` to inhibit idle system sleep while it runs.

The reader listener binds to `0.0.0.0` by default. `--base-url` is the origin advertised to the device, not a bind address; localhost and wildcard URLs are rejected. Direct legacy controls retain loopback Host/Origin checks. For remote dashboard access and iOS, configure the separate Tailscale Serve listener with `--app-origin` as described in [application setup](docs/SERVER_API.md). No public hosting or port forwarding is needed.

Optional flags: `--host`, `--port`, `--data-dir`, `--device-id AA:BB:CC:DD:EE:FF`, `--poll-seconds 20`, `--app-origin`, `--serve-port 8001`, and `--no-worker`. The polling flag initializes **new state only**; later dashboard changes persist across restarts. One process owns both listeners and the device store; the launcher enforces a process lock. Do not use multiple Uvicorn workers. Tailscale Serve proxies only to the dedicated loopback application listener.

## Connect the X

1. Record the installed firmware, old custom-server value, battery configuration, current charge, Wi-Fi band, and physical location.
2. Hold both ends of the X touch bar to enter pairing mode. Join its TRMNL Wi-Fi network and open the portal.
3. Under **Advanced → Custom Server → Yes**, enter your LAN origin, e.g. `http://192.168.1.20:8000`, **without a trailing slash**.
4. Return to Wi-Fi, select the home SSID, enter its password, and connect.
5. The first valid setup MAC becomes the single paired device (or use `--device-id` to preselect it). The server generates and persists its API key. Check **Logs & errors** for firmware and telemetry; send a saved recipe from **Recipes** and verify it displays correctly.
6. Confirm installed firmware accepts the included **1872 × 1404, one-bit grayscale PNG** files. Actual hardware compatibility is pending.

Setup is idempotent for the paired MAC. Restarts preserve pairing, polling settings, run ID, and the last image. The integrated server stores credentials and device state in `recipes.sqlite3`. Existing `state.json` is backed up and imported once; see [migration details](docs/SERVER_API.md#persistence-and-migration). Preserve the complete data directory and do not share credential-bearing files.

**Restore:** hold both touch-bar ends, reconnect to the TRMNL portal, and restore the previous custom origin. For TRMNL's hosted service, follow the official rollback: Advanced → Soft Reset, then normal Wi-Fi setup with the API Server URL completely blank. This resets device configuration; record settings first. The server never requests firmware reset or OTA.

## Device behavior

After a recipe is fetched, a center tap returns to the calendar when calendar integration is configured and its image is available. Side controls browse the device's recent screens. See [calendar setup and behavior](docs/SERVER_API.md#calendar-fallback-and-center-tap-dismissal).

The dashboard battery reading uses valid `Gauge-SOC` first and falls back to the firmware's `Percent-Charged` estimate. Missing values remain unavailable. Telemetry is shown in **Logs & errors**; `WAKE_TIME` remains raw because firmware and documentation have differed on its units.

Preview requests use a separate route and do not count as device image fetches. Image requests are transport evidence, not physical display acknowledgments.

## Repeatable checks

```sh
server/.venv/bin/python -m pytest server/tests -q
server/.venv/bin/python -m server.smoke --output server/results/software-smoke.json
server/.venv/bin/python -m server.trials --count 30 --seed 42
server/.venv/bin/python -m server.report server/data/events.jsonl
```

The smoke script starts its own isolated server and reports **loopback software latency only**; it does not contact or pair your X. Its temporary device state is removed afterward.

Legacy hardware trial helpers remain available from the command line for reproducibility. The trial helper waits reproducible random offsets, publishes fresh A/B versions, and records Enter when the screen is fully readable. Use `--cached` for the separate cached-reselection experiment. Film the device and publication control together for more precise timing; keyboard measurements include reaction time. A missed observation remains pending, never a pass.

Raw UTC events live in ignored `server/data/events.jsonl`. They include run/mode/publication/version, check-ins, telemetry, settings, image requests, firmware logs, request latency, and manual observations. Known credentials and credential-shaped fields are redacted. Firmware message text is otherwise untrusted diagnostic material; inspect before sharing. Device state and publication history commit in SQLite after immutable assets exist. A disk failure stops the affected operation; state is never silently replaced by a default recipe.

Assets are immutable, content-addressed and retained for reproducibility. Stress runs produce many assets (roughly 50–150 MB/day depending on image complexity and interval); monitor disk usage and archive the complete data directory after each experiment. A crash can leave an orphan asset or truncate the last log line, but should not publish a partially written image. Preserve originals before repairing a log copy.

Fixtures are checked in under `server/fixtures/`. Regenerate with `python -m server.generate_fixtures`; the renderer uses Arial on macOS, DejaVu Sans on Linux, or Pillow's bundled fallback. `ERECIPE_FONT` can select a fixed font for reproducibility across machines. Rendered content detects body overflow.

See [hardware runbook](docs/HARDWARE_RUNBOOK.md), [protocol evidence](docs/PROTOCOL.md), and [stage report](results/STAGE1.md). Hardware gates remain pending.
