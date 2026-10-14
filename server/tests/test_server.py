import json
from io import BytesIO
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from server.app import create_app, Store, number
from server.report import summary

MAC = "AA:BB:CC:DD:EE:01"
CTRL = {"X-eRecipe-Control": "1"}
BASE = "http://192.168.1.50:8000"

@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path, BASE)) as c:
        yield c

def pair(c):
    r = c.get("/api/setup", headers={"ID": MAC, "FW-Version": "1.8.16"})
    assert r.status_code == 200
    assert r.json()["status"] == 200
    return {"ID": MAC, "Access-Token": r.json()["api_key"]}

def control(c, path, body=None):
    return c.post("/control/" + path, json=body or {}, headers=CTRL)

def test_protocol_and_stable_asset(client):
    auth = pair(client)
    first = client.get("/api/display", headers=auth).json()
    second = client.get("/api/display", headers=auth).json()
    assert first == second
    assert first["status"] == 0 and first["refresh_rate"] == 20
    assert first["reset_firmware"] is False and first["update_firmware"] is False
    assert first["image_url"].startswith(BASE)
    image = client.get("/images/" + first["filename"])
    assert image.headers["content-type"] == "image/png"
    assert int(image.headers["content-length"]) == len(image.content)
    with Image.open(BytesIO(image.content)) as im:
        assert im.size == (1872, 1404) and im.mode == "1"
    assert client.post("/api/log", headers=auth, json={"logs": []}).status_code == 204

def test_auth_setup_and_header_variants(client):
    auth = pair(client)
    assert client.get("/api/display").status_code == 401
    assert client.get("/api/setup", headers={"ID": "AA:BB:CC:DD:EE:02"}).status_code == 403
    assert client.get("/api/setup", headers={"ID": "bad"}).status_code == 400
    assert pair(client) == auth
    response = client.get("/api/display", headers={"access_token": auth["Access-Token"],
        "percent_charged": "85.7", "battery_charging": "0", "usb_connected": "false",
        "wake_time": "1250", "fw_version": "1.8.16", "rssi": "-44"})
    assert response.status_code == 200
    s = client.get("/control/status").json()
    assert s["battery_percent"] == 85.7
    assert s["telemetry"]["WAKE_TIME"] == "1250"
    assert "api_key" not in s

def test_cached_fresh_and_restart(client, tmp_path):
    auth = pair(client)
    first = client.get("/api/display", headers=auth).json()
    control(client, "publish", {"fixture": "B"})
    b = client.get("/api/display", headers=auth).json()
    assert b["filename"] != first["filename"]
    control(client, "publish", {"fixture": "A"})
    assert client.get("/api/display", headers=auth).json()["filename"] == first["filename"]
    control(client, "publish", {"fixture": "A", "fresh": True})
    fresh = client.get("/api/display", headers=auth).json()
    assert fresh["filename"] != first["filename"]
    with TestClient(create_app(tmp_path, BASE)) as restored:
        assert pair(restored) == auth
        assert restored.get("/api/display", headers=auth).json() == fresh

def test_no_render_on_normal_poll_and_preview_not_fetch(client):
    auth = pair(client)
    with patch("server.app.render", side_effect=AssertionError("poll rendered")):
        for _ in range(4):
            assert client.get("/api/display", headers=auth).status_code == 200
    client.get("/control/preview")
    assert client.get("/control/status").json()["last_fetch"] is None

def test_snapshot_and_stress(client):
    auth = pair(client)
    client.get("/api/display", headers=auth)
    assert control(client, "snapshot").status_code == 200
    a = client.get("/api/display", headers=auth).json()
    assert a == client.get("/api/display", headers=auth).json()
    control(client, "settings", {"mode": "stress", "poll_seconds": 10})
    before = client.get("/control/status").json()["current"]["filename"]
    first = client.get("/api/display", headers=auth).json()
    # Background render prepares the next cycle, never the current response.
    assert first["filename"] == before
    second = client.get("/api/display", headers=auth).json()
    assert second["filename"] != first["filename"]
    assert second["refresh_rate"] == 10
    control(client, "publish", {"fixture": "B"})
    assert client.get("/control/status").json()["mode"] == "soak"

