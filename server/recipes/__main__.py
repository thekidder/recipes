"""Operator CLI for the processing domain; independent of the in-progress HTTP API."""
import argparse
import json
from pathlib import Path
import signal

from .config import ProcessingConfig
from .db import Library, Problem, uid
from .processing import ProcessingService, Worker


def main():
    parser = argparse.ArgumentParser(description="eRecipe durable processing jobs")
    parser.add_argument("--data-dir", type=Path, default=Path(__file__).resolve().parents[1] / "data")
    commands = parser.add_subparsers(dest="command", required=True)
    text = commands.add_parser("import-text", help="Save a UTF-8 file and enqueue extraction")
    text.add_argument("path", type=Path)
    text.add_argument("--key", default=None)
    images = commands.add_parser("import-images", help="Save ordered JPEG/PNG/WebP originals and enqueue extraction")
    images.add_argument("paths", type=Path, nargs="+")
    images.add_argument("--key", default=None)
    url = commands.add_parser("import-url", help="Capture a public recipe webpage and enqueue extraction")
    url.add_argument("url")
    url.add_argument("--key", default=None)
    worker = commands.add_parser("worker", help="Run extraction and rendering jobs (may make billable OpenAI calls)")
    modes = worker.add_mutually_exclusive_group()
    modes.add_argument("--once", action="store_true", help="Process at most one stage")
    modes.add_argument("--drain", action="store_true", help="Process available stages until idle")
    commands.add_parser("status", help="Show provider readiness without credentials")
    commands.add_parser("list", help="List imported recipes")
    for name in ("job", "recipe", "retry", "cancel"):
        commands.add_parser(name).add_argument("id")
    edit = commands.add_parser("edit", help="Save edited Markdown and enqueue rendering")
    edit.add_argument("recipe_id")
    edit.add_argument("path", type=Path)
    edit.add_argument("--expected-version", type=int, required=True)
    extract = commands.add_parser("extract", help="Re-extract a saved capture as a candidate")
    extract.add_argument("recipe_id")
    extract.add_argument("capture_id")
    extract.add_argument("--key", default=None)
    render = commands.add_parser("render", help="Ensure a cached render without an LLM call")
    render.add_argument("recipe_id")
    render.add_argument("revision_id")
    render.add_argument("--key", default=None)
    preview = commands.add_parser("export-preview", help="Copy a cached render page to a PNG file")
    preview.add_argument("render_set_id")
    preview.add_argument("path", type=Path)
    preview.add_argument("--page", type=int, default=1)
    args = parser.parse_args()
    library, config = Library(args.data_dir), ProcessingConfig.from_env()
    service = ProcessingService(library)
    try:
        if args.command == "import-text":
            result = service.submit_text(args.path.read_text(encoding="utf-8"), key=args.key or uid())
        elif args.command == "import-images":
            result = service.submit_images([(p.name, p.read_bytes()) for p in args.paths], key=args.key or uid())
        elif args.command == "import-url":
            result = service.submit_url(args.url, key=args.key or uid())
        elif args.command == "worker":
            runner = Worker(library, config)
            signal.signal(signal.SIGTERM, lambda *_: runner.stop())
            signal.signal(signal.SIGINT, lambda *_: runner.stop())
            if args.once:
                result = {"processed": runner.run_once()}
            elif args.drain:
                count = 0
                while not runner.stop_event.is_set() and runner.run_once():
                    count += 1
                result = {"processed_stages": count}
            else:
                runner.run()
                result = {"status": "stopped"}
        elif args.command == "status":
            result = {**config.readiness(), "worker_kinds": ["capture", "extract", "render"], "url_capture_implemented": True}
        elif args.command == "list":
            result = library.all("SELECT r.*,v.title FROM recipes r LEFT JOIN recipe_revisions v ON v.id=r.current_revision_id ORDER BY r.updated_at DESC,r.id")
        elif args.command == "job":
            result = service.job_detail(args.id)
        elif args.command == "recipe":
            result = library.detail(args.id)
        elif args.command in {"retry", "cancel"}:
            getattr(library, args.command)(args.id)
            result = service.job_detail(args.id)
        elif args.command == "edit":
            result = service.save_markdown(args.recipe_id, args.path.read_text(encoding="utf-8"), expected_lock_version=args.expected_version)
        elif args.command == "export-preview":
            page = library.one("SELECT asset_id FROM render_pages WHERE render_set_id=? AND page_number=?", (args.render_set_id, args.page))
            if not page:
                raise Problem("Render page not found", 404)
            with args.path.open("xb") as output:
                output.write(library.asset_bytes(page["asset_id"]))
            result = {"path": str(args.path.resolve())}
        elif args.command == "extract":
            result = service.reextract(args.recipe_id, args.capture_id, key=args.key or uid())
        else:
            result = service.render(args.recipe_id, args.revision_id, key=args.key or uid())
    except (Problem, OSError, UnicodeError) as error:
        parser.exit(1, f"{error}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
