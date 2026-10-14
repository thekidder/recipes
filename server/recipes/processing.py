"""Domain operations for API adapters and a restart-safe sequential job worker.

The worker never publishes. Captures, accepted revisions and device selection have
separate lifecycles. Network and rendering work run outside SQLite transactions.
"""
import base64
from io import BytesIO
import json
import logging
from threading import Event, Thread

from PIL import Image, ImageOps, UnidentifiedImageError

from .config import ProcessingConfig
from .capture import URLFetcher, Page, validate_url, page_evidence
from .db import Library, Problem, digest, dumps
from .document import RecipeError, parse_markdown, to_markdown
from .openai import OpenAIExtractor
from . import renderer

log = logging.getLogger(__name__)
MAX_TEXT = 100_000
MAX_IMAGES = 6
MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_TOTAL_IMAGE_BYTES = 24 * 1024 * 1024
MAX_IMAGE_PIXELS = 24_000_000
IMAGE_TYPES = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}


def inspect_image(data):
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise RecipeError("invalid_image", "Each image must be nonempty and at most 12 MiB.")
    try:
        with Image.open(BytesIO(data)) as image:
            if image.format not in IMAGE_TYPES or image.width * image.height > MAX_IMAGE_PIXELS:
                raise RecipeError("unsupported_image", "Use JPEG, PNG, or WebP images of at most 24 million pixels. HEIC is not supported yet.")
            if getattr(image, "n_frames", 1) != 1:
                raise RecipeError("unsupported_image", "Animated or multi-frame images are unsupported; upload separate ordered images.")
            media_type = IMAGE_TYPES[image.format]
            image.verify()
        with Image.open(BytesIO(data)) as image:
            image.load()
        return media_type
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, EOFError, Image.DecompressionBombError) as exc:
        raise RecipeError("invalid_image", "The uploaded image could not be decoded.") from exc


def normalized_image(data):
    inspect_image(data)
    try:
        with Image.open(BytesIO(data)) as original:
            # Reduce before allocating orientation, RGBA and compositing copies.
            # JPEG draft decoding also avoids a full camera-sized bitmap.
            original.draft("RGB", (2048, 2048))
            original.thumbnail((2048, 2048))
            image = ImageOps.exif_transpose(original).convert("RGBA")
            background = Image.new("RGBA", image.size, "white")
            background.alpha_composite(image)
            result = BytesIO()
            background.convert("RGB").save(result, format="PNG")
            return result.getvalue()
    except (OSError, ValueError, SyntaxError, EOFError) as exc:
        raise RecipeError("invalid_image", "The image could not be prepared for extraction.") from exc