def test_render_failure_retains_selected_image(client):
    pair(client)
    before = client.get("/control/status").json()["current"]
    with patch("server.app.render", side_effect=ValueError("overflow")):
        with pytest.raises(ValueError):
            control(client, "publish", {"fixture": "A", "fresh": True})
    assert client.get("/control/status").json()["current"] == before

def test_diagnostics_redact_secrets_and_log_validation(client):
    auth = pair(client)
    key = auth["Access-Token"]
    assert client.post("/api/log", headers=auth, json={"logs": [
        {"message": f"secret {key} password=hunter2", "api_key": "another-secret", "level": "error"}]}).status_code == 204
    log = client.app.state.store.events_path.read_text()
    assert key not in log and "hunter2" not in log and "another-secret" not in log
    assert "REDACTED" in log
    assert client.post("/api/log", headers=auth, content="bad").status_code == 400
    assert client.post("/api/log", headers=auth, json={"logs": {}}).status_code == 400
    assert client.post("/api/log", headers=auth, content=b"x" * 262145).status_code == 413

def test_control_boundary(client):
    assert client.post("/control/publish", json={"fixture": "B"}).status_code == 403
    assert client.post("/control/publish", headers={**CTRL, "Origin": "https://evil.example"},
                       json={"fixture": "B"}).status_code == 403
    assert client.get("/", headers={"Host": "evil.example"}).status_code == 403
    with TestClient(client.app, client=("192.168.1.90", 1234)) as remote:
        assert remote.get("/control/status").status_code == 403
        assert remote.get("/health").status_code == 200

def test_measurements_and_confirmation(client):
    auth = pair(client)
    assert client.get("/control/status").json()["battery_percent"] is None
    control(client, "unplugged")
    client.get("/api/display", headers={**auth, "Percent-Charged": "95", "USB-Connected": "false"})
    s = client.get("/control/status").json()
    assert s["unplugged_at"] and s["charge_samples"][0]["percent"] == 95
    client.get("/api/display", headers={**auth, "Percent-Charged": "-1", "USB-Connected": "true"})
    s = client.get("/control/status").json()
    assert s["unplugged_at"] is None and s["battery_percent"] is None
    pub = control(client, "publish", {"fixture": "B"}).json()
    assert control(client, "confirm", {"publication_id": pub["id"], "observed_at": "2020-01-01"}).status_code == 400
    assert control(client, "confirm", {"publication_id": pub["id"]}).status_code == 200
    assert control(client, "confirm", {"publication_id": pub["id"]}).status_code == 409
    report = summary([json.loads(x) for x in client.app.state.store.events_path.read_text().splitlines()])
    assert report[0]["observations"] == 1
    assert report[0]["latency_seconds"]["max"] >= 0

def test_settings_run_and_sentinel_handling(client):
    auth = pair(client)
    control(client, "unplugged")
    assert control(client, "settings", {"mode": "stress", "poll_seconds": 0}).status_code == 422
    control(client, "settings", {"mode": "soak", "poll_seconds": 30})
    assert client.get("/control/status").json()["unplugged_at"] is None
    before = client.get("/control/status").json()
    after = control(client, "run").json()
    assert after["run_id"] != before["run_id"]
    assert after["current"] == before["current"] and after["checkins"] == 0
    assert number("-1", 0, 100) is None and number("nan", 0, 100) is None
    assert number("inf") is None
    assert pair(client) == auth

def test_charge_projection_requires_window(client):
    store = client.app.state.store
    t = datetime.now(timezone.utc)
    store.state["charge_samples"] = [{"at": (t-timedelta(hours=3)).isoformat(), "percent": 100},
                                    {"at": t.isoformat(), "percent": 97}]
    trend = store.status()["charge_trend"]
    assert trend["window_hours"] == 3 and trend["estimated_remaining_hours"] == 97

@pytest.mark.parametrize("bad", ["http://localhost:8000", "http://0.0.0.0:8000", "file:///tmp", "http://user:pass@host", "http://host/path"])
def test_bad_advertised_urls(tmp_path, bad):
    with pytest.raises(ValueError):
        Store(tmp_path, bad)

def test_fixtures_are_distinct_and_one_bit():
    from server.app import ROOT
    from server.render import render
    images = [render(k, "test-v1") for k in ("A", "B", "LONG")]
    assert len(set(images)) == 3
    for kind in ("A", "B", "LONG"):
        with Image.open(ROOT / "fixtures" / f"{kind}.png") as im:
            assert im.mode == "1" and im.size == (1872, 1404)

