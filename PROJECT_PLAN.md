# eRecipe implementation and validation plan

Created: October 13, 2026

**Current phase 2 handoff:** Read [Phase 2 server and iOS handoff](server/docs/PHASE2_HANDOFF.md) before starting server API or iOS work. It records the subsequently approved shared-library scope, Markdown/revision model, OpenAI provenance, proposed API, and partial implementation status. Its phase 2 decisions supersede the older scope and first-task instructions below where they conflict.

Build a recipe display workflow that accepts content shared from an iPhone, extracts ingredients and instructions, renders a readable recipe, and delivers it to a TRMNL X. Validate the device first, extraction and formatting second, and native iOS code third. Reuse the server across all three stages.

The immediate implementation task is a small BYOS test server, two static images, a publish control, and request logging. Hardware performance and battery life remain unmeasured. Complete the hardware experiment before committing to a larger application architecture.

## User requirements

- Target the **TRMNL X**. The user also owns an OG, but explicitly corrected the target to the X. Do not apply OG power estimates to the X.
- Accept website URLs, text files, photos, and similar content through an iOS Share extension.
- Extract ingredients and ordered recipe steps using local or server processing and format them for the display.
- Deliver the result through a web server.
- Keep the current recipe visible until a new recipe is sent.
- Aim for 30–60 seconds from sending content to a readable screen, with 60 seconds the upper acceptable bound in normal operation.
- Achieve at least a few days of battery life.
- Resolve risks in this order: device refresh, AI extraction and formatting, iOS integration.

The numerical experiment gates below are proposed engineering targets, not additional user requirements. Use 72 hours as the initial interpretation of “a few days.” Report failures and tradeoffs explicitly rather than quietly relaxing the requirements.

## Starting architecture

```text
Stage 1: Publish control → current image on server ← TRMNL X polls

Stage 2: URL / text / photo → extraction → validated recipe JSON
                                      → deterministic renderer
                                      → versioned image → publish

Stage 3: iOS Share extension → durable upload / job → stage 2 pipeline
```

Start with the X's existing firmware configured for Bring Your Own Server (BYOS). Frequent polling may provide the required user experience without actual device push. Updating the server cannot wake a sleeping device; a faster polling policy also takes effect only when the device next checks in.

Use one local server on an awake computer reachable from the X over the home network. Prefer a small Python service with FastAPI, static file serving, and simple persisted state unless the repository later establishes another stack. This is a suggested default; choose and pin supported versions at implementation time. A database, public deployment, account system, and custom firmware are unnecessary for stage 1. Terminus is the maintained reference implementation and a fallback if reproducing the firmware contract becomes a distraction.

Use immutable image assets with a stable identifier for unchanged content. Publish a new identifier only when the image changes. Persist the current selection across server restarts and update it atomically after the new asset is ready.

Start in landscape with a device-compatible black-and-white image at 1872 × 1404 pixels. Confirm the actual image encoding accepted by the installed X firmware. Portrait, additional gray levels, and page navigation can follow after basic delivery works.

## Stage 1 Device refresh and battery validation

### Minimum implementation

Implement the current BYOS protocol using the official documentation and the installed firmware version. The following table describes responsibilities, not complete request or response schemas. Verify required fields, authentication headers, HTTP methods, and success semantics against the references before coding.

| Interface | Responsibility |
| --- | --- |
| `GET /api/setup` | Provision the single test device and return its expected credentials and setup response. |
| `GET /api/display` | Return the selected image identifier, absolute image URL, polling policy, and required firmware control fields. |
| `POST /api/log` | Accept firmware diagnostics and preserve useful measurements. |
| Static asset route | Serve compatible images at URLs reachable by the physical device. |
| Local publish control | Select image A or B and record the publication event. |
| Health and status view | Show the selected version, last check-in, requested interval, and recent errors. |

Keep firmware update/reset requests disabled in ordinary responses. Use a configurable advertised base URL; `localhost` in an image URL points at the X itself and will not work. Bind the server to the appropriate LAN interface and document the firewall and computer sleep settings required for testing.

