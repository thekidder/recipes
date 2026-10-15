# Application server and dashboard

The server now runs the versioned application API, user/phone enrollment dashboard, durable processing worker, and the existing TRMNL firmware endpoints. The authoritative interface is [openapi.yaml](openapi.yaml); [the contract guide](contract/REVIEW.md) explains its behavior.

Text, still JPEG/PNG images, and public HTTP(S) recipe URL imports are enabled. Status advertises `url` and `source_refresh: true`. Rendering still produces one page with explicit overflow errors; automatic pagination is future processing work.

## URL shares

Share a public recipe page from Safari to eRecipe, or use **Paste recipe link** in the app. A standalone HTTP(S) link shared as text is also recognized. The existing automatic flow follows `capture → extract → render → publication`; it never sends before rendering finishes.

`POST /api/v1/imports` accepts `{"kind":"url","url":"https://example.com/recipe#method"}`. The exact submitted URL, including query and fragment, is retained. Capture runs asynchronously outside API transactions. The server saves the HTML response as `response_html`, the resolved URL and redirect history, visible text, and any Schema.org Recipe JSON-LD. Source HTML downloads require authentication and are served as attachments. A recovered capture reuses an already saved response rather than silently fetching changed evidence.

Only public HTTP(S) destinations on ports 80/443 are supported. Credentials, local/private/link-local/Tailscale addresses, mixed public/private DNS answers, and unsafe redirects are rejected. Connections pin a validated numeric IP while TLS verifies the original hostname; no cookies, authorization, proxy environment, or subresource requests are used. Limits: five redirects, 4 MiB HTML, 400,000 bytes of extraction evidence, and bounded network timeouts. Evidence that exceeds the limit fails instead of being silently truncated.

Pages must expose recipe text or recipe metadata in their HTML. There is no JavaScript browser, login, paywall bypass, or PDF importer. Download requests ask for uncompressed content; sites that insist on compression return an explicit unsupported-source error. Share text or photos when a site cannot be captured.

After a capture failure, iOS offers **Fetch link again**. This calls `POST /api/v1/recipes/{id}/refresh` with `source_capture_id`, creates a linked capture, and preserves the previous attempt. Refresh keys survive ambiguous responses. Refreshing an already extracted recipe creates a candidate and does not replace the current revision automatically; refreshing text/images returns `refresh_unsupported`.

Verified with isolated tests for DNS/redirect safety, transport bounds, HTML/JSON-LD parsing, cancellation, lease recovery, import/refresh replay, and the full publication chain. A read-only fetch of a public recipe page succeeded; automated model extraction remains mocked.

## Image shares

iOS accepts up to six still JPEG/PNG/HEIC recipe photos from Photos, Files, or the in-app photo picker. It converts HEIC locally to JPEG and preserves page order. Image shares follow the client's automatic delivery flow after extraction and rendering.

`POST /api/v1/imports` accepts multipart `metadata` plus ordered image parts using the existing contract. The server advertises `images` in `/api/v1/status` and enforces 10 MiB per file, 30 MiB total, six images, and 20 million pixels per image. MIME types must match decoded still JPEG/PNG content. Both container verification and pixel decoding happen before recipe/job creation, including for truncated JPEGs. The server itself does not accept HEIC bytes.

Uploaded bytes and manifest order are retained. The worker applies EXIF orientation, reduces model input to 2048 pixels on its longest side, and composites transparency onto white. The image-aware extraction prompt handles continued pages and overlapping screenshots, reports unreadable recipe details, and rejects dish-only photos without a usable written recipe. Extraction calls are mocked in automated checks; real-photo recognition quality and physical device delivery still require device testing.

## Run one server process

Install dependencies from the project root:

```sh
server/.venv/bin/python -m pip install -r server/requirements.txt
```

Configure the server-only OpenAI or OpenRouter key and model using [PROCESSING.md](PROCESSING.md). Select the provider with `ERECIPE_LLM_PROVIDER` (defaults to `openai`). The existing `server/.env` loader works for the integrated launcher. Do not replace an existing credential file. The worker makes provider calls when it processes accepted imports; API integration tests use a mocked provider.

Configure Tailscale Serve to proxy to the application's dedicated loopback port:

```sh
tailscale serve --bg http://127.0.0.1:8001
tailscale serve status
```

Use the HTTPS origin printed by Serve, without a path, in the command below. Substitute the LAN address reachable by your reader separately:

```sh
server/.venv/bin/python -m server \
  --base-url http://YOUR-LAN-IP:8000 \
  --app-origin https://YOUR-SERVER.YOUR-TAILNET.ts.net
```

`ERECIPE_APP_ORIGIN` in the environment or `server/.env` is an alternative to `--app-origin`. An explicit CLI value takes precedence. Both listeners run in one process against one library:

| Listener | Default bind | Purpose |
| --- | --- | --- |
| Reader | `0.0.0.0:8000` | Existing firmware setup, polling, logging, image delivery, and direct-loopback legacy controls |
| Application | `127.0.0.1:8001` | Tailscale Serve upstream for dashboard, enrollment, `/control/v1`, and `/api/v1` |

