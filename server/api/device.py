"""One device store shared by both listeners; SQLite owns selection and history."""
from copy import deepcopy
from contextlib import contextmanager
from io import BytesIO
import json
import os
import secrets

from PIL import Image, PngImagePlugin
from server.app import Store
from server.recipes.db import dumps
from .errors import ApiError


class StateBackend:
    def __init__(self, library):
        self.db = library

    def load(self):
        row = self.db.one('SELECT state_json FROM device_state WHERE id=1')
        if row:
            return json.loads(row['state_json'])
        path = self.db.directory / 'state.json'
        if not path.exists():
            return None
        data = path.read_bytes()
        state = json.loads(data)
        backup = path.with_name('state.json.pre-sqlite.bak')
        if not backup.exists():
            with backup.open('xb') as out:
                os.chmod(backup, 0o600)
                out.write(data)
                out.flush()
                os.fsync(out.fileno())
            fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        return state

    def save(self, state):
        with self.db.connect(write=True) as c:
            c.execute('INSERT INTO device_state VALUES(1,?) ON CONFLICT(id) DO UPDATE SET state_json=excluded.state_json', (dumps(state),))
            # Retain available legacy history on the first import as well as the
            # current selection, which stage 1 did not put in history initially.
            for item in state.get('publications', []):
                descriptor = {key:item[key] for key in ('version','label') if key in item}
                c.execute('INSERT OR IGNORE INTO publications VALUES(?,NULL,NULL,?,NULL,?,?)',
                          (item['id'],dumps(descriptor),'legacy-history',item['at']))
            descriptor = state['current']
            c.execute('INSERT OR IGNORE INTO publications VALUES(?,?,?,?,?,?,?)',
                      (state['publication_id'],descriptor.get('render_set_id'),descriptor.get('page_number'),dumps(descriptor),
                       descriptor.get('requested_by'),'initial-selection',state['published_at']))


class DeviceStore(Store):
    def __init__(self, library, base_url, interval=20, device_id=None):
        self.library = library
        super().__init__(library.directory,base_url,interval,device_id,state_backend=StateBackend(library))

    def asset(self, data, kind, label):
        result = super().asset(data,kind,label)
        # Flush the firmware file and its directory before any SQLite reference.
        fd = os.open(self.assets,os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        return result

    @contextmanager
    def trial_update(self):
        # Include mode/page changes as well as publication history in the same
        # transaction, rolling memory back even if the final commit fails.
        with super().trial_update(), self.library.connect(write=True):
            yield

    def publish(self, asset, reason):
        with self.lock:
            previous = deepcopy(self.state)
            try:
                with self.library.connect(write=True) as c:
                    result = super().publish(asset,reason)
                    c.execute('UPDATE publications SET render_set_id=?,page_number=?,asset_descriptor_json=?,requested_by=?,reason=? WHERE id=?',
                              (asset.get('render_set_id'),asset.get('page_number'),dumps(asset),asset.get('requested_by'),reason,result['id']))
                return result
            except BaseException:
                self.state = previous
                raise

    def event(self, kind, **fields):
        if getattr(self.library._transaction, 'connection', None) is not None:
            # A committed publication must not appear in telemetry before its
            # enclosing idempotent API command has committed.
            fields = deepcopy(fields)
            self.library.after_commit(lambda: super(DeviceStore,self).event(kind,**fields))
            return {'event':kind, **fields}
        return super().event(kind,**fields)

    def force_recipe_refresh(self, selected):
        with self.lock:
            if any(self.state[k]!=selected[k] for k in ('publication_id','run_id','mode')):
                raise ApiError('selection_conflict','Selection changed. Retry the refresh.',409)
            # Preserve the exact page pixels, adding only a PNG nonce to defeat
            # the reader cache. Do not rerender arbitrary Markdown or call an LLM.
            with Image.open(self.assets / selected['current']['filename']) as im:
                info = PngImagePlugin.PngInfo()
                info.add_text('erecipe_refresh',secrets.token_hex(16))
                data=BytesIO();im.save(data,format='PNG',pnginfo=info)
            descriptor={**selected['current'],**self.asset(data.getvalue(),'RECIPE',selected['current']['label'])}
            descriptor['requested_by']='local'
            return self.publish(descriptor,'force-refresh')
