# Recipe extraction and processing jobs

The processing domain is ready for local testing independently of the application API. It accepts plain text and ordered JPEG/PNG/WebP images, preserves originals, records every provider call, creates Markdown revisions, and generates cached one-bit PNG previews. Extraction supports OpenAI and OpenRouter. The worker does not publish images to the reader. The integrated application API and iOS client support text, image, and public recipe URL sharing.

## Configure OpenAI

From the project root, install the pinned dependencies if needed:

```sh
server/.venv/bin/python -m pip install -r server/requirements.txt
```

A blank `server/.env` was prepared locally if none existed. For another checkout, copy `.env.example` without overwriting an existing credential file:

```sh
cp -n server/.env.example server/.env
chmod 600 server/.env
```

Edit **`server/.env`** locally:

```dotenv
ERECIPE_LLM_PROVIDER=openai
OPENAI_API_KEY=your_actual_openai_api_key
ERECIPE_OPENAI_MODEL=gpt-4.1-mini
```

Keep the key on the server; do not paste it into chat, the iOS application, API examples, or recipe files. `.env` and `.env.*` are ignored, except the placeholder `.env.example`. Existing process environment variables take precedence over the file. A running worker must be restarted after credentials/model changes. `ProcessingConfig` does not expose the key in its repr or readiness response.

