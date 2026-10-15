"""Single-device BYOS experiment. One process; device never triggers synchronous rendering."""
from __future__ import annotations
from copy import deepcopy
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Literal
from urllib.parse import urlsplit
import hashlib
import json
import math
import os
import re
import secrets
import time
import uuid

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from server.render import render, render_tap_trial

# Official firmware maps GPIO/EXT0/EXT1 to these values. This is a wake
# reason, not a gesture ID: hardware verification remains part of the trial.
TAP_WAKE_SOURCES = {"button", "ext0", "ext1"}
TAP_DEBOUNCE_SECONDS = 3

ROOT = Path(__file__).resolve().parent
TELEMETRY = set("""PERCENT_CHARGED BATTERY_VOLTAGE BATTERY_CHARGING USB_CONNECTED BATTERY_COUNT
BATTERY_CAPACITY BATTERY_CURRENT BATTERY_HEALTH BATTERY_TEMP RSSI FW_VERSION REFRESH_RATE
WAKE_TIME UPDATE_SOURCE WIDTH HEIGHT MODEL IMAGE_CACHED TEMPERATURE_PROFILE FW_COMMIT WIFI_BAND PANEL_REV GAUGE_SOC GAUGE_HEALTH GAUGE_CAPACITY""".split())

def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")

def seconds(a, b):
    return (datetime.fromisoformat(a) - datetime.fromisoformat(b)).total_seconds()

def headers(request):
    return {k.upper().replace("-", "_"): v for k, v in request.headers.items()}

def number(value, low=None, high=None):
    try:
        n = float(value)
        return n if math.isfinite(n) and (low is None or n >= low) and (high is None or n <= high) else None
    except (ValueError, TypeError):
        return None

def battery_reading(values):
    gauge = number(values.get('GAUGE_SOC'), 0, 100)
    firmware = number(values.get('PERCENT_CHARGED'), 0, 100)
    if str(values.get('MODEL', 'x')).lower() == 'x' and gauge is not None:
        return gauge, 'GAUGE_SOC'
    return (firmware, 'PERCENT_CHARGED') if firmware is not None else (None, 'unavailable')

