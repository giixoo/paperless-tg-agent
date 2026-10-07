from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from paperbot.config import Settings
from paperbot.dups.db import DupsStore
from paperbot.dups.scan import scan_once
from paperbot.paperless import PaperlessClient

EMPTY_PAGE = {"count": 0, "next": None, "previous": None, "results": []}


def _doc(
    doc_id: int,
    content: str,
    *,
    tags: list[int] | None = None,
    duplicate_documents: list[int] | None = None,
) -> dict[str, object]:
    return {
        "id": doc_id,
        "correspondent": None,
        "document_type": None,
        "title": f"Doc {doc_id}",
        "content": content,
        "tags": tags or [],
        "created": "2026-01-01",
        "notes": [],
        "custom_fields": [],
        "page_count": 1,
        "mime_type": "application/pdf",
        "original_file_name": f"doc{doc_id}.pdf",
        "duplicate_documents": duplicate_documents or [],
    }


@pytest.fixture
def settings() -> Settings:
    return Settings(
        telegram_bot_token="test-token",  # noqa: S106
        telegram_allowed_users=[1],
        paperless_url="http://paperless.test",  # type: ignore[arg-type]
        paperless_token="test-paperless-token",  # noqa: S106
        anthropic_api_key="test-anthropic-key",  # noqa: S106
        dups_min_chars=10,
    )


@pytest.fixture
async def client(settings: Settings) -> PaperlessClient:
    async with httpx.AsyncClient() as http_client:
        yield PaperlessClient(settings, http_client)


def _mock_taxonomy_empty(respx_mock: respx.MockRouter) -> None:
    respx_mock.get("http://paperless.test/api/tags/").respond(json=EMPTY_PAGE)
    respx_mock.get("http://paperless.test/api/document_types/").respond(json=EMPTY_PAGE)
    respx_mock.get("http://paperless.test/api/correspondents/").respond(json=EMPTY_PAGE)
    respx_mock.get("http://paperless.test/api/custom_fields/").respond(json=EMPTY_PAGE)


async def test_scan_finds_text_similarity_candidate(
    client: PaperlessClient, settings: Settings, respx_mock: respx.MockRouter, tmp_path: Path
) -> None:
    _mock_taxonomy_empty(respx_mock)
    boilerplate = "insurance policy for the car valid until the end of the year. " * 4
    respx_mock.get("http://paperless.test/api/documents/").respond(
        json={
            "count": 2,
            "next": None,
            "previous": None,
            "results": [_doc(1, boilerplate), _doc(2, boilerplate)],
        }
    )
    db = DupsStore(tmp_path / "dups.sqlite3")

    new_count = await scan_once(client, db, settings)

    assert new_count == 1
    pair = await db.next_open_pair()
    assert pair is not None
    assert (pair.a_id, pair.b_id) == (1, 2)
    assert pair.text_sim == 1.0


async def test_scan_skips_documents_under_min_chars(
    client: PaperlessClient, settings: Settings, respx_mock: respx.MockRouter, tmp_path: Path
) -> None:
    _mock_taxonomy_empty(respx_mock)
    respx_mock.get("http://paperless.test/api/documents/").respond(
        json={
            "count": 2,
            "next": None,
            "previous": None,
            "results": [_doc(1, "short"), _doc(2, "short")],
        }
    )
    db = DupsStore(tmp_path / "dups.sqlite3")

    new_count = await scan_once(client, db, settings)

    assert new_count == 0
    assert await db.next_open_pair() is None


