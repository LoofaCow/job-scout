"""
Application config — typed settings loaded from .env.

Every spine and building module imports `settings` from here instead of
reading environment variables directly. This gives us autocomplete, type
safety, and a single source of truth.
"""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


# Project root (G:\agent\job-scout) — useful for resolving relative paths
PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    # === Ollama (local) ===
    OLLAMA_BASE_URL: str = "http://localhost:11434"

    # === Featherless (heavy tier) ===
    FEATHERLESS_API_KEY: str = ""
    FEATHERLESS_BASE_URL: str = "https://api.featherless.ai/v1"

    # === NanoGPT (frontier tier) ===
    NANOGPT_API_KEY: str = ""
    NANOGPT_BASE_URL: str = "https://nano-gpt.com/api/v1"

    # === Database ===
    DATABASE_URL: str = "sqlite:///./job_scout.db"

    # === Default models per tier ===
    MODEL_LOCAL: str = "qwen2.5:7b-instruct"
    MODEL_HEAVY: str = "meta-llama/Meta-Llama-3.1-70B-Instruct"
    MODEL_FRONTIER: str = "claude-sonnet-4-5"

    # Tells pydantic-settings to load from .env in the project root
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",  # ignore env vars we don't declare
    )


# Singleton — import `settings` anywhere you need config values
settings = Settings()