# Phase 2 server and iOS handoff

Updated: October 13, 2026

This document records the agreed recipe pipeline design and proposed wire contract for parallel server and iOS work. The user approved building the five-step server plan, beginning with plain text and the OpenAI API. After this handoff was prepared, extraction/processing work resumed. The text/image → OpenAI → Markdown → cached PNG domain pipeline is now implemented and testable through a CLI. No phase 2 HTTP endpoint or recipe publication integration has been added by the processing workstream. See [Processing jobs](PROCESSING.md) for current setup and integration interfaces.

Read this document before the older [project plan](../../PROJECT_PLAN.md). This document supersedes its phase 2 scope where they conflict: a shared recipe library and simple multi-user support are now in scope, Markdown is the authoritative editable recipe, and all imported recipes pass through an LLM. The original hardware goals and distinction between publication and physical display remain applicable.

## Product requirements and decisions

- Accept plain text, images, and website URLs. Start implementation and live testing with plain text.
- Preserve the original input before processing. The server is canonical; the eventual iOS application maintains a cache.
- For URLs, preserve both the original URL and a snapshot of the fetched content after JavaScript executes. Images/styles do not need to be archived merely to reproduce the original website appearance.
- Pass every imported recipe through an LLM formatter, including websites with structured Recipe data. Remove ads and irrelevant narrative while preserving cooking information, quantities, units, temperatures, timing, and step order.
- Store an editable standardized Markdown recipe with header metadata, ingredients, method, and optional notes/footer. Render it using a controlled two-column template.
- Preserve complete LLM provenance: provider/model, expanded prompts, exact inputs, parameters, output schema, full returned response, and validated structured output. Each invocation must be independently inspectable.
- Cache generated display images on the server. Saving an edit queues a render; rendering alone must not call the LLM or fetch the source again.
- Keep source refresh, re-extraction, rendering, and sending to the reader as separate actions. Re-extraction and refreshing produce candidates; they do not silently replace user edits.
- Provide one shared library. All active users can view, import, edit, and send all recipes. User identity is for attribution, not private ownership or access tiers.
- The iOS application connects through Tailscale. The TRMNL connects over the local network. Public hosting and Internet exposure are out of scope.
- Target the TRMNL X in landscape at 1872 × 1404, using the existing one-bit PNG path until physical testing establishes otherwise.
- Support multiple pages in the data model now. Automatic pagination and phone-controlled page navigation can follow. Keep one logical Markdown recipe; display pages are derived views.
- Leave unit preferences/conversion for later. Preserve original amounts now; future preferences produce derived render variants rather than modifying the source recipe.
- Use an OpenAI API key for the first implementation. The key belongs only on the server. Tell the user exactly how to configure it when the plain-text path is ready to test. No live API test has occurred.

## Architecture and delivery behavior

```text
Share / browser import
  → durable source capture and processing job
  → optional URL acquisition or image preparation
  → LLM call with saved request and response
  → validated structured recipe
  → immutable Markdown revision
  → deterministic render set with ordered PNG pages
  → review / edit / explicitly send
  → existing TRMNL BYOS polling and image delivery
```

Three separate layers are fundamental: immutable source evidence, immutable editable-document revisions, and regenerable render assets. Parsed Markdown and validated extraction JSON are derived data. Do not introduce a second independently editable JSON recipe that can disagree with Markdown.

An import must not replace the reader's current image. The first successful extraction can become the recipe's first current revision automatically; it still requires an explicit Send to reader action. Subsequent extractions create candidates, preserving the current accepted revision. Accepting a candidate, saving an edit, and publishing an image are separate state changes.

Editing a recipe that is currently displayed does not automatically change the reader. A publication identifies an exact render set and page. On any processing failure, retain both the saved source and the last selected display image.

The existing product goal is 30–60 seconds from sharing to a readable screen under normal conditions, with 60 seconds as the upper acceptable bound. Measure upload, queueing, capture, LLM processing, rendering, device polling, and display separately. Human review time is separate. Background iOS scheduling and TRMNL sleep mean this is not yet a proven guarantee.

## Five implementation increments

