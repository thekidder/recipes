"""Bounded public-web acquisition. No browser, credentials, proxies, or subrequests."""
from dataclasses import dataclass
from email.message import Message
from html.parser import HTMLParser
import http.client
import ipaddress
import json
import re
import socket
import ssl
import time
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

from .db import dumps
from .document import RecipeError

MAX_BODY = 4 * 1024 * 1024
MAX_EVIDENCE = 400_000
MAX_REDIRECTS = 5
TIMEOUT = 30


def public_address(value):
    address = ipaddress.ip_address(value)
    if not address.is_global or address.is_multicast:
        return False
    if address.version == 6:
        # Exclude mapped/translated addresses and transition mechanisms as well.
        return address in ipaddress.ip_network('2000::/3') and not address.sixtofour and not address.teredo
    return True


def validate_url(value):
    """Validate syntax without resolving DNS inside an API transaction."""
    try:
        if not isinstance(value, str) or not value or len(value) > 8192 or re.search(r'[\s\\\x00-\x1f\x7f]', value):
            raise ValueError()
        parts = urlsplit(value)
        if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username is not None or parts.password is not None:
            raise ValueError()
        port = parts.port if parts.port is not None else (443 if parts.scheme == 'https' else 80)
        if port != (443 if parts.scheme == 'https' else 80):
            raise ValueError()
        host = parts.hostname.encode('idna').decode('ascii').lower().rstrip('.')
        if '%' in host or host == 'localhost' or host.endswith(('.localhost', '.local', '.internal')):
            raise ValueError()
        try:
            ipaddress.ip_address(host)
        except ValueError:
            if not re.fullmatch(r'[a-z0-9.-]+', host) or '.' not in host:
                raise ValueError()
        else:
            if not public_address(host):
                raise ValueError()
        authority = '[' + host + ']' if ':' in host else host
        path = quote(parts.path or '/', safe="/%:@!$&'()*+,;=-._~")
        query = quote(parts.query, safe="/%?:@!$&'()*+,;=-._~")
        return urlunsplit((parts.scheme, authority, path, query, ''))
    except (ValueError, UnicodeError):
        raise RecipeError('unsafe_url', 'Use a public HTTP(S) recipe URL on its standard port, without credentials or local addresses.')


def resolve_public(host, port):
    try:
        addresses = list(dict.fromkeys(row[4][0] for row in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)))
    except OSError:
        raise RecipeError('capture_failed', 'The recipe website could not be resolved. Try fetching it again.', retryable=True)
    if not addresses or any(not public_address(address) for address in addresses):
        raise RecipeError('unsafe_url', 'The recipe URL resolves to a nonpublic address.')
    return addresses


class PinnedConnection(http.client.HTTPConnection):
    def __init__(self, host, port, address, secure, timeout):
        super().__init__(host, port, timeout=timeout)
        self.address, self.secure = address, secure

    def connect(self):
        # Connect to the validated numeric IP, never resolve the hostname again.
        self.sock = socket.create_connection((self.address, self.port), self.timeout)
        if self.secure:
            try:
                self.sock = ssl.create_default_context().wrap_socket(self.sock, server_hostname=self.host)
            except BaseException:
                self.sock.close()
                raise


@dataclass
class Page:
    url: str
    body: bytes
    content_type: str
    redirects: list


class URLFetcher:
    def __init__(self, *, resolver=resolve_public, connection_factory=PinnedConnection):
        self.resolver, self.connection_factory = resolver, connection_factory

    def fetch(self, submitted_url):
        url = validate_url(submitted_url)
        redirects, seen = [], set()
        deadline = time.monotonic() + TIMEOUT
        for hop in range(MAX_REDIRECTS + 1):
            if url in seen:
                raise RecipeError('capture_failed', 'The website redirects in a loop. Share the final recipe link.')
            seen.add(url)
            parts = urlsplit(url)
            port = 443 if parts.scheme == 'https' else 80
            addresses = self.resolver(parts.hostname, port)
            connection = None
            response = None
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError()
                connection = self.connection_factory(parts.hostname, port, addresses[0], parts.scheme == 'https', min(10, remaining))
                connection.request('GET', parts.path + ('?' + parts.query if parts.query else ''), headers={
                    'User-Agent': 'eRecipe/1.0 (recipe import)', 'Accept': 'text/html, application/xhtml+xml',
                    'Accept-Encoding': 'identity', 'Connection': 'close'})
                response = connection.getresponse()
                if response.status in (301, 302, 303, 307, 308):
                    location = response.getheader('Location')
                    if not location or hop == MAX_REDIRECTS:
                        raise RecipeError('capture_failed', 'The website has too many redirects or an invalid redirect. Share the final recipe link.')
                    next_url = validate_url(urljoin(url, location))
                    redirects.append({'url': url, 'status': response.status, 'location': next_url})
                    url = next_url
                    continue
                if response.status != 200:
                    raise RecipeError('capture_failed', 'The website could not provide this recipe. Try again or share recipe text/photos instead.',
                                      retryable=response.status in (408, 429) or response.status >= 500, details={'http_status': response.status})
                content_type = response.getheader('Content-Type', '')
                if content_type.split(';')[0].strip().lower() not in ('text/html', 'application/xhtml+xml'):
                    raise RecipeError('unsupported_source', 'This link is not an HTML recipe page. Share text or photos instead.')
                if response.getheader('Content-Encoding', 'identity').lower() != 'identity':
                    raise RecipeError('unsupported_source', 'The website returned an unsupported encoded page. Share recipe text/photos instead.')
                length = response.getheader('Content-Length')
                if length and (not length.isdigit() or int(length) > MAX_BODY):
                    raise RecipeError('source_too_large', 'The recipe page exceeds the 4 MiB download limit.')
                body = bytearray()
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError()
                    # HTTP/1.0 responses can detach the socket from the connection;
                    # their socket retains the bounded timeout set at connection time.
                    if connection.sock:
                        connection.sock.settimeout(min(10, remaining))
                    chunk = response.read1(min(65536, MAX_BODY + 1 - len(body)))
                    if not chunk:
                        break
                    body.extend(chunk)
                    if len(body) > MAX_BODY:
                        raise RecipeError('source_too_large', 'The recipe page exceeds the 4 MiB download limit.')
                if length and len(body) != int(length):
                    raise RecipeError('capture_failed', 'The website returned an incomplete page. Try fetching it again.', retryable=True)
                return Page(url, bytes(body), content_type, redirects)
            except (OSError, http.client.HTTPException):
                raise RecipeError('capture_failed', 'The recipe website could not be reached securely or timed out. Try fetching it again.', retryable=True)
            finally:
                if response:
                    response.close()
                if connection:
                    connection.close()
        raise AssertionError('unreachable')