The application listener checks the configured public Host and browser Origin. Opening port 8001 directly in a browser with a localhost address will not satisfy that configuration. Use the Serve HTTPS URL. Direct LAN requests cannot access the application/control-v1 routes by supplying proxy headers. Native iOS API requests need a bearer token; dashboard access needs only permission to reach the Serve service under your tailnet rules. No dashboard login or administrator identity list is required.

The reader's advertised LAN image origin never becomes the QR/iOS origin. No Funnel or Internet-facing setup is used. See [Tailscale Serve documentation](https://tailscale.com/docs/features/tailscale-serve) for Serve configuration and tailnet access rules.

`--serve-port` changes the loopback upstream; update Serve's target accordingly. `--no-worker` leaves accepted jobs durable and queued while disabling processing in this process. The application origin can be omitted when running only the LAN experiment; the application listener then returns a configuration error. Only one launcher may own a data directory. Stop the previous server before starting this one; the process lock rejects concurrent launchers. Do not run independent LAN and application server processes against the same directory.

## Connect an iPhone

1. Open the Serve HTTPS dashboard and select **Users and phone enrollment** (or open `/admin`).
2. Create or select a user. All users share the same recipe library.
3. Enter a device label and select **Create enrollment QR**.
4. Scan the QR on a phone with the eRecipe app installed and Tailscale connected.
5. The app exchanges the single-use code and saves its device token in Keychain. The dashboard hides the QR after redemption.

QR codes expire after ten minutes. Creating another pending QR for the same user revokes the old pending code. The code contains the application URL and a temporary enrollment secret, never the permanent bearer token. The server stores only secret hashes. The dashboard has no token-reveal function.

If the phone loses the successful exchange response, its credential still appears in the dashboard: revoke that credential, then create a new QR. Device credentials can be revoked individually. Disabling a user revokes all their credentials and pending enrollments while retaining recipe history. No physical phone enrollment was exercised during server implementation; the iOS app still needs its matching URL handler and Keychain implementation.

## Processing and publication

Import acceptance is `202`, not a successful extraction or delivery. Follow stage jobs and their downstream references, or poll recipe detail. Missing configuration for the selected provider preserves the input and produces a retryable configuration error. Restart the server after setting the key and model, then explicitly retry the failed stage.

Saving Markdown creates a revision and queues rendering without another provider call. Re-extraction creates a candidate when a current revision exists. Accepting a candidate changes the current revision with an optimistic version check, and retains the old revision. Rendering and accepting never send a recipe automatically.

Explicit publication selects an exact complete render set/page. Its history and device selection commit together in SQLite. Firmware images retain the existing 24-character hash filenames, while library assets use full SHA-256 IDs. A retry of an earlier send returns its receipt without selecting that earlier image again. A recipe page supports the existing force-refresh button by changing PNG metadata while preserving its pixels; this does not re-extract or rerender Markdown.

Archive keeps sources/history and the current reader selection, while cancelling active jobs and preventing their results from committing. All active users can inspect retained source and provider-call evidence through authenticated downloads. Archived HTML is an attachment with inert browser headers. Image fetch evidence remains separate from proof of physical display.

## Persistence and migration

The selected `--data-dir` contains `recipes.sqlite3`, content-addressed `blobs/`, firmware `images/`, and `events.jsonl`. API metadata, enrollment hashes, retry receipts, acceptance records, publications, and device state all live in the same database. The worker continues using its existing domain tables. API migrations add separate tables and a current-revision acceptance trigger; processing source files are unchanged.

On the first integrated start, an existing `state.json` is preserved as `state.json.pre-sqlite.bak` and imported into SQLite. Subsequent starts read SQLite, and never independently update the old JSON selection. Device credentials, selection, telemetry, and available legacy publication history are retained. No migration was run against the user's active data as part of implementation; tests used temporary directories. Do not restart the old JSON-only implementation against a migrated directory.

Pre-API libraries did not record every historical candidate acceptance. Migration records the current revisions, edits, and known prior revision bases/parents as accepted. An old acceptance with no surviving pointer/base evidence cannot be reconstructed with certainty. All subsequent current-pointer changes are tracked atomically.

A backup needs a consistent SQLite backup plus its referenced blobs and firmware images. The launcher does not perform backup retention or garbage collection. Do not copy a live SQLite file alone as a backup. No source deletion, credential changes, or live device sends were performed by the implementation tests.

## Verification

```sh
server/.venv/bin/python -m pytest server/tests -q
server/.venv/bin/python -m server.smoke
server/.venv/bin/python -m server.api_smoke
```

Both smoke commands allocate temporary loopback ports and data, and disable the worker. The API smoke checks actual HTTP startup, enrollment, route separation, process locking, and restart recovery. It simulates the configured Serve Host on loopback; it does not establish real Tailscale HTTPS connectivity. Unit/integration tests cover the provider-to-PNG-to-publication path with mocked OpenAI responses.

