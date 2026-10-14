"""Explicit server-side configuration; never load a credential into a client response."""
from dataclasses import dataclass, field
import os
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class ProcessingConfig:
    api_key: str = field(default="", repr=False)
    model: str = "gpt-4.1-mini"
    timeout_seconds: float = 60
    max_output_tokens: int = 10000

    @classmethod
    def from_env(cls, env_file=None):
        # Only the explicit project file is loaded, never an upward directory search.
        location = Path(env_file) if env_file else Path(__file__).resolve().parents[1] / ".env"
        load_dotenv(location, override=False)
        return cls(api_key=os.environ.get("OPENAI_API_KEY", "").strip(),
                   model=os.environ.get("ERECIPE_OPENAI_MODEL", "gpt-4.1-mini").strip() or "gpt-4.1-mini")

    def readiness(self):
        return {"provider": "openai", "model": self.model, "configured": bool(self.api_key)}
