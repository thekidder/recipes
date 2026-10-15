"""Calendar fallback, cloud isolation, and stock-firmware dismissal regressions."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from io import BytesIO
import json
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image
import pytest

from server.application import create_apps
from server.calendar import CalendarClient, CalendarConfig, CalendarError, CalendarWorker, download
from server.recipes.config import ProcessingConfig
from server.tests.test_api import Harness, checked, key, LAN, ORIGIN

SECRET = 'test-cloud-key-do-not-leak'
CONFIG = CalendarConfig(api_key=SECRET)


def png(color='white', size=(1872, 1404)):
    out = BytesIO()
    Image.new('1', size, color).save(out, format='PNG')
    return out.getvalue()


@pytest.fixture
def rig(tmp_path):
    lan, web, service, _ = create_apps(tmp_path, LAN, ORIGIN, config=ProcessingConfig(), calendar_config=CONFIG)
    with TestClient(lan) as client:
        result = client.get('/api/setup', headers={'ID': 'AA:BB:CC:DD:EE:01'}).json()
        yield service.store, client, {'Access-Token': result['api_key']}


def publish_recipe(store):
    store.set_mode('soak')
    return store.publish(store.asset(png('black'), 'RECIPE', 'Test recipe'), 'explicit-send')


def fetched(store, client):
    client.get('/images/' + store.state['current']['filename'])
    store.state['recipe_session']['fetched_at'] = (datetime.now(timezone.utc)-timedelta(seconds=5)).isoformat()
    store.save()


def test_calendar_recipe_dismissal_and_restart(rig, tmp_path):
    store, client, auth = rig
    store.cache_calendar(png(), 900)
    assert store.calendar_status()['content_mode'] == 'calendar'
    assert client.get('/control/calendar/preview').status_code == 200
    assert store.state['last_fetch'] is None  # Preview isn't delivery.
    publication = publish_recipe(store)
    recipe = store.state['current']['filename']
    tap = {**auth, 'Update-Source': 'EXT0', 'Image-Cached': 'true'}
    assert client.get('/api/display', headers=tap).json()['filename'] == recipe
    assert store.state['recipe_session']['last_action'] == 'waiting_for_fetch'
    fetched(store, client)
    for source in ('timer', 'powercycle', 'unknown'):
        assert client.get('/api/display', headers={**auth, 'Update-Source': source}).json()['filename'] == recipe
    store.cache_calendar(png('gray'), 900)
    assert store.state['publication_id'] == publication['id']
    # An unchanged recipe persists across a real store reconstruction.
    _, _, restored, _ = create_apps(tmp_path, LAN, ORIGIN, config=ProcessingConfig(), calendar_config=CONFIG)
    assert restored.store.state['recipe_session']['fetched_at']
    with TestClient(create_apps(tmp_path, LAN, ORIGIN, config=ProcessingConfig(), calendar_config=CONFIG)[0]) as restarted:
        response = restarted.get('/api/display', headers=tap).json()
        assert response['filename'] == store.state['calendar']['asset']['filename']
        assert response['reset_firmware'] is False and response['update_firmware'] is False
        assert response['refresh_rate'] == 20
        assert restarted.get('/api/display', headers=tap).json() == response


def test_resending_same_pixels_requires_new_fetch_and_debounces(rig):
    store, client, auth = rig
    store.cache_calendar(png(), 900)
    publish_recipe(store)
    first = store.state['current']['filename']
    fetched(store, client)
    publish_recipe(store)
    second = store.state['current']['filename']
    assert second != first
    with Image.open(store.assets / first) as a, Image.open(store.assets / second) as b:
        assert a.tobytes() == b.tobytes()
    tap = {**auth, 'Update-Source': 'EXT0', 'Image-Cached': 'true'}
    client.get('/images/' + first)  # Delayed fetch from prior selection must not arm this one.
    client.get('/api/display', headers=tap)
    assert store.state['recipe_session']['last_action'] == 'waiting_for_fetch'
    client.get('/images/' + second)
    client.get('/api/display', headers=tap)
    assert store.state['recipe_session']['last_action'] == 'held_debounce'
    assert client.get('/api/display', headers={'Update-Source': 'EXT0'}).status_code == 401


def test_cloud_outage_never_overwrites_recipe_and_stale_cache_can_dismiss(rig):
    store, client, auth = rig
    publish_recipe(store)
    fetched(store, client)
    tap = {**auth, 'Update-Source': 'EXT0'}
    client.get('/api/display', headers=tap)
    assert store.state['recipe_session']['last_action'] == 'calendar_unavailable'
    store.cache_calendar(png(), 900)
    store.calendar_failure('cloud_http_503', 300)
    assert store.calendar_status()['content_mode'] == 'recipe'
    client.get('/api/display', headers=tap)
    assert store.calendar_status()['content_mode'] == 'calendar'
    assert store.calendar_status()['last_error'] == 'cloud_http_503'


def test_trial_and_manual_calendar_controls(rig):
    store, client, auth = rig
    h = {'Origin': 'http://testserver', 'X-eRecipe-Control': '1'}
    assert client.post('/control/calendar/show', json={}, headers=h).status_code == 409
    assert client.post('/control/calendar/show', json={}).status_code == 403
    store.start_tap_trial()
    before = store.state['current']['filename']
    store.cache_calendar(png(), 900)
    assert store.state['current']['filename'] == before
    assert client.post('/control/calendar/show', json={}, headers=h).status_code == 200
    assert store.state['tap_trial'] is None
    assert client.post('/control/force-refresh', json={}, headers=h).status_code == 409


def test_dismissal_transaction_rollback(rig):
    store, client, auth = rig
    store.cache_calendar(png(), 900)
    publish_recipe(store)
    fetched(store, client)
    before = deepcopy(store.state)
    store.library.execute("CREATE TRIGGER reject_dismiss BEFORE UPDATE ON publications BEGIN SELECT RAISE(ABORT,'failure'); END")
    with pytest.raises(Exception):
        store.content_checkin({'UPDATE_SOURCE': 'EXT0'})
    assert store.state == before
    persisted = json.loads(store.library.one('SELECT state_json FROM device_state WHERE id=1')['state_json'])
    assert persisted == before


def test_actual_phone_send_receipt_replay_and_calendar_contract(tmp_path):
    h = Harness(tmp_path)
    try:
        h.service.store.calendar_config = CONFIG
        h.pair()
        h.service.store.cache_calendar(png(), 900)
        _, recipe = h.ready()
        body = {'render_set_id': recipe['processing']['current_render_set_id'], 'page_number': 1}
        idem = key()
        receipt = h.post('/api/v1/publications', body, 'createPublication', 201, idem)
        fetched(h.service.store, h.lan)
        h.lan.get('/api/display', headers={**h.firmware_auth, 'Update-Source': 'EXT0', 'Image-Cached': 'true'})
        state = h.get('/api/v1/device', 'getDevice')
        assert state['calendar']['content_mode'] == 'calendar'
        assert state['selected_publication']['kind'] == 'legacy'
        assert h.post('/api/v1/publications', body, 'createPublication', 201, idem) == receipt
        assert h.get('/api/v1/device', 'getDevice')['calendar']['content_mode'] == 'calendar'
        assert SECRET not in (h.db.directory/'events.jsonl').read_text()
    finally:
        h.web.close(); h.lan.close()


def test_migrated_recipe_gets_delivery_identity(tmp_path):
    _, _, service, _ = create_apps(tmp_path, LAN, config=ProcessingConfig())
    publish_recipe(service.store)
    old = service.store.state['current']['filename']
    _, _, restored, _ = create_apps(tmp_path, LAN, config=ProcessingConfig(), calendar_config=CONFIG)
    assert restored.store.state['current']['filename'] != old
    assert restored.store.state['recipe_session']['fetched_at'] is None
    assert restored.store.state['current']['delivery_id']


def test_cloud_client_normalizes_image_and_never_forwards_cloud_controls():
    calls = []
    def fetch(url, **kwargs):
        calls.append((url, kwargs))
        if len(calls) == 1:
            return json.dumps({'status': 0, 'image_url': 'https://cdn.example.com/calendar.png',
                'filename': 'calendar-v1', 'refresh_rate': 1800, 'update_firmware': True,
                'firmware_url': 'https://cdn.example.com/fw.bin', 'special_function': 'sleep'}).encode()
        assert 'headers' not in kwargs
        return png()
    data, delay = CalendarClient(CONFIG, fetch).fetch_screen()
    assert delay == 1800
    assert calls[0][1]['headers']['Access-Token'] == SECRET
    with Image.open(BytesIO(data)) as im:
        assert im.mode == '1' and im.size == (1872, 1404)


@pytest.mark.parametrize('metadata', [
    {'status': 500, 'reset_firmware': True},
    {'status': 202},
    {'status': 0, 'filename': 'x', 'image_url': 'http://127.0.0.1/secret'},
    {'status': 0, 'filename': 'x', 'image_url': 'https://example.com/a', 'refresh_rate': -1},
])
def test_invalid_cloud_response_rejected(metadata):
    with pytest.raises(CalendarError):
        CalendarClient(CONFIG, lambda *a, **k: json.dumps(metadata).encode()).fetch_screen()


def test_invalid_image_and_worker_backoff(rig):
    store, _, _ = rig
    class FailingClient:
        def fetch_screen(self):
            raise RuntimeError(SECRET + ' sensitive signed URL')
    worker = CalendarWorker(store, CONFIG, FailingClient())
    assert worker.run_once() == 300
    assert worker.run_once() == 600
    assert store.calendar_status()['last_error'] == 'calendar_sync_failed'
    assert SECRET not in json.dumps(store.status())
    assert SECRET not in store.events_path.read_text()
    from server.calendar import normalize_image
    for data in (b'not an image', png(size=(800, 480))):
        with pytest.raises(CalendarError): normalize_image(data)


def test_background_download_cannot_overwrite_concurrent_phone_send(rig):
    store, _, _ = rig
    class InterleavedClient:
        def fetch_screen(self):
            publish_recipe(store)
            return png(), 900
    worker = CalendarWorker(store, CONFIG, InterleavedClient())
    assert worker.run_once() == 900
    assert store.calendar_status()['available']
    assert store.calendar_status()['content_mode'] == 'recipe'


def test_download_rejects_credential_redirects_and_bounds_body():
    class Reply:
        status = 302
        def getheader(self, name, default=None): return {'Location': 'https://evil.example/key'}.get(name, default)
        def close(self): pass
    class Connection:
        sock = None
        def __init__(self, *a): pass
        def request(self, *a, **k): pass
        def getresponse(self): return Reply()
        def close(self): pass
    with pytest.raises(CalendarError, match='cloud_redirect_rejected'):
        download('https://trmnl.com/api/display', headers={'Access-Token': SECRET}, limit=65536,
                 resolver=lambda *a: ['1.1.1.1'], connection_factory=Connection)
    Reply.status = 200
    Reply.getheader = lambda self, name, default=None: {'Content-Length': '999999'}.get(name, default)
    with pytest.raises(CalendarError, match='cloud_response_too_large'):
        download('https://cdn.example.com/a.png', limit=100,
                 resolver=lambda *a: ['1.1.1.1'], connection_factory=Connection)


def test_battery_reports_both_readings_without_inventing_conversion(rig):
    store, client, auth = rig
    client.get('/api/display', headers={**auth, 'Percent-Charged': '40', 'Gauge-SOC': '2'})
    status = store.status()
    assert status['battery_percent'] == 2
    assert status['battery_firmware_percent'] == 40
    assert status['battery_source'] == 'GAUGE_SOC'
    assert status['battery_gauge_percent'] == 2
    client.get('/api/display', headers={**auth, 'Percent-Charged': '-1', 'Gauge-SOC': '-1'})
    assert store.status()['battery_percent'] is None
    assert store.status()['battery_gauge_percent'] is None


def test_battery_fallback_does_not_mix_trend_sources(rig):
    store, client, auth = rig
    store.state['unplugged_at'] = datetime.now(timezone.utc).isoformat()
    store.state['charge_samples'] = [{'at': '2026-01-01T00:00:00+00:00', 'percent': 60}]
    client.get('/api/display', headers={**auth, 'Model': 'x', 'Percent-Charged': '40', 'Gauge-SOC': '2'})
    assert len(store.state['charge_samples']) == 1
    assert store.state['charge_samples'][0]['source'] == 'GAUGE_SOC'
    assert store.status()['charge_trend'] is None
    client.get('/api/display', headers={**auth, 'Model': 'x', 'Percent-Charged': '40', 'Gauge-SOC': '-1'})
    assert store.status()['battery_percent'] == 40
    assert store.status()['battery_source'] == 'PERCENT_CHARGED'
    assert len(store.state['charge_samples']) == 1
    assert store.state['charge_samples'][0]['source'] == 'PERCENT_CHARGED'
