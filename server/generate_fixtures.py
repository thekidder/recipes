"""Regenerate deterministic fixtures: python -m server.generate_fixtures."""
from pathlib import Path
from server.render import render

if __name__ == "__main__":
    output = Path(__file__).resolve().parent / "fixtures"
    output.mkdir(exist_ok=True)
    for kind in ("A", "B", "LONG"):
        path = output / f"{kind}.png"
        path.write_bytes(render(kind, f"fixture-{kind}-v1"))
        print(f"{path.name}: {path.stat().st_size} bytes")