The adapter uses the [OpenAI Responses API](https://developers.openai.com/api/reference/resources/responses/methods/create), [strict structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs), and `store: false`. The configurable default is `gpt-4.1-mini`, which supports text and image input. Account/model access and live extraction quality still require a real test. No live OpenAI calls were made during implementation.

## Configure OpenRouter instead

Set these values in the same ignored `server/.env` file:

```dotenv
ERECIPE_LLM_PROVIDER=openrouter
OPENROUTER_API_KEY=your_actual_openrouter_api_key
ERECIPE_OPENROUTER_MODEL=provider/model-id
```

Replace `provider/model-id` with an exact ID from the [OpenRouter model catalog](https://openrouter.ai/models). Select a model/endpoint that supports structured outputs and, for image shares, image input. The model is required explicitly; an empty key or model leaves extraction unconfigured. OpenRouter uses only `OPENROUTER_API_KEY` and `ERECIPE_OPENROUTER_MODEL`; it never borrows the OpenAI key or model. Unknown provider names fail startup. OpenAI remains the default when `ERECIPE_LLM_PROVIDER` is absent or blank.

Restart the server after changes, then check `server/.venv/bin/python -m server.recipes status`. It reports the selected provider/model and whether both settings are present; it does not validate remote account access. Retry an existing failed extraction from the app to reuse its saved source. Each new call records the provider/model used, including after switching providers. There is no automatic fallback between OpenAI and OpenRouter. To switch back, set `ERECIPE_LLM_PROVIDER=openai` and restart; the existing OpenAI settings remain available.

The OpenRouter adapter uses [Chat Completions](https://openrouter.ai/docs/api/reference/overview), preserving ordered text and base64 image evidence. It requests [strict JSON Schema output](https://openrouter.ai/docs/guides/features/structured-outputs) with `provider.require_parameters=true`, then validates the result locally with the same recipe schema. Unsupported models/endpoints fail explicitly; malformed, partial, refused and filtered output never creates a recipe revision. OpenRouter's routing within the selected model follows its provider preferences; `store: false` is specific to our direct OpenAI request and is not a retention guarantee for OpenRouter. Account/provider data settings remain applicable.

The existing iOS imports, job polling, retry, rendering and automatic delivery flow need no client changes. The automated integration tests mock OpenRouter. A live smoke test on October 14, 2026 used the locally configured `meta/muse-wonder` model to extract a small synthetic cucumber salad recipe and render one page in a temporary library. It did not publish to the reader or retry existing user recipes. Image and URL paths are covered by mocked integration tests; live image quality still needs evaluation.

## First plain-text test

Use an isolated test library, leaving the existing hardware experiment data alone:

```sh
server/.venv/bin/python -m server.recipes --data-dir server/data/recipe-test status
server/.venv/bin/python -m server.recipes --data-dir server/data/recipe-test import-text server/fixtures/recipe-text.txt --key first-salad-test
server/.venv/bin/python -m server.recipes --data-dir server/data/recipe-test worker --drain
server/.venv/bin/python -m server.recipes --data-dir server/data/recipe-test list
```

`status` reports whether credentials are configured without showing them. Importing is local and does not call a model provider. **Running the worker with credentials may make billable API calls.** `--drain` processes available extraction/render stages until idle; `--once` processes at most one stage. With neither flag, the worker waits for new jobs until interrupted.

The import response includes `id` (the extraction job ID), `recipe_id`, and `source_capture_id`. A successful extraction queues rendering atomically. Inspect the recipe using the returned ID:

```sh
server/.venv/bin/python -m server.recipes --data-dir server/data/recipe-test recipe RECIPE_ID
server/.venv/bin/python -m server.recipes --data-dir server/data/recipe-test job JOB_ID
```

Recipe detail includes revisions and render sets. The current Markdown is in `revisions`; the preview is in `renders[].pages[]`. Export the chosen render set's first page to a new file:

```sh
server/.venv/bin/python -m server.recipes --data-dir server/data/recipe-test export-preview RENDER_SET_ID /tmp/salad-preview.png
```

The export refuses to overwrite an existing file. Open the resulting PNG normally. The image is 1872 × 1404, one-bit black and white. This command does not send it to TRMNL.

If the extraction failed because credentials were missing, configure them and explicitly retry the original job:

```sh
server/.venv/bin/python -m server.recipes --data-dir server/data/recipe-test retry JOB_ID
server/.venv/bin/python -m server.recipes --data-dir server/data/recipe-test worker --drain
```

Retry preserves attempts and call history. Reusing the import key with the same input returns the original job rather than making another recipe; a different input with the same key conflicts. Reimporting alone does not restart a failed job. Provider calls have no hidden automatic retry. Each explicit retry may make another billable call. A job allows five attempts; after that, request a new extraction/render job.

## Recipe URLs

```sh
server/.venv/bin/python -m server.recipes import-url 'https://example.com/recipe' --key link-import-1
server/.venv/bin/python -m server.recipes worker --drain
```

URL imports return a capture job first. Follow its extraction and rendering children. Acquisition uses no model call; extraction requires the configured provider. Raw HTML and extracted evidence are retained, including HTML that fails parsing. The same public-network restrictions and limits apply to CLI and HTTP imports.

## Editing, re-extraction, and images

Copy a revision's Markdown into a local file, edit it, then save with the recipe's current `lock_version`:

```sh
server/.venv/bin/python -m server.recipes --data-dir server/data/recipe-test edit RECIPE_ID /path/to/recipe.md --expected-version 1
server/.venv/bin/python -m server.recipes --data-dir server/data/recipe-test worker --drain
```

An edit saves a new revision and queues a render without calling OpenAI. Stale versions conflict. Invalid Markdown is rejected before creating a revision. Long recipes use condensed pagination without cropping. Unrenderable values (such as a word wider than a column) retain the revision with an explicit `render_overflow` job error.

Other commands:

```sh
server/.venv/bin/python -m server.recipes --data-dir server/data/recipe-test extract RECIPE_ID CAPTURE_ID --key extract-again-1
server/.venv/bin/python -m server.recipes --data-dir server/data/recipe-test render RECIPE_ID REVISION_ID --key render-again-1
server/.venv/bin/python -m server.recipes --data-dir server/data/recipe-test import-images /path/page1.jpg /path/page2.png --key photo-import-1
server/.venv/bin/python -m server.recipes --data-dir server/data/recipe-test cancel JOB_ID
```

Re-extraction creates a candidate and its preview. It does not replace an existing current revision. Candidate acceptance is available through `Library.accept`; its HTTP/UI integration belongs to the API workstream.

Image limits are 1–6 JPEG/PNG/WebP files, at most 12 MiB each, 24 MiB total, and 24 million pixels each. These are the direct processing/CLI limits; the application API instead enforces 10 MiB/file, 30 MiB total, 20 million pixels, and JPEG/PNG only. Animated/multi-frame images and raw HEIC are rejected by the server. The iOS client converts HEIC to JPEG before upload. Original uploaded bytes and ordering are preserved, and both container integrity and pixel decoding are checked before import. For model input, images are EXIF-oriented, composited onto white, reduced to a maximum 2048-pixel dimension, and encoded as PNG. The exact processed PNG bytes are embedded in the saved request body. Model/OCR quality remains unverified until evaluated on real source images.

## Integration interface for the server API agent

Use `ProcessingService` for import/edit validation and `Library` for transactional domain operations. Do not expose their raw SQL dictionaries directly as the public wire contract. Their JSON-string fields, internal leases, hashes, and storage keys need mapping or omission in API DTOs.

```python
from server.recipes.config import ProcessingConfig
from server.recipes.db import Library
from server.recipes.processing import ProcessingService, Worker

library = Library(data_directory)
service = ProcessingService(library)
config = ProcessingConfig.from_env()  # Explicit server/.env, not an upward search

job = service.submit_text(text, user=user_id, key=idempotency_key)
job = service.submit_images([(filename, original_bytes)], user=user_id, key=idempotency_key)
result = service.save_markdown(recipe_id, markdown,
    user=user_id, expected_lock_version=expected_version)
job = service.reextract(recipe_id, capture_id, user=user_id, key=idempotency_key)
job = service.render(recipe_id, revision_id, user=user_id, key=idempotency_key)

library.accept(recipe_id, candidate_revision_id, expected_version)
library.retry(job_id)
library.cancel(job_id)
detail = service.job_detail(job_id)
recipe = library.detail(recipe_id)
readiness = config.readiness()
```

`submit_*` and job requests return a raw persisted job dictionary, including `id`, `recipe_id`, and `source_capture_id`. `save_markdown` returns `revision_id` and `job_id`; read the recipe to obtain its new `lock_version` when mapping the public response. `accept` uses optimistic locking and returns no payload. Every user ID must exist; `local` is the built-in CLI/operator identity. `Library.create_user(name, label)` returns a user ID and a one-time bearer token, while `Library.authenticate(token)` verifies active identities.

`service.job_detail` adds `attempts` with `llm_calls`, `downstream_job_ids`, `result_revision_ids`, and `result_render_set_ids`. Stage completion is not whole-workflow completion: follow the render child after extraction succeeds. Schema version 2 adds `jobs.parent_job_id`; existing draft version 1 databases are upgraded without deleting data. Use recipe detail to select a particular candidate/current revision and its render, not the most recently completed job regardless of revision.

`RecipeError` extends `Problem` and supplies `code`, `retryable`, `details`, and `as_dict()`. It defaults to status 422 for input validation. When processing asynchronously, errors are persisted in `jobs.error_json`; the job GET itself can still return HTTP 200. `Problem.status` supplies conflict/not-found statuses for domain actions. Unexpected worker exceptions become a generic `processing_error`; logs record only exception class and job ID.

Provider calls emit `openai_call_started` / `openai_call_finished` or `openrouter_call_started` / `openrouter_call_finished` log lines with a JSON diagnostic object. Both server launchers enable these messages; failed or abandoned calls log at WARNING, successful calls at INFO. With the background launch used locally, these appear in `server/data/server.log`. Find the latest calls with:

```sh
rg '(openai|openrouter)_call_(started|finished)' server/data/server.log | tail -20
server/.venv/bin/python -m server.recipes job JOB_ID
```

Each completion includes the recipe, job, attempt and call IDs; provider and requested/reported model; prompt/adapter versions; elapsed milliseconds; request/response byte counts; token usage when supplied; HTTP status and provider request/response IDs. OpenRouter also records the generation ID, finish/native finish reason, upstream provider when present, and normalized usage counters. Provider failures retain the bounded `incomplete_reason`, provider error code/type, or exception class. OpenRouter errors inside HTTP 200 responses are also detected, including errors on individual choices; only allowlisted error metadata is logged. These diagnostics are saved in both the call and job error `details`, available through the authenticated job/attempt APIs and operator CLI. Saved request/response asset IDs locate the original bytes in the private `server/data/blobs/` directory. No database migration is needed.

For an incomplete response, `content_filter` and `max_output_tokens` now produce specific user-facing messages while retaining the existing `incomplete_response` code and retry behavior. See [OpenAI structured output error handling](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=responses). A content-filter reason does not identify the triggering passage; the server cannot infer it. Unknown reasons are preserved as bounded diagnostic labels. Routine logs exclude source text, images, model output, free-form provider error/refusal text, exception messages and authorization headers. Full saved assets remain private and can contain recipe content. Existing historical records retain their original errors; these diagnostics apply to subsequent calls.

The service reserves idempotency keys beginning `internal:`. HTTP limits must be enforced before buffering large request bodies; the service validation is not a substitute for transport limits. Apply authentication before accepting input. Worker internals do not authenticate HTTP clients.

### Worker lifecycle

Use the independent CLI worker, or explicitly start one managed background thread in the API process:

```python
from threading import Thread

worker = Worker(library, config)
thread = Thread(target=worker.run, name="recipe-worker")
thread.start()
# On controlled shutdown:
worker.stop()
thread.join()  # Finish the in-flight operation; do not abandon accepted work in memory.
```

Do not call blocking `worker.run()` on the API event loop or start a worker per request. `Worker` claims `capture`, `extract`, and `render` jobs. URL capture validates each destination and redirect, saves response HTML, and queues extraction atomically after parsing visible text and Recipe JSON-LD. Saved capture responses survive restart and are reused after lease recovery. Re-extraction consumes `visible_text` and `structured_recipe_data` without refetching; `service.refresh(recipe_id, capture_id, user=user_id, key=key)` creates a new linked URL capture. See [URL sharing limits](SERVER_API.md#url-shares).

Claims use `BEGIN IMMEDIATE`, a five-minute lease, and a heartbeat every 30 seconds during processing. Another worker can recover expired work. Commit guards reject cancelled, expired, or superseded leases. A crash after an external API call but before its result is durable can require another call after recovery; external calls are at-least-once, while recipe creation and next-stage enqueueing commit atomically. All returned model responses are preserved when received, even when cancellation prevents accepting their results.

The first successful extraction initializes an empty recipe. Later extraction completions create candidates only. Cancelling an individual stage does not cancel already-completed stages or independently queued sibling jobs. A capture cancelled through `Library.cancel` is marked failed while retaining its submitted URL.

No processing code writes `device_state`, `publications`, `state.json`, or the existing device images directory. The application API/publication integration must adapt the selected cached PNG to the existing firmware image path and migrate device selection deliberately. The original stage 1 service and controls remain unchanged.

## Markdown and render profile

`document.ExtractedRecipe` is the provider output schema. All provider fields are required; unavailable metadata is null. A non-recipe or incomplete recipe preserves the structured result but fails the job before creating an accepted document. Warnings are retained on revisions; schema validation alone does not establish extraction accuracy.

Prompt `recipe-extraction-v5` retains website recipe identification and conflict reporting between structured data and visible recipe text. It retains v3 ordered image-page extraction, overlap deduplication, and explicit handling of unreadable recipe details. It retains the v2 editing policy and removes editorial voice throughout the recipe, including commentary embedded in otherwise useful steps. It preserves all operational details and optional alternatives, uses neutral imperatives, and does not shorten instructions merely to fit the display. Timing summaries appear only in header metadata (`prep_time`, `cook_time`, `total_time`, or labeled `other_time`). Timed resting/storage instructions from source notes move into the method with their durations intact. Notes/footer contain no timing instructions or summaries. Standalone equipment requirements are omitted; equipment used in actual method actions is preserved. Existing revisions and their original prompts remain preserved; re-extraction uses the new prompt.

`to_markdown` serializes literal recipe strings into the same dialect accepted by `parse_markdown`: safe YAML metadata, `## Ingredients`, `## Method`, optional `## Notes`, optional `###` groups, ingredient bullets, method numbering, and simple emphasis. Links, embedded images, code, HTML, nested lists, unsupported sections, duplicate metadata keys, and YAML anchors/aliases are rejected. Emphasis content is preserved as plain text on the e-reader in this first rendering profile.

Rendering uses Pillow and the project's existing font selector (`ERECIPE_FONT` when supplied). Cache keys include revision identity/content hash, renderer version, font bytes hash, Pillow version, dimensions/encoding, and effective preferences. No unit conversion or custom profile is supported yet. Render sets are committed only after complete pages exist. Missing/corrupt cached PNG bytes are regenerated without an LLM call and must match the original content hash. A changed renderer/font requires a new profile/version rather than rewriting immutable render metadata.

Renderer `erecipe-pillow-v6` measures the complete document against three static layouts, largest first. It chooses the first layout that fits one 1872 × 1404 screen. If none fits, every page uses condensed typography; it never reduces text below that preset or removes recipe content.

| Layout | Body / line height | Item gap | Margin | Ingredient width / column gap |
| --- | --- | --- | --- | --- |
| Large print | 44 / 53 px | 12 px | 48 px | 760 / 56 px |
| Normal | 30 / 36 px | 4 px | 36 px | 700 / 32 px |
| Condensed | 26 / 31 px | 4 px | 36 px | 680 / 32 px |

These are native image pixels, calibrated with saved recipes and the local font, not TRMNL CSS sizes. The wider ingredient columns in v6 let Super-Simple Tangy Buttermilk Coleslaw select large print instead of normal without changing its text. Cucumber salad, Basic Tehina Sauce, and Masabacha still select large print. Narrower method columns can require a smaller preset or extra pages: tomato risotto now selects condensed, and the black bean burger fixture takes two condensed pages. The black bean recipe is retained as `server/fixtures/black-bean-burgers.formatted.md` for regression coverage alongside the chicken tacos fixture. Existing cached images remain unchanged until a new render is requested; v6 uses a distinct cache identity.

The design draws on the official [TRMNL rich-text guidance](https://trmnl.com/framework/docs/3.4/rich_text) (left alignment and approximately 1.2 line height), [text scaling](https://trmnl.com/framework/docs/3.4/text_scale) (explicit typography tiers independent of geometry), and [TRMNL X guide](https://trmnl.com/framework/docs/3.4/trmnl_x_guide) (larger, denser screens need deliberate sizing). This remains a native Pillow renderer with scalable fonts and one-bit output, rather than a TRMNL HTML/CSS renderer. The exact sizes are application defaults; test at actual cooking distance on the reader before treating them as physically validated.

Servings sit beside the title with reserved width; optional prep/cook/total/other timing metadata appears below. Existing Markdown without the new timing fields remains valid. Ingredients and numbered instructions use hanging indents. A single ingredient or method group omits its extra heading; multiple groups retain their names on reverse bars. This presentation rule applies to existing revisions without changing stored Markdown or asking the LLM to reformat them. Notes/footer reserve full-width space on single-page recipes.

On multi-page recipes, ordinary items stay together when they fit a full column. Oversized items split only between lines, with continuation markers. Group headings stay with the following content. Headers repeat and pages show `PAGE n / N`; notes continue after the body as needed. Each immutable render set stores the selected layout and ordered page assets. The existing iOS page picker and explicit page publication work with these manifests; automatic delivery still sends the first page. Layout metrics and selection policy are included in the versioned cache key, so existing revisions can be rendered again without extraction.

## Verification

The subsequent timing/equipment formatting update passed all 205 server tests, including new timing-field round trips, header placement, strict nullable schema coverage, and legacy Markdown compatibility. Provider behavior is covered with mocks; no new live extraction was run for this prompt change. The rules apply to new imports/re-extractions, not retroactively to stored recipe text.

October 14, 2026: the layout change passed the full 201-test server suite, plus a new API integration test for multi-page previews and publishing page two (202 passing tests total). The five saved recipes were rendered locally and the three typography tiers and continuation pages were visually inspected. Regression coverage checks selection, duplicate heading suppression, complete text preservation, glyph bounds, large paragraphs/notes, and cache identity. No physical reader test was performed.

October 13, 2026: all 65 server tests passed (38 processing tests plus existing stage 1 coverage). CLI preview export passed. The live chicken tacos extraction was reviewed, and its complete text was successfully rendered through a durable job into one native-size image with renderer v2. The image was visually inspected; no physical reader test was performed. The existing Starlette/httpx test-client deprecation warning remains.

```sh
server/.venv/bin/python -m pytest server/tests/test_processing.py -q
server/.venv/bin/python -m pytest server/tests -q
```

Processing tests use `httpx.MockTransport`; they do not contact OpenAI or OpenRouter. Coverage includes provider/key isolation, text/image/URL processing through OpenRouter, source preservation, complete provenance, image ordering, missing credentials/retry, provider failures/refusals/incomplete output, HTTP 200 provider errors, cancellation during calls, lease recovery, concurrent claims, transaction rollback, stale edits, candidate isolation, version 1 migration, overflow, Markdown rejection, and cache repair. Run the full suite for integration changes to retain existing stage 1 coverage.