1. **Storage and revision model:** SQLite, content-addressed assets, source captures, shared identities/tokens, revisions, durable jobs/attempts, idempotency, conflict checks, and restart recovery.
2. **Markdown and rendering:** parser and validation, fixed two-column template, editing, previews, overflow detection, and cached render sets.
3. **Text and image extraction:** OpenAI adapter, per-call provenance, structured-output validation, image normalization, extraction warnings, and source/result review. Complete the text-only vertical slice first.
4. **URL capture:** JavaScript execution, original and post-JavaScript snapshots, structured Recipe data, capture failures, and source-refresh comparisons.
5. **Shared library and delivery:** recipe list/detail, history, candidate acceptance, concurrent-edit behavior, explicit publication, and a documented client API.

The first live milestone is: paste text → save original → OpenAI extraction → edit Markdown → preview PNG → explicitly send to TRMNL. This milestone is part of the broader five-step scope, not a replacement for image and URL support.

## Actual repository status at handoff

The workspace is `/Users/kidder/Documents/Projects/erecipe`. No Git repository metadata was present at its root during inspection; do not assume a branch or worktree already exists.

### Existing stage 1 implementation

| File | Responsibility |
| --- | --- |
| `server/app.py` | FastAPI BYOS endpoints, paired-device credentials, telemetry, control routes, persisted image selection |
| `server/__main__.py` | CLI launcher, one-process lock, Uvicorn configuration |
| `server/render.py` | Pillow-based A/B/long recipe fixtures and diagnostic images |
| `server/dashboard.html` | Local transport/battery dashboard |
| `server/tests/test_server.py` | Existing stage 1 software tests |
| `server/docs/PROTOCOL.md` | Firmware contract evidence |
| `server/docs/HARDWARE_RUNBOOK.md` | Physical validation procedures |
| `server/results/STAGE1.md` | Earlier software results and hardware gates marked pending |

Firmware routes are `GET /api/setup`, `GET /api/display`, `POST /api/log`, and `GET /images/{filename}`. Preserve their firmware contract. Existing dashboard/control routes are loopback-only and require `X-eRecipe-Control: 1` for POSTs. They are not the authentication design for remote iOS clients.

Stage 1 currently persists state in `server/data/state.json`, logs in `events.jsonl`, and images in `images/`. Its `Store.publish()` changes the selected immutable image. `Store.asset()` uses 24 hexadecimal characters of the image hash for the existing firmware filename. The new library uses full SHA-256 hashes; keep these namespaces distinct and adapt between them.

The old stage 1 report records 21 passing tests; those tests have not been rerun against a completed phase 2 implementation because none exists. Physical display, battery, and full latency gates remain unverified in the recorded report.

### New partial scaffold

The initial scaffold consisted of:

- `server/recipes/__init__.py`
- `server/recipes/schema.sql`
- `server/recipes/db.py`

The schema includes users/tokens, assets, recipes/revisions, source captures/assets, jobs/attempts, LLM calls, render sets/pages, a singleton device-state record, and publication history. The Python scaffold includes database initialization, file assets, user/token creation, import records, enqueue/claim/fail/retry/cancel helpers, editing/acceptance, and recipe detail queries.

Processing implementation has since added `config.py`, `document.py`, `renderer.py`, `openai.py`, `processing.py`, and `__main__.py` under `server/recipes/`, plus `server/tests/test_processing.py`. These provide a configurable OpenAI adapter, per-call provenance, validated Markdown, deterministic PNG rendering, text/image import, and a durable extraction/render worker. The CLI is `python -m server.recipes`. Schema version 2 adds `jobs.parent_job_id` for durable stage relationships and upgrades draft version 1 libraries in place.

These modules are still **not wired into `server/app.py` or the stage 1 launcher**. URL capture, the recipe library HTTP/UI, and reader publication integration are not implemented by this workstream. The existing stage 1 application has not been changed to use SQLite; no existing device data was migrated. Consult the actual workspace for changes from other agents.

Behavioral processing tests now cover text-to-PNG results, exact request/response preservation, image order, retries, cancellation, expired leases, transactional stage chaining, revision conflicts, candidate isolation, overflow, migration, and cache regeneration. Provider calls are mocked; no live OpenAI extraction or hardware validation has been performed. See `server/tests/test_processing.py` and rerun the suite after integration changes.

