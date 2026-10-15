"""Nighttime schedule boundaries, firmware replies, and durable settings."""
from datetime import datetime
from unittest.mock import patch

from fastapi.testclient import TestClient
import pytest

from server.app import create_app
from server.application import create_apps
from server.polling import NighttimePolling, polling_status
from server.recipes.config import ProcessingConfig
from server.tests.test_server import BASE, CTRL, pair

NIGHT = dict(enabled=True, start='22:00', end='07:00', poll_seconds=900, timezone='UTC')


@pytest.mark.parametrize('stamp,active,interval', [
    ('2026-10-14T12:00:00+00:00', False, 20),
    ('2026-10-14T21:59:50+00:00', False, 10),
    ('2026-10-14T22:00:00+00:00', True, 900),
    ('2026-10-14T23:59:59+00:00', True, 900),
    ('2026-10-15T00:00:00+00:00', True, 900),
    ('2026-10-15T06:59:30+00:00', True, 30),
    ('2026-10-15T06:59:59.500+00:00', True, 5),
    ('2026-10-15T07:00:00+00:00', False, 20),
])
def test_overnight_schedule(stamp, active, interval):
    result = polling_status(dict(poll_seconds=20, nighttime=NIGHT), datetime.fromisoformat(stamp))
    assert result == dict(nighttime_active=active, effective_poll_seconds=interval)


@pytest.mark.parametrize('stamp,active,interval', [
    ('2026-10-14T08:59:50+00:00', False, 10),
    ('2026-10-14T09:00:00+00:00', True, 900),
    ('2026-10-14T16:59:30+00:00', True, 30),
    ('2026-10-14T17:00:00+00:00', False, 20),
])
def test_same_day_window(stamp, active, interval):
    state = dict(poll_seconds=20, nighttime={**NIGHT, 'start':'09:00', 'end':'17:00'})
    assert polling_status(state, datetime.fromisoformat(stamp)) == dict(nighttime_active=active, effective_poll_seconds=interval)


@pytest.mark.parametrize('stamp,end,active,interval', [
    ('2026-10-15T05:00:00+00:00', '07:00', True, 900),  # 10 PM Pacific, previous date.
    ('2026-10-15T13:59:30+00:00', '07:00', True, 30),
    ('2026-10-15T14:00:00+00:00', '07:00', False, 20),
    ('2026-03-08T09:59:30+00:00', '02:30', True, 30),  # Spring skips 2:30; end at 3 AM.
    ('2026-03-08T10:00:00+00:00', '02:30', False, 20),
    ('2026-11-01T08:59:30+00:00', '02:30', True, 900),  # Fall repeats 1 AM; remain asleep.
    ('2026-11-01T09:59:30+00:00', '02:30', True, 900),
    ('2026-11-01T10:29:30+00:00', '02:30', True, 30),
    ('2026-11-01T10:30:00+00:00', '02:30', False, 20),
])
def test_time_zone_and_daylight_saving(stamp, end, active, interval):
    state = dict(poll_seconds=20, nighttime={**NIGHT, 'timezone':'America/Los_Angeles', 'end':end})
    assert polling_status(state, datetime.fromisoformat(stamp)) == dict(nighttime_active=active, effective_poll_seconds=interval)


def test_disabled_schedule_keeps_normal_interval():
    state = dict(poll_seconds=45, nighttime={**NIGHT, 'enabled':False})
    assert polling_status(state, datetime.fromisoformat('2026-10-14T23:00:00+00:00')) == dict(nighttime_active=False, effective_poll_seconds=45)
    assert NighttimePolling().enabled is False


def test_firmware_schedule_and_existing_settings_clients(tmp_path):
    with TestClient(create_app(tmp_path, BASE)) as client:
        auth = pair(client)
        before = client.get('/control/status').json()
        assert not before['nighttime']['enabled']
        response = client.post('/control/settings', headers=CTRL, json=dict(mode='soak', poll_seconds=20, nighttime=NIGHT))
        assert response.status_code == 200, response.text
        for stamp, interval in [('2026-10-14T23:00:00+00:00',900), ('2026-10-15T06:59:30+00:00',30), ('2026-10-15T07:00:00+00:00',20)]:
            with patch('server.app.now', return_value=stamp):
                display = client.get('/api/display', headers=auth).json()
                status = client.get('/control/status').json()
            assert display['refresh_rate'] == interval
            assert display['filename'] == before['current']['filename']
            assert status['effective_poll_seconds'] == status['last_requested_poll_seconds'] == interval
            assert status['poll_seconds'] == 20
            assert status['publication_id'] == before['publication_id']
        # Legacy requests must not erase the saved schedule.
        assert client.post('/control/settings', headers=CTRL, json=dict(mode='soak', poll_seconds=30)).json()['nighttime'] == NIGHT
    with TestClient(create_app(tmp_path, BASE)) as restarted:
        status = restarted.get('/control/status').json()
        assert status['nighttime'] == NIGHT and status['poll_seconds'] == 30
        assert status['last_requested_poll_seconds'] == 20


def test_integrated_settings_persist_and_disabled_schedule_retains_hours(tmp_path):
    lan, _, _, _ = create_apps(tmp_path, BASE, config=ProcessingConfig())
    with TestClient(lan) as client:
        response = client.post('/control/settings', headers=CTRL, json=dict(mode='soak', poll_seconds=60, nighttime=NIGHT))
        assert response.status_code == 200
    lan, _, _, _ = create_apps(tmp_path, BASE, config=ProcessingConfig())
    with TestClient(lan) as restarted:
        assert restarted.get('/control/status').json()['nighttime'] == NIGHT
        response = restarted.post('/control/settings', headers=CTRL, json=dict(mode='soak', poll_seconds=60, nighttime={**NIGHT, 'enabled':False}))
        assert response.json()['nighttime'] == {**NIGHT, 'enabled':False}
        assert response.json()['effective_poll_seconds'] == 60


@pytest.mark.parametrize('changed', [
    {'start':'24:00'}, {'end':'7:00'}, {'start':'22:00:00'}, {'end':'22:00'},
    {'timezone':'Not/AZone'}, {'timezone':'../UTC'}, {'poll_seconds':0},
    {'poll_seconds':3601}, {'poll_seconds':10}, {'poll_seconds':900.5}, {'enabled':'true'},
])
def test_invalid_settings_do_not_replace_schedule(tmp_path, changed):
    with TestClient(create_app(tmp_path, BASE)) as client:
        before = client.get('/control/status').json()['nighttime']
        response = client.post('/control/settings', headers=CTRL, json=dict(mode='soak', poll_seconds=20, nighttime={**NIGHT, **changed}))
        assert response.status_code == 422, response.text
        assert client.get('/control/status').json()['nighttime'] == before


def test_raising_normal_interval_validates_saved_nighttime_and_controls(tmp_path):
    with TestClient(create_app(tmp_path, BASE)) as client:
        body = dict(mode='soak', poll_seconds=20, nighttime=NIGHT)
        assert client.post('/control/settings', json=body).status_code == 403
        assert client.post('/control/settings', json=body, headers={**CTRL, 'Origin':'https://evil.invalid'}).status_code == 403
        assert client.post('/control/settings', json=body, headers=CTRL).status_code == 200
        assert client.post('/control/settings', json=dict(mode='soak', poll_seconds=1000), headers=CTRL).status_code == 422
        assert client.get('/control/status').json()['poll_seconds'] == 20