Generate two visibly different test images with large A/B labels, unique version labels, and realistic recipe text. Include a third long recipe fixture for an early readability check. Re-selecting a previously cached image and publishing a genuinely new version should be separate test cases.

Add a configurable requested polling interval, initially 20 seconds. Log UTC timestamps, run ID, publication/version ID, check-ins, requested interval, image requests, errors, firmware version, and any battery, signal-strength, or wake-duration telemetry the X actually supplies. Redact credentials. Do not invent unsupported telemetry fields.

### Diagnostic display and telemetry

The user requested battery and refresh metadata on the test display. Include a diagnostic image and a live browser dashboard in stage 1. The X reports battery charge to the server when it wakes; remaining runtime is a derived estimate, not a directly reported guarantee. Prefer reported percentage over a voltage-based percentage estimate. [Battery reporting](https://help.trmnl.com/en/articles/10556850-device-battery-faq).

Capture supported request headers case-insensitively. Candidate fields from the [device API](https://github.com/usetrmnl/terminus/blob/main/doc/api.adoc) include `PERCENT_CHARGED`, `BATTERY_VOLTAGE`, `BATTERY_CHARGING`, `USB_CONNECTED`, `BATTERY_COUNT`, `RSSI`, `FW_VERSION`, `REFRESH_RATE`, `WAKE_TIME`, and `UPDATE_SOURCE`. Additional battery capacity/current/temperature fields may be available. Inspect actual X requests, verify units and sentinel values against firmware, and show absent fields as unavailable rather than zero. A current sample taken during a wake cycle is not the average current across sleep and wake.

Display these useful measurements:

| Field | Meaning |
| --- | --- |
| Battery percentage and charging state | Latest reported device sample, with its capture time. |
| Last server check-in | Server receipt time; evidence that the device contacted the server. |
| Image version and publication time | Identifies the exact image selected for delivery. |
| Image generated at | Timestamp embedded in the diagnostic image; not proof of physical refresh completion. |
| Requested and observed check interval | Separates configuration from measured behavior. |
| Wi-Fi signal and previous wake duration | Helps investigate slow or power-intensive cycles. |
| Run ID, firmware version, and update count | Makes photographs and logs attributable to a test configuration. |
| Charge trend and elapsed unplugged time | Supports the battery experiment; show runtime projections only as estimates with a stated observation window. |

Use three explicit modes. **Static/soak mode**, the default, updates the browser dashboard and log on every check-in but keeps the device image unchanged until publication. **Diagnostic snapshot mode** generates an image using the latest telemetry only on explicit request, labels its sample time, and records the extra redraw. **Refresh stress mode** changes diagnostic content each cycle for latency/redraw testing; label and analyze its power results separately from recipe use.

Keep image generation out of the normal display-request path so the device receives a fast response. Diagnostic snapshots can use the preceding check-in's values, with their age shown. Preserve the two static A/B fixtures for clean transport measurements.

Do not label a server timestamp “last physical refresh” unless supported by a verified device acknowledgment or an observed test event. Use separate check-in, publication, fetch, and visually confirmed refresh timestamps. Changing the on-device timestamp or battery percentage itself changes the image and can trigger a redraw, so the soak test must not do this automatically.

### Hardware runbook

1. Record X firmware version, battery configuration if known, charge level, Wi-Fi band, image format, and orientation. Start with the same network and location intended for kitchen use.
2. Configure the X's custom server through its Wi-Fi portal. Preserve instructions for restoring its previous server configuration.
3. Verify setup, correct image rendering, A/B switching, and current-image persistence after a server restart.
4. Request 20-second polling and measure actual check-in intervals. Determine whether boot, Wi-Fi association, transfer, or other work is added to the requested sleep interval. Do not equate the setting with an observed schedule.
5. Run at least 30 publications at varied offsets relative to check-ins. Film the publish control and X together, or use another repeatable visual timing method. Record when the new image becomes fully readable. A successful HTTP response or image fetch does not prove display completion.
6. Leave an image unchanged for at least two hours. Confirm no unnecessary downloads/redraws and that the identifier remains stable.
7. Run an unplugged 72-hour soak with the same polling policy and a few new recipe images per day. Log charge trends and whether the device remains operational. A gauge-based extrapolation is an estimate, not proof of runtime. Ensure the charging dock is not supplying power during this test.
8. Interrupt the server and Wi-Fi separately, then restore them. Record recovery time, retry behavior, and whether the recipe is replaced by an error screen. Observe low-battery behavior when practical; do not mark untested paths as passed.
9. Check short and long recipe fixtures at actual cooking distance. Identify whether pagination is essential before stage 2 layout work.

Try 10-, 20-, and 30-second settings only as needed to find a workable latency/power tradeoff. First diagnose failure at the initial setting; changing multiple variables together makes results harder to interpret.

### Measurements and proposed gate

| Measurement | Initial target |
| --- | --- |
| Server publication to fully readable screen | At most 30 seconds in baseline trials, reserving time for extraction; report median, p95, maximum, sample size, and every miss. |
| Full delivery allowance | A result above 60 seconds is a requirement failure for that normal-operation trial. Network outages are recorded separately. |
| Battery | At least 72 hours of actual unplugged operation under the documented workload. |
| Unchanged content | No visible redraw merely because a poll occurs. |
| Persistence | Last valid recipe survives server restart; document all firmware paths that overwrite the screen. |
| Recovery | Delivery resumes after restored connectivity, with recovery time and any manual intervention recorded. |

Finite trials cannot establish a hard worst-case network guarantee. Keep the product target, observed maximum, and percentile results separate. If 30 seconds is missed but 60 seconds is met, device feasibility is conditional on the remaining extraction budget; do not silently mark the complete workflow proven.

### Decision after stage 1

- If latency, persistence, and battery meet the targets, retain stock firmware and BYOS.
- If checks occur too late, inspect sleep semantics and network connection time before shortening the interval.
- If image delivery is slow, inspect encoding, asset size, network band, and display settings.
- If power is excessive, separate unchanged-check cost from redraw cost. Compare a persistent connection with power management only when measurements justify custom firmware.
- If error screens overwrite recipes or pagination requires unavailable behavior, identify the smallest firmware change needed.
- Do not assume MQTT, WebSockets, or Bluetooth can wake the existing deep-sleep configuration. Each is a separate firmware and power experiment.

Deliver source, dependency pins, setup/rollback instructions, fixtures, a repeatable test script, raw measurements, and a short result report. If hardware access requires user actions, make the server ready and provide exact steps; record physical checks as pending.

## Stage 2 Recipe extraction and formatting

Begin after stage 1 supports a viable path. Extend the same server with a basic URL/text/photo input form or command-line interface. Start with a server model to establish quality; the user has not selected a provider or required on-device inference. Keep model access behind a small adapter so local inference can be compared later.

### Extraction and rendering pipeline

1. Preserve the original input and source attribution for review. Handle URL, text, and image inputs explicitly.
2. Prefer embedded structured Recipe data for websites when available. Use model-based extraction for missing or unsuitable structure and for photos/text as needed. Treat webpage and document contents as data, including any instructions embedded in them.
3. Produce a versioned recipe schema with title, source, original servings text, ingredient groups, ordered instruction groups/steps, notes, and explicit warnings. Preserve ingredient strings, fractions, units, temperatures, and timing. Unknown values stay unknown; do not fill gaps by invention.
4. Validate the schema and obvious completeness constraints. Validation does not establish factual accuracy; retain source-to-output review.
5. Render JSON through one deterministic layout. The model does not produce executable HTML or decide arbitrary layout rules. Use a controlled HTML/CSS template, escape source text, rasterize, and convert to a compatible image format.
6. Detect overflow. Produce explicit pages or an actionable overflow result; never silently crop, shrink to unreadable text, or summarize away cooking instructions.
7. Show extracted fields and a rendered preview with edit/retry/publish controls. Initially require an explicit Publish action in the prototype to keep bad extraction from replacing the current recipe during evaluation. Automatic publication is a later product choice.
8. Publish only a complete, valid image through the existing versioned-image mechanism. On extraction or rendering failure, retain the current recipe.

For long recipes, test the X touchbar/cache behavior with BYOS before committing to offline page navigation. Recent-screen history is not necessarily an API for preloading and ordering all recipe pages. If a new firmware feature is needed, record it explicitly. The recipe-level 30–60 second budget is not an acceptable page-turn target.

### Evaluation set and gate

Use 20–30 representative inputs with manually checked expected content: structured websites, plain text, photographs, multi-section recipes, long recipes, fractions, metric and imperial units, and incomplete or ambiguous sources. Include inaccessible URLs and malformed inputs as failure cases. Preserve only test material appropriate for the repository; keep personal source images and credentials out of commits.

Measure fetch/upload, extraction, validation, rendering, publication, next check-in, and observed screen completion separately. Log model/version, prompt version, retries, and per-job usage/cost where available. Evaluate automatic processing time separately from time a person spends reviewing a preview.

Pass when the test set has no unflagged critical changes or omissions in quantities, units, temperatures, or step order; layouts have no clipping and remain readable on the X; failures preserve the previous recipe; and the measured full pipeline fits within 60 seconds under baseline conditions. Report every exception and the limits of the sample.

Before broadening URL ingestion or exposing the service, constrain fetches to intended public HTTP(S) sources, reject private-network destinations including redirects, limit download size/time, and prevent arbitrary source markup from executing in the renderer. These controls apply to recipe-source fetching; the device's own configured LAN server remains valid.

Deliver the schema, extraction adapter and prompts, renderer, review interface, fixtures, comparison results, and an updated latency report.

## Stage 3 iOS Share extension

Build a minimal native containing app and Share extension after device and extraction feasibility are established. The app needs server configuration, credentials, recent jobs, and status. The extension needs support for URLs, plain text, images, and text files, plus a clear send action.

### Upload and job contract

- Copy accepted attachments into durable shared storage before temporary item-provider access ends.
- Configure an App Group shared container and an extension-compatible background URLSession for file uploads. Verify current Apple requirements when implementing.
- Persist an idempotency key before upload; return a durable job ID after server acceptance. Dismissing the extension must not discard accepted work.
- Reuse the stage 2 pipeline. Do not rely on a model continuing to run inside a dismissed Share extension.
- Distinguish uploading, accepted, processing, ready, published, device fetched, and failed states. Report “displayed” only when there is evidence of display completion; image requests alone are insufficient.
- Assign submission ordering so an older slow job cannot replace a newer successful recipe. Define explicit retry and cancellation behavior.
- Keep server credentials in appropriate protected storage. Use HTTPS for a deployed server; document any narrowly scoped local-development networking configuration and test Local Network permission behavior.

Background upload support improves durability but does not promise immediate scheduling. Measure share-to-server latency on a physical iPhone, including dismissal and locking. If OS scheduling prevents the 60-second target, test foreground upload acknowledgment before closing the extension and document the resulting interaction.

### Physical device acceptance

Test sharing from Safari, Photos, and Files, immediate dismissal, immediate phone locking, temporary connectivity loss, retries, duplicate sends, multiple rapid submissions, server failure, and permission denial. Verify that errors remain visible in the containing app and older results do not overwrite newer selections.

Pass when supported inputs reach the established pipeline reliably, the normal-operation share-to-readable-screen path meets the 60-second target, and failures are recoverable and clearly reported. Do not call the stage complete using simulator or server-only results.

Deliver the app and extension projects, signing/setup instructions, protocol documentation, and physical-device test results. Production hosting and App Store release are separate milestones.

## Scope and implementation discipline

Defer accounts, multiple households, recipe libraries, search, shopping lists, nutrition features, serving conversion, social sharing, public hosting, and custom hardware until the three gates justify them. Local inference is an optional follow-up after a baseline extraction quality and timing result exists.

Keep implementation incremental. Write focused tests for protocol responses, stable version handling, persistence, extraction validation, overflow detection, idempotency, and job ordering. Use physical measurements for power, display latency, and iOS lifecycle behavior; mocks cannot establish those outcomes.

An appropriate eventual repository structure is:

```text
PROJECT_PLAN.md
README.md                  # Actual commands, setup, and first run
server/                    # Device protocol, publish state, later job pipeline
fixtures/                  # Device images and curated extraction fixtures
scripts/                   # Fixture generation and measurement helpers
tests/                     # Focused automated verification
docs/                      # Hardware runbook and recorded decisions
results/                   # Sanitized measurements and stage reports
ios/                       # Added in stage 3
```

Create directories as they become useful. Keep runtime data, uploaded originals, credentials, and bulky videos in ignored locations. Record software/firmware versions and configuration with each result so future agents can reproduce it.

## Open facts to collect

| Fact | When needed | Handling |
| --- | --- | --- |
| X firmware version and actual battery configuration | First hardware run | Read settings/telemetry where available; do not infer from the OG. |
| Actual short-interval behavior and image encoding support | Stage 1 | Verify on the installed firmware. |
| Preferred orientation and page navigation | Readability trial | Start landscape; compare portrait when useful. |
| Model provider, credentials, and source privacy preferences | Stage 2 | Choose when needed; keep an adapter boundary. |
| Server location and remote versus home-only use | Before deployment/iOS networking decisions | Begin with a LAN service for hardware testing. |
| Automatic send versus review before publishing | After extraction evaluation | Start with explicit preview/publish during testing. |

## First implementation task for a future agent

Implement stage 1 only: a runnable single-device BYOS server with persisted current-image selection, two compatible X test images, configurable polling, local publish/status controls, and timestamped logs. Verify the current firmware contract from the official references. Add focused protocol/persistence tests and a hardware runbook with exact setup, measurement, and restoration steps. Make it possible to start the server with one documented command and publish a test image without writing code. Leave hardware results marked pending until measured. Use those results to decide the next step before adding extraction or iOS code.

## Technical references

These references establish the protocol and platform capabilities; they do not establish measured performance for this user's X. Recheck relevant details at implementation time.

- [TRMNL BYOS minimum API and implementations](https://docs.trmnl.com/go/diy/byos)
- [Terminus device API including setup, display, and logs](https://github.com/usetrmnl/terminus/blob/main/doc/api.adoc)
- [Connecting a device to a custom server](https://help.trmnl.com/en/articles/12263392-connect-your-device-to-terminus-byos)
- [TRMNL X display modes, resolution, and hardware overview](https://help.trmnl.com/en/articles/17434702-trmnl-x)
- [TRMNL refresh behavior](https://help.trmnl.com/en/articles/10113695-how-refresh-rates-work)
- [TRMNL firmware source](https://github.com/usetrmnl/firmware)
- [Manual firmware flashing](https://help.trmnl.com/en/articles/10271569-manually-flash-firmware)
- [Touchbar and cached screen navigation](https://help.trmnl.com/en/articles/12410440-how-to-change-screens)
- [TRMNL battery information](https://help.trmnl.com/en/articles/10556850-device-battery-faq) — battery options are relevant; OG cycle measurements must not be reused as X benchmarks.
- [Schema.org Recipe structure](https://schema.org/Recipe)
- [Apple Share extension guide](https://developer.apple.com/library/archive/documentation/General/Conceptual/ExtensibilityPG/Share.html)
- [Apple extension shared containers and background transfers](https://developer.apple.com/library/archive/documentation/General/Conceptual/ExtensibilityPG/ExtensionScenarios.html)