Additional packages were installed into `server/.venv` during initial development:

```text
PyYAML==6.0.3
markdown-it-py==4.0.0
playwright==1.56.0
beautifulsoup4==4.14.2
python-dotenv==1.2.1
```

PyYAML, markdown-it-py, mdurl, and python-dotenv are now pinned in `server/requirements.txt` because the processing code uses them. Playwright/BeautifulSoup and their transitive dependencies remain installed locally for future URL work but are not yet required by the implemented path. A Playwright browser binary has not been installed. The adapter uses the Responses HTTP API through the existing pinned httpx dependency; no OpenAI SDK is needed.

`ProcessingConfig.from_env()` now loads the explicit `server/.env`, with process environment taking precedence. A placeholder `.env.example`, ignore rules, and a blank local `.env` (if absent) have been prepared. Default model: `gpt-4.1-mini`, configurable with `ERECIPE_OPENAI_MODEL`. The user supplies `OPENAI_API_KEY` locally; the key is never sent to iOS or saved in provenance. See [credential setup](PROCESSING.md#configure-openai).

## Storage model

The actual draft DDL is [schema.sql](../recipes/schema.sql). IDs are opaque strings for clients; the scaffold generates UUID hex strings. Timestamps are ISO 8601 UTC strings. Clients must tolerate fractional seconds. Foreign-key enforcement must be enabled on every SQLite connection.

| Table | Purpose and important fields |
| --- | --- |
| `users` | Display name, creation time, disabled time; no recipe ownership permissions |
| `user_tokens` | User reference, hashed bearer token, label, revocation time |
| `assets` | Full SHA-256 ID, relative storage key, MIME type, byte count, creation time |
| `recipes` | Stable ID, current revision pointer, creator, timestamps, optimistic `lock_version`, archive time |
| `source_captures` | Recipe, prior capture, input kind, original/resolved URL, acquisition state and metadata |
| `source_assets` | Capture-to-asset links with role, ordinal, original filename |
| `recipe_revisions` | Parent, capture, generating attempt, origin, exact Markdown/hash, format, derived title, warnings, author |
| `jobs` | Stage, input references, base revision, status, requester, idempotency key/fingerprint, lease, errors |
| `job_attempts` | One execution attempt of a job, sequence, timing, status, error |
| `llm_calls` | One provider invocation within an attempt; full provenance asset references |
| `render_sets` | Exact revision, cache key, renderer version, effective display/preferences, page count |
| `render_pages` | Render set, one-based page number, PNG asset |
| `device_state` | Proposed SQLite storage for the single device's existing state representation |
| `publications` | Exact rendered page or legacy fixture descriptor, actor, reason, time |

The schema deliberately stores one paired device's state rather than implementing a multi-device product. Publications can reference a rendered recipe page or a legacy fixture/diagnostic image. The nullable render reference supports the existing stage 1 controls.

The intended migration makes SQLite authoritative for device selection too: import existing JSON state once, preserve a backup, and stop independently writing two competing current-selection values. This migration has not been implemented. Publication history and selection must commit together. Continue to support the existing fixture/diagnostic flows and force-refresh behavior; the fixture renderer does not yet understand arbitrary recipe images.

### Source capture details

`source_captures.submitted_url` stores the exact original shared URL string, including query and fragment. `resolved_url` stores the browser's final URL after redirects. The URL is a capture field, not a source asset. Do not silently normalize away the submitted value.

Proposed source asset roles:

```text
original_text
original_image
response_html
rendered_dom_html
visible_text
structured_recipe_data
```

Use attachment ordinals to preserve image order. Preserve original bytes before resizing/transcoding; retain the actual processed images used by the model as separate assets too. The role for processed images can be added when the image pipeline is implemented.

A capture begins as pending for a URL and becomes complete or failed once acquisition finishes. Completed evidence is immutable. A failed capture preserves the submitted URL and failure; retry acquisition by creating a new linked capture. Text/images can be complete at durable acceptance because their original bytes are already present.

An HTML snapshot preserves retrieved content, not the complete original visual website. Resources may need to load during capture without being archived. The snapshot records the server's view at retrieval time, which may differ from a personalized or authenticated page on the phone. Image-only instructions, frames, and canvas content need explicit handling or an unsupported-source warning.

### Revisions and concurrency

- Markdown is immutable per revision. Store title as a query projection derived from that Markdown, not an independent title editor.
- User edits inherit their source association. An edit does not run the LLM again.
- Re-extraction from a saved capture and refreshing a URL create candidate revisions. They do not move the current revision pointer when one already exists.
- Saving or accepting uses an expected `lock_version`. Return a conflict when it no longer matches; retain the client's draft for comparison.
- Parent revisions, source references, current-revision pointers, and job inputs must belong to the same recipe. The draft uses composite foreign keys for these relationships.
- A restore can create a new revision from historical Markdown rather than mutating history. Final API behavior for restoring history is not yet fixed.
- Archive recipes initially. Hard deletion and garbage collection need an explicit retention policy before implementation.

### Jobs and LLM provenance

Jobs have stage kinds `capture`, `extract`, and `render`, with states `queued`, `running`, `succeeded`, `failed`, and `cancelled`. Attempts use `running`, `succeeded`, `failed`, and `abandoned`. Stage completion and enqueueing the dependent stage must be atomic to avoid losing work after a crash.

The worker claims work transactionally, uses a five-minute lease renewed every 30 seconds, guards result commits with token/status/expiry checks, and limits a job to five attempts. Retries are explicit, with no hidden provider retry. Expired attempts are retained as abandoned and work becomes reclaimable. Job success, revision creation, and dependent render enqueueing commit together. Do not replace this durable queue with a FastAPI in-memory background task.

Each `llm_calls` record stores:

- Provider, requested/reported model, provider request ID, prompt version, adapter version, status and timing.
- `request_asset_id`: expanded system/developer/user messages, all context actually sent, parameters, tool definitions if any, and structured-output schema.
- `response_asset_id`: complete provider-returned response, including refusal or incomplete-result information where returned.
- `structured_output_asset_id`: parsed and validated recipe JSON before Markdown serialization.
- Usage and errors when available.

Keep a distinct call row for every invocation, including correction passes; do not overwrite a failed call with the successful retry. Large input bytes can be separate immutable assets referenced from the request manifest rather than duplicated base64. Those referenced assets must remain retained. Do not archive API keys, authorization headers, or unrelated process secrets. Provider-returned context cannot include internal model state the provider does not expose.

The provenance path is: recipe revision → attempt → individual calls → exact inputs and responses. A saved request enables inspection and replay attempts; it does not guarantee identical future output.

### Render and asset invariants

- A render set is visible only when all page files exist and the complete set can commit atomically.
- Page numbers start at 1, are contiguous, and agree with `page_count`.
- Cache identity covers revision content, renderer/template/font versions, dimensions/encoding, and the effective preference snapshot.
- Initially `preferences_json` is `{}` and there is one page. Future preferences must not mutate shared source amounts.
- Detect overflow and expose an actionable failure. Never crop steps, drop ingredients, or shrink indefinitely.
- Write and flush immutable files before committing database references. Interrupted writes can leave collectible orphans, but no committed record may point at a partially written file.
- Backups include a consistent SQLite backup plus referenced asset files. Copying a live database file alone is not the backup strategy.

## Markdown contract

This is the agreed direction; exact parser limits and error codes still need implementation. Use `erecipe/v1`, safe YAML front matter, and a deliberately restricted Markdown grammar. Reject unsupported or unsafe constructs with useful line/field errors. Do not execute source HTML, scripts, or arbitrary user CSS.

```markdown
---
format: erecipe/v1
title: Lemon chickpea salad
servings: "4"
prep_time: "15 minutes"
layout: ingredients-method
footer: "Serve chilled or at room temperature."
---

## Ingredients

### Salad
- 2 cans chickpeas, drained
- 1 cucumber, diced

### Dressing
- 3 tbsp olive oil
- 2 tbsp lemon juice
- 1/2 tsp salt

## Method

1. Rinse and drain the chickpeas.
2. Dice the cucumber and combine with the chickpeas.
3. Whisk the dressing ingredients together.
4. Toss and serve.

## Notes

- Reserve some dressing if preparing ahead.
```

The template maps title/metadata to a header, Ingredients to the left column, Method to the right, and Notes/footer to controlled areas. Support ingredient and instruction groups without losing order. Exact column widths and font sizes are renderer decisions and require actual-size preview/device review.

The LLM should produce constrained structured JSON, then server code serializes canonical Markdown. Preserve fractions, unknown quantities, ranges, optional ingredients, and original servings wording. Unknown values stay unknown. Keep useful recipe information from a site's preamble while stripping unrelated narrative. Flag ambiguities and multiple plausible recipes instead of confidently merging unrelated dishes.

Volume-to-mass conversion is not generic unit arithmetic; it requires ingredient-specific assumptions. Do not implement preferences by asking the LLM to silently rewrite the canonical recipe. Future derived quantity metadata must be invalidated when Markdown changes.

## Proposed server API contract for review

**Everything in this section is a proposed seed contract, not an implemented interface.** The server API agent should finalize schemas, examples, status/error semantics, and limits in a machine-readable OpenAPI contract before iOS treats the wire format as stable. Keep the user-approved behavior above; endpoint naming and envelope details below can be refined through coordination.

Use `/api/v1` for application endpoints to avoid colliding with firmware `/api/setup`, `/api/display`, and `/api/log`. Do not expose SQL rows with `*_json` strings directly. Decode structured fields into native JSON objects/arrays and omit internal storage paths, token hashes, leases, and credentials.

### Access and transport

- Native clients send `Authorization: Bearer <user-token>` over the configured application base URL.
- Tokens are per client/user, locally provisioned, revocable, and equally privileged for shared recipes.
- Tailscale provides the remote network boundary. Decide the actual HTTPS/Tailscale hostname configuration before physical iOS testing; do not assume an arbitrary LAN HTTP URL will work without platform networking configuration.
- Keep the reader's advertised LAN image origin separate from the iOS API base URL. API responses should use relative application asset paths or URLs derived from the application origin, not the device's LAN-only origin.
- The server OpenAI key is never sent to or stored by iOS. It is not interchangeable with an application bearer token or the firmware device credential.
- A loopback browser convenience session can be added, but must retain the existing Host/Origin protections. Do not weaken those checks merely to make iOS work.

### Endpoint outline

| Method and path | Purpose |
| --- | --- |
| `GET /api/v1/status` | API version, current user, worker/configuration readiness and supported import capabilities; no secrets |
| `POST /api/v1/imports` | Persist an original input and enqueue processing; requires `Idempotency-Key` |
| `GET /api/v1/recipes` | Shared library, cursor pagination, current revision and processing summary |
| `GET /api/v1/recipes/{id}` | Current document, source captures, candidate summaries, render availability, processing state |
| `GET /api/v1/recipes/{id}/revisions` | Revision history, with pagination if needed |
| `GET /api/v1/revisions/{id}` | Exact Markdown, parent, warnings, source and render links |
| `POST /api/v1/recipes/{id}/revisions` | Save edited Markdown with `expected_lock_version`; queue render |
| `POST /api/v1/recipes/{id}/accept` | Accept a candidate revision with conflict protection |
| `POST /api/v1/recipes/{id}/extract` | Re-extract an existing capture into a candidate; idempotent request |
| `POST /api/v1/recipes/{id}/refresh` | Create a new URL capture and candidate; idempotent request |
| `POST /api/v1/revisions/{id}/render` | Ensure a render for that exact revision/profile; idempotent request |
| `GET /api/v1/jobs/{id}` | Stage, status, errors, result references and downstream-job references |
| `POST /api/v1/jobs/{id}/retry` | Retry an eligible failed processing stage; retain attempt history |
| `POST /api/v1/jobs/{id}/cancel` | Cancel queued/in-progress work without deleting the source |
| `GET /api/v1/render-sets/{id}` | Exact revision/profile and ordered page assets |
| `GET /api/v1/assets/{id}` | Authenticated immutable asset retrieval; MIME/length/ETag |
| `POST /api/v1/publications` | Explicitly select an exact render set/page for the one reader |
| `GET /api/v1/device` | Selected publication and available check-in/fetch evidence |

Provide a source/provenance inspection interface for review, including capture metadata and LLM-call request/response downloads. The API agent should settle its routes and pagination. Archived raw HTML must be an attachment or sanitized inert view, not executable same-origin HTML.

`GET /recipes` should have a bounded page size and opaque cursor. Include archived/tombstone changes or use complete paginated reconciliation so an iOS cache can learn that a recipe was archived. `updated_at` alone is not a safe unique pagination cursor when timestamps collide; use a stable tie-breaker. Do not promise a complete incremental sync protocol without defining it.

### Initial import payloads

Proposed text request:

```http
POST /api/v1/imports
Authorization: Bearer <application-token>
Idempotency-Key: <stable-client-generated-UUID>
Content-Type: application/json

{
  "kind": "text",
  "text": "Lemon chickpea salad\nIngredients..."
}
```

Proposed URL body:

```json
{"kind": "url", "url": "https://example.com/recipe?ref=share#ingredients"}
```

For images, use file uploads rather than requiring iOS to hold large base64 payloads in memory. A proposed multipart form has a JSON `metadata` part with `kind: images` and an ordered attachment manifest, plus file parts referenced by that manifest. Finalize the precise part names, MIME allowlist, per-file/total size limits, count limit, and error behavior before implementing both sides. Preserve attachment order independently of multipart-parser quirks.

Suggested initial limits for API review, not established requirements: 100,000 text characters, six images, and a bounded total upload size. Set server limits explicitly and return them in capabilities where useful. Image normalization, pixel-count limits, supported HEIC behavior, and rejected/unsupported formats must be defined. Do not claim HEIC support until a decoder or client conversion path is implemented.

Successful durable acceptance returns HTTP 202 with stable references, for example:

```json
{
  "recipe_id": "opaque-recipe-id",
  "source_capture_id": "opaque-capture-id",
  "job_id": "opaque-initial-job-id",
  "status": "accepted"
}
```

The response means the original is durably stored and work is queued. It does not mean extraction succeeded. A repeated key with the same logical request returns the original references; the same key with different input returns HTTP 409. Fingerprint logical fields and file bytes, not a multipart boundary or volatile headers.

Jobs represent stages, not the entire import lifecycle. A succeeded capture job may be followed by extraction and rendering. Clients must follow downstream references or poll the recipe processing summary; they must not treat the first succeeded job as a ready recipe. The API agent must define this distinction in response models and fixtures.

### Editing and publication payloads

Proposed edit body:

```json
{
  "expected_lock_version": 3,
  "markdown": "---\nformat: erecipe/v1\ntitle: ...\n---\n..."
}
```

Return the new revision ID, new lock version, and render-job ID. A validation failure must preserve the user's unsaved draft client-side. On HTTP 409, return enough information to reload/compare the current revision; never silently retry a stale save as if it were current.

Candidate acceptance supplies `revision_id` and `expected_lock_version`. Render requests target exact revisions. Later rendering completion must not move the current revision pointer backward.

Proposed publication body:

```json
{"render_set_id": "opaque-render-set-id", "page_number": 1}
```

The publication response confirms server selection only. The last committed explicit send wins. It must not send whichever render happens to finish last. Consider idempotency for publication retries too so uncertain network responses do not create misleading duplicate sends/history. The backend must verify that the page exists in a complete render set before committing selection.

Expose `selected`, `device_fetched`, and any truly verified display evidence separately. The current firmware path provides image fetch/check-in evidence, not proof that the screen became readable. Do not label a fetched image as displayed.

### Error and cache conventions to finalize

Use a consistent structured error envelope, for example:

```json
{
  "error": {
    "code": "revision_conflict",
    "message": "Recipe changed. Reload and compare your edits before saving.",
    "retryable": false,
    "details": {"current_revision_id": "...", "lock_version": 4}
  }
}
```

Distinguish unsupported input, missing OpenAI configuration, capture failure, provider refusal, provider/transient failure, invalid extraction, Markdown validation, overflow, revision conflict, and unavailable rendering. Do not return secrets or raw provider authorization details in errors.

Use stable IDs/revision IDs and content hashes for cache validation. Immutable application assets can use private cache semantics and ETags; the existing firmware image path has separate public immutable caching behavior. Finalize conditional GET behavior with the contract agent. The server remains authoritative; cache eviction must not delete a server recipe.

## iOS app and Share extension work

The user now wants separate agents to begin the API contract and iOS work. iOS work can proceed against an explicitly versioned mock contract while the backend is completed. Do not claim successful device integration from mocks or simulator testing.

### Containing app

- Configure an application server URL and application user token. Store the token in Keychain, not in recipe documents or ordinary preferences.
- Display the shared historical recipe list, pending/failed imports, current recipe details, warnings, preview pages, and revision/source information needed for review.
- Provide a Markdown editor with a preserved local draft, save/version conflict handling, and rendered preview.
- Allow explicit Send to reader. Keep “saved,” “ready,” “selected,” and “fetched” distinct.
- Support candidate comparison/acceptance without replacing edits in the background.
- Cache recipe metadata, revisions, and previews by stable IDs; retain an outbox for uploads that have not been durably accepted.
- Make archive/cache reconciliation explicit. Offline editing can keep a local draft; do not silently implement last-write-wins when reconnecting.
- Keep render pages as an ordered collection even while the server returns only one. Full page navigation is deferred.

### Share extension

- Accept plain text first, then URLs and images. Establish precedence when an item provider offers both a URL and text representations; do not assume all shared text is a URL.
- Copy accepted temporary attachments into durable App Group storage before access ends.
- Persist a submission ID/idempotency key and upload body before starting upload.
- Reuse server extraction. Do not call OpenAI from the extension or rely on an LLM continuing inside a dismissed extension.
- Use an extension-compatible background URLSession with file-backed uploads and the shared container where appropriate. Verify current Apple requirements at implementation time.
- A server 202 means accepted; missing/uncertain acknowledgment should leave the submission retryable with the same key. Do not discard local originals before durable acknowledgment.
- Surface errors in the containing app after extension dismissal. Background scheduling is not an immediate-delivery guarantee.
- Use real-device tests for Safari/Photos/Files, immediate dismissal, screen lock, offline/online transitions, duplicate retries, Tailscale reachability, permission denial, and multiple rapid shares.

Decide the minimum iOS version, app/bundle identifiers, signing team, and actual network origin with the user when needed. None has been selected in this conversation. App Store release is outside the current task.

## Agent ownership and coordination

Suggested work boundaries, subject to the user's assignments:

| Workstream | Owns | Coordinates with |
| --- | --- | --- |
| Server processing | `server/recipes/db.py`, schema/migrations, worker, OpenAI adapter, capture, Markdown, renderer, storage integration | API agent on domain operations, states, and errors |
| Server API contract | OpenAPI document, request/response examples, DTOs, routes/adapters if assigned, auth/error/idempotency/sync semantics | Processing agent on implemented operations; iOS agent on upload and polling needs |
| iOS | New `ios/` project, app, Share extension, cache/outbox, mock client, networking and UI | API agent on versioned wire contract and sample fixtures |

The contract agent should own a canonical `server/docs/openapi.yaml` (or equivalent agreed path) and examples once created; this prose is a seed, not a second competing contract. Separate Swift models from SQLite row shapes. Agree on interfaces before multiple agents edit the same schema or server integration file.

There is no authorization in this handoff to remove existing runtime data, publish externally, change device firmware, or auto-send recipes. Existing data may contain credentials and private source material; do not copy it into fixtures or documentation. Do not expose secrets in logs or tool outputs.

## OpenAI integration and configuration plan

Use the Responses API with a strict structured-output schema for the recipe. Text and image inputs should share a provider adapter. Keep the provider boundary small for later alternatives, but implement OpenAI first.

The official documentation reviewed supports Responses structured outputs and image inputs. The implemented configurable default is `gpt-4.1-mini`; the user did not require a particular model. Validate output quality on representative recipes rather than treating schema adherence as factual accuracy.

Implemented environment names are `OPENAI_API_KEY` and `ERECIPE_OPENAI_MODEL`; configuration, ignore rules, and a placeholder example are in place. See [Processing jobs](PROCESSING.md) for tested setup commands. Keep real values out of the browser and provenance. The user will provide credentials locally; do not search unrelated files for keys.

Do not make live billable calls merely to test the HTTP adapter when a mocked provider response suffices. A later live end-to-end test should be identified as such and use the configured key. Store complete request/response evidence without authentication headers. Prefer stateless requests with provider storage disabled where supported, while retaining the application's own provenance records.

## URL acquisition and network boundaries

URL acquisition is not implemented. A headless browser should capture initial HTML, post-JavaScript DOM, visible text, and structured Recipe data with a bounded readiness policy. Network inactivity alone is not a reliable definition of completion for dynamic/ad-heavy pages.

Use public HTTP(S) recipe sources only. Reject credentials in URLs and block loopback, private/LAN, Tailscale/CGNAT, link-local, and metadata-service destinations, including redirects and subresources. DNS validation must apply to the actual connection and resist rebinding; a one-time hostname check is insufficient. Browser isolation must not expose application secrets to imported scripts. Resource/time/size limits must cover nested fetches too. The implementation approach has not yet been chosen or validated.

Do not route archived website markup through the trusted recipe renderer. Capture and deterministic recipe rendering have separate trust boundaries. Authenticated/paywalled sites may fail; preserve a useful error and allow text/image import rather than bypassing access controls.

## Validation and completion criteria

Before calling the first text milestone ready:

1. Original text, job, generated revision, exact LLM request/response, validated JSON, and PNG are durable and inspectable.
2. Missing credentials produce a clear configuration error while preserving the original and allowing retry.
3. Markdown edits create history, queue a render, and do not call the LLM or auto-publish.
4. Preview and explicit Send reach the existing firmware image path without breaking the original control tests.
5. Idempotent import retries return the same submission; conflicting key reuse is rejected.
6. Concurrent edits conflict; slow jobs and failed renders cannot replace newer accepted work or the selected reader image.
7. Restart/lease recovery preserves accepted jobs and records abandoned attempts. Cancellation and stage chaining do not strand active captures or silently lose dependent jobs.
8. Sources/revisions remain intact after failures, overflow is explicit, and pages contain all accepted content.
9. API errors, authentication boundaries, source HTML handling, and input size limits are tested.
10. The user receives accurate setup instructions for OpenAI credentials and a reproducible text example.

For the broader pipeline, add representative images, dynamic URL fixtures, failed redirects/private-network destinations, multi-section and long recipes, fractions/units, malformed/refused LLM outputs, source-refresh candidates, and source-vs-output checks. Measure extraction quality on a manually reviewed 20–30 recipe set; do not treat mocked output as proof of LLM accuracy. Test the actual generated image at readable scale and on the X before claiming hardware readability or timing.

Useful existing commands from the project root:

```sh
server/.venv/bin/python -m pytest server/tests -q
server/.venv/bin/python -m server --base-url http://YOUR-LAN-IP:8000
```

The second command currently starts the stage 1 application only. Do not start a second server against the user's active data directory while another instance owns it. Use temporary isolated data directories for tests and database experiments.

## Reference links

- [Original project plan](../../PROJECT_PLAN.md)
- [Server setup and existing controls](../README.md)
- [Draft database schema](../recipes/schema.sql)
- [Draft database helper](../recipes/db.py)
- [Recorded stage 1 results](../results/STAGE1.md)
- [OpenAI structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs)
- [OpenAI image inputs](https://developers.openai.com/api/docs/guides/images-vision)
- [OpenAI Responses create API](https://developers.openai.com/api/reference/resources/responses/methods/create)
- [Playwright navigation and dynamic loading](https://playwright.dev/python/docs/navigations)
- [Schema.org Recipe](https://schema.org/Recipe)
- [SQLite foreign-key behavior](https://www.sqlite.org/foreignkeys.html)
- [SQLite backup API](https://www.sqlite.org/backup.html)
- [Apple Share extensions](https://developer.apple.com/library/archive/documentation/General/Conceptual/ExtensibilityPG/Share.html)
- [Apple shared containers and background transfers](https://developer.apple.com/library/archive/documentation/General/Conceptual/ExtensibilityPG/ExtensionScenarios.html)

Recheck platform requirements against current official documentation during implementation. These references explain capabilities; they are not evidence that this repository has implemented or tested them.
