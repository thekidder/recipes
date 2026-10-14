"""Behavioral tests for durable processing; no external API calls or live credentials."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from io import BytesIO
import json
import logging
from pathlib import Path
import sqlite3
from unittest.mock import patch

import httpx
from PIL import Image
import pytest

from server.recipes.config import ProcessingConfig
from server.recipes.db import Library, Problem, digest, dumps
from server.recipes.document import ExtractedRecipe, RecipeError, parse_markdown, to_markdown
from server.recipes.openai import OpenAIExtractor
from server.recipes.processing import ProcessingService, Worker
from server.recipes import renderer

RECIPE = {
    "is_recipe": True, "title": "Lemon chickpea salad", "servings": "4", "prep_time": "15 minutes", "cook_time": None,
    "ingredients": [{"name": None, "items": ["2 cans chickpeas, drained", "1 cucumber, diced", "3 tbsp olive oil", "2 tbsp lemon juice", "1/2 tsp salt"]}],
    "method": [{"name": None, "items": ["Rinse and drain the chickpeas well.", "Combine the chickpeas and cucumber in a bowl.",
                                           "Whisk the olive oil, lemon juice and salt together.", "Toss the salad with the dressing. Serve at room temperature."]}],
    "notes": [], "footer": None, "warnings": []
}
SOURCE = Path(__file__).resolve().parents[1].joinpath("fixtures/recipe-text.txt").read_text()


def response(recipe=None, **overrides):
    return {"id": "resp_test", "status": "completed", "model": "test-reported-model",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": dumps(recipe or RECIPE)}]}],
            "usage": {"input_tokens": 150, "output_tokens": 90}, **overrides}


@pytest.fixture
def library(tmp_path):
    return Library(tmp_path)


def worker(library, handler=None):
    def default(request):
        return httpx.Response(200, json=response(), headers={"x-request-id": "request_test"})
    adapter = OpenAIExtractor(ProcessingConfig(api_key="test-secret-never-archive"), httpx.MockTransport(handler or default))
    return Worker(library, extractor=adapter)


def submit(library, key="submission"):
    return ProcessingService(library).submit_text(SOURCE, key=key)


def finish(library):
    job = submit(library)
    runner = worker(library)
    assert runner.run_once() and runner.run_once()
    assert not runner.run_once()
    return job, library.detail(job["recipe_id"])


def test_text_to_png_provenance_and_durable_restart(library):
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=response(), headers={"x-request-id": "request_test"})
    job = submit(library)
    worker(library, handler).run_once()
    # Restart between stages: rendering was durably queued in the extraction transaction.
    restarted = Library(library.directory)
    assert worker(restarted, lambda request: pytest.fail("Rendering called the model")).run_once()
    detail = restarted.detail(job["recipe_id"])
    assert len(detail["revisions"]) == len(detail["renders"]) == 1
    assert detail["current_revision_id"] == detail["revisions"][0]["id"]
    assert all(j["status"] == "succeeded" for j in detail["jobs"])
    source_asset = detail["captures"][0]["assets"][0]["asset_id"]
    assert restarted.asset_bytes(source_asset).decode() == SOURCE
    call = restarted.one("SELECT * FROM llm_calls")
    assert restarted.asset_bytes(call["request_asset_id"]) == seen[0].content
    request = json.loads(seen[0].content)
    assert request["store"] is False
    assert request["text"]["format"]["strict"] is True
    assert SOURCE in request["input"][1]["content"][0]["text"]
    assert call["reported_model"] == "test-reported-model" and call["provider_request_id"] == "request_test"
    assert json.loads(restarted.asset_bytes(call["structured_output_asset_id"])) == RECIPE
    assert json.loads(restarted.asset_bytes(call["response_asset_id"])) == response()
    assert all(b"test-secret-never-archive" not in f.read_bytes() for f in restarted.blobs.iterdir())
    assert restarted.all("SELECT * FROM publications") == []
    assert restarted.all("SELECT * FROM device_state") == []
    render = detail["renders"][0]
    with Image.open(BytesIO(restarted.asset_bytes(render["pages"][0]["asset_id"]))) as image:
        assert image.size == (1872, 1404) and image.mode == "1"
    assert restarted.one("PRAGMA foreign_key_check") is None
    assert ProcessingService(restarted).job_detail(job["id"])["downstream_job_ids"]


def test_import_idempotency_and_conflicting_reuse(library):
    one, two = submit(library), submit(library)
    assert one["id"] == two["id"]
    assert len(library.all("SELECT * FROM recipes")) == 1
    with pytest.raises(Problem) as error:
        ProcessingService(library).submit_text("Other recipe", key="submission")
    assert error.value.status == 409


def test_missing_credentials_retry_keeps_attempt_history(library):
    job = submit(library)
    assert Worker(library, ProcessingConfig()).run_once()
    failed = library.required("jobs", job["id"])
    assert json.loads(failed["error_json"])["code"] == "missing_credentials"
    assert library.all("SELECT * FROM llm_calls") == []
    library.retry(job["id"])
    worker(library).run_once()
    detail = ProcessingService(library).job_detail(job["id"])
    assert [a["status"] for a in detail["attempts"]] == ["failed", "succeeded"]
    assert len(library.all("SELECT * FROM source_captures")) == 1


@pytest.mark.parametrize("kind,expected", [
    ("refusal", "provider_refusal"), ("incomplete", "incomplete_response"),
    ("malformed", "invalid_extraction"), ("bad_schema", "invalid_extraction"),
    ("not_recipe", "incomplete_recipe"), ("http429", "provider_error"),
    ("http401", "provider_authentication"), ("timeout", "provider_timeout")])
def test_failed_model_results_never_create_revision(library, kind, expected):
    def handler(request):
        if kind == "timeout":
            raise httpx.ReadTimeout("timeout containing secret is not exposed", request=request)
        if kind.startswith("http"):
            return httpx.Response(int(kind[4:]), json={"error": {"message": "fixture"}})
        if kind == "malformed":
            return httpx.Response(200, content=b"not json")
        value = response()
        if kind == "refusal":
            value["output"][0]["content"] = [{"type": "refusal", "refusal": "fixture refusal"}]
        elif kind == "incomplete":
            value["status"] = "incomplete"
        elif kind == "bad_schema":
            value["output"][0]["content"][0]["text"] = '{"title":"Soup"}'
        elif kind == "not_recipe":
            value = response({**RECIPE, "is_recipe": False, "warnings": ["Multiple recipes require selection"]})
        return httpx.Response(200, json=value)
    job = submit(library)
    worker(library, handler).run_once()
    failed = library.required("jobs", job["id"])
    assert failed["status"] == "failed"
    assert json.loads(failed["error_json"])["code"] == expected
    assert not library.all("SELECT * FROM recipe_revisions")
    call = library.one("SELECT * FROM llm_calls")
    assert bool(call["response_asset_id"]) == (kind != "timeout")
    assert call["status"] == ("succeeded" if kind == "not_recipe" else "failed")


@pytest.mark.parametrize("reason", ["content_filter", "max_output_tokens", "future_reason"])
def test_incomplete_response_diagnostics_reach_job_and_logs(library, caplog, reason):
    caplog.set_level(logging.INFO, logger="server.recipes.openai")
    payload = response(status="incomplete", incomplete_details={"reason": reason}, output=[],
                       usage={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0})
    job = submit(library)
    worker(library, lambda _: httpx.Response(200, json=payload, headers={"x-request-id": "req_diagnostic"})).run_once()
    error = json.loads(library.required("jobs", job["id"])["error_json"])
    call = library.one("SELECT * FROM llm_calls")
    details = error["details"]
    assert details["incomplete_reason"] == reason
    assert details["provider_status"] == "incomplete"
    assert details["provider_request_id"] == "req_diagnostic"
    assert details["provider_response_id"] == "resp_test"
    assert details["http_status"] == 200
    assert details["usage"]["total_tokens"] == 0
    assert details["duration_ms"] >= 0
    assert details["response_asset_id"] == call["response_asset_id"]
    assert details["request_asset_id"] == call["request_asset_id"]
    assert details["job_id"] == job["id"] and details["recipe_id"] == job["recipe_id"]
    assert json.loads(call["error_json"])["details"] == details
    if reason == "content_filter":
        assert "content filter" in error["message"]
    if reason == "max_output_tokens":
        assert "token limit" in error["message"]
    finished = [r for r in caplog.records if r.getMessage().startswith("openai_call_finished ")]
    assert len(finished) == 1
    event = json.loads(finished[0].getMessage().split(" ", 1)[1])
    assert event["call_id"] == call["id"] and event["incomplete_reason"] == reason
    assert event["status"] == "failed" and event["error_code"] == "incomplete_response"
    assert not library.all("SELECT * FROM recipe_revisions")
    assert SOURCE not in caplog.text and "test-secret-never-archive" not in caplog.text


@pytest.mark.parametrize("kind", ["http", "timeout", "malformed", "schema", "success", "cancelled"])
def test_provider_diagnostics_are_safe_and_cover_outcomes(library, caplog, kind):
    caplog.set_level(logging.INFO, logger="server.recipes.openai")
    job = submit(library)
    private = "PRIVATE RECIPE TEXT and credential test-secret-never-archive"
    def handler(request):
        if kind == "timeout":
            raise httpx.ReadTimeout(private, request=request)
        if kind == "http":
            return httpx.Response(429, json={"error": {"code": "rate_limit_exceeded", "type": "requests", "message": private},
                                            "id": "test-secret-never-archive", "usage": {"input_tokens": private}},
                                  headers={"x-request-id": "test-secret-never-archive"})
        if kind == "malformed":
            return httpx.Response(200, content=private.encode())
        if kind == "schema":
            payload = response()
            payload["output"][0]["content"][0]["text"] = dumps({"title": private})
            return httpx.Response(200, json=payload)
        if kind == "cancelled":
            library.cancel(job["id"])
        return httpx.Response(200, json=response())
    worker(library, handler).run_once()
    call = library.one("SELECT * FROM llm_calls")
    events = [json.loads(r.getMessage().split(" ", 1)[1]) for r in caplog.records
              if r.getMessage().startswith("openai_call_finished ")]
    assert len(events) == 1
    event = events[0]
    assert event["status"] == ("succeeded" if kind == "success" else "abandoned" if kind == "cancelled" else "failed")
    assert private not in caplog.text and "test-secret-never-archive" not in caplog.text
    assert "PRIVATE RECIPE TEXT" not in (call["error_json"] or "")
    assert "test-secret-never-archive" not in (call["error_json"] or "")
    if kind == "http":
        assert event["provider_error_code"] == "rate_limit_exceeded"
        assert event["provider_error_type"] == "requests" and event["http_status"] == 429
        assert event["provider_request_id"] is None
    elif kind == "timeout":
        assert event["exception_type"] == "ReadTimeout"
        assert event["error_code"] == "provider_timeout"
        assert call["response_asset_id"] is None
    elif kind == "schema":
        assert event["validation_error_count"] > 0
    elif kind == "success":
        assert event["usage"]["input_tokens"] == 150
        assert event["error_code"] is None


def test_cancellation_during_network_call_discards_result_but_preserves_response(library):
    job = submit(library)
    def handler(request):
        library.cancel(job["id"])
        return httpx.Response(200, json=response())
    worker(library, handler).run_once()
    assert library.required("jobs", job["id"])["status"] == "cancelled"
    assert not library.all("SELECT * FROM recipe_revisions")
    call = library.one("SELECT * FROM llm_calls")
    assert call["status"] == "abandoned" and call["response_asset_id"]


def test_cancel_pending_capture_marks_it_failed(library):
    job = library.submit("local", "url", "url", "fingerprint", url="https://example.com/recipe")
    library.cancel(job["id"])
    assert library.required("source_captures", job["source_capture_id"])["state"] == "failed"


def test_expired_worker_cannot_commit_and_recovery_has_new_attempt(library):
    original = submit(library)
    stale = library.claim()
    library.execute("UPDATE jobs SET lease_expires_at='2000-01-01T00:00:00.000+00:00' WHERE id=?", (original["id"],))
    with pytest.raises(Problem):
        library.complete_extraction(stale, to_markdown(ExtractedRecipe(**RECIPE)), RECIPE["title"], [])
    worker(library).run_once()
    attempts = library.all("SELECT * FROM job_attempts ORDER BY attempt_number")
    assert [a["status"] for a in attempts] == ["abandoned", "succeeded"]
    assert len(library.all("SELECT * FROM recipe_revisions")) == 1


def test_two_workers_cannot_claim_same_job(library):
    submit(library)
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda _: library.claim(), range(2)))
    assert sum(c is not None for c in claims) == 1


def test_revision_and_next_stage_roll_back_together(library):
    job = submit(library)
    with patch.object(library, "enqueue", side_effect=OSError("simulated queue persistence failure")):
        worker(library).run_once()
    assert library.required("jobs", job["id"])["status"] == "failed"
    assert not library.all("SELECT * FROM recipe_revisions")
    assert library.required("recipes", job["recipe_id"])["current_revision_id"] is None
    assert library.one("SELECT * FROM llm_calls")["response_asset_id"]


def test_edits_conflict_and_reextract_does_not_overwrite(library):
    job, detail = finish(library)
    service = ProcessingService(library)
    current = detail["revisions"][0]
    extract_job = service.reextract(job["recipe_id"], job["source_capture_id"], key="again")
    edited = current["markdown"].replace("title: Lemon chickpea salad", "title: My salad")
    result = service.save_markdown(job["recipe_id"], edited, expected_lock_version=detail["lock_version"])
    with pytest.raises(Problem) as conflict:
        service.save_markdown(job["recipe_id"], edited, expected_lock_version=detail["lock_version"])
    assert conflict.value.status == 409
    runner = worker(library)
    while runner.run_once():
        pass
    recipe = library.detail(job["recipe_id"])
    assert recipe["current_revision_id"] == result["revision_id"]
    candidate = service.job_detail(extract_job["id"])["result_revision_ids"][0]
    assert candidate != result["revision_id"]
    library.accept(job["recipe_id"], candidate, recipe["lock_version"])
    assert library.required("recipes", job["recipe_id"])["current_revision_id"] == candidate


def test_edit_renders_without_api_key_and_cache_reuses_bytes(library):
    job, detail = finish(library)
    revision = detail["revisions"][0]
    service = ProcessingService(library)
    service.render(job["recipe_id"], revision["id"], key="render-again")
    with patch("server.recipes.renderer.render_pages", side_effect=AssertionError("Cache missed")):
        Worker(library, ProcessingConfig()).run_once()
    assert len(library.all("SELECT * FROM render_sets")) == 1
    service.save_markdown(job["recipe_id"], revision["markdown"].replace("title: Lemon", "title: Fresh lemon"), expected_lock_version=detail["lock_version"])
    Worker(library, ProcessingConfig()).run_once()
    assert len(library.all("SELECT * FROM llm_calls")) == 1
    assert len(library.all("SELECT * FROM render_sets")) == 2


def test_overflow_keeps_complete_markdown_and_no_publishable_image(library):
    long_recipe = deepcopy(RECIPE)
    long_recipe["method"][0]["items"] = ["Stir for 2 minutes and preserve every detail in this unusually long method step."] * 100
    job = submit(library)
    runner = worker(library, lambda request: httpx.Response(200, json=response(long_recipe)))
    runner.run_once()
    runner.run_once()
    detail = library.detail(job["recipe_id"])
    assert detail["current_revision_id"] and len(detail["revisions"]) == 1
    assert detail["revisions"][0]["markdown"].count("Stir for") == 100
    assert detail["renders"] == []
    failed = next(j for j in detail["jobs"] if j["kind"] == "render")
    assert json.loads(failed["error_json"])["code"] == "render_overflow"


def test_image_order_and_actual_normalized_bytes_preserved_in_request(library):
    images = []
    for color in ("red", "blue"):
        out = BytesIO()
        Image.new("RGB", (32, 48), color).save(out, format="PNG")
        images.append((color + ".png", out.getvalue()))
    job = ProcessingService(library).submit_images(images, key="photos")
    worker(library).run_once()
    detail = library.detail(job["recipe_id"])
    originals = detail["captures"][0]["assets"]
    assert [a["original_filename"] for a in originals] == ["red.png", "blue.png"]
    assert [library.asset_bytes(a["asset_id"]) for a in originals] == [data for _, data in images]
    call = library.one("SELECT * FROM llm_calls")
    content = json.loads(library.asset_bytes(call["request_asset_id"]))["input"][1]["content"]
    import base64
    for part, color in zip(content, [(255, 0, 0), (0, 0, 255)]):
        with Image.open(BytesIO(base64.b64decode(part["image_url"].split(",", 1)[1]))) as image:
            assert image.getpixel((0, 0)) == color


def test_image_normalization_preserves_orientation_and_flattens_transparency():
    from server.recipes.processing import normalized_image
    out=BytesIO();photo=Image.new('RGB',(3000,1500),'red')
    exif=Image.Exif();exif[274]=6
    photo.save(out,format='JPEG',exif=exif)
    with Image.open(BytesIO(normalized_image(out.getvalue()))) as result:
        assert result.size==(1024,2048)
        assert result.mode=='RGB'
        assert result.getexif().get(274,1)==1
    out=BytesIO();Image.new('RGBA',(20,10),(0,0,0,0)).save(out,format='PNG')
    with Image.open(BytesIO(normalized_image(out.getvalue()))) as result:
        assert result.getpixel((0,0))==(255,255,255)


def test_damaged_image_rejected_by_processing_before_persistence(library):
    out=BytesIO();Image.new('RGB',(32,32),'red').save(out,format='JPEG')
    with pytest.raises(RecipeError,match='decoded'):
        ProcessingService(library).submit_images([('broken.jpg',out.getvalue()[:-10])],key='broken-photo')
    assert not library.all('SELECT * FROM recipes')


@pytest.mark.parametrize("change", [
    lambda m: m.replace("format: erecipe/v1", "format: erecipe/v2"),
    lambda m: m.replace("format: erecipe/v1", "format: erecipe/v1\nformat: erecipe/v1"),
    lambda m: m.replace("## Method", "## Directions"),
    lambda m: m + '\n<script>alert(1)</script>\n',
    lambda m: m.replace("2 cans chickpeas, drained", "[click](https://example.com)"),
    lambda m: m.replace("- 2 cans", "  - 2 cans\n    - nested"),
])
def test_invalid_markdown_rejected(change):
    with pytest.raises(RecipeError):
        parse_markdown(change(to_markdown(ExtractedRecipe(**RECIPE))))


def test_markdown_literal_roundtrip_preserves_fractions_and_special_characters():
    recipe = deepcopy(RECIPE)
    recipe["ingredients"][0]["items"] = ["1½ cups flour (sifted)", "1/4–1/2 tsp salt", "2 *special* [ingredients] <not HTML>"]
    recipe["method"][0]["items"] = ["Bake at 180°C / 350°F for 20–25 minutes; do not substitute."]
    doc = parse_markdown(to_markdown(ExtractedRecipe(**recipe)))
    assert doc["ingredients"][0]["items"] == recipe["ingredients"][0]["items"]
    assert doc["method"][0]["items"] == recipe["method"][0]["items"]


def test_cross_recipe_current_revision_constraint(library):
    _, one = finish(library)
    other = submit(library, key="other")
    with pytest.raises(sqlite3.IntegrityError):
        library.execute("UPDATE recipes SET current_revision_id=? WHERE id=?", (one["current_revision_id"], other["recipe_id"]))


def test_retry_limit_is_explicit(library):
    job = submit(library)
    for attempt in range(5):
        if attempt:
            library.retry(job["id"])
        Worker(library, ProcessingConfig()).run_once()
    with pytest.raises(Problem):
        library.retry(job["id"])


@pytest.mark.parametrize("damage", ["delete", "corrupt"])
def test_render_cache_repairs_files_without_an_llm(library, damage):
    job, detail = finish(library)
    render = detail["renders"][0]
    page = render["pages"][0]
    asset_path = library.asset_path(page["asset_id"])
    original = asset_path.read_bytes()
    if damage == "delete":
        asset_path.unlink()
    else:
        asset_path.write_bytes(b"corrupt fixture")
    ProcessingService(library).render(job["recipe_id"], detail["current_revision_id"], key="repair")
    Worker(library, ProcessingConfig()).run_once()
    assert asset_path.read_bytes() == original
    assert len(library.all("SELECT * FROM llm_calls")) == 1
    assert len(library.all("SELECT * FROM render_sets")) == 1


def test_version_one_database_migrates_without_losing_jobs(tmp_path):
    schema = Path(__file__).resolve().parents[1].joinpath("recipes/schema.sql").read_text()
    with sqlite3.connect(tmp_path / "recipes.sqlite3") as c:
        c.executescript(schema.replace("    parent_job_id TEXT REFERENCES jobs(id),\n", ""))
        c.execute("INSERT INTO users VALUES ('existing','Existing','2026-01-01',NULL)")
        c.execute("INSERT INTO schema_versions VALUES (1,'2026-01-01')")
    library = Library(tmp_path)
    assert library.one("SELECT display_name FROM users WHERE id='existing'")["display_name"] == "Existing"
    assert library.one("SELECT max(version) AS version FROM schema_versions")["version"] == 2
    assert "parent_job_id" in [r["name"] for r in library.all("PRAGMA table_info(jobs)")]
    finish(library)


def test_sqlite_constraint_prevents_orphan_render_pages(library):
    with pytest.raises(sqlite3.IntegrityError):
        library.execute("INSERT INTO render_pages VALUES ('missing-render',1,'missing-asset')")


def test_empty_markdown_item_is_not_silently_dropped():
    markdown = to_markdown(ExtractedRecipe(**RECIPE)).replace("## Method", "-\n\n## Method")
    with pytest.raises(RecipeError):
        parse_markdown(markdown)


def test_archived_recipe_rejects_late_extraction(library):
    job = submit(library)
    def handler(request):
        library.execute("UPDATE recipes SET archived_at='2026-01-01' WHERE id=?", (job["recipe_id"],))
        return httpx.Response(200, json=response())
    worker(library, handler).run_once()
    assert not library.all("SELECT * FROM recipe_revisions")
    assert library.required("jobs", job["id"])["status"] == "failed"


def test_previously_captured_url_evidence_uses_saved_text_and_json(library):
    job = library.submit("local", "url", "url", "url-fingerprint", url="https://example.com/salad")
    text_asset = library.asset(SOURCE.encode(), "text/plain")
    json_asset = library.json_asset({"@type": "Recipe", "name": "Lemon chickpea salad"})
    # Simulate an acquisition component completing its own transaction. No website fetched.
    with library.connect(write=True) as c:
        c.execute("UPDATE source_captures SET state='complete' WHERE id=?", (job["source_capture_id"],))
        c.execute("INSERT INTO source_assets VALUES (?,'visible_text',0,?,NULL)", (job["source_capture_id"], text_asset))
        c.execute("INSERT INTO source_assets VALUES (?,'structured_recipe_data',0,?,NULL)", (job["source_capture_id"], json_asset))
        c.execute("UPDATE jobs SET status='succeeded' WHERE id=?", (job["id"],))
    ProcessingService(library).reextract(job["recipe_id"], job["source_capture_id"], key="url-extract")
    worker(library).run_once()
    call = library.one("SELECT * FROM llm_calls")
    contents = json.loads(library.asset_bytes(call["request_asset_id"]))["input"][1]["content"]
    assert len(contents) == 2 and all(part["type"] == "input_text" for part in contents)


def test_chicken_tacos_fits_native_screen_with_all_content():
    from PIL import ImageDraw
    markdown = Path(__file__).resolve().parents[1].joinpath("fixtures/chicken-tacos.formatted.md").read_text()
    document = parse_markdown(markdown)
    drawn = []
    original = ImageDraw.ImageDraw.text

    def record(draw, xy, text, *args, **kwargs):
        drawn.append(text)
        return original(draw, xy, text, *args, **kwargs)

    with patch.object(ImageDraw.ImageDraw, "text", record):
        pages = renderer.render_pages(markdown)
    assert len(pages) == 1
    with Image.open(BytesIO(pages[0])) as image:
        assert image.size == (1872, 1404) and image.mode == "1"
    rendered_text = " ".join(drawn)
    for groups in (document["ingredients"], document["method"]):
        for group in groups:
            assert group["name"] in rendered_text
            for item in group["items"]:
                assert " ".join(item.split()) in rendered_text
    for note in document["notes"]:
        assert " ".join(note.split()) in rendered_text


def test_renderer_rejects_notes_that_leave_no_body_space():
    recipe = deepcopy(RECIPE)
    recipe["notes"] = ["Preserve this operational detail. " * 30] * 20
    with pytest.raises(RecipeError) as error:
        renderer.render_pages(to_markdown(ExtractedRecipe(**recipe)))
    assert error.value.code == "render_overflow"
