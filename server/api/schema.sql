CREATE TABLE IF NOT EXISTS api_metadata (
    key TEXT PRIMARY KEY, value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS api_receipts (
    namespace TEXT NOT NULL, key TEXT NOT NULL, fingerprint TEXT NOT NULL,
    status INTEGER NOT NULL, body_json TEXT, location TEXT,
    created_at TEXT NOT NULL, PRIMARY KEY(namespace,key)
);
CREATE TABLE IF NOT EXISTS enrollments (
    id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
    device_label TEXT NOT NULL, code_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
    revoked_at TEXT, credential_id TEXT REFERENCES user_tokens(id)
);
CREATE TABLE IF NOT EXISTS revision_acceptances (
    revision_id TEXT PRIMARY KEY REFERENCES recipe_revisions(id), accepted_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS api_record_acceptance AFTER UPDATE OF current_revision_id ON recipes
WHEN NEW.current_revision_id IS NOT NULL AND NEW.current_revision_id IS NOT OLD.current_revision_id
BEGIN
    INSERT OR IGNORE INTO revision_acceptances VALUES(NEW.current_revision_id, NEW.updated_at);
END;
CREATE TABLE IF NOT EXISTS api_job_results (
    job_id TEXT PRIMARY KEY REFERENCES jobs(id), render_set_id TEXT NOT NULL REFERENCES render_sets(id)
);
CREATE INDEX IF NOT EXISTS enrollment_user ON enrollments(user_id,created_at);
CREATE INDEX IF NOT EXISTS recipes_created ON recipes(created_at,id);
