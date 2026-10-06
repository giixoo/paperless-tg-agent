from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date
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
    assert doc.notes == "Renewed early, cheaper rate."


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


async def test_parses_real_server_document_list_response(
    client: PaperlessClient,
    mock_taxonomy: None,
    respx_mock: respx.MockRouter,
    load_fixture: Callable[[str], dict[str, Any]],
) -> None:
    """Pinned against a real GET /api/documents/?page_size=1 response
    (Paperless-ngx, pasted by the user from a live server), to catch any
    drift between our assumptions and the actual API shape."""
    respx_mock.get("http://paperless.test/api/documents/").respond(
        json=load_fixture("real_documents_list.json")
    )

    docs = await client.recent_documents(limit=1)

    assert len(docs) == 1
    doc = docs[0]
    assert doc.id == 1159
    assert doc.title == "Implant information"
    assert doc.created == date(2026, 10, 4)
    assert doc.correspondent == "PZU"  # id 20, per correspondents.json fixture
    assert doc.tags == ["Inbox"]  # tag id 2, per tags.json fixture
    assert doc.custom_fields == {}
    assert doc.notes is None
    assert doc.original_file_name == "photo_20261004_130125.jpg"
    assert doc.page_count is None


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


async def test_download_document_uses_content_disposition_filename(
    client: PaperlessClient, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get("http://paperless.test/api/documents/412/download/").respond(
        content=b"%PDF-1.4 fake bytes",
        headers={"content-disposition": 'attachment; filename="car_insurance_2026.pdf"'},
    )

    result = await client.download_document(412, original=True)

    assert result is not None
    content, filename = result
    assert content == b"%PDF-1.4 fake bytes"
    assert filename == "car_insurance_2026.pdf"


async def test_download_document_returns_none_on_404(
    client: PaperlessClient, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get("http://paperless.test/api/documents/999/download/").respond(status_code=404)

    result = await client.download_document(999, original=True)

    assert result is None


async def test_download_document_falls_back_to_generic_filename(
    client: PaperlessClient, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get("http://paperless.test/api/documents/412/download/").respond(content=b"data")

    result = await client.download_document(412, original=True)

    assert result is not None
    _, filename = result
    assert filename == "document_412"


async def test_upload_document_handles_bare_string_task_id(
    client: PaperlessClient, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.post("http://paperless.test/api/documents/post_document/")
    route.respond(json="3b1f1e2e-aaaa-bbbb-cccc-1234567890ab")

    task_id = await client.upload_document(b"filebytes", "scan.pdf", title="Scan")

    assert task_id == "3b1f1e2e-aaaa-bbbb-cccc-1234567890ab"
    sent_request = route.calls.last.request
    assert b"scan.pdf" in sent_request.content


async def test_upload_document_handles_dict_task_id(
    client: PaperlessClient, respx_mock: respx.MockRouter
) -> None:
    respx_mock.post("http://paperless.test/api/documents/post_document/").respond(
        json={"task_id": "3b1f1e2e-aaaa-bbbb-cccc-1234567890ab"}
    )

    task_id = await client.upload_document(b"filebytes", "scan.pdf")

    assert task_id == "3b1f1e2e-aaaa-bbbb-cccc-1234567890ab"


async def test_upload_document_raises_on_error_status(
    client: PaperlessClient, respx_mock: respx.MockRouter
) -> None:
    respx_mock.post("http://paperless.test/api/documents/post_document/").respond(
        status_code=400, text="bad request"
    )

    with pytest.raises(PaperlessError):
        await client.upload_document(b"filebytes", "scan.pdf")


async def test_get_task_returns_result_from_bare_list(
    client: PaperlessClient, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get("http://paperless.test/api/tasks/").respond(
        json=[
            {
                "task_id": "abc",
                "status": "SUCCESS",
                "result": "Success. New document id 412 created",
                "related_document": 412,
            }
        ]
    )

    result = await client.get_task("abc")

    assert result is not None
    assert result.status == "SUCCESS"
    assert result.related_document == 412


async def test_get_task_returns_none_when_empty(
    client: PaperlessClient, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get("http://paperless.test/api/tasks/").respond(json=[])

    result = await client.get_task("missing")

    assert result is None


async def test_inbox_tag_ids_filters_hidden_and_non_inbox_tags(
    client: PaperlessClient, mock_taxonomy: None, respx_mock: respx.MockRouter
) -> None:
    # tags.json: id=2 "Inbox" is_inbox_tag=true; id=4 "gpt-processed" is hidden
    ids = await client.taxonomy.inbox_tag_ids()

    assert ids == [2]


async def test_list_by_tag_ids_returns_empty_for_no_tags(client: PaperlessClient) -> None:
    docs = await client.list_by_tag_ids([])

    assert docs == []


async def test_list_by_tag_ids_queries_tags_id_in(
    client: PaperlessClient,
    mock_taxonomy: None,
    respx_mock: respx.MockRouter,
    load_fixture: Callable[[str], dict[str, Any]],
) -> None:
    route = respx_mock.get("http://paperless.test/api/documents/").respond(
        json=load_fixture("documents_recent.json")
    )

    docs = await client.list_by_tag_ids([1, 2], limit=10)

    assert len(docs) == 2
    request = route.calls.last.request
    assert request.url.params["tags__id__in"] == "1,2"
    assert request.url.params["page_size"] == "10"


async def test_bulk_remove_tags_noop_without_tags_or_documents(
    client: PaperlessClient, respx_mock: respx.MockRouter
) -> None:
    await client.bulk_remove_tags([], [1])
    await client.bulk_remove_tags([412], [])

    assert respx_mock.calls.call_count == 0


async def test_bulk_remove_tags_posts_modify_tags(
    client: PaperlessClient, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.post("http://paperless.test/api/documents/bulk_edit/").respond(json="OK")

    await client.bulk_remove_tags([412], [2])

    request = route.calls.last.request
    body = json.loads(request.content)
    assert body == {
        "documents": [412],
        "method": "modify_tags",
        "parameters": {"add_tags": [], "remove_tags": [2]},
    }


async def test_bulk_remove_tags_raises_on_error(
    client: PaperlessClient, respx_mock: respx.MockRouter
) -> None:
    respx_mock.post("http://paperless.test/api/documents/bulk_edit/").respond(status_code=400)

    with pytest.raises(PaperlessError):
        await client.bulk_remove_tags([412], [2])
