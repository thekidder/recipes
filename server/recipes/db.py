"""SQLite records and content-addressed files. No network calls inside transactions."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import uuid


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def uid():
    return uuid.uuid4().hex


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(data):
    return hashlib.sha256(data).hexdigest()


class Problem(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class Library:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.blobs = self.directory / "blobs"
        self.blobs.mkdir(exist_ok=True, mode=0o700)
        self.path = self.directory / "recipes.sqlite3"
        with self.connect() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(Path(__file__).with_name("schema.sql").read_text())
            c.execute("INSERT OR IGNORE INTO schema_versions VALUES (1, ?)", (now(),))
            if "parent_job_id" not in {row[1] for row in c.execute("PRAGMA table_info(jobs)")}:
                c.execute("ALTER TABLE jobs ADD COLUMN parent_job_id TEXT REFERENCES jobs(id)")
            c.execute("CREATE INDEX IF NOT EXISTS jobs_parent ON jobs(parent_job_id)")
            c.execute("INSERT OR IGNORE INTO schema_versions VALUES (2, ?)", (now(),))
            c.execute("INSERT OR IGNORE INTO users VALUES ('local', 'Local operator', ?, NULL)", (now(),))
        self.path.chmod(0o600)

    @contextmanager
    def connect(self, write=False):
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA busy_timeout=10000")
        try:
            if write:
                c.execute("BEGIN IMMEDIATE")
            yield c
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    def one(self, sql, args=()):
        with self.connect() as c:
            row = c.execute(sql, args).fetchone()
            return dict(row) if row else None

    def all(self, sql, args=()):
        with self.connect() as c:
            return [dict(row) for row in c.execute(sql, args)]

    def execute(self, sql, args=()):
        with self.connect(write=True) as c:
            c.execute(sql, args)

    def required(self, table, identifier):
        if table not in {"recipes", "recipe_revisions", "source_captures", "jobs", "assets", "render_sets"}:
            raise ValueError("Unsupported record type")
        item = self.one(f"SELECT * FROM {table} WHERE id=?", (identifier,))
        if not item:
            raise Problem("Record not found", 404)
        return item

    def asset(self, data, media_type):
        identifier = digest(data)
        path = self.blobs / identifier
        if not path.exists() or digest(path.read_bytes()) != identifier:
            temp = path.with_name(identifier + "." + uid() + ".tmp")
            try:
                with temp.open("xb") as out:
                    os.chmod(temp, 0o600)
                    out.write(data)
                    out.flush()
                    os.fsync(out.fileno())
                os.replace(temp, path)
                descriptor = os.open(self.blobs, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            finally:
                temp.unlink(missing_ok=True)
        self.execute("INSERT OR IGNORE INTO assets VALUES (?, ?, ?, ?, ?)",
                     (identifier, identifier, media_type, len(data), now()))
        return identifier

    def json_asset(self, value):
        return self.asset(dumps(value).encode(), "application/json")

    def asset_path(self, identifier):
        item = self.required("assets", identifier)
        return self.blobs / item["storage_key"]

    def asset_bytes(self, identifier):
        return self.asset_path(identifier).read_bytes()

    def create_user(self, name, label="client"):
        identifier, token = uid(), secrets.token_urlsafe(32)
        with self.connect(write=True) as c:
            c.execute("INSERT INTO users VALUES (?, ?, ?, NULL)", (identifier, name, now()))
            c.execute("INSERT INTO user_tokens VALUES (?, ?, ?, ?, ?, NULL)",
                      (uid(), identifier, digest(token.encode()), label, now()))
        return identifier, token

    def authenticate(self, token):
        return self.one("""SELECT u.id, u.display_name FROM user_tokens t JOIN users u ON u.id=t.user_id
            WHERE t.token_hash=? AND t.revoked_at IS NULL AND u.disabled_at IS NULL""", (digest(token.encode()),))

    def enqueue(self, c, *, recipe, kind, user, key, request_hash, capture=None,
                revision=None, base=None, parameters=None, parent_job=None):
        identifier, stamp = uid(), now()
        c.execute("""INSERT INTO jobs(id,recipe_id,kind,source_capture_id,input_revision_id,base_revision_id,
            status,requested_by,idempotency_key,request_sha256,parameters_json,created_at,available_at,parent_job_id)
            VALUES(?,?,?,?,?,?,'queued',?,?,?,?,?,?,?)""",
                  (identifier, recipe, kind, capture, revision, base, user, key, request_hash,
                   dumps(parameters or {}), stamp, stamp, parent_job))
        return identifier

    @staticmethod
    def existing_request(c, user, key, request_hash):
        row = c.execute("SELECT * FROM jobs WHERE requested_by=? AND idempotency_key=?", (user, key)).fetchone()
        if row and row["request_sha256"] != request_hash:
            raise Problem("Idempotency key was already used with different content", 409)
        return dict(row) if row else None

    def submit(self, user, key, kind, request_hash, assets=(), url=None):
        recipe, capture, stamp = uid(), uid(), now()
        with self.connect(write=True) as c:
            existing = self.existing_request(c, user, key, request_hash)
            if existing:
                return existing
            c.execute("INSERT INTO recipes(id,created_by,created_at,updated_at) VALUES(?,?,?,?)",
                      (recipe, user, stamp, stamp))
            c.execute("""INSERT INTO source_captures(id,recipe_id,kind,submitted_url,state,submitted_at,captured_at,created_by)
                VALUES(?,?,?,?,?,?,?,?)""", (capture, recipe, kind, url, "pending" if kind == "url" else "complete",
                                          stamp, None if kind == "url" else stamp, user))
            for role, ordinal, asset, filename in assets:
                c.execute("INSERT INTO source_assets VALUES (?,?,?,?,?)", (capture, role, ordinal, asset, filename))
            job = self.enqueue(c, recipe=recipe, kind="capture" if kind == "url" else "extract", user=user,
                               key=key, request_hash=request_hash, capture=capture)
        return self.required("jobs", job)

    def request_work(self, recipe_id, user, key, operation, capture_id=None, revision_id=None):
        if operation not in {"extract", "refresh", "render"}:
            raise Problem("Unsupported processing operation")
        request_hash = digest(dumps([recipe_id, operation, capture_id, revision_id]).encode())
        with self.connect(write=True) as c:
            existing = self.existing_request(c, user, key, request_hash)
            if existing:
                return existing
            recipe = c.execute("SELECT * FROM recipes WHERE id=?", (recipe_id,)).fetchone()
            if not recipe or recipe["archived_at"]:
                raise Problem("Recipe not found or archived", 404)
            if operation in ("extract", "refresh"):
                capture = c.execute("SELECT * FROM source_captures WHERE id=? AND recipe_id=?",
                                    (capture_id, recipe_id)).fetchone()
                if not capture:
                    raise Problem("Choose a source capture belonging to this recipe", 404)
                if operation == "refresh":
                    if capture["kind"] != "url":
                        raise Problem("Only URL sources can be refreshed")
                    capture_id = uid()
                    c.execute("""INSERT INTO source_captures(id,recipe_id,previous_capture_id,kind,submitted_url,
                        state,submitted_at,created_by) VALUES(?,?,?,'url',?,'pending',?,?)""",
                              (capture_id, recipe_id, capture["id"], capture["submitted_url"], now(), user))
                elif capture["state"] != "complete":
                    raise Problem("Source capture is not complete", 409)
            else:
                if not c.execute("SELECT 1 FROM recipe_revisions WHERE id=? AND recipe_id=?",
                                 (revision_id, recipe_id)).fetchone():
                    raise Problem("Revision not found", 404)
            job = self.enqueue(c, recipe=recipe_id, kind="capture" if operation == "refresh" else operation,
                               user=user, key=key, request_hash=request_hash, capture=capture_id,
                               revision=revision_id, base=recipe["current_revision_id"])
        return self.required("jobs", job)

    def claim(self, kinds=None):
        stamp = now()
        with self.connect(write=True) as c:
            expired = c.execute("SELECT id FROM jobs WHERE status='running' AND lease_expires_at < ?", (stamp,)).fetchall()
            for job in expired:
                c.execute("""UPDATE llm_calls SET status='abandoned',finished_at=? WHERE status='running' AND attempt_id IN
                    (SELECT id FROM job_attempts WHERE job_id=?)""", (stamp, job["id"]))
                c.execute("UPDATE job_attempts SET status='abandoned',finished_at=? WHERE job_id=? AND status='running'", (stamp, job["id"]))
                c.execute("UPDATE jobs SET status='queued',lease_token=NULL,lease_expires_at=NULL WHERE id=?", (job["id"],))
            kinds = tuple(kinds or ("capture", "extract", "render"))
            placeholders = ",".join("?" for _ in kinds)
            while True:
                row = c.execute(f"SELECT * FROM jobs WHERE status='queued' AND available_at <= ? AND kind IN ({placeholders}) ORDER BY created_at,id LIMIT 1", (stamp, *kinds)).fetchone()
                if not row:
                    return None
                job = dict(row)
                number = c.execute("SELECT count(*)+1 FROM job_attempts WHERE job_id=?", (job["id"],)).fetchone()[0]
                if number <= 5:
                    break
                c.execute("UPDATE jobs SET status='failed',finished_at=?,error_json=? WHERE id=?",
                          (stamp, dumps({"code": "attempts_exhausted", "message": "Five attempts exhausted; create a new extraction/render request.", "retryable": False, "details": {}}), job["id"]))
                if job['kind'] == 'capture':
                    c.execute("UPDATE source_captures SET state='failed',error_json=? WHERE id=? AND state='pending'",
                              (dumps(dict(code='capture_failed', message='Capture attempts exhausted. Fetch the link again.', retryable=True, details={})), job['source_capture_id']))
            attempt = uid()
            lease = uid()
            expires = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(timespec="milliseconds")
            c.execute("UPDATE jobs SET status='running',lease_token=?,lease_expires_at=?,error_json=NULL WHERE id=?",
                      (lease, expires, job["id"]))
            c.execute("INSERT INTO job_attempts(id,job_id,attempt_number,started_at,status) VALUES(?,?,?,?,'running')",
                      (attempt, job["id"], number, stamp))
            job.update(status="running", lease_token=lease, lease_expires_at=expires, attempt_id=attempt)
            return job

    @staticmethod
    def guard(c, job):
        row = c.execute("SELECT status,lease_token,lease_expires_at FROM jobs WHERE id=?", (job["id"],)).fetchone()
        if not row or row["status"] != "running" or row["lease_token"] != job["lease_token"] or row["lease_expires_at"] <= now():
            raise Problem("Job was cancelled or superseded", 409)

    @staticmethod
    def finish(c, job):
        Library.guard(c, job)
        stamp = now()
        c.execute("UPDATE jobs SET status='succeeded',finished_at=?,lease_token=NULL,lease_expires_at=NULL WHERE id=?", (stamp, job["id"]))
        c.execute("UPDATE job_attempts SET status='succeeded',finished_at=? WHERE id=?", (stamp, job["attempt_id"]))

    def fail(self, job, message):
        error = dumps(message if isinstance(message, dict) else {"code": "processing_error", "message": message, "retryable": False, "details": {}})
        with self.connect(write=True) as c:
            row = c.execute("SELECT lease_token,lease_expires_at FROM jobs WHERE id=?", (job["id"],)).fetchone()
            if not row or row["lease_token"] != job["lease_token"] or row["lease_expires_at"] <= now():
                return
            c.execute("UPDATE jobs SET status='failed',finished_at=?,error_json=?,lease_token=NULL,lease_expires_at=NULL WHERE id=?",
                      (now(), error, job["id"]))
            c.execute("UPDATE job_attempts SET status='failed',finished_at=?,error_json=? WHERE id=?", (now(), error, job["attempt_id"]))
            if job["kind"] == "capture":
                c.execute("UPDATE source_captures SET state='failed',error_json=? WHERE id=? AND state='pending'", (error, job["source_capture_id"]))

    def retry(self, identifier):
        with self.connect(write=True) as c:
            job = c.execute("SELECT * FROM jobs WHERE id=?", (identifier,)).fetchone()
            if not job:
                raise Problem("Job not found", 404)
            if job["status"] != "failed":
                raise Problem("Only failed jobs can be retried", 409)
            if job["kind"] == "capture":
                raise Problem("Use Refresh source to create a new URL capture", 409)
            if c.execute("SELECT count(*) FROM job_attempts WHERE job_id=?", (identifier,)).fetchone()[0] >= 5:
                raise Problem("Five attempts exhausted. Create a new extraction or render request.", 409)
            c.execute("UPDATE jobs SET status='queued',available_at=?,finished_at=NULL,error_json=NULL WHERE id=?", (now(), identifier))

    def cancel(self, identifier):
        with self.connect(write=True) as c:
            if not c.execute("UPDATE jobs SET status='cancelled',finished_at=?,lease_token=NULL,lease_expires_at=NULL WHERE id=? AND status IN ('queued','running')",
                             (now(), identifier)).rowcount:
                raise Problem("Job is not queued or running", 409)
            c.execute("UPDATE job_attempts SET status='abandoned',finished_at=? WHERE job_id=? AND status='running'", (now(), identifier))
            c.execute("""UPDATE llm_calls SET status='abandoned',finished_at=? WHERE status='running'
                AND attempt_id IN (SELECT id FROM job_attempts WHERE job_id=?)""", (now(), identifier))
            c.execute("""UPDATE source_captures SET state='failed',error_json=? WHERE state='pending'
                AND id=(SELECT source_capture_id FROM jobs WHERE id=? AND kind='capture')""",
                      (dumps({"code": "cancelled", "message": "Source capture was cancelled."}), identifier))

    def renew(self, job):
        with self.connect(write=True) as c:
            self.guard(c, job)
            expires = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(timespec="milliseconds")
            c.execute("UPDATE jobs SET lease_expires_at=? WHERE id=?", (expires, job["id"]))

    def record_capture_response(self, job, resolved_url, raw_asset, metadata):
        with self.connect(write=True) as c:
            self.guard(c, job)
            c.execute("INSERT INTO source_assets VALUES (?,'response_html',0,?,NULL)", (job['source_capture_id'], raw_asset))
            c.execute('UPDATE source_captures SET resolved_url=?,captured_at=?,capture_metadata_json=? WHERE id=?',
                      (resolved_url, now(), dumps(metadata), job['source_capture_id']))

    def complete_capture(self, job, assets):
        with self.connect(write=True) as c:
            self.guard(c, job)
            recipe = c.execute('SELECT * FROM recipes WHERE id=?', (job['recipe_id'],)).fetchone()
            if recipe['archived_at']:
                raise Problem('Recipe was archived while capturing', 409)
            for role, asset in assets:
                c.execute('INSERT INTO source_assets VALUES (?,?,0,?,NULL)', (job['source_capture_id'], role, asset))
            c.execute("UPDATE source_captures SET state='complete',error_json=NULL WHERE id=?", (job['source_capture_id'],))
            self.enqueue(c, recipe=job['recipe_id'], kind='extract', user=job['requested_by'],
                         key='internal:extract:' + job['source_capture_id'], request_hash=digest(job['source_capture_id'].encode()),
                         capture=job['source_capture_id'], base=job['base_revision_id'], parent_job=job['id'])
            self.finish(c, job)

    def complete_extraction(self, job, markdown, title, warnings):
        with self.connect(write=True) as c:
            self.guard(c, job)
            recipe = c.execute("SELECT * FROM recipes WHERE id=?", (job["recipe_id"],)).fetchone()
            if recipe["archived_at"]:
                raise Problem("Recipe was archived while processing", 409)
            revision = self.insert_revision(c, recipe=job["recipe_id"], parent=job["base_revision_id"],
                                           capture=job["source_capture_id"], user=job["requested_by"],
                                           origin="extraction", markdown=markdown, title=title,
                                           warnings=warnings, attempt=job["attempt_id"])
            # Only the first successful extraction can initialize the current document.
            if recipe["current_revision_id"] is None:
                c.execute("UPDATE recipes SET current_revision_id=?,lock_version=lock_version+1,updated_at=? WHERE id=?",
                          (revision, now(), job["recipe_id"]))
            else:
                c.execute("UPDATE recipes SET updated_at=? WHERE id=?", (now(), job["recipe_id"]))
            self.enqueue(c, recipe=job["recipe_id"], kind="render", user=job["requested_by"],
                         key="internal:render:" + revision, request_hash=digest(revision.encode()),
                         revision=revision, base=revision, parent_job=job["id"])
            self.finish(c, job)
        return revision

    def complete_render(self, job, key, renderer_version, profile, pages):
        if not pages:
            raise Problem("Renderer produced no pages")
        with self.connect(write=True) as c:
            self.guard(c, job)
            existing = c.execute("SELECT id FROM render_sets WHERE cache_key=?", (key,)).fetchone()
            render = existing["id"] if existing else uid()
            if not existing:
                c.execute("INSERT INTO render_sets VALUES(?,?,?,?,?,?,?,?,?)",
                          (render, job["input_revision_id"], job["attempt_id"], key, renderer_version,
                           dumps(profile), "{}", len(pages), now()))
                c.executemany("INSERT INTO render_pages VALUES(?,?,?)", [(render, n, asset) for n, asset in enumerate(pages, 1)])
            c.execute("UPDATE recipes SET updated_at=? WHERE id=?", (now(), job["recipe_id"]))
            self.finish(c, job)
        return render

    @staticmethod
    def insert_revision(c, *, recipe, parent, capture, user, origin, markdown, title, warnings, attempt=None):
        revision = uid()
        c.execute("""INSERT INTO recipe_revisions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (revision, recipe, parent, capture, attempt, origin, markdown, digest(markdown.encode()),
                   "erecipe/v1", title, dumps(warnings), user, now()))
        return revision

    def edit(self, recipe_id, user, markdown, title, expected):
        from .document import parse_markdown
        title = parse_markdown(markdown)["metadata"]["title"]
        with self.connect(write=True) as c:
            recipe = c.execute("SELECT * FROM recipes WHERE id=?", (recipe_id,)).fetchone()
            if not recipe or not recipe["current_revision_id"] or recipe["archived_at"]:
                raise Problem("Recipe has no current revision", 409)
            if recipe["lock_version"] != expected:
                raise Problem("Recipe changed. Reload and compare your edits before saving.", 409)
            parent = c.execute("SELECT * FROM recipe_revisions WHERE id=?", (recipe["current_revision_id"],)).fetchone()
            revision = self.insert_revision(c, recipe=recipe_id, parent=parent["id"], capture=parent["source_capture_id"],
                                           user=user, origin="user_edit", markdown=markdown, title=title,
                                           warnings=json.loads(parent["warnings_json"]))
            c.execute("UPDATE recipes SET current_revision_id=?,lock_version=lock_version+1,updated_at=? WHERE id=?", (revision, now(), recipe_id))
            job = self.enqueue(c, recipe=recipe_id, kind="render", user=user, key="render:" + revision,
                               request_hash=digest(revision.encode()), revision=revision, base=revision)
        return {"revision_id": revision, "job_id": job}

    def accept(self, recipe_id, revision_id, expected):
        with self.connect(write=True) as c:
            if not c.execute("SELECT 1 FROM recipe_revisions WHERE id=? AND recipe_id=?", (revision_id, recipe_id)).fetchone():
                raise Problem("Revision not found", 404)
            if not c.execute("UPDATE recipes SET current_revision_id=?,lock_version=lock_version+1,updated_at=? WHERE id=? AND lock_version=? AND archived_at IS NULL",
                             (revision_id, now(), recipe_id, expected)).rowcount:
                raise Problem("Recipe changed. Reload before accepting this revision.", 409)

    def detail(self, recipe_id):
        recipe = self.required("recipes", recipe_id)
        recipe["revisions"] = self.all("SELECT * FROM recipe_revisions WHERE recipe_id=? ORDER BY created_at DESC,id", (recipe_id,))
        recipe["captures"] = self.all("SELECT * FROM source_captures WHERE recipe_id=? ORDER BY submitted_at DESC,id", (recipe_id,))
        for capture in recipe["captures"]:
            capture["assets"] = self.all("SELECT s.*,a.media_type,a.byte_size FROM source_assets s JOIN assets a ON a.id=s.asset_id WHERE capture_id=? ORDER BY role,ordinal", (capture["id"],))
        recipe["jobs"] = self.all("SELECT * FROM jobs WHERE recipe_id=? ORDER BY created_at DESC,id", (recipe_id,))
        recipe["renders"] = self.all("SELECT rs.* FROM render_sets rs JOIN recipe_revisions r ON r.id=rs.revision_id WHERE r.recipe_id=? ORDER BY rs.created_at DESC", (recipe_id,))
        for render in recipe["renders"]:
            render["pages"] = self.all("SELECT * FROM render_pages WHERE render_set_id=? ORDER BY page_number", (render["id"],))
        return recipe