class ProcessingService:
    """Synchronous durable commands; constructing a service does not start a worker."""
    def __init__(self, library):
        self.library = library

    @staticmethod
    def key(value):
        if not isinstance(value, str) or not value.strip() or len(value) > 200 or value.startswith("internal:"):
            raise RecipeError("invalid_idempotency_key", "Supply a nonempty idempotency key of at most 200 characters, outside the internal: namespace.")
        return value

    def submit_text(self, text, *, user="local", key):
        self.key(key)
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
            raise RecipeError("invalid_text", "Submit nonempty plain text of at most 100,000 characters.")
        data = text.encode("utf-8")
        asset = self.library.asset(data, "text/plain; charset=utf-8")
        return self.library.submit(user, key, "text", digest(dumps(["text", asset]).encode()),
                                   [("original_text", 0, asset, None)])

    def submit_images(self, files, *, user="local", key):
        """files is an ordered sequence of (original_filename, bytes) pairs."""
        self.key(key)
        if not 1 <= len(files) <= MAX_IMAGES or sum(len(data) for _, data in files) > MAX_TOTAL_IMAGE_BYTES:
            raise RecipeError("invalid_images", "Submit 1–6 images totaling at most 24 MiB.")
        types = [inspect_image(data) for _, data in files]  # Validate all before writing.
        assets = []
        for ordinal, ((filename, data), media_type) in enumerate(zip(files, types)):
            if not isinstance(filename, str) or len(filename) > 512:
                raise RecipeError("invalid_image", "Image filenames must be strings of at most 512 characters.")
            asset = self.library.asset(data, media_type)
            assets.append(("original_image", ordinal, asset, filename))
        request_hash = digest(dumps(["images", [(a[2], a[3]) for a in assets]]).encode())
        return self.library.submit(user, key, "images", request_hash, assets)

    def submit_url(self, url, *, user="local", key):
        self.key(key)
        validate_url(url)
        return self.library.submit(user, key, "url", digest(dumps(["url", url]).encode()), url=url)

    def refresh(self, recipe_id, capture_id, *, user="local", key):
        return self.library.request_work(recipe_id, user, self.key(key), "refresh", capture_id=capture_id)

    def save_markdown(self, recipe_id, markdown, *, expected_lock_version, user="local"):
        parsed = parse_markdown(markdown)
        return self.library.edit(recipe_id, user, markdown, parsed["metadata"]["title"], expected_lock_version)

    def reextract(self, recipe_id, capture_id, *, user="local", key):
        return self.library.request_work(recipe_id, user, self.key(key), "extract", capture_id=capture_id)

    def render(self, recipe_id, revision_id, *, user="local", key):
        return self.library.request_work(recipe_id, user, self.key(key), "render", revision_id=revision_id)

    def job_detail(self, identifier):
        job = self.library.required("jobs", identifier)
        job["attempts"] = self.library.all("SELECT * FROM job_attempts WHERE job_id=? ORDER BY attempt_number", (identifier,))
        for attempt in job["attempts"]:
            attempt["llm_calls"] = self.library.all("SELECT * FROM llm_calls WHERE attempt_id=? ORDER BY sequence", (attempt["id"],))
        job["downstream_job_ids"] = [r["id"] for r in self.library.all("SELECT id FROM jobs WHERE parent_job_id=? ORDER BY created_at,id", (identifier,))]
        job["result_revision_ids"] = [r["id"] for r in self.library.all("""SELECT r.id FROM recipe_revisions r
            JOIN job_attempts a ON a.id=r.generated_by_attempt WHERE a.job_id=?""", (identifier,))]
        job["result_render_set_ids"] = [r["id"] for r in self.library.all("SELECT id FROM render_sets WHERE revision_id=?", (job["input_revision_id"],))] if job["kind"] == "render" and job["status"] == "succeeded" else []
        return job