class RecipeHTML(HTMLParser):
    hidden_tags = {'script', 'style', 'noscript', 'template', 'svg', 'head'}
    block_tags = {'p', 'li', 'div', 'section', 'article', 'h1', 'h2', 'h3', 'h4', 'br', 'tr'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden = []
        self.text = []
        self.structured = []
        self.script = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'script' and attrs.get('type', '').split(';')[0].strip().lower() == 'application/ld+json':
            self.script = []
        if tag in self.hidden_tags:
            self.hidden.append(tag)
        elif not self.hidden and tag in self.block_tags:
            self.text.append('\n')

    def handle_endtag(self, tag):
        if tag == 'script' and self.script is not None:
            try:
                self.structured.extend(recipe_objects(json.loads(''.join(self.script))))
            except (ValueError, RecursionError):
                pass  # Malformed metadata never hides usable visible recipe text.
            self.script = None
        if tag in self.hidden:
            self.hidden = self.hidden[:len(self.hidden) - 1 - self.hidden[::-1].index(tag)]
        if not self.hidden and tag in self.block_tags:
            self.text.append('\n')

    def handle_data(self, data):
        if self.script is not None:
            self.script.append(data)
        elif not self.hidden:
            self.text.append(data)


def recipe_objects(value, depth=0):
    if depth > 30:
        return []
    result = []
    if isinstance(value, dict):
        types = value.get('@type', [])
        if isinstance(types, str):
            types = [types]
        if isinstance(types, list) and any(t in ('Recipe', 'https://schema.org/Recipe', 'http://schema.org/Recipe') for t in types):
            return [value]
        for child in value.values():
            result.extend(recipe_objects(child, depth + 1))
    elif isinstance(value, list):
        for child in value:
            result.extend(recipe_objects(child, depth + 1))
    return result


def page_evidence(page):
    message = Message(); message['content-type'] = page.content_type
    encoding = message.get_content_charset()
    if not encoding:
        match = re.search(rb'<meta\b[^>]*charset\s*=\s*[\"\']?\s*([a-zA-Z0-9_-]+)', page.body[:8192], re.I)
        encoding = match.group(1).decode('ascii') if match else 'utf-8-sig'
    try:
        html = page.body.decode(encoding)
    except (UnicodeError, LookupError):
        raise RecipeError('unsupported_source', 'The website text encoding could not be read. Share recipe text/photos instead.')
    parser = RecipeHTML()
    try:
        parser.feed(html); parser.close()
        # Keep line/ingredient boundaries while removing layout-only whitespace.
        text = '\n'.join(line for line in (' '.join(line.split()) for line in ''.join(parser.text).splitlines()) if line)
        structured = list({dumps(item): item for item in parser.structured}.values())
        serialized = dumps(structured) if structured else ''
        if len(text.encode()) + len(serialized.encode()) > MAX_EVIDENCE:
            raise RecipeError('source_too_large', 'The recipe page contains too much text to extract without truncation. Share the recipe text or screenshots instead.')
    except (UnicodeError, RecursionError):
        raise RecipeError('unsupported_source', 'The website contains malformed recipe data. Share recipe text/photos instead.')
    if not text and not structured:
        raise RecipeError('capture_failed', 'No recipe text was available. Pages requiring sign-in or JavaScript should be shared as text/photos.')
    return text, serialized
