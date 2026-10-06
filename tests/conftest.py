from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import respx

from paperbot.config import Settings

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _load_fixture(name: str) -> dict[str, Any]:
    with (FIXTURES_DIR / name).open(encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def load_fixture() -> Callable[[str], dict[str, Any]]:
    return _load_fixture


@pytest.fixture
def settings() -> Settings:
    return Settings(
        telegram_bot_token="test-token",  # noqa: S106
        telegram_allowed_users=[1],
        paperless_url="http://paperless.test",  # type: ignore[arg-type]
        paperless_token="test-paperless-token",  # noqa: S106
        anthropic_api_key="test-anthropic-key",  # noqa: S106
    )


@pytest.fixture
def mock_taxonomy(respx_mock: respx.MockRouter) -> None:
    respx_mock.get("http://paperless.test/api/tags/").respond(json=_load_fixture("tags.json"))
    respx_mock.get("http://paperless.test/api/document_types/").respond(
        json=_load_fixture("document_types.json")
    )
    respx_mock.get("http://paperless.test/api/correspondents/").respond(
        json=_load_fixture("correspondents.json")
    )
    respx_mock.get("http://paperless.test/api/custom_fields/").respond(
        json=_load_fixture("custom_fields.json")
    )
