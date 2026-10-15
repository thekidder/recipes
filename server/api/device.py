"""One device store shared by both listeners; SQLite owns selection and history."""
from copy import deepcopy
from contextlib import contextmanager
from io import BytesIO
import json
import os
import secrets

from PIL import Image, PngImagePlugin
from server.app import Store, TAP_WAKE_SOURCES, TAP_DEBOUNCE_SECONDS, now, seconds
from server.calendar import CalendarConfig
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
    def __init__(self, library, base_url, interval=20, device_id=None, calendar_config=None):
        self.library = library
        self.calendar_config = calendar_config or CalendarConfig()
        super().__init__(library.directory,base_url,interval,device_id,state_backend=StateBackend(library))
        self.state.setdefault('calendar', {'asset': None, 'synced_at': None, 'last_attempt_at': None,
            'last_error': None, 'next_sync_at': None, 'initialized': False})
        self.state.setdefault('recipe_session', None)
        # Migrated recipe selections need a new transport identity before tap
        # dismissal can be armed; an old firmware cache hit has no fetch evidence.
        if self.calendar_config.enabled and self.state['current']['kind'] == 'RECIPE' and not self.state['current'].get('delivery_id'):
            old = self.state['current']
            self.publish(old, 'calendar-enabled')
        self.save()

    def recipe_asset(self, data, label):
        delivery_id = None
        if self.calendar_config.enabled:
            delivery_id = secrets.token_hex(16)
            with Image.open(BytesIO(data)) as im:
                info = PngImagePlugin.PngInfo()
                info.add_text('erecipe_delivery', delivery_id)
                output = BytesIO()
                im.save(output, format='PNG', pnginfo=info)
                data = output.getvalue()
        result = self.asset(data, 'RECIPE', label)
        if delivery_id:
            result['delivery_id'] = delivery_id
        return result

    def calendar_status(self):
        with self.lock:
            calendar = self.state['calendar']
            asset = calendar['asset']
            available = bool(asset and (self.assets / asset['filename']).is_file())
            session = self.state['recipe_session']
            kind = self.state['current']['kind']
            return dict(configured=self.calendar_config.enabled, available=available,
                content_mode='calendar' if kind == 'CALENDAR' else 'recipe' if kind == 'RECIPE' else 'other',
                synced_at=calendar['synced_at'], last_attempt_at=calendar['last_attempt_at'],
                next_sync_at=calendar['next_sync_at'], last_error=calendar['last_error'],
                cache_age_seconds=seconds(now(), calendar['synced_at']) if calendar['synced_at'] else None,
                dismissal_ready=bool(self.calendar_config.enabled and available and session and session['fetched_at']),
                last_input_action=session.get('last_action') if session else None)

    def status(self):
        with self.lock:
            result = super().status()
            result['calendar_status'] = self.calendar_status()
            return result

    def cache_calendar(self, data, delay):
        from datetime import datetime, timedelta, timezone
        # Network and decoding happen before this lock. Selection is evaluated
        # only after download finishes, so a concurrent phone send always wins.
        with self.lock, self.trial_update():
            asset = self.asset(data, 'CALENDAR', 'Calendar and weather')
            calendar = self.state['calendar']
            select_default = (not calendar['initialized'] and self.state['mode'] == 'soak'
                              and self.state['current']['kind'] not in ('RECIPE', 'TAP_TRIAL', 'DIAG'))
            calendar.update(asset=asset, synced_at=now(), last_attempt_at=now(), last_error=None,
                next_sync_at=(datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat(), initialized=True)
            if select_default or (self.state['current']['kind'] == 'CALENDAR' and self.state['current']['version'] != asset['version']):
                self.publish(asset, 'calendar-sync')
            self.save()
            self.event('calendar_synced', version=asset['version'])

    def calendar_failure(self, code, delay):
        from datetime import datetime, timedelta, timezone
        with self.lock, self.trial_update():
            self.state['calendar'].update(last_attempt_at=now(), last_error=code,
                next_sync_at=(datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat())
            self.save()
            self.event('calendar_sync_failed', code=code)

    def show_calendar(self, reason='calendar-selected'):
        with self.lock, self.trial_update():
            status = self.calendar_status()
            if not status['configured'] or not status['available']:
                raise ApiError('calendar_unavailable', 'Configure the cloud key and wait for a valid calendar image.', 409)
            self.set_mode('soak')
            if self.state['current']['kind'] != 'CALENDAR' or self.state['current']['version'] != self.state['calendar']['asset']['version']:
                self.publish(self.state['calendar']['asset'], reason)
            self.save()
        return self.status()

    def content_checkin(self, values):
        with self.lock, self.trial_update():
            session = self.state['recipe_session']
            if not self.calendar_config.enabled or self.state['current']['kind'] != 'RECIPE' or not session:
                return
            source = str(values.get('UPDATE_SOURCE', '')).strip().lower()
            if source not in TAP_WAKE_SOURCES:
                action = 'held_timer' if source == 'timer' else 'held_unknown_source'
            elif not session['fetched_at']:
                action = 'waiting_for_fetch'
            elif seconds(now(), session['fetched_at']) < TAP_DEBOUNCE_SECONDS:
                action = 'held_debounce'
            elif not self.calendar_status()['available']:
                action = 'calendar_unavailable'
            else:
                action = 'dismissed'
            session['last_action'] = action
            publication_id = session['publication_id']
            if action == 'dismissed':
                self.show_calendar('center-tap')
            else:
                self.save()
            self.event('recipe_input', recipe_publication_id=publication_id,
                       source=source, image_cached=values.get('IMAGE_CACHED'), action=action)

    def content_fetched(self, filename):
        session = self.state['recipe_session']
        if session and filename == self.state['current']['filename'] and not session['fetched_at']:
            session['fetched_at'] = now()

    def redact(self, value):
        result = super().redact(value)
        if isinstance(result, str) and self.calendar_config.api_key:
            result = result.replace(self.calendar_config.api_key, '[REDACTED]')
        return result

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
                    if asset['kind'] == 'RECIPE' and self.calendar_config.enabled:
                        asset = {**asset, **self.recipe_asset((self.assets / asset['filename']).read_bytes(), asset['label'])}
                    result = super().publish(asset,reason)
                    self.state['recipe_session'] = (dict(publication_id=result['id'], fetched_at=None,
                        last_action='waiting_for_fetch') if asset['kind'] == 'RECIPE' else None)
                    self.save()
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
