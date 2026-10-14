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
    provider: str = "openai"

    def __post_init__(self):
        if self.provider not in {"openai", "openrouter"}:
            raise ValueError("ERECIPE_LLM_PROVIDER must be openai or openrouter")

    @property
    def provider_name(self):
        return "OpenRouter" if self.provider == "openrouter" else "OpenAI"

    @property
    def configuration_issue(self):
        if not self.api_key:
            return f"Set {self.provider.upper()}_API_KEY in server/.env, restart the worker, and retry this job."
        if not self.model:
            return f"Set ERECIPE_{self.provider.upper()}_MODEL in server/.env, restart the worker, and retry this job."
        return None

    @classmethod
    def from_env(cls, env_file=None):
        # Only the explicit project file is loaded, never an upward directory search.
        location = Path(env_file) if env_file else Path(__file__).resolve().parents[1] / ".env"
        load_dotenv(location, override=False)
        provider = os.environ.get("ERECIPE_LLM_PROVIDER", "openai").strip().lower() or "openai"
        if provider not in {"openai", "openrouter"}:
            raise ValueError("ERECIPE_LLM_PROVIDER must be openai or openrouter")
        default_model = "gpt-4.1-mini" if provider == "openai" else ""
        return cls(provider=provider, api_key=os.environ.get(f"{provider.upper()}_API_KEY", "").strip(),
                   model=os.environ.get(f"ERECIPE_{provider.upper()}_MODEL", default_model).strip() or default_model)

    def readiness(self):
        return {"provider": self.provider, "model": self.model, "configured": self.configuration_issue is None}