The enrollment dashboard was checked in headless Chrome at desktop and phone widths using intercepted requests into an isolated test server. User creation, QR display, redemption polling, and revocation completed without JavaScript errors. Actual Tailscale deployment, iOS integration, and physical reader display remain deployment checks. See [API implementation verification](../results/API_IMPLEMENTATION.md) for recorded results.

## Calendar fallback and center-tap dismissal

Configure `ERECIPE_TRMNL_API_KEY` in `server/.env` using the hosted device's key from Devices → Edit → Developer perks. This requires TRMNL Developer access; keep its cloud playlist limited to the calendar/weather composition. This is a separate credential from the local reader key. Never put it in phone configuration. Leave it empty to disable fetching and dismissal. Restart the integrated launcher after changing it.

The background calendar worker calls the official `https://trmnl.com/api/display` endpoint, which advances the cloud playlist. It downloads the screen over HTTPS without sending the cloud key to the image host, validates landscape X dimensions (1872 × 1404), and converts it to the one-bit PNG format already used locally. Wrong-size, malformed, or non-PNG images fail without replacing the previous valid cache. The server never forwards cloud reset, OTA, or special-function instructions to the physical device.

Cloud requests run independently of reader check-ins, at the returned refresh interval (minimum five minutes). Errors retry with backoff and keep the last good image. The worker also runs with `--no-worker`, which disables recipe processing only. Calendar timestamps describe successful downloads, not the underlying Google/weather data freshness. Cached calendar updates during a recipe do not change the selection. The first successful sync selects the calendar if an ordinary fixture was selected; recipes, diagnostic screens, and active tap trials are preserved. Use **Return to calendar** to leave the tap trial after setup.

The dashboard's **Calendar and recipes** panel shows configuration, selection, cache time, and errors. **Preview cached calendar** does not create device delivery evidence. **Return to calendar** selects the cached image for the next check-in. These routes have the same local/Serve protections as the other dashboard controls:

- `GET /control/calendar/preview`
- `POST /control/calendar/show` (409 until configured and a valid cache exists)

Phone publication still uses `POST /api/v1/publications`. With calendar integration configured, each recipe selection gets a fresh PNG metadata nonce without changing pixels, so even an identical recipe requires a new firmware image fetch. The selection and delivery gate are persisted with publication history. Before that fetch, a physical wake retrieves the recipe rather than dismissing it. After the fetch and a three-second settling window, a physical wake (`button`, `EXT0`, or `EXT1`) returns the cached calendar. Timer, reboot, and unknown wakes hold. `Image-Cached` is not a gesture filter. Without a calendar cache, the recipe stays selected.

The existing polling setting stays unchanged in both modes; use short local intervals for timely phone sends. Calendar sync does not apply the cloud's much longer sleep interval to the reader. The API's `Device.calendar` object reports content mode and cache/dismissal status. Calendar publications use the existing `legacy` publication shape with label “Calendar and weather,” preserving compatibility with current phones. Replaying an earlier send receipt after dismissal does not reselect that recipe.

Stock firmware supplies no unique input event ID or display acknowledgement. First-fetch gating and a settling window mitigate immediate retries, but delayed retries or failed side-cache navigation can still resemble a new center tap. Side navigation can show old cloud screens or dismissed recipes without changing server selection. Page navigation remains on the phone; no firmware pagination is added.

Removing the cloud key disables new syncs and tap dismissal but retains the selected image and cache. Restarting preserves the recipe/calendar selection. Existing recipes first migrated into enabled calendar mode receive a fresh delivery identifier so cached old images cannot bypass the delivery gate.

### Battery readings on firmware 1.8.17

For the X, the dashboard uses valid `Gauge-SOC` as the primary battery percentage and shows `Percent-Charged` as a secondary firmware estimate. If the gauge is missing or invalid, it uses the firmware value with an explicit fallback label. It does not calculate either from voltage. In official firmware commit `9acd348`, the X build enables `BYPASS_BQ27427_SOC`: the main charge field is a voltage estimate while `Gauge-SOC` retains the fuel gauge's measurement. During the October 14 trial these disagreed substantially (about 41% versus 1%, later 60% versus 27%). Neither field alone establishes actual runtime. New charge trends use the selected primary source. When the source changes, the trend samples reset; old unlabelled samples are treated as firmware estimates and are never combined with gauge readings. The cloud's last displayed battery sample may also be stale while the device is pointed at our server.

Sources: [Display API](https://docs.trmnl.com/go/private-api/screens), [X battery implementation](https://github.com/usetrmnl/trmnl-firmware/blob/9acd348270a493ca5f1c627ac91a9469543267ae/src/battery/bq27427_battery.cpp), [X build configuration](https://github.com/usetrmnl/trmnl-firmware/blob/9acd348270a493ca5f1c627ac91a9469543267ae/platformio.ini).