def atomic(path, data):
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temp.open("xb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)

class Store:
    def __init__(self, directory, base_url, interval=20, device_id=None, state_backend=None):
        self.state_backend = state_backend
        url = urlsplit(base_url)
        if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password or url.query or url.fragment or url.path not in ("", "/"):
            raise ValueError("base-url must be an absolute http(s) origin without a path or credentials")
        if url.hostname in ("localhost", "127.0.0.1", "::1", "0.0.0.0", "::"):
            raise ValueError("Advertise a LAN address reachable by the X, not localhost or a wildcard")
        if device_id and not re.fullmatch(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", device_id):
            raise ValueError("device-id must be a colon-separated MAC address")
        if not 5 <= interval <= 3600:
            raise ValueError("poll-seconds must be between 5 and 3600")
        self.base_url, self.directory = base_url.rstrip("/"), Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.assets = self.directory / "images"
        self.assets.mkdir(exist_ok=True)
        self.lock = RLock()
        self.state_path = self.directory / "state.json"
        self.events_path = self.directory / "events.jsonl"
        self.fixtures = {}
        for kind in ("A", "B", "LONG"):
            data = (ROOT / "fixtures" / f"{kind}.png").read_bytes()
            self.fixtures[kind] = self.asset(data, kind, f"fixture-{kind}-v1")
        persisted = state_backend.load() if state_backend else json.loads(self.state_path.read_text()) if self.state_path.exists() else None
        if persisted is not None:
            self.state = persisted
            if not (self.assets / self.state["current"]["filename"]).is_file():
                raise ValueError("Persisted image missing; restore data instead of replacing the recipe")
            if device_id and self.state["device_id"] not in (None, device_id.upper()):
                raise ValueError("Configured device differs from persisted device")
        else:
            self.state = {"schema": 1, "device_id": device_id.upper() if device_id else None,
                          "api_key": secrets.token_urlsafe(32), "run_id": uuid.uuid4().hex[:12],
                          "run_started_at": now(), "mode": "soak", "poll_seconds": interval,
                          "current": self.fixtures["A"], "publication_id": uuid.uuid4().hex[:12],
                          "published_at": now(), "updates": 0, "checkins": 0, "last_checkin": None,
                          "last_fetch": None, "interval_baseline": None, "telemetry": {}, "intervals": [], "errors": [],
                          "publications": [], "unplugged_at": None, "charge_samples": []}
        if device_id:
            self.state["device_id"] = device_id.upper()
        self.state.setdefault("tap_trial", None)
        # Starting settings are used only for a new data directory. Controls persist thereafter.
        self.save()
        self.event("server_start", base_url=self.base_url)

    def asset(self, data, kind, label):
        version = hashlib.sha256(data).hexdigest()[:24]
        filename = version + ".png"
        path = self.assets / filename
        if not path.exists():
            atomic(path, data)
        return {"version": version, "filename": filename, "kind": kind, "label": label}

    def save(self):
        if self.state_backend:
            self.state_backend.save(self.state)
            return
        atomic(self.state_path, json.dumps(self.state, indent=2).encode())
        self.state_path.chmod(0o600)

    def redact(self, value):
        if isinstance(value, dict):
            return {k: ("[REDACTED]" if any(s in k.lower().replace("-", "_") for s in ("token", "api_key", "password", "authorization", "secret")) else self.redact(v)) for k, v in value.items()}
        if isinstance(value, list):
            return [self.redact(v) for v in value]
        if isinstance(value, str):
            value = value.replace(self.state["api_key"], "[REDACTED]")
            return re.sub(r'(?i)((?:access[_-]?token|api[_-]?key|password|authorization)\s*[:=]\s*)[^\s,;]+', r'\1[REDACTED]', value)
        return value

    def event(self, kind, **fields):
        with self.lock:
            event = {"at": now(), "event": kind, "run_id": self.state["run_id"],
                     "mode": self.state["mode"], "publication_id": self.state["publication_id"],
                     "version": self.state["current"]["version"], "poll_seconds": self.state["poll_seconds"],
                     **self.redact(fields)}
            with self.events_path.open("a") as out:
                out.write(json.dumps(event) + "\n")
            return event

    def error(self, message):
        with self.lock:
            item = self.event("error", message=message)
            self.state["errors"] = (self.state["errors"] + [item])[-20:]
            self.save()

    def publish(self, asset, reason):
        with self.lock:
            if asset["kind"] != "TAP_TRIAL" and self.state.get("tap_trial"):
                self.set_mode("soak")
            changed = asset["version"] != self.state["current"]["version"]
            self.state.update(current=asset, publication_id=uuid.uuid4().hex[:12], published_at=now())
            self.state["updates"] += int(changed)
            publication = {"id": self.state["publication_id"], "version": asset["version"],
                           "label": asset["label"], "at": self.state["published_at"], "changed": changed,
                           "run_id": self.state["run_id"], "mode": self.state["mode"],
                           "poll_seconds": self.state["poll_seconds"]}
            self.state["publications"] = (self.state["publications"] + [publication])[-100:]
            self.save()
            self.event("publish", reason=reason, changed=changed, label=asset["label"])
            return publication

    def set_mode(self, mode):
        """A workload change ends the battery window, including implicit snapshot/recipe switches."""
        if self.state["mode"] != mode:
            self.event("experiment_boundary", next_mode=mode)
            self.state.update(unplugged_at=None, charge_samples=[], intervals=[], interval_baseline=None)
        self.state["mode"] = mode
        if mode != "tap_trial":
            self.state["tap_trial"] = None

    def start_tap_trial(self):
        # Prepare both images before switching selection; never render on a tap.
        selected = self.status()
        trial_id = uuid.uuid4().hex[:12]
        pages = [self.asset(render_tap_trial(n, trial_id), "TAP_TRIAL",
                            f"Tap trial / page {n} of 2") for n in (1, 2)]
        with self.lock, self.trial_update():
            if any(self.state[k] != selected[k] for k in ("publication_id", "mode", "run_id")):
                raise HTTPException(409, "Selection changed while preparing the trial; try again")
            previous = (selected.get("tap_trial") or {}).get("previous") or {
                "current": selected["current"], "mode": selected["mode"]}
            self.set_mode("tap_trial")
            self.state["tap_trial"] = {"id": trial_id, "pages": pages, "page": 1,
                "previous": previous, "awaiting_fetch": True, "last_advance_at": None,
                "fetched_pages": [], "advances": 0, "last_source": None, "last_action": "waiting_for_fetch"}
            self.publish(pages[0], "tap-trial-start")
            self.event("tap_trial_started", trial_id=trial_id)
        return self.status()

    def stop_tap_trial(self):
        with self.lock, self.trial_update():
            trial = self.state.get("tap_trial")
            if trial:
                previous = trial["previous"]
                self.set_mode(previous["mode"])
                self.publish(previous["current"], "tap-trial-stop")
                self.event("tap_trial_stopped", trial_id=trial["id"])
        return self.status()

    def trial_checkin(self, values):
        """Advance only in the opt-in trial; unknown/timer wakes always hold."""
        if not self.state.get("tap_trial"):
            return
        with self.lock, self.trial_update():
            self._trial_checkin(values)

    def content_checkin(self, values):
        """Integrated device store handles recipe dismissal."""

    def content_fetched(self, filename):
        """Integrated device store records publication-specific fetch evidence."""

    @contextmanager
    def trial_update(self):
        """Keep trial counters and selection together when persistence fails."""
        previous = deepcopy(self.state)
        try:
            yield
        except BaseException:
            self.state = previous
            raise

    def _trial_checkin(self, values):
        trial = self.state.get("tap_trial")
        if not trial or self.state["mode"] != "tap_trial":
            return
        source = str(values.get("UPDATE_SOURCE", "")).strip().lower()
        trial["last_source"] = self.redact(source) or None
        if source not in TAP_WAKE_SOURCES:
            action = "held_timer" if source == "timer" else "held_unknown_source"
        elif trial["awaiting_fetch"]:
            action = "waiting_for_fetch"
        elif trial["last_advance_at"] and seconds(now(), trial["last_advance_at"]) < TAP_DEBOUNCE_SECONDS:
            action = "held_debounce"
        else:
            trial["page"] = 3 - trial["page"]
            trial["advances"] += 1
            # A previously fetched page can be served from firmware cache with
            # no new image GET. Only wait for the first fetch of each page.
            trial["awaiting_fetch"] = trial["page"] not in trial["fetched_pages"]
            trial["last_advance_at"] = now()
            action = "advanced"
        trial["last_action"] = action
        if action == "advanced":
            self.publish(trial["pages"][trial["page"] - 1], "tap-trial-advance")
        else:
            self.save()
        # IMAGE_CACHED is also true on observed X center taps (FW 1.8.17).
        # Retain it as telemetry, not as evidence of a left/right gesture.
        self.event("tap_trial_input", trial_id=trial["id"], source=trial["last_source"],
                   image_cached=values.get("IMAGE_CACHED"),
                   action=action, page=trial["page"], advances=trial["advances"])

    def checkin(self, values):
        with self.lock:
            stamp = now()
            previous = self.state.get("interval_baseline")
            if previous:
                self.state["intervals"] = (self.state["intervals"] + [seconds(stamp, previous)])[-300:]
            self.state["last_checkin"] = stamp
            self.state["interval_baseline"] = stamp
            self.state["checkins"] += 1
            self.state["telemetry"] = {k: self.redact(v) for k, v in values.items() if k in TELEMETRY}
            charge, charge_source = battery_reading(values)
            if self.state["unplugged_at"]:
                if str(values.get("USB_CONNECTED", "")).lower() in ("1", "true") or str(values.get("BATTERY_CHARGING", "")).lower() in ("1", "true"):
                    self.event("soak_interrupted", reason="Device reported external power or charging")
                    self.state["unplugged_at"] = None
                    self.state["charge_samples"] = []
                elif charge is not None:
                    samples = self.state["charge_samples"]
                    if samples and samples[-1].get('source', 'PERCENT_CHARGED') != charge_source:
                        samples = []
                        self.state['charge_samples'] = samples
                    if not samples or seconds(stamp, samples[-1]["at"]) >= 300:
                        samples.append({"at": stamp, "percent": charge, "source": charge_source})
                        self.state["charge_samples"] = samples[-1000:]
            self.save()
            self.event("checkin", telemetry=self.state["telemetry"],
                       observed_interval_seconds=seconds(stamp, previous) if previous else None)

    def status(self):
        with self.lock:
            s = deepcopy(self.state)
        s.pop("api_key")
        s["base_url"] = self.base_url
        s["image_url"] = self.base_url + "/images/" + s["current"]["filename"]
        intervals = s["intervals"]
        s["observed_interval_seconds"] = intervals[-1] if intervals else None
        s["battery_percent"], s["battery_source"] = battery_reading(s['telemetry'])
        s["battery_firmware_percent"] = number(s["telemetry"].get("PERCENT_CHARGED"), 0, 100)
        s["battery_gauge_percent"] = number(s["telemetry"].get("GAUGE_SOC"), 0, 100)
        s["sample_age_seconds"] = seconds(now(), s["last_checkin"]) if s["last_checkin"] else None
        s["unplugged_elapsed_hours"] = seconds(now(), s["unplugged_at"]) / 3600 if s["unplugged_at"] else None
        samples = s["charge_samples"]
        s["charge_trend"] = None
        if len(samples) >= 2 and all(sample.get('source', 'PERCENT_CHARGED') == s['battery_source'] for sample in samples):
            hours = seconds(samples[-1]["at"], samples[0]["at"]) / 3600
            drop = samples[0]["percent"] - samples[-1]["percent"]
            s["charge_trend"] = {"window_hours": hours, "drop_percentage_points": drop,
                "estimated_remaining_hours": samples[-1]["percent"] * hours / drop if hours >= 2 and drop > 0 else None,
                "basis": "First-to-last " + s['battery_source'] + ", user-marked unplugged window; estimate only"}
        return s

    def diagnostic(self, stress=False, reason=None, selected=None):
        # Render outside the state lock and outside the device response path.
        s = selected if selected is not None else self.status()
        label = ("stress-" if stress else "snapshot-") + uuid.uuid4().hex[:10]
        t = s["telemetry"]
        fields = [("Mode", "REFRESH STRESS - extra redraw each cycle" if stress else "Diagnostic snapshot - explicit redraw"),
                  ("Generated UTC", now()), ("Telemetry sample UTC", s["last_checkin"] or "unavailable"),
                  ("Sample age at generation", f'{s["sample_age_seconds"]:.1f} seconds' if s["sample_age_seconds"] is not None else "unavailable"),
                  ("Battery / charging / USB", f'{s["battery_percent"] if s["battery_percent"] is not None else "unavailable"} % / {t.get("BATTERY_CHARGING", "unavailable")} / {t.get("USB_CONNECTED", "unavailable")}'),
                  ("Requested / observed interval", f'{s["poll_seconds"]} / {s["observed_interval_seconds"] if s["observed_interval_seconds"] is not None else "unavailable"} seconds'),
                  ("RSSI / previous wake", f'{t.get("RSSI", "unavailable")} dBm / {t.get("WAKE_TIME", "unavailable")} raw'),
                  ("Run / firmware / updates", f'{s["run_id"]} / {t.get("FW_VERSION", "unavailable")} / {s["updates"]}'),
                  ("Prior image published UTC", s["published_at"]),
                  ("Physical refresh completion", "Requires visual observation; not measured by server")]
        data = render("DIAG", label, fields)
        with self.lock:
            if self.state["run_id"] != s["run_id"] or self.state["publication_id"] != s["publication_id"] or self.state["mode"] != s["mode"] or (stress and self.state["mode"] != "stress"):
                return
            asset = self.asset(data, "DIAG", label)
            return self.publish(asset, reason or ("stress" if stress else "snapshot"))

class Publish(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fixture: Literal["A", "B", "LONG"]
    fresh: bool = False

class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["soak", "snapshot", "stress", "tap_trial"]
    poll_seconds: int = Field(ge=5, le=3600)

class Confirmation(BaseModel):
    publication_id: str
    observed_at: str | None = None
    note: str = Field(default="", max_length=1000)

def create_app(directory, base_url, interval=20, device_id=None, *, store=None, control_guard=None):
    store = store or Store(directory, base_url, interval, device_id)
    app = FastAPI(title="eRecipe BYOS stage 1")
    app.state.store = store

    @app.middleware("http")
    async def audit(request, call_next):
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception as error:
            store.error(f"{request.method} {request.url.path}: {type(error).__name__}")
            raise
        response.headers.setdefault("Cache-Control", "no-store" if not request.url.path.startswith("/images/") else "public, max-age=31536000, immutable")
        if response.status_code >= 400:
            store.error(f"{request.method} {request.url.path}: HTTP {response.status_code}")
        if request.url.path.startswith("/api/"):
            store.event("request", method=request.method, path=request.url.path,
                        status=response.status_code, duration_ms=round((time.perf_counter() - start) * 1000, 3))
        return response

    def local(request: Request):
        if control_guard:
            return control_guard(request)
        # Browser controls are only available on the server computer. Prevent DNS rebinding/CSRF.
        peer = request.client.host if request.client else ""
        host = urlsplit("http://" + request.headers.get("host", "")).hostname
        if peer not in ("127.0.0.1", "::1", "testclient") or host not in ("127.0.0.1", "localhost", "::1", "testserver"):
            raise HTTPException(403, "Open controls at http://localhost:8000 on the server computer")
        origin = request.headers.get("origin")
        if origin and origin != str(request.base_url).rstrip("/"):
            raise HTTPException(403, "Cross-origin controls are disabled")
        if request.method == "POST" and request.headers.get("x-erecipe-control") != "1":
            raise HTTPException(403, "Missing local control header")

    def authenticate(request: Request):
        values = headers(request)
        token = values.get("ACCESS_TOKEN", "")
        if not secrets.compare_digest(token, store.state["api_key"]) or not store.state["device_id"]:
            raise HTTPException(401, "Invalid device credential")
        if values.get("ID") and values["ID"].upper() != store.state["device_id"]:
            raise HTTPException(403, "Different device")
        return values

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/api/setup")
    def setup(request: Request):
        values = headers(request)
        device = values.get("ID", "").upper()
        if not re.fullmatch(r"(?:[0-9A-F]{2}:){5}[0-9A-F]{2}", device):
            raise HTTPException(400, "ID must be a MAC address")
        with store.lock:
            if store.state["device_id"] not in (None, device):
                raise HTTPException(403, "This server is paired with another device")
            store.state["device_id"] = device
            store.save()
            store.event("setup", firmware=values.get("FW_VERSION"), model=values.get("MODEL"))
            return {"status": 200, "api_key": store.state["api_key"], "friendly_id": "EREC01",
                    "image_url": store.base_url + "/images/" + store.state["current"]["filename"],
                    "message": "eRecipe X battery experiment"}

    @app.get("/api/display")
    def display(request: Request, background: BackgroundTasks):
        values = authenticate(request)
        with store.lock:
            store.checkin(values)
            store.trial_checkin(values)
            store.content_checkin(values)
            s = store.state
            result = {"status": 0, "filename": s["current"]["filename"],
                      "image_url": store.base_url + "/images/" + s["current"]["filename"],
                      "refresh_rate": s["poll_seconds"], "reset_firmware": False,
                      "update_firmware": False, "firmware_url": None, "firmware_version": None,
                      "special_function": "none", "temperature_profile": "default", "image_url_timeout": 0, "maximum_compatibility": False}
            if s["mode"] == "stress":
                background.add_task(stress_snapshot)
            return result

    def stress_snapshot():
        try:
            store.diagnostic(stress=True)
        except Exception as error:
            store.error("Stress render failed: " + type(error).__name__)

    @app.post("/api/log", status_code=204)
    async def log(request: Request):
        authenticate(request)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 256 * 1024:
                raise HTTPException(413, "Log payload exceeds 256 KiB")
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(400, "Invalid log JSON")
        if not isinstance(payload, dict) or not isinstance(payload.get("logs"), list):
            raise HTTPException(400, "Expected a logs array")
        store.event("firmware_log", payload=payload)
        for item in payload["logs"]:
            if isinstance(item, dict) and str(item.get("level", "")).lower() in ("error", "fatal", "warn", "warning"):
                store.error("Firmware: " + str(item.get("message", ""))[:2000])
        return Response(status_code=204)

    @app.get("/images/{filename}")
    def asset(filename: str, request: Request):
        if not re.fullmatch(r"[a-f0-9]{24}\.png", filename) or not (store.assets / filename).is_file():
            raise HTTPException(404)
        # Browsers use /control/preview, so these requests represent transport fetch attempts.
        with store.lock, store.trial_update():
            store.state["last_fetch"] = {"at": now(), "version": filename[:-4]}
            store.content_fetched(filename)
            trial = store.state.get("tap_trial")
            if trial and filename == store.state["current"]["filename"]:
                # Transport evidence only. Stock firmware supplies no display ack.
                trial["awaiting_fetch"] = False
                if trial["page"] not in trial["fetched_pages"]:
                    trial["fetched_pages"].append(trial["page"])
            store.save()
            store.event("image_request", requested_version=filename[:-4])
        return FileResponse(store.assets / filename, media_type="image/png")

    @app.get("/", dependencies=[Depends(local)])
    def dashboard():
        html = (ROOT / "dashboard.html").read_text()
        if control_guard:
            html = html.replace("<header>", '<p><a href="/admin">Users and phone enrollment</a></p><header>', 1)
        return HTMLResponse(html)

    @app.get("/control/status", dependencies=[Depends(local)])
    def status():
        return store.status()

    @app.get("/control/preview", dependencies=[Depends(local)])
    def preview():
        with store.lock:
            return FileResponse(store.assets / store.state["current"]["filename"], media_type="image/png")

    @app.post("/control/publish", dependencies=[Depends(local)])
    def publish(body: Publish):
        if body.fresh:
            label = body.fixture + "-" + uuid.uuid4().hex[:10]
            asset = store.asset(render(body.fixture, label), body.fixture, label)
        else:
            asset = store.fixtures[body.fixture]
        with store.lock:
            store.set_mode("soak")
            return store.publish(asset, "fresh" if body.fresh else "cached-fixture")

    @app.post("/control/force-refresh", dependencies=[Depends(local)])
    def force_refresh():
        selected = store.status()
        kind = selected["current"]["kind"]
        if kind == "TAP_TRIAL":
            raise HTTPException(409, "Use Start / restart two-page trial to publish fresh trial images")
        if kind == "CALENDAR":
            raise HTTPException(409, "Calendar refreshes automatically from TRMNL")
        if kind == "RECIPE" and hasattr(store, "force_recipe_refresh"):
            return store.force_recipe_refresh(selected)
        if kind == "DIAG":
            publication = store.diagnostic(stress=selected["mode"] == "stress", reason="force-refresh", selected=selected)
            if publication is None:
                raise HTTPException(409, "Selection changed while rendering; try again")
            return publication
        label = kind + "-" + uuid.uuid4().hex[:10]
        data = render(kind, label)
        with store.lock:
            if any(store.state[key] != selected[key] for key in ("run_id", "publication_id", "mode")):
                raise HTTPException(409, "Selection changed while rendering; try again")
            asset = store.asset(data, kind, label)
            return store.publish(asset, "force-refresh")

    @app.post("/control/calendar/show", dependencies=[Depends(local)])
    def show_calendar():
        if not hasattr(store, "show_calendar"):
            raise HTTPException(409, "Calendar requires the integrated server")
        from server.api.errors import ApiError
        try:
            return store.show_calendar()
        except ApiError as error:
            raise HTTPException(error.status, str(error))

    @app.get("/control/calendar/preview", dependencies=[Depends(local)])
    def calendar_preview():
        with store.lock:
            asset = store.state.get("calendar", {}).get("asset")
            if not asset or not (store.assets / asset['filename']).is_file():
                raise HTTPException(404, "Calendar image not yet available")
            return FileResponse(store.assets / asset['filename'], media_type="image/png", headers={"Cache-Control": "no-store"})

    @app.post("/control/settings", dependencies=[Depends(local)])
    def settings(body: Settings):
        with store.lock:
            if body.mode == "tap_trial" and not store.state.get("tap_trial"):
                raise HTTPException(409, "Start the two-page trial first")
            if store.state["mode"] != body.mode or store.state["poll_seconds"] != body.poll_seconds:
                store.event("experiment_boundary", next_mode=body.mode, next_poll_seconds=body.poll_seconds)
                store.state.update(unplugged_at=None, charge_samples=[], intervals=[], interval_baseline=None)
            store.set_mode(body.mode)
            store.state.update(poll_seconds=body.poll_seconds)
            store.save()
            store.event("settings")
        return store.status()

    @app.post("/control/tap-trial/start", dependencies=[Depends(local)])
    def start_tap_trial():
        return store.start_tap_trial()

    @app.post("/control/tap-trial/stop", dependencies=[Depends(local)])
    def stop_tap_trial():
        return store.stop_tap_trial()

    @app.post("/control/snapshot", dependencies=[Depends(local)])
    def snapshot():
        with store.lock:
            store.set_mode("snapshot")
            store.save()
        store.diagnostic()
        return store.status()

    @app.post("/control/run", dependencies=[Depends(local)])
    def run():
        with store.lock:
            store.state.update(run_id=uuid.uuid4().hex[:12], run_started_at=now(), checkins=0,
                updates=0, intervals=[], publications=[], unplugged_at=None, charge_samples=[],
                last_checkin=None, interval_baseline=None, last_fetch=None, telemetry={}, errors=[])
            store.save()
            store.event("run_start")
        return store.status()

    @app.post("/control/unplugged", dependencies=[Depends(local)])
    def unplugged():
        with store.lock:
            store.state.update(unplugged_at=now(), charge_samples=[])
            store.save()
            store.event("unplugged_mark", source="operator")
        return store.status()

    @app.post("/control/confirm", dependencies=[Depends(local)])
    def confirm(body: Confirmation):
        with store.lock:
            pub = next((p for p in store.state["publications"] if p["id"] == body.publication_id), None)
            if not pub:
                raise HTTPException(404, "Publication no longer in recent history")
            if "confirmed_at" in pub:
                raise HTTPException(409, "Publication already confirmed")
            stamp = body.observed_at or now()
            try:
                dt = datetime.fromisoformat(stamp)
                if dt.tzinfo is None or seconds(stamp, pub["at"]) < 0 or seconds(stamp, now()) > 5:
                    raise ValueError()
            except ValueError:
                raise HTTPException(400, "Use a timezone-aware observation between publication and now")
            latency = seconds(stamp, pub["at"])
            pub.update(confirmed_at=stamp, latency_seconds=latency)
            store.save()
            return store.event("visual_confirmation", observed_at=stamp, confirmed_publication_id=pub["id"],
                confirmed_version=pub["version"], latency_seconds=latency, note=body.note,
                run_id=pub["run_id"], mode=pub["mode"], poll_seconds=pub["poll_seconds"])

    return app
