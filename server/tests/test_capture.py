"""URL acquisition, parsing and network-boundary checks; no external requests."""
from io import BytesIO
import json
import socket
import ssl

import pytest

from server.recipes import capture
from server.recipes.capture import Page, URLFetcher, page_evidence, validate_url, resolve_public, PinnedConnection
from server.recipes.document import RecipeError

HTML = b'''<!doctype html><html><head><script type="application/ld+json">
{"@context":"https://schema.org","@graph":[{"@type":"WebSite","name":"Site"},
{"@type":["Recipe"],"name":"Lemon salad","recipeIngredient":["2 cans chickpeas","2 tbsp lemon juice"],
"recipeInstructions":[{"@type":"HowToStep","text":"Mix and serve."}]}]}</script>
<style>unwanted style</style></head><body><h1>Lemon salad</h1><ul><li>2 cans chickpeas</li>
<li>2 tbsp lemon juice</li></ul><p>Mix &amp; serve.</p><script>unwanted script</script></body></html>'''


class Response:
    def __init__(self, body=HTML, status=200, **headers):
        self.status = status
        self.headers = {'Content-Type': 'text/html; charset=utf-8', **headers}
        self.body = BytesIO(body)
        self.closed = False

    def getheader(self, name, default=None):
        return self.headers.get(name, default)

    def read1(self, size):
        return self.body.read(size)

    def close(self):
        self.closed = True


def fake_fetcher(responses, resolver=None):
    calls = []

    class Connection:
        sock = None

        def __init__(self, host, port, address, secure, timeout):
            self.identity = (host, port, address, secure, timeout)

        def request(self, method, target, headers):
            calls.append((self.identity, method, target, headers))

        def getresponse(self):
            return responses.pop(0)

        def close(self):
            pass

    return URLFetcher(resolver=resolver or (lambda *_: ['93.184.216.34']), connection_factory=Connection), calls


@pytest.mark.parametrize('url', [
    'file:///etc/passwd', 'ftp://example.com/recipe', 'http://localhost/', 'http://127.0.0.1/',
    'http://10.1.2.3/', 'https://192.168.1.1/', 'http://100.64.0.1/', 'http://169.254.169.254/',
    'http://[::1]/', 'http://[::ffff:127.0.0.1]/', 'http://[64:ff9b::7f00:1]/', 'http://[2002:7f00:1::]/',
    'https://user:pass@example.com/', 'https://example.com:8000/', 'https://example.com:0/',
    'https://example.com:99999/', 'https://example.com\\@127.0.0.1/', 'https://example.com/\r\nsecret',
    'https://example.local/', 'https://example.internal/', 'http://224.0.0.1/', 'http://[ff02::1]/',
])
def test_unsafe_urls_rejected(url):
    with pytest.raises(RecipeError) as error:
        validate_url(url)
    assert error.value.code == 'unsafe_url'


def test_normalization_preserves_query_and_encodes_unicode():
    assert validate_url('https://Example.com:443/caf\u00e9?q=a%20b&x=2#recipe') == 'https://example.com/caf%C3%A9?q=a%20b&x=2'


@pytest.mark.parametrize('addresses', [
    ['127.0.0.1'], ['93.184.216.34', '10.0.0.1'], ['::1'], ['100.100.100.100'], ['169.254.169.254']
])
def test_dns_private_or_mixed_answers_are_rejected(monkeypatch, addresses):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *_args, **_kwargs: [(2, 1, 6, '', (ip, 443)) for ip in addresses])
    with pytest.raises(RecipeError, match='nonpublic'):
        resolve_public('recipe.example.com', 443)


def test_redirects_revalidate_dns_and_do_not_forward_secrets(monkeypatch):
    def dns(host, *_args, **_kwargs):
        return [(2, 1, 6, '', ('93.184.216.34' if host == 'public.example.com' else '127.0.0.1', 443))]
    monkeypatch.setattr(socket, 'getaddrinfo', dns)
    response = Response(status=302, Location='https://internal.example.com/secret')
    fetcher, calls = fake_fetcher([response], resolve_public)
    with pytest.raises(RecipeError, match='nonpublic'):
        fetcher.fetch('https://public.example.com/recipe')
    assert len(calls) == 1 and response.closed
    assert not {'Authorization', 'Cookie', 'Referer'} & calls[0][3].keys()


def test_public_redirect_and_body_preserved():
    first = Response(status=301, Location='/salad?x=a%20b#recipe')
    final = Response(**{'Content-Length': str(len(HTML))})
    fetcher, calls = fake_fetcher([first, final])
    page = fetcher.fetch('https://example.com/start#top')
    assert page.body == HTML and page.url == 'https://example.com/salad?x=a%20b'
    assert len(page.redirects) == 1 and first.closed and final.closed
    assert [call[2] for call in calls] == ['/start', '/salad?x=a%20b']
    assert all(call[0][2] == '93.184.216.34' for call in calls)


