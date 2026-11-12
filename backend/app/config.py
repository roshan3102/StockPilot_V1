"""Application settings, loaded from environment variables and the repo-root `.env` file.

Only settings that exist today are declared here. Later milestones add fields
(database URL, OpenAI key, PayPal credentials) as the code that uses them is written.
"""

from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/app/config.py -> parents[2] is the repository root, where `.env` lives.
REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    # Field names map to env vars case-insensitively: app_env <- APP_ENV.
    app_env: str = "dev"
    frontend_origin: str = "http://localhost:3000"
    # Relative SQLite paths resolve against the current working directory, so run backend
    # commands from `backend/` to always get the same `backend/stockpilot.db` file.
    database_url: str = "sqlite:///./stockpilot.db"

    # PayPal REST app credentials (Sandbox only in this MVP; see integrations/paypal.py, which
    # refuses any non-sandbox URL). Empty means "not configured": the payment endpoints answer
    # 503 instead of crashing. SecretStr hides the value from repr() and logs; call
    # `.get_secret_value()` only where the credential is actually sent.
    paypal_client_id: str = ""
    paypal_client_secret: SecretStr = SecretStr("")
    paypal_base_url: str = "https://api-m.sandbox.paypal.com"

    # LLM provider (OpenAI). Empty key means "not configured": the agent reports a clear error
    # instead of crashing, and the rest of the app keeps working. The model name is
    # configuration, not code, so it can change without a deploy of new code.
    openai_api_key: SecretStr = SecretStr("")
    llm_model: str = "gpt-6-luna"
    llm_timeout_seconds: float = 30.0  # per request to the provider
    # gpt-6-luna rejects function tools on the Chat Completions API unless reasoning is turned
    # off (found by a live call). "" means "do not send the parameter", for models that
    # reject it. Revisit if we move to the Responses API, which supports tools with reasoning.
    llm_reasoning_effort: str = "none"

    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        # `.env` also contains variables we haven't modeled yet (API keys, etc.).
        # Ignore them instead of raising a validation error.
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    """Build Settings once and reuse it (reading .env on every request would be wasteful)."""
    return Settings()
