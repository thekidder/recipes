PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_versions (
    version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY NOT NULL, display_name TEXT NOT NULL,
    created_at TEXT NOT NULL, disabled_at TEXT
);
CREATE TABLE IF NOT EXISTS user_tokens (
    id TEXT PRIMARY KEY NOT NULL, user_id TEXT NOT NULL REFERENCES users(id),
    token_hash TEXT NOT NULL UNIQUE, label TEXT NOT NULL,
    created_at TEXT NOT NULL, revoked_at TEXT
);
CREATE TABLE IF NOT EXISTS assets (
    id TEXT PRIMARY KEY NOT NULL, storage_key TEXT NOT NULL UNIQUE,
    media_type TEXT NOT NULL, byte_size INTEGER NOT NULL CHECK(byte_size >= 0),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS recipes (
    id TEXT PRIMARY KEY NOT NULL, current_revision_id TEXT,
    created_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL, lock_version INTEGER NOT NULL DEFAULT 0,
    archived_at TEXT,
    FOREIGN KEY(id, current_revision_id) REFERENCES recipe_revisions(recipe_id, id)
);
CREATE TABLE IF NOT EXISTS source_captures (
    id TEXT PRIMARY KEY NOT NULL, recipe_id TEXT NOT NULL REFERENCES recipes(id),
    previous_capture_id TEXT,
    kind TEXT NOT NULL CHECK(kind IN ('text','images','url')),
    submitted_url TEXT, resolved_url TEXT,
    state TEXT NOT NULL CHECK(state IN ('pending','complete','failed')),
    submitted_at TEXT NOT NULL, captured_at TEXT,
    capture_metadata_json TEXT NOT NULL DEFAULT '{}', error_json TEXT,
    created_by TEXT NOT NULL REFERENCES users(id),
    UNIQUE(recipe_id, id),
    FOREIGN KEY(recipe_id, previous_capture_id) REFERENCES source_captures(recipe_id, id),
    CHECK((kind = 'url' AND submitted_url IS NOT NULL) OR (kind != 'url' AND submitted_url IS NULL))
);
CREATE TABLE IF NOT EXISTS source_assets (
    capture_id TEXT NOT NULL REFERENCES source_captures(id), role TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK(ordinal >= 0), asset_id TEXT NOT NULL REFERENCES assets(id),
    original_filename TEXT,
    PRIMARY KEY(capture_id, role, ordinal)
);
CREATE TABLE IF NOT EXISTS recipe_revisions (
    id TEXT PRIMARY KEY NOT NULL, recipe_id TEXT NOT NULL REFERENCES recipes(id),
    parent_revision_id TEXT, source_capture_id TEXT NOT NULL,
    generated_by_attempt TEXT REFERENCES job_attempts(id),
    origin TEXT NOT NULL CHECK(origin IN ('extraction','user_edit','restore')),
    markdown TEXT NOT NULL, markdown_sha256 TEXT NOT NULL, format_version TEXT NOT NULL,
    title TEXT NOT NULL, warnings_json TEXT NOT NULL DEFAULT '[]',
    created_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL,
    UNIQUE(recipe_id, id),
    FOREIGN KEY(recipe_id, parent_revision_id) REFERENCES recipe_revisions(recipe_id, id),
    FOREIGN KEY(recipe_id, source_capture_id) REFERENCES source_captures(recipe_id, id)
);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY NOT NULL, recipe_id TEXT NOT NULL REFERENCES recipes(id),
    parent_job_id TEXT REFERENCES jobs(id),
    kind TEXT NOT NULL CHECK(kind IN ('capture','extract','render')),
    source_capture_id TEXT, input_revision_id TEXT, base_revision_id TEXT,
    status TEXT NOT NULL CHECK(status IN ('queued','running','succeeded','failed','cancelled')),
    requested_by TEXT NOT NULL REFERENCES users(id), idempotency_key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL, parameters_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL, available_at TEXT NOT NULL,
    lease_token TEXT, lease_expires_at TEXT, finished_at TEXT, error_json TEXT,
    UNIQUE(requested_by, idempotency_key),
    FOREIGN KEY(recipe_id, source_capture_id) REFERENCES source_captures(recipe_id, id),
    FOREIGN KEY(recipe_id, input_revision_id) REFERENCES recipe_revisions(recipe_id, id),
    FOREIGN KEY(recipe_id, base_revision_id) REFERENCES recipe_revisions(recipe_id, id),
    CHECK((kind IN ('capture','extract') AND source_capture_id IS NOT NULL AND input_revision_id IS NULL)
       OR (kind = 'render' AND input_revision_id IS NOT NULL))
);
CREATE TABLE IF NOT EXISTS job_attempts (
    id TEXT PRIMARY KEY NOT NULL, job_id TEXT NOT NULL REFERENCES jobs(id),
    attempt_number INTEGER NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
    status TEXT NOT NULL CHECK(status IN ('running','succeeded','failed','abandoned')),
    error_json TEXT, UNIQUE(job_id, attempt_number)
);
CREATE TABLE IF NOT EXISTS llm_calls (
    id TEXT PRIMARY KEY NOT NULL, attempt_id TEXT NOT NULL REFERENCES job_attempts(id),
    sequence INTEGER NOT NULL, provider TEXT NOT NULL, requested_model TEXT NOT NULL,
    reported_model TEXT, provider_request_id TEXT, prompt_version TEXT NOT NULL,
    adapter_version TEXT NOT NULL, request_asset_id TEXT NOT NULL REFERENCES assets(id),
    response_asset_id TEXT REFERENCES assets(id), structured_output_asset_id TEXT REFERENCES assets(id),
    status TEXT NOT NULL CHECK(status IN ('running','succeeded','failed','abandoned')),
    started_at TEXT NOT NULL, finished_at TEXT, usage_json TEXT, error_json TEXT,
    UNIQUE(attempt_id, sequence)
);
CREATE TABLE IF NOT EXISTS render_sets (
    id TEXT PRIMARY KEY NOT NULL, revision_id TEXT NOT NULL REFERENCES recipe_revisions(id),
    generated_by_attempt TEXT REFERENCES job_attempts(id), cache_key TEXT NOT NULL UNIQUE,
    renderer_version TEXT NOT NULL, display_profile_json TEXT NOT NULL,
    preferences_json TEXT NOT NULL, page_count INTEGER NOT NULL CHECK(page_count > 0),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS render_pages (
    render_set_id TEXT NOT NULL REFERENCES render_sets(id),
    page_number INTEGER NOT NULL CHECK(page_number > 0), asset_id TEXT NOT NULL REFERENCES assets(id),
    PRIMARY KEY(render_set_id, page_number)
);
-- The one paired device retains its stage-1 telemetry/settings representation.
-- SQLite is authoritative; legacy state.json is imported once, never dual-written.
CREATE TABLE IF NOT EXISTS device_state (
    id INTEGER PRIMARY KEY CHECK(id = 1), state_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS publications (
    id TEXT PRIMARY KEY NOT NULL, render_set_id TEXT, page_number INTEGER,
    asset_descriptor_json TEXT NOT NULL, requested_by TEXT REFERENCES users(id),
    reason TEXT NOT NULL, created_at TEXT NOT NULL,
    FOREIGN KEY(render_set_id, page_number) REFERENCES render_pages(render_set_id, page_number),
    CHECK((render_set_id IS NULL AND page_number IS NULL) OR
          (render_set_id IS NOT NULL AND page_number IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS recipes_updated ON recipes(updated_at, id);
CREATE INDEX IF NOT EXISTS revisions_recipe ON recipe_revisions(recipe_id, created_at);
CREATE INDEX IF NOT EXISTS captures_recipe ON source_captures(recipe_id, submitted_at);
CREATE INDEX IF NOT EXISTS jobs_ready ON jobs(status, available_at);
CREATE INDEX IF NOT EXISTS jobs_recipe ON jobs(recipe_id, created_at);
CREATE INDEX IF NOT EXISTS renders_revision ON render_sets(revision_id);
