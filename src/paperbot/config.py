"""Application configuration, loaded from environment variables.

Per CLAUDE.md: config is only read here; the rest of the app receives a
`Settings` instance rather than reading `os.environ` directly.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import AnyHttpUrl, Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    telegram_bot_token: str
    # NoDecode: pydantic-settings otherwise JSON-decodes the env string before
    # our validator runs, which turns a single bare ID like "1" into the int
    # 1 (valid JSON) rather than leaving it as "1" for our comma-split logic.
    telegram_allowed_users: Annotated[list[int], NoDecode]

    paperless_url: AnyHttpUrl
    paperless_public_url: AnyHttpUrl | None = None
    paperless_token: str
    paperless_api_version: int = 9

    anthropic_api_key: str
    llm_model: str = "claude-haiku-4-5"
    agent_max_steps: int = 5
    agent_max_tokens: int = 1024
    doc_content_max_chars: int = 3000
    history_turns: int = 6

    daily_budget_usd: float = 0.20
    price_input_per_mtok: float = 1.00
    price_output_per_mtok: float = 5.00
    price_cache_write_per_mtok: float = 1.25
    price_cache_read_per_mtok: float = 0.10

    expiry_field_name: str = "Expires"
    reminder_enabled: bool = True
    reminder_time: str = "09:00"
    reminder_days: int = 30

    hidden_tag_prefixes: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["gpt", "sonnet"]
    )

    tz: str = "Europe/Warsaw"
    data_dir: str = "/data"
    health_port: int = 8080
    log_level: str = "INFO"

    @field_validator("telegram_allowed_users", mode="before")
    @classmethod
    def _parse_allowed_users(cls, value: object) -> object:
        if isinstance(value, str):
            return [int(v.strip()) for v in value.split(",") if v.strip()]
        return value

    @field_validator("hidden_tag_prefixes", mode="before")
    @classmethod
    def _parse_hidden_tag_prefixes(cls, value: object) -> object:
        if isinstance(value, str):
            return [v.strip() for v in value.split(",") if v.strip()]
        return value

    @property
    def paperless_public_url_or_default(self) -> AnyHttpUrl:
        return self.paperless_public_url or self.paperless_url


def load_settings() -> Settings:
    """Load settings, failing fast with a clear message on invalid config."""
    try:
        return Settings()  # type: ignore[call-arg]
    except Exception as exc:  # pydantic ValidationError et al.
        raise SystemExit(f"Invalid configuration: {exc}") from exc
