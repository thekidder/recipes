"""Provider selection and OpenRouter integration using isolated data and HTTP mocks."""
from io import BytesIO
import json
import logging
from types import SimpleNamespace

import httpx
from PIL import Image
import pytest

from server.recipes.capture import Page
from server.recipes.config import ProcessingConfig
from server.recipes.db import Library, dumps
from server.recipes.openai import OpenAIExtractor
from server.recipes.openrouter import OpenRouterExtractor
from server.recipes.processing import ProcessingService, Worker

RECIPE = dict(is_recipe=True, title="Salad", servings="2", prep_time=None, cook_time=None,
              ingredients=[dict(name=None, items=["1 cucumber", "1 tbsp lemon juice"])],
              method=[dict(name=None, items=["Slice cucumber and toss with lemon juice."])],
              notes=[], footer=None, warnings=[])
CONFIG = ProcessingConfig(provider="openrouter", api_key="router-test-secret", model="vendor/vision-model")
ENV_KEYS = ("ERECIPE_LLM_PROVIDER", "OPENAI_API_KEY", "OPENROUTER_API_KEY",
            "ERECIPE_OPENAI_MODEL", "ERECIPE_OPENROUTER_MODEL")


def completion(**overrides):
    return {"id": "gen_test", "model": CONFIG.model, "provider": "TestProvider",
            "choices": [{"message": {"role": "assistant", "content": dumps(RECIPE)},
                         "finish_reason": "stop", "native_finish_reason": "end_turn"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 80, "total_tokens": 180,
                      "prompt_tokens_details": {"cached_tokens": 20},
                      "completion_tokens_details": {"reasoning_tokens": 10}}, **overrides}


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    path = tmp_path / ".env"
    path.write_text("")
    return path


def test_provider_configuration_is_explicit_and_keys_are_isolated(env_file, monkeypatch):
    env_file.write_text("OPENAI_API_KEY=openai-test-secret\nERECIPE_OPENAI_MODEL=existing-model\n"
                        "OPENROUTER_API_KEY=router-test-secret\nERECIPE_OPENROUTER_MODEL=vendor/vision-model\n")
    direct = ProcessingConfig.from_env(env_file)
    assert direct.provider == "openai" and direct.api_key == "openai-test-secret"
    assert direct.model == "existing-model"
    monkeypatch.setenv("ERECIPE_LLM_PROVIDER", "openrouter")
    router = ProcessingConfig.from_env(env_file)
    assert router.api_key == "router-test-secret" and router.model == "vendor/vision-model"
    assert router.readiness() == dict(provider="openrouter", model="vendor/vision-model", configured=True)
    assert "router-test-secret" not in repr(router) + dumps(router.readiness())
    # A missing selected-provider key never falls back to the other credential.
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    assert not ProcessingConfig.from_env(env_file).readiness()["configured"]
    monkeypatch.setenv("ERECIPE_LLM_PROVIDER", "typo")
    with pytest.raises(ValueError, match="must be openai or openrouter"):
        ProcessingConfig.from_env(env_file)


def test_environment_overrides_file_and_openai_defaults_are_preserved(env_file, monkeypatch):
    assert ProcessingConfig.from_env(env_file).model == "gpt-4.1-mini"
    env_file.write_text("ERECIPE_LLM_PROVIDER=openrouter\nOPENROUTER_API_KEY=file-key\nERECIPE_OPENROUTER_MODEL=vendor/file-model\n")
    monkeypatch.setenv("OPENROUTER_API_KEY", "environment-key")
    monkeypatch.setenv("ERECIPE_OPENROUTER_MODEL", "vendor/environment-model")
    config = ProcessingConfig.from_env(env_file)
    assert config.api_key == "environment-key" and config.model == "vendor/environment-model"


@pytest.mark.parametrize("missing", ["key", "model"])
def test_missing_openrouter_configuration_fails_before_network(env_file, monkeypatch, tmp_path, missing):
    monkeypatch.setenv("ERECIPE_LLM_PROVIDER", "openrouter")
    if missing != "key":
        monkeypatch.setenv("OPENROUTER_API_KEY", "router-test-secret")
    if missing != "model":
        monkeypatch.setenv("ERECIPE_OPENROUTER_MODEL", "vendor/model")
    config = ProcessingConfig.from_env(env_file)
    assert not config.readiness()["configured"]
    library = Library(tmp_path / "library")
    job = ProcessingService(library).submit_text("Slice cucumber.", key="missing")
    runner = Worker(library, config)
    runner.extractor.transport = httpx.MockTransport(lambda _: pytest.fail("Network call with missing settings"))
    runner.run_once()
    error = json.loads(library.required("jobs", job["id"])["error_json"])
    assert ("OPENROUTER_API_KEY" if missing == "key" else "ERECIPE_OPENROUTER_MODEL") in error["message"]
    assert not library.all("SELECT * FROM llm_calls")


@pytest.mark.parametrize("kind", ["text", "images", "url"])
def test_openrouter_pipeline_preserves_evidence_and_provenance(tmp_path, caplog, kind):
    caplog.set_level(logging.INFO, logger="server.recipes.openai")
    library = Library(tmp_path)
    service = ProcessingService(library)
    if kind == "text":
        job = service.submit_text("Slice cucumber and toss with lemon juice.", key="text")
    elif kind == "images":
        originals = []
        for color in ("white", "black"):
            output = BytesIO()
            Image.new("RGB", (20, 20), color).save(output, format="PNG")
            originals.append((color + ".png", output.getvalue()))
        job = service.submit_images(originals, key="images")
    else:
        job = service.submit_url("https://example.com/salad", key="url")
    seen = []
    def handler(request):
        seen.append(request)
        assert str(request.url) == "https://openrouter.ai/api/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer router-test-secret"
        return httpx.Response(200, json=completion(), headers={"x-request-id": "req_router"})
    runner = Worker(library, CONFIG, fetcher=SimpleNamespace(fetch=lambda url:
        Page(url, b'<html><p>Slice cucumber and toss with lemon juice.</p><script type="application/ld+json">'
             b'{"@type":"Recipe","name":"Salad","recipeIngredient":["1 cucumber"]}</script></html>', "text/html", [])))
    assert isinstance(runner.extractor, OpenRouterExtractor)
    runner.extractor.transport = httpx.MockTransport(handler)
    while runner.run_once():
        pass
    detail = library.detail(job["recipe_id"])
    assert len(detail["revisions"]) == len(detail["renders"]) == len(seen) == 1
    call = library.one("SELECT * FROM llm_calls")
    assert call["provider"] == "openrouter" and call["status"] == "succeeded"
    assert call["reported_model"] == CONFIG.model and call["provider_request_id"] == "req_router"
    assert library.asset_bytes(call["request_asset_id"]) == seen[0].content
    assert json.loads(library.asset_bytes(call["response_asset_id"])) == completion()
    request = json.loads(seen[0].content)
    assert request["provider"] == {"require_parameters": True}
    assert request["response_format"]["json_schema"]["strict"] is True
    assert request["max_tokens"] == 10000 and request["stream"] is False
    content = request["messages"][1]["content"]
    if kind == "images":
        images = [item["image_url"] for item in content if item["type"] == "image_url"]
        assert len(images) == 2 and images[0] != images[1]
        assert all(image["url"].startswith("data:image/png;base64,") and image["detail"] == "high" for image in images)
        # Evidence conversion preserves page order and exact normalized bytes.
        claimed = {**job, "source_capture_id": detail["captures"][0]["id"]}
        expected = [item["image_url"] for item in runner.evidence(claimed) if item["type"] == "input_image"]
        assert [image["url"] for image in images] == expected
    else:
        assert all(item["type"] == "text" for item in content)
        assert "Slice cucumber" in dumps(content)
    event = next(json.loads(record.getMessage().split(" ", 1)[1]) for record in caplog.records
                 if record.getMessage().startswith("openrouter_call_finished "))
    assert event["provider"] == "openrouter" and event["reported_model"] == CONFIG.model
    assert event["usage"] == dict(input_tokens=100, output_tokens=80, total_tokens=180, cached_tokens=20, reasoning_tokens=10)
    assert event["provider_response_id"] == "gen_test"
    assert "router-test-secret" not in caplog.text
    assert all(b"router-test-secret" not in path.read_bytes() for path in library.blobs.iterdir())


@pytest.mark.parametrize("kind,expected", [("length", "incomplete_response"), ("content_filter", "incomplete_response"),
    ("refusal", "provider_refusal"), ("error_body", "provider_error"), ("choice_error", "provider_error"),
    ("http401", "provider_authentication"), ("http402", "provider_error"), ("http429", "provider_error"),
    ("http503", "provider_error"), ("malformed", "invalid_extraction"), ("schema", "invalid_extraction"),
    ("timeout", "provider_timeout"), ("network", "provider_network"), ("null_choices", "invalid_extraction"),
    ("missing_finish", "incomplete_response"), ("tool_calls", "incomplete_response")])
def test_openrouter_failures_preserve_diagnostics_without_accepting_output(tmp_path, caplog, kind, expected):
    library = Library(tmp_path)
    job = ProcessingService(library).submit_text("Slice cucumber.", key="failure")
    payload = completion()
    private = "private recipe text router-test-secret"
    error_body = {"code": 503, "message": private, "metadata": {"error_type": "provider_unavailable", "raw": private}}
    def handler(request):
        if kind == "timeout":
            raise httpx.ReadTimeout(private, request=request)
        if kind == "network":
            raise httpx.ConnectError(private, request=request)
        if kind.startswith("http"):
            return httpx.Response(int(kind[4:]), json={"error": error_body})
        if kind == "malformed":
            return httpx.Response(200, content=private.encode())
        if kind == "error_body":
            payload["error"] = error_body
        elif kind == "choice_error":
            payload["choices"][0]["error"] = error_body
        elif kind == "refusal":
            payload["choices"][0]["message"]["refusal"] = private
        elif kind == "schema":
            payload["choices"][0]["message"]["content"] = dumps({"title": private})
        elif kind == "null_choices":
            payload["choices"] = None
        else:
            payload["choices"][0]["finish_reason"] = None if kind == "missing_finish" else kind
        return httpx.Response(200, json=payload)
    runner = Worker(library, CONFIG)
    runner.extractor.transport = httpx.MockTransport(handler)
    runner.run_once()
    error = json.loads(library.required("jobs", job["id"])["error_json"])
    call = library.one("SELECT * FROM llm_calls")
    assert error["code"] == expected and call["status"] == "failed"
    assert "OpenRouter" in error["message"]
    assert not library.all("SELECT * FROM recipe_revisions")
    assert bool(call["response_asset_id"]) == (kind not in {"timeout", "network"})
    assert error["details"]["provider"] == "openrouter"
    assert "router-test-secret" not in caplog.text + dumps(error)
    assert "private recipe text" not in caplog.text + dumps(error)
    if kind in {"error_body", "choice_error"}:
        assert error["retryable"] and error["details"]["upstream_error_type"] == "provider_unavailable"
        assert error["details"]["provider_status"] == "failed"
    if kind in {"length", "content_filter"}:
        assert error["details"]["incomplete_reason"] == ("max_output_tokens" if kind == "length" else kind)


def test_openrouter_cancellation_keeps_raw_response_but_no_revision(tmp_path):
    library = Library(tmp_path)
    job = ProcessingService(library).submit_text("Slice cucumber.", key="cancel")
    def handler(request):
        library.cancel(job["id"])
        return httpx.Response(200, json=completion())
    runner = Worker(library, CONFIG)
    runner.extractor.transport = httpx.MockTransport(handler)
    runner.run_once()
    call = library.one("SELECT * FROM llm_calls")
    assert call["status"] == "abandoned" and call["response_asset_id"]
    assert not library.all("SELECT * FROM recipe_revisions")


def test_provider_mismatch_fails_closed():
    with pytest.raises(ValueError, match="does not match"):
        OpenAIExtractor(CONFIG)
    with pytest.raises(ValueError, match="does not match"):
        OpenRouterExtractor(ProcessingConfig())


def test_openrouter_configuration_in_authenticated_api_status(tmp_path):
    from server.tests.test_api import Harness
    api = Harness(tmp_path)
    try:
        api.service.config = ProcessingConfig(provider="openrouter", api_key="router-test-secret", model="")
        status = api.get('/api/v1/status', 'getStatus')
        assert status['readiness']['extraction'] == 'not_configured'
        assert 'ERECIPE_OPENROUTER_MODEL' in status['readiness']['issues'][0]['message']
        api.service.config = CONFIG
        assert api.get('/api/v1/status', 'getStatus')['readiness']['extraction'] == 'ready'
        assert 'router-test-secret' not in dumps(status)
    finally:
        api.web.close()
        api.lan.close()


@pytest.mark.parametrize("filtered", [False, True])
def test_openrouter_results_follow_existing_ios_api_contract(tmp_path, filtered):
    from server.tests.test_api import Harness
    api = Harness(tmp_path)
    try:
        api.service.config = CONFIG
        runner = Worker(api.db, CONFIG)
        payload = completion()
        if filtered:
            payload['choices'][0]['finish_reason'] = 'content_filter'
        runner.extractor.transport = httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
        imported = api.imported()
        assert runner.run_once()
        job = api.get('/api/v1/jobs/' + imported['job_id'], 'getJob')
        call = api.db.one('SELECT * FROM llm_calls')
        public_call = api.get('/api/v1/llm-calls/' + call['id'], 'getLlmCall')
        assert public_call['provider'] == 'openrouter'
        if filtered:
            assert job['status'] == 'failed'
            assert job['error']['details']['incomplete_reason'] == 'content_filter'
            assert 'OpenRouter' in job['error']['message']
            assert public_call['error']['details']['provider_response_id'] == 'gen_test'
        else:
            assert job['status'] == 'succeeded'
            assert runner.run_once()
            recipe = api.get('/api/v1/recipes/' + imported['recipe_id'], 'getRecipe')
            assert recipe['current_revision_id']
            assert public_call['usage']['prompt_tokens'] == 100
    finally:
        api.web.close()
        api.lan.close()
