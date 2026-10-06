from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest
import respx

from paperbot.config import Settings
from paperbot.paperless import PaperlessClient, PaperlessError


@pytest.fixture
async def client(settings: Settings) -> PaperlessClient:
    async with httpx.AsyncClient() as http_client:
        yield PaperlessClient(settings, http_client)


async def test_search_documents_resolves_names_and_strips_hidden_tags(
    client: PaperlessClient,
    mock_taxonomy: None,
    respx_mock: respx.MockRouter,
    load_fixture: Callable[[str], dict[str, Any]],
) -> None:
    respx_mock.get("http://paperless.test/api/documents/").respond(
        json=load_fixture("documents_search.json")
    )

    docs, total = await client.search_documents("insurance")

    assert total == 1
    assert len(docs) == 1
    doc = docs[0]
    assert doc.id == 412
    assert doc.title == "Car insurance 2026"
    assert doc.correspondent == "PZU"
    assert doc.document_type == "Insurance policy"
    # "gpt-processed" (tag id 4) matches HIDDEN_TAG_PREFIXES default ("gpt") and must not appear
    assert doc.tags == ["Insurance"]
    assert doc.custom_fields == {"Expires": "14.12.2026"}
    assert doc.snippet is not None and "14.12.2026" in doc.snippet


async def test_get_document_returns_none_on_404(
    client: PaperlessClient, mock_taxonomy: None, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get("http://paperless.test/api/documents/999/").respond(status_code=404)

    doc = await client.get_document(999)

    assert doc is None


async def test_get_document_returns_full_detail(
    client: PaperlessClient,
    mock_taxonomy: None,
    respx_mock: respx.MockRouter,
    load_fixture: Callable[[str], dict[str, Any]],
) -> None:
    respx_mock.get("http://paperless.test/api/documents/412/").respond(
        json=load_fixture("document_detail.json")
    )

    doc = await client.get_document(412)

    assert doc is not None
    assert doc.page_count == 3
    assert doc.custom_fields == {"Expires": "14.12.2026"}


async def test_recent_documents_orders_by_created_desc(
    client: PaperlessClient,
    mock_taxonomy: None,
    respx_mock: respx.MockRouter,
    load_fixture: Callable[[str], dict[str, Any]],
) -> None:
    route = respx_mock.get("http://paperless.test/api/documents/").respond(
        json=load_fixture("documents_recent.json")
    )

    docs = await client.recent_documents(limit=2)

    assert [d.id for d in docs] == [500, 412]
    request = route.calls.last.request
    assert request.url.params["ordering"] == "-created"
    assert request.url.params["page_size"] == "2"


async def test_get_json_retries_on_5xx_then_succeeds(
    client: PaperlessClient,
    respx_mock: respx.MockRouter,
    load_fixture: Callable[[str], dict[str, Any]],
) -> None:
    route = respx_mock.get("http://paperless.test/api/tags/")
    route.side_effect = [
        httpx.Response(503),
        httpx.Response(200, json=load_fixture("tags.json")),
    ]

    tags = await client.list_tags_raw()

    assert len(tags) == 4
    assert route.call_count == 2


async def test_get_json_raises_after_exhausting_retries(
    client: PaperlessClient, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get("http://paperless.test/api/tags/").respond(status_code=503)

    with pytest.raises(PaperlessError):
        await client.list_tags_raw()
