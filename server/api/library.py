"""API migrations and composable transactions on the processing library.

No changes to the processing schema are needed. Nested domain operations share
one connection so successful receipts and their effects commit together.
"""
from contextlib import contextmanager
from pathlib import Path
from threading import local
import secrets
import sqlite3

from server.recipes.db import Library, now, uid


class ApiLibrary(Library):
    def __init__(self, directory):
        self._transaction = local()
        self._initializing = True
        super().__init__(directory)
        self._initializing = False
        with super().connect() as c:
            c.executescript(Path(__file__).with_name('schema.sql').read_text())
        with self.connect(write=True) as c:
            for key, value in [('server_id', uid()), ('cursor_secret', secrets.token_urlsafe(32))]:
                c.execute('INSERT OR IGNORE INTO api_metadata VALUES (?,?)', (key, value))
            if not c.execute("SELECT 1 FROM api_metadata WHERE key='acceptance_backfill'").fetchone():
                # Current revisions, edits, and prior bases/parents have positive evidence
                # of having been current. Older scaffold did not record every acceptance.
                c.execute('''INSERT OR IGNORE INTO revision_acceptances
                    SELECT id,created_at FROM recipe_revisions WHERE origin!='extraction'
                    OR id IN (SELECT current_revision_id FROM recipes)
                    OR id IN (SELECT parent_revision_id FROM recipe_revisions)
                    OR id IN (SELECT base_revision_id FROM jobs)''')
                c.execute("INSERT INTO api_metadata VALUES ('acceptance_backfill','1')")
        self.server_id = self.one("SELECT value FROM api_metadata WHERE key='server_id'")['value']
        self.cursor_secret = self.one("SELECT value FROM api_metadata WHERE key='cursor_secret'")['value'].encode()

    @contextmanager
    def connect(self, write=False):
        if self._initializing:
            with super().connect(write=write) as c:
                yield c
            return
        active = getattr(self._transaction, 'connection', None)
        if active is not None:
            if write and not self._transaction.write:
                raise RuntimeError('Cannot upgrade a read snapshot to a write transaction')
            yield active
            return
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA foreign_keys=ON')
        c.execute('PRAGMA busy_timeout=10000')
        c.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
        self._transaction.connection, self._transaction.write = c, write
        self._transaction.callbacks = []
        callbacks = []
        try:
            yield c
            c.commit()
            callbacks = self._transaction.callbacks
        except BaseException:
            c.rollback()
            raise
        finally:
            self._transaction.connection = None
            self._transaction.callbacks = []
            c.close()
        for callback in callbacks:
            try:
                callback()
            except Exception:
                # An auxiliary telemetry failure cannot roll back a committed
                # selection or make the caller restore stale in-memory state.
                import logging
                logging.getLogger(__name__).error('Post-commit telemetry failed')

    def after_commit(self, callback):
        if getattr(self._transaction, 'connection', None) is not None:
            self._transaction.callbacks.append(callback)
        else:
            callback()

    def complete_render(self, job, key, renderer_version, profile, pages):
        with self.connect(write=True) as c:
            result = super().complete_render(job, key, renderer_version, profile, pages)
            c.execute('INSERT OR REPLACE INTO api_job_results VALUES (?,?)', (job['id'], result))
            return result

    def finish(self, c, job):
        # A cache-hit render also needs its exact result recorded, not the newest
        # render for the revision. The worker computes the same cache identity.
        if job['kind'] == 'render' and not c.execute('SELECT 1 FROM api_job_results WHERE job_id=?', (job['id'],)).fetchone():
            from server.recipes import renderer
            revision = dict(c.execute('SELECT * FROM recipe_revisions WHERE id=?', (job['input_revision_id'],)).fetchone())
            cached = c.execute('SELECT id FROM render_sets WHERE cache_key=?',
                               (renderer.cache_key(revision, renderer.profile()),)).fetchone()
            if cached:
                c.execute('INSERT OR IGNORE INTO api_job_results VALUES (?,?)', (job['id'], cached['id']))
        super().finish(c, job)
