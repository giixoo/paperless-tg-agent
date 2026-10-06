from __future__ import annotations

import pytest

from paperbot.config import Settings, load_settings


def test_parses_comma_separated_allowed_users() -> None:
    settings = Settings(
        telegram_bot_token="t",  # noqa: S106
        telegram_allowed_users="123, 456,789",  # type: ignore[arg-type]
        paperless_url="http://paperless.test",  # type: ignore[arg-type]
        paperless_token="pt",  # noqa: S106
        anthropic_api_key="ak",  # noqa: S106
    )
    assert settings.telegram_allowed_users == [123, 456, 789]


def test_parses_single_allowed_user_without_comma() -> None:
    settings = Settings(
        telegram_bot_token="t",  # noqa: S106
        telegram_allowed_users="1",  # type: ignore[arg-type]
        paperless_url="http://paperless.test",  # type: ignore[arg-type]
        paperless_token="pt",  # noqa: S106
        anthropic_api_key="ak",  # noqa: S106
    )
    assert settings.telegram_allowed_users == [1]


def test_default_hidden_tag_prefixes() -> None:
    settings = Settings(
        telegram_bot_token="t",  # noqa: S106
        telegram_allowed_users=[1],
        paperless_url="http://paperless.test",  # type: ignore[arg-type]
        paperless_token="pt",  # noqa: S106
        anthropic_api_key="ak",  # noqa: S106
    )
    assert settings.hidden_tag_prefixes == ["gpt", "sonnet"]


def test_paperless_public_url_defaults_to_paperless_url() -> None:
    settings = Settings(
        telegram_bot_token="t",  # noqa: S106
        telegram_allowed_users=[1],
        paperless_url="http://paperless.test",  # type: ignore[arg-type]
        paperless_token="pt",  # noqa: S106
        anthropic_api_key="ak",  # noqa: S106
    )
    assert str(settings.paperless_public_url_or_default) == "http://paperless.test/"


def test_load_settings_fails_fast_on_missing_required_var(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_ALLOWED_USERS",
        "PAPERLESS_URL",
        "PAPERLESS_TOKEN",
        "ANTHROPIC_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir("/")  # avoid picking up a stray .env in the repo root
    with pytest.raises(SystemExit):
        load_settings()
