"""Fetch TRMNL cloud screens independently of the reader's fast polling loop."""
from dataclasses import dataclass, field
from io import BytesIO
import json
import os
from threading import Event
import time
from urllib.parse import urljoin, urlsplit

from PIL import Image
from server.recipes.capture import PinnedConnection, resolve_public, validate_url

DISPLAY_URL = 'https://trmnl.com/api/display'
SIZE = (1872, 1404)


class CalendarError(Exception):
    """Only stable, credential-free codes may reach status or logs."""
    def __init__(self, code, retry_after=300):
        super().__init__(code)
        self.code = code
        self.retry_after = retry_after


@dataclass(frozen=True)
class CalendarConfig:
    api_key: str = field(default='', repr=False)

    @property
    def enabled(self):
        return bool(self.api_key)

    @classmethod
    def from_env(cls):
        # The launcher has already loaded the explicit server/.env file.
        return cls(api_key=os.environ.get('ERECIPE_TRMNL_API_KEY', '').strip())


def download(url, *, headers=None, limit, resolver=resolve_public, connection_factory=PinnedConnection):
    """Bounded HTTPS reads, pinned public DNS, no proxies or credential redirects."""
    deadline = time.monotonic() + 30
    for hop in range(4):
        url = validate_url(url)
        parts = urlsplit(url)
        if parts.scheme != 'https':
            raise CalendarError('invalid_image_url')
        addresses = resolver(parts.hostname, 443)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CalendarError('cloud_timeout')
        connection = connection_factory(parts.hostname, 443, addresses[0], True, min(10, remaining))
        response = None
        try:
            connection.request('GET', parts.path + ('?' + parts.query if parts.query else ''), headers={
                'User-Agent': 'eRecipe/1.0 (calendar)', 'Accept-Encoding': 'identity',
                'Connection': 'close', **(headers or {})})
            response = connection.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                # The authenticated API has a fixed origin; never follow its redirects.
                location = response.getheader('Location')
                if headers or not location or hop == 3:
                    raise CalendarError('cloud_redirect_rejected')
                url = urljoin(url, location)
                continue
            if response.status != 200:
                delay = response.getheader('Retry-After', '')
                delay = min(86400, max(300, int(delay))) if delay.isdigit() else 300
                raise CalendarError('cloud_http_' + str(response.status), delay)
            if response.getheader('Content-Encoding', 'identity').lower() != 'identity':
                raise CalendarError('unsupported_encoding')
            length = response.getheader('Content-Length')
            if length and (not length.isdigit() or int(length) > limit):
                raise CalendarError('cloud_response_too_large')
            body = bytearray()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CalendarError('cloud_timeout')
                if connection.sock:
                    connection.sock.settimeout(min(10, remaining))
                chunk = response.read1(min(65536, limit + 1 - len(body)))
                if not chunk:
                    break
                body.extend(chunk)
                if len(body) > limit:
                    raise CalendarError('cloud_response_too_large')
            if length and len(body) != int(length):
                raise CalendarError('incomplete_cloud_response')
            return bytes(body)
        finally:
            if response:
                response.close()
            connection.close()
    raise CalendarError('cloud_redirect_rejected')


def normalize_image(data):
    """Use the landscape one-bit format already validated on this reader."""
    try:
        with Image.open(BytesIO(data)) as im:
            if im.format != 'PNG' or im.size != SIZE or getattr(im, 'n_frames', 1) != 1:
                raise CalendarError('invalid_calendar_image')
            im.verify()
        with Image.open(BytesIO(data)) as im:
            im.load()
            # Flatten transparency on white before converting cloud grayscale palettes.
            rgba = im.convert('RGBA')
            white = Image.new('RGBA', SIZE, 'white')
            white.alpha_composite(rgba)
            result = BytesIO()
            white.convert('L').convert('1').save(result, format='PNG')
            return result.getvalue()
    except CalendarError:
        raise
    except Exception:
        raise CalendarError('invalid_calendar_image') from None


class CalendarClient:
    def __init__(self, config, fetch=download):
        self.config, self.fetch = config, fetch

    def fetch_screen(self):
        try:
            raw = self.fetch(DISPLAY_URL, headers={'Access-Token': self.config.api_key,
                'Model': 'x', 'Width': str(SIZE[0]), 'Height': str(SIZE[1]),
                'Accept': 'application/json'}, limit=65536)
            metadata = json.loads(raw)
            if not isinstance(metadata, dict) or metadata.get('status') != 0 or metadata.get('reset_firmware'):
                raise CalendarError('cloud_screen_unavailable')
            url = metadata.get('image_url')
            filename = metadata.get('filename')
            if not isinstance(url, str) or not isinstance(filename, str) or not filename or len(filename) > 512:
                raise CalendarError('invalid_cloud_response')
            # Validate even when a custom transport is injected.
            url = validate_url(url)
            if urlsplit(url).scheme != 'https':
                raise CalendarError('invalid_image_url')
            delay = metadata.get('refresh_rate', 900)
            if type(delay) is not int or not 0 < delay <= 86400:
                raise CalendarError('invalid_cloud_refresh_rate')
            data = self.fetch(url, limit=8 * 1024 * 1024)
            return normalize_image(data), max(300, delay)
        except CalendarError:
            raise
        except Exception:
            # Never log response bodies, signed URLs, or raw transport exceptions.
            raise CalendarError('calendar_sync_failed') from None


class CalendarWorker:
    def __init__(self, store, config, client=None):
        self.store, self.config = store, config
        self.client = client or CalendarClient(config)
        self.stopped = Event()
        self.failures = 0

    def run_once(self):
        if not self.config.enabled:
            return 300
        try:
            data, delay = self.client.fetch_screen()
            self.store.cache_calendar(data, delay)
            self.failures = 0
            return delay
        except Exception as error:
            self.failures += 1
            delay = max(min(3600, 300 * 2 ** min(self.failures - 1, 4)),
                        error.retry_after if isinstance(error, CalendarError) else 300)
            code = error.code if isinstance(error, CalendarError) else 'calendar_sync_failed'
            self.store.calendar_failure(code, delay)
            return delay

    def run(self):
        while not self.stopped.is_set():
            try:
                delay = self.run_once()
            except Exception:
                # Persistence failures must not terminate the background service.
                delay = 300
            if self.stopped.wait(delay):
                break

    def stop(self):
        self.stopped.set()