class Worker:
    def __init__(self, library: Library, config=None, *, extractor=None, render_module=renderer, fetcher=None):
        self.library = library
        self.extractor = extractor or OpenAIExtractor(config or ProcessingConfig.from_env())
        self.renderer = render_module
        self.fetcher = fetcher or URLFetcher()
        self.stop_event = Event()

    def capture(self, job):
        capture = self.library.required('source_captures', job['source_capture_id'])
        saved = self.library.one("SELECT asset_id FROM source_assets WHERE capture_id=? AND role='response_html'", (capture['id'],))
        if saved:
            # Resume parsing an already captured response after a crash without
            # silently replacing the original evidence with a newer website.
            metadata = json.loads(capture['capture_metadata_json'])
            page = Page(capture['resolved_url'], self.library.asset_bytes(saved['asset_id']), metadata['content_type'], metadata['redirects'])
        else:
            page = self.fetcher.fetch(capture['submitted_url'])
            raw = self.library.asset(page.body, page.content_type)
            self.library.record_capture_response(job, page.url, raw, dict(content_type=page.content_type, redirects=page.redirects))
        text, structured = page_evidence(page)
        assets = []
        if text:
            assets.append(('visible_text', self.library.asset(text.encode(), 'text/plain; charset=utf-8')))
        if structured:
            assets.append(('structured_recipe_data', self.library.asset(structured.encode(), 'application/json')))
        self.library.complete_capture(job, assets)

    def evidence(self, job):
        capture = self.library.required("source_captures", job["source_capture_id"])
        if capture["state"] != "complete":
            raise RecipeError("incomplete_source", "Source acquisition must complete before extraction.")
        assets = self.library.all("SELECT * FROM source_assets WHERE capture_id=? ORDER BY ordinal,role", (capture["id"],))
        content = []
        text_count = 0
        for asset in assets:
            role = asset["role"]
            if role in {"original_text", "visible_text", "structured_recipe_data"}:
                data = self.library.asset_bytes(asset["asset_id"])
                text_count += len(data)
                if text_count > 400_000:
                    raise RecipeError("source_too_large", "Source evidence is too large for this extraction profile; no evidence was silently truncated.")
                try:
                    text = data.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise RecipeError("invalid_source", "Source text must be UTF-8.") from exc
                content.append({"type": "input_text", "text": f"Source evidence ({role}):\n{text}"})
            elif role == "original_image":
                data = normalized_image(self.library.asset_bytes(asset["asset_id"]))
                content.append({"type": "input_image", "image_url": "data:image/png;base64," + base64.b64encode(data).decode(), "detail": "high"})
        if not content:
            raise RecipeError("missing_source", "No usable source text or images were preserved for this capture.")
        return content

    def extract(self, job):
        recipe = self.extractor.extract(self.library, job, self.evidence(job))
        markdown = to_markdown(recipe)
        self.library.complete_extraction(job, markdown, recipe.title, recipe.warnings)

    def render(self, job):
        revision = self.library.required("recipe_revisions", job["input_revision_id"])
        parameters = json.loads(job["parameters_json"])
        if parameters:
            raise RecipeError("unsupported_render_settings", "This renderer supports only its default display profile and no unit conversions.")
        display_profile = self.renderer.profile()
        key = self.renderer.cache_key(revision, display_profile)
        cached = self.library.one("SELECT * FROM render_sets WHERE cache_key=?", (key,))
        if cached:
            pages = self.library.all("SELECT * FROM render_pages WHERE render_set_id=? ORDER BY page_number", (cached["id"],))
            valid = len(pages) == cached["page_count"] and [p["page_number"] for p in pages] == list(range(1, len(pages) + 1))
            if not valid:
                raise RecipeError("invalid_render_manifest", "The cached page manifest is corrupt. Restore the database before sending.")
            if not all(self.library.asset_path(p["asset_id"]).is_file() and digest(self.library.asset_bytes(p["asset_id"])) == p["asset_id"] for p in pages):
                # Images are a cache: rebuild missing/corrupt bytes without a model call.
                images = self.renderer.render_pages(revision["markdown"])
                if [digest(data) for data in images] != [p["asset_id"] for p in pages]:
                    raise RecipeError("render_mismatch", "Rebuilt images differ for the same renderer profile. Update the renderer version before regenerating.")
                for data in images:
                    self.library.asset(data, "image/png")
            with self.library.connect(write=True) as c:
                self.library.finish(c, job)
            return
        images = self.renderer.render_pages(revision["markdown"])
        assets = [self.library.asset(data, "image/png") for data in images]
        self.library.complete_render(job, key, self.renderer.VERSION, display_profile, assets)

    def _heartbeat(self, job, done):
        while not done.wait(30):
            try:
                self.library.renew(job)
            except Problem:
                return
            except Exception:
                # Never log request payloads, exception strings, or credentials.
                log.error("Could not renew processing lease for job %s", job["id"])
                return

    def run_once(self):
        job = self.library.claim(kinds=("capture", "extract", "render"))
        if not job:
            return False
        done = Event()
        heartbeat = Thread(target=self._heartbeat, args=(job, done), daemon=True)
        heartbeat.start()
        try:
            {"capture": self.capture, "extract": self.extract, "render": self.render}[job["kind"]](job)
        except RecipeError as error:
            self.library.fail(job, error.as_dict())
        except Problem as error:
            self.library.fail(job, {"code": "job_conflict", "message": str(error), "retryable": False, "details": {}})
        except Exception as error:
            log.error("Processing job %s failed (%s)", job["id"], type(error).__name__)
            self.library.fail(job, {"code": "processing_error", "message": "Processing failed unexpectedly. Check server diagnostics; the source was retained.", "retryable": True, "details": {}})
        finally:
            done.set()
            heartbeat.join(timeout=1)
        return True

    def run(self):
        while not self.stop_event.is_set():
            try:
                if not self.run_once():
                    self.stop_event.wait(1)
            except Exception as error:
                log.error("Worker infrastructure error (%s); retained jobs will be retried after lease recovery", type(error).__name__)
                self.stop_event.wait(5)

    def stop(self):
        self.stop_event.set()