async def test_scan_skips_documents_with_skip_tags(
    client: PaperlessClient, respx_mock: respx.MockRouter, tmp_path: Path
) -> None:
    settings = Settings(
        telegram_bot_token="test-token",  # noqa: S106
        telegram_allowed_users=[1],
        paperless_url="http://paperless.test",  # type: ignore[arg-type]
        paperless_token="test-paperless-token",  # noqa: S106
        anthropic_api_key="test-anthropic-key",  # noqa: S106
        dups_min_chars=10,
        dups_skip_tags="gpt-ocr",  # type: ignore[arg-type]
    )
    _mock_taxonomy_empty(respx_mock)
    respx_mock.get("http://paperless.test/api/tags/").respond(
        json={
            "count": 1,
            "next": None,
            "previous": None,
            "results": [{"id": 9, "name": "gpt-ocr", "is_inbox_tag": False}],
        }
    )
    boilerplate = "insurance policy for the car valid until the end of the year. " * 4
    respx_mock.get("http://paperless.test/api/documents/").respond(
        json={
            "count": 2,
            "next": None,
            "previous": None,
            "results": [
                _doc(1, boilerplate, tags=[9]),  # has skip tag -> excluded
                _doc(2, boilerplate),
            ],
        }
    )
    db = DupsStore(tmp_path / "dups.sqlite3")

    new_count = await scan_once(client, db, settings)

    assert new_count == 0


async def test_scan_uses_duplicate_documents_field_for_exact_pairs(
    client: PaperlessClient, settings: Settings, respx_mock: respx.MockRouter, tmp_path: Path
) -> None:
    _mock_taxonomy_empty(respx_mock)
    respx_mock.get("http://paperless.test/api/documents/").respond(
        json={
            "count": 2,
            "next": None,
            "previous": None,
            "results": [
                _doc(1, "short a", duplicate_documents=[2]),
                _doc(2, "short b", duplicate_documents=[1]),
            ],
        }
    )
    db = DupsStore(tmp_path / "dups.sqlite3")

    new_count = await scan_once(client, db, settings)

    assert new_count == 1
    pair = await db.next_open_pair()
    assert pair is not None
    assert pair.text_sim == 1.0
    assert pair.num_sim == 1.0


async def test_scan_forms_with_different_numbers_are_not_candidates(
    client: PaperlessClient, settings: Settings, respx_mock: respx.MockRouter, tmp_path: Path
) -> None:
    _mock_taxonomy_empty(respx_mock)
    boilerplate = (
        "This is an official tax declaration form issued by the local tax office for "
        "the fiscal year under review. The taxpayer identified in the records below "
        "must settle the stated amount by the specified deadline printed on this "
        "notice. Failure to pay by the deadline may result in additional penalties "
        "and interest charges being applied to the outstanding balance. Please keep "
        "this document for your personal records and contact the regional tax "
        "office if you have any questions regarding this notice. This notice was "
        "generated automatically and does not require a signature to be considered "
        "valid for the purposes described above. "
    )
    doc_a = boilerplate + "Year: 2025. Amount due: 1200.50 USD. Reference: AB-2025-001."
    doc_b = boilerplate + "Year: 2026. Amount due: 1450.75 USD. Reference: AB-2026-002."
    respx_mock.get("http://paperless.test/api/documents/").respond(
        json={
            "count": 2,
            "next": None,
            "previous": None,
            "results": [_doc(1, doc_a), _doc(2, doc_b)],
        }
    )
    db = DupsStore(tmp_path / "dups.sqlite3")

    new_count = await scan_once(client, db, settings)

    assert new_count == 0


async def test_scan_caches_signature_and_skips_recompute_when_unchanged(
    client: PaperlessClient, settings: Settings, respx_mock: respx.MockRouter, tmp_path: Path
) -> None:
    _mock_taxonomy_empty(respx_mock)
    boilerplate = "insurance policy for the car valid until the end of the year. " * 4
    respx_mock.get("http://paperless.test/api/documents/").respond(
        json={
            "count": 2,
            "next": None,
            "previous": None,
            "results": [_doc(1, boilerplate), _doc(2, boilerplate)],
        }
    )
    db = DupsStore(tmp_path / "dups.sqlite3")

    await scan_once(client, db, settings)
    cached = await db.get_doc_cache(1)

    assert cached is not None
    assert cached.shingles  # populated from the first scan