def test_implicit_mode_changes_end_soak(client):
    auth = pair(client)
    client.get("/api/display", headers=auth)
    last = client.get("/control/status").json()["last_checkin"]
    control(client, "unplugged")
    control(client, "snapshot")
    s = client.get("/control/status").json()
    assert s["unplugged_at"] is None and s["last_checkin"] == last
    control(client, "settings", {"mode": "stress", "poll_seconds": 20})
    control(client, "unplugged")
    control(client, "publish", {"fixture": "B"})
    assert client.get("/control/status").json()["unplugged_at"] is None

def test_stale_background_render_cannot_overwrite_new_publication(client):
    store = client.app.state.store
    from server.render import render as real_render
    control(client, "settings", {"mode": "stress", "poll_seconds": 20})
    def render_with_interleaved_publish(*args):
        control(client, "publish", {"fixture": "B"})
        return real_render(*args)
    with patch("server.app.render", side_effect=render_with_interleaved_publish):
        store.diagnostic(stress=True)
    assert store.status()["current"]["kind"] == "B"

def test_confirmation_keeps_original_experiment_group(client):
    pub = control(client, "publish", {"fixture": "B"}).json()
    control(client, "settings", {"mode": "stress", "poll_seconds": 10})
    confirmation = control(client, "confirm", {"publication_id": pub["id"]}).json()
    assert confirmation["mode"] == "soak" and confirmation["poll_seconds"] == 20
    rows = summary([json.loads(x) for x in client.app.state.store.events_path.read_text().splitlines()])
    soak = next(x for x in rows if x["mode"] == "soak")
    assert soak["observations"] == 1 and soak["pending_observations"] == []


def test_allowlist_can_be_applied_before_first_pairing(tmp_path):
    create_app(tmp_path, BASE)
    with TestClient(create_app(tmp_path, BASE, device_id=MAC)) as c:
        assert c.get("/api/setup", headers={"ID": "AA:BB:CC:DD:EE:02"}).status_code == 403
        pair(c)


@pytest.mark.parametrize("kind", ["A", "B", "LONG", "DIAG"])
def test_force_refresh_changes_cached_image_without_changing_experiment(client, tmp_path, kind):
    auth = pair(client)
    if kind == "DIAG":
        control(client, "snapshot")
    else:
        control(client, "publish", {"fixture": kind})
    control(client, "settings", {"mode": "snapshot", "poll_seconds": 30})
    control(client, "unplugged")
    before = client.get("/control/status").json()
    original = client.get("/control/preview").content
    response = control(client, "force-refresh")
    assert response.status_code == 200 and response.json()["changed"]
    after = client.get("/control/status").json()
    assert after["current"]["kind"] == kind
    assert after["current"]["version"] != before["current"]["version"]
    assert client.get("/control/preview").content != original
    for key in ("mode", "poll_seconds", "run_id", "unplugged_at"):
        assert after[key] == before[key]
    assert after["updates"] == before["updates"] + 1
    display = client.get("/api/display", headers=auth).json()
    assert display["filename"] == after["current"]["filename"]
    assert client.get("/api/display", headers=auth).json() == display
    assert client.get("/images/" + display["filename"]).status_code == 200
    with TestClient(create_app(tmp_path, BASE)) as restored:
        assert restored.get("/api/display", headers=auth).json() == display
    events = [json.loads(line) for line in client.app.state.store.events_path.read_text().splitlines()]
    assert any(e.get("reason") == "force-refresh" and e["version"] == after["current"]["version"] for e in events)


def test_force_refresh_failure_retains_current_image(client):
    before = client.get("/control/status").json()["current"]
    with patch("server.app.render", side_effect=ValueError("overflow")):
        with pytest.raises(ValueError):
            control(client, "force-refresh")
    assert client.get("/control/status").json()["current"] == before
    assert client.post("/control/force-refresh").status_code == 403


def test_force_refresh_cannot_overwrite_concurrent_selection(client):
    from server.render import render as real_render
    def interleaved_render(*args):
        control(client, "publish", {"fixture": "B"})
        return real_render(*args)
    with patch("server.app.render", side_effect=interleaved_render):
        assert control(client, "force-refresh").status_code == 409
    assert client.get("/control/status").json()["current"]["kind"] == "B"
