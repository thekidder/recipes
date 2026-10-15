"""Daily reader polling schedule, evaluated in its configured time zone."""
from datetime import datetime, timedelta, timezone
import math
import os
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def server_timezone():
    candidates = [os.environ.get('TZ', '').removeprefix(':')]
    local = str(Path('/etc/localtime').resolve())
    if '/zoneinfo/' in local:
        candidates.append(local.split('/zoneinfo/', 1)[1])
    try:
        candidates.append(Path('/etc/timezone').read_text().strip())
    except OSError:
        pass
    for name in candidates:
        try:
            ZoneInfo(name)
            return name
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return 'UTC'


class NighttimePolling(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: bool = Field(default=False, strict=True)
    start: str = Field(default='22:00', pattern=r'^(?:[01][0-9]|2[0-3]):[0-5][0-9]$')
    end: str = Field(default='07:00', pattern=r'^(?:[01][0-9]|2[0-3]):[0-5][0-9]$')
    poll_seconds: int = Field(default=900, ge=5, le=3600, strict=True)
    timezone: str = Field(default_factory=server_timezone, max_length=100)

    @field_validator('timezone')
    @classmethod
    def valid_timezone(cls, value):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError('Use a valid time zone, such as America/Los_Angeles.')
        return value

    @model_validator(mode='after')
    def distinct_hours(self):
        if self.start == self.end:
            raise ValueError('Nighttime start and end must be different.')
        return self


def polling_status(state, instant: datetime):
    schedule = state['nighttime']
    normal = state['poll_seconds']
    if not schedule['enabled']:
        return dict(nighttime_active=False, effective_poll_seconds=normal)
    zone = ZoneInfo(schedule['timezone'])

    def active(at):
        clock = at.astimezone(zone).strftime('%H:%M')
        start, end = schedule['start'], schedule['end']
        return start <= clock < end if start < end else clock >= start or clock < end

    instant = instant.astimezone(timezone.utc)
    nighttime = active(instant)
    interval = schedule['poll_seconds'] if nighttime else normal
    # Check minute boundaries in UTC, so missing/repeated local times during
    # DST are handled by ZoneInfo. At most 60 checks with the one-hour limit.
    boundary = instant.replace(second=0, microsecond=0) + timedelta(minutes=1)
    until = instant + timedelta(seconds=interval)
    while boundary <= until:
        if active(boundary) != nighttime:
            interval = max(5, math.ceil((boundary - instant).total_seconds()))
            break
        boundary += timedelta(minutes=1)
    return dict(nighttime_active=nighttime, effective_poll_seconds=interval)
