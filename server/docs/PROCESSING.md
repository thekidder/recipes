# Recipe extraction and processing jobs

The processing domain is ready for local testing independently of the application API. It accepts plain text and ordered JPEG/PNG/WebP images, preserves originals, records every OpenAI call, creates Markdown revisions, and generates cached one-bit PNG previews. The worker does not publish images to the reader. The integrated application API and iOS client support text, image, and public recipe URL sharing.

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
OPENAI_API_KEY=your_actual_openai_api_key
ERECIPE_OPENAI_MODEL=gpt-4.1-mini
```

Keep the key on the server; do not paste it into chat, the iOS application, API examples, or recipe files. `.env` and `.env.*` are ignored, except the placeholder `.env.example`. Existing process environment variables take precedence over the file. A running worker must be restarted after credentials/model changes. `ProcessingConfig` does not expose the key in its repr or readiness response.

The adapter uses the [OpenAI Responses API](https://developers.openai.com/api/reference/resources/responses/methods/create), [strict structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs), and `store: false`. The configurable default is `gpt-4.1-mini`, which supports text and image input. Account/model access and live extraction quality still require a real test. No live OpenAI calls were made during implementation.

## First plain-text test

Use an isolated test library, leaving the existing hardware experiment data alone:

```sh
server/.venv/bin/python -m server.recipes --data-dir server/data/recipe-test status
server/.venv/bin/python -m server.recipes --data-dir server/data/recipe-test import-text server/fixtures/recipe-text.txt --key first-salad-test
server/.venv/bin/python -m server.recipes --data-dir server/data/recipe-test worker --drain
server/.venv/bin/python -m server.recipes --data-dir server/data/recipe-test list
```

`status` reports whether credentials are configured without showing them. Importing is local and does not call OpenAI. **Running the worker with credentials may make billable API calls.** `--drain` processes available extraction/render stages until idle; `--once` processes at most one stage. With neither flag, the worker waits for new jobs until interrupted.

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

An edit saves a new revision and queues a render without calling OpenAI. Stale versions conflict. Invalid Markdown is rejected before creating a revision. Accepted content that overflows the readable page is retained with an explicit `render_overflow` job error; it is not cropped.

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

Prompt `recipe-extraction-v4` adds website recipe identification and conflict reporting between structured data and visible recipe text. It retains v3 ordered image-page extraction, overlap deduplication, and explicit handling of unreadable recipe details. It retains the v2 editing policy and removes editorial voice throughout the recipe, including commentary embedded in otherwise useful steps. It preserves all operational details and optional alternatives, uses neutral imperatives, and does not shorten instructions merely to fit the display. Source total/uncategorized time stays a note rather than being mislabelled as cooking time. Existing revisions and their original prompts remain preserved; re-extraction uses the new prompt.

`to_markdown` serializes literal recipe strings into the same dialect accepted by `parse_markdown`: safe YAML metadata, `## Ingredients`, `## Method`, optional `## Notes`, optional `###` groups, ingredient bullets, method numbering, and simple emphasis. Links, embedded images, code, HTML, nested lists, unsupported sections, duplicate metadata keys, and YAML anchors/aliases are rejected. Emphasis content is preserved as plain text on the e-reader in this first rendering profile.

Rendering uses Pillow and the project's existing font selector (`ERECIPE_FONT` when supplied). Cache keys include revision identity/content hash, renderer version, font bytes hash, Pillow version, dimensions/encoding, and effective preferences. No unit conversion or custom profile is supported yet. Render sets are committed only after complete pages exist. Missing/corrupt cached PNG bytes are regenerated without an LLM call and must match the original content hash. A changed renderer/font requires a new profile/version rather than rewriting immutable render metadata.

Renderer `erecipe-pillow-v3` uses 32 px body text with 5 px line spacing, 8 px between items, hanging list indents, and white group headings on black bars. Servings are right-aligned alongside the title, with reserved width to prevent overlap on long titles. The header has no divider; the divider above notes/footer remains. Prep/cook metadata uses a secondary row only when present. The ingredient column is 580 px wide, separated from the wider method column by 56 px; page margins are 48 px. Notes/footer space is reserved before placing either column. The font is fixed rather than automatically shrunk to make arbitrary content fit.

The complete cleaned chicken tacos recipe now fits one native 1872 × 1404 screen and is retained as `server/fixtures/chicken-tacos.formatted.md` for regression testing. That fixture is the reviewed live extraction from prompt v2; renderer tuning does not change its text. The rendered output has been visually inspected, and a regression test checks that every ingredient, instruction, group heading, and note is drawn. Test at actual cooking distance on the X before treating the typography as physically validated. Longer recipes still receive an overflow error. Full pagination is deferred, but results use ordered page collections from the outset.

## Verification

October 13, 2026: all 65 server tests passed (38 processing tests plus existing stage 1 coverage). CLI preview export passed. The live chicken tacos extraction was reviewed, and its complete text was successfully rendered through a durable job into one native-size image with renderer v2. The image was visually inspected; no physical reader test was performed. The existing Starlette/httpx test-client deprecation warning remains.

```sh
server/.venv/bin/python -m pytest server/tests/test_processing.py -q
server/.venv/bin/python -m pytest server/tests -q
```

Processing tests use `httpx.MockTransport`; they do not contact OpenAI. Coverage includes source preservation, complete provenance, text-to-PNG processing, image ordering, missing credentials/retry, provider failures/refusals/incomplete output, cancellation during calls, lease recovery, concurrent claims, transaction rollback, stale edits, candidate isolation, version 1 migration, overflow, Markdown rejection, and cache repair. Run the full suite for integration changes to retain existing stage 1 coverage.