def test_connection_pins_ip_but_tls_verifies_original_hostname(monkeypatch):
    connected = []; tls = []
    class Socket:
        def close(self): pass
    monkeypatch.setattr(socket, 'create_connection', lambda address, timeout: connected.append((address, timeout)) or Socket())
    context = ssl.create_default_context()
    assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
    class Context:
        def wrap_socket(self, sock, server_hostname):
            tls.append(server_hostname); return sock
    monkeypatch.setattr(ssl, 'create_default_context', Context)
    connection = PinnedConnection('recipes.example.com', 443, '93.184.216.34', True, 10)
    connection.connect()
    assert connected == [(('93.184.216.34', 443), 10)] and tls == ['recipes.example.com']


@pytest.mark.parametrize('response,code', [
    (Response(status=403), 'capture_failed'), (Response(status=429), 'capture_failed'),
    (Response(**{'Content-Type': 'application/pdf'}), 'unsupported_source'),
    (Response(**{'Content-Encoding': 'gzip'}), 'unsupported_source'),
    (Response(**{'Content-Length': str(capture.MAX_BODY + 1)}), 'source_too_large'),
    (Response(**{'Content-Length': str(len(HTML) + 1)}), 'capture_failed'),
])
def test_http_failures_are_structured_and_close_response(response, code):
    fetcher, _ = fake_fetcher([response])
    with pytest.raises(RecipeError) as error:
        fetcher.fetch('https://example.com/recipe')
    assert error.value.code == code and response.closed


def test_chunked_size_limit_and_redirect_loops(monkeypatch):
    monkeypatch.setattr(capture, 'MAX_BODY', 10)
    fetcher, _ = fake_fetcher([Response(body=b'x' * 11)])
    with pytest.raises(RecipeError, match='download limit'):
        fetcher.fetch('https://example.com/')
    fetcher, calls = fake_fetcher([Response(status=302, Location='/')])
    with pytest.raises(RecipeError, match='loop'):
        fetcher.fetch('https://example.com/')
    assert len(calls) == 1


def test_redirect_limit_and_private_literal_targets():
    fetcher, calls = fake_fetcher([Response(status=302, Location=f'/hop-{n}') for n in range(6)])
    with pytest.raises(RecipeError, match='too many redirects'):
        fetcher.fetch('https://example.com/start')
    assert len(calls) == 6
    fetcher, calls = fake_fetcher([Response(status=302, Location='http://169.254.169.254/latest/meta-data/')])
    with pytest.raises(RecipeError) as error:
        fetcher.fetch('https://example.com/')
    assert error.value.code == 'unsafe_url' and len(calls) == 1


def test_network_failure_and_elapsed_budget_are_bounded(monkeypatch):
    class BrokenConnection:
        def __init__(self, *_): pass
        def request(self, *_args, **_kwargs): raise TimeoutError()
        def close(self): pass
    fetcher = URLFetcher(resolver=lambda *_: ['93.184.216.34'], connection_factory=BrokenConnection)
    with pytest.raises(RecipeError) as error:
        fetcher.fetch('https://example.com/')
    assert error.value.code == 'capture_failed' and error.value.retryable
    fetcher, calls = fake_fetcher([Response()])
    clock = iter([0, capture.TIMEOUT + 1])
    monkeypatch.setattr(capture.time, 'monotonic', lambda: next(clock))
    with pytest.raises(RecipeError, match='timed out'):
        fetcher.fetch('https://example.com/')
    assert not calls


def test_page_extracts_recipe_jsonld_and_visible_text():
    text, structured = page_evidence(Page('https://example.com/', HTML, 'text/html', []))
    assert '2 cans chickpeas\n2 tbsp lemon juice' in text and 'Mix & serve.' in text
    assert 'unwanted' not in text
    recipes = json.loads(structured)
    assert len(recipes) == 1 and recipes[0]['name'] == 'Lemon salad'
    assert recipes[0]['recipeInstructions'][0]['text'] == 'Mix and serve.'


def test_fallback_text_encoding_and_evidence_limit(monkeypatch):
    body = '<meta charset="windows-1252"><script type="application/ld+json">{broken}</script><p>Crème: ½ cup</p>'.encode('cp1252')
    text, structured = page_evidence(Page('https://example.com/', body, 'text/html', []))
    assert text == 'Crème: ½ cup' and structured == ''
    monkeypatch.setattr(capture, 'MAX_EVIDENCE', 2)
    with pytest.raises(RecipeError, match='without truncation'):
        page_evidence(Page('https://example.com/', HTML, 'text/html', []))


def test_javascript_only_page_has_actionable_error():
    with pytest.raises(RecipeError, match='JavaScript'):
        page_evidence(Page('https://example.com/', b'<script>renderRecipe()</script>', 'text/html', []))
