from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date
from typing import Any

import httpx
import pytest
import respx

from paperbot.config import Settings
from paperbot.paperless import PaperlessClient, PaperlessError, _format_custom_field_value


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
    # tag_ids is raw/unfiltered - the hidden tag id is still there for the AI menu
    assert doc.tag_ids == [1, 4]
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


async def test_non_json_response_raises_paperless_error_not_json_decode_error(
    client: PaperlessClient, respx_mock: respx.MockRouter
) -> None:
    """Regression: a 200 response with a non-JSON (e.g. empty/redirect-page)
    body must surface as PaperlessError, not an uncaught JSONDecodeError
    that would crash ping()/callers whose `except PaperlessError` can't
    catch a bare ValueError."""
    respx_mock.get("http://paperless.test/api/").respond(status_code=200, content=b"")

    with pytest.raises(PaperlessError):
        await client._get_json("/api/")


async def test_ping_returns_false_on_non_json_response(
    client: PaperlessClient, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get("http://paperless.test/api/documents/").respond(status_code=200, content=b"")

    assert await client.ping() is False


async def test_ping_returns_true_on_success(
    client: PaperlessClient, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get("http://paperless.test/api/documents/").respond(
        json={"count": 0, "next": None, "previous": None, "results": []}
    )

    assert await client.ping() is True


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


async def test_taxonomy_loads_on_first_use_even_with_low_monotonic_clock(
    client: PaperlessClient,
    mock_taxonomy: None,
    respx_mock: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: time.monotonic() is seconds since an arbitrary reference
    point (e.g. system boot on Linux), not wall-clock time. On a
    freshly-booted CI container it can be well under TAXONOMY_TTL_SECONDS,
    which previously made the very first load think it was already
    "fresh" and skip fetching entirely, leaving every taxonomy lookup
    empty."""
    monkeypatch.setattr("paperbot.paperless.time.monotonic", lambda: 1.0)

    ids = await client.taxonomy.inbox_tag_ids()

    assert ids == [2]


async def test_inbox_tag_ids_filters_hidden_and_non_inbox_tags(
    client: PaperlessClient, mock_taxonomy: None, respx_mock: respx.MockRouter
) -> None:
    # tags.json: id=2 "Inbox" is_inbox_tag=true; id=4 "gpt-processed" is hidden
    ids = await client.taxonomy.inbox_tag_ids()

    assert ids == [2]


async def test_hidden_tags_returns_only_hidden_prefixed_tags(
    client: PaperlessClient, mock_taxonomy: None, respx_mock: respx.MockRouter
) -> None:
    tags = await client.taxonomy.hidden_tags()

    assert [t.name for t in tags] == ["gpt-processed"]


async def test_list_by_tag_ids_returns_empty_for_no_tags(client: PaperlessClient) -> None:
    docs, total = await client.list_by_tag_ids([])

    assert docs == []
    assert total == 0


async def test_list_by_tag_ids_queries_tags_id_in(
    client: PaperlessClient,
    mock_taxonomy: None,
    respx_mock: respx.MockRouter,
    load_fixture: Callable[[str], dict[str, Any]],
) -> None:
    route = respx_mock.get("http://paperless.test/api/documents/").respond(
        json=load_fixture("documents_recent.json")
    )

    docs, total = await client.list_by_tag_ids([1, 2], page=2, limit=10)

    assert len(docs) == 2
    assert total == 2
    request = route.calls.last.request
    assert request.url.params["tags__id__in"] == "1,2"
    assert request.url.params["page"] == "2"
    assert request.url.params["page_size"] == "10"


async def test_bulk_modify_tags_noop_without_tags_or_documents(
    client: PaperlessClient, respx_mock: respx.MockRouter
) -> None:
    await client.bulk_modify_tags([], remove_tags=[1])
    await client.bulk_modify_tags([412])

    assert respx_mock.calls.call_count == 0


async def test_bulk_modify_tags_posts_remove(
    client: PaperlessClient, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.post("http://paperless.test/api/documents/bulk_edit/").respond(json="OK")

    await client.bulk_modify_tags([412], remove_tags=[2])

    request = route.calls.last.request
    body = json.loads(request.content)
    assert body == {
        "documents": [412],
        "method": "modify_tags",
        "parameters": {"add_tags": [], "remove_tags": [2]},
    }


async def test_bulk_modify_tags_posts_add(
    client: PaperlessClient, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.post("http://paperless.test/api/documents/bulk_edit/").respond(json="OK")

    await client.bulk_modify_tags([412], add_tags=[3])

    request = route.calls.last.request
    body = json.loads(request.content)
    assert body == {
        "documents": [412],
        "method": "modify_tags",
        "parameters": {"add_tags": [3], "remove_tags": []},
    }


async def test_bulk_modify_tags_raises_on_error(
    client: PaperlessClient, respx_mock: respx.MockRouter
) -> None:
    respx_mock.post("http://paperless.test/api/documents/bulk_edit/").respond(status_code=400)

    with pytest.raises(PaperlessError):
        await client.bulk_modify_tags([412], remove_tags=[2])


async def test_update_document_patches_fields(
    client: PaperlessClient, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.patch("http://paperless.test/api/documents/412/").respond(json={"id": 412})

    await client.update_document(412, title="New title", correspondent=20)

    request = route.calls.last.request
    body = json.loads(request.content)
    assert body == {"title": "New title", "correspondent": 20}


async def test_update_document_noop_without_fields(
    client: PaperlessClient, respx_mock: respx.MockRouter
) -> None:
    await client.update_document(412)

    assert respx_mock.calls.call_count == 0


async def test_update_document_raises_on_error(
    client: PaperlessClient, respx_mock: respx.MockRouter
) -> None:
    respx_mock.patch("http://paperless.test/api/documents/412/").respond(status_code=400)

    with pytest.raises(PaperlessError):
        await client.update_document(412, title="x")


def test_format_custom_field_value_monetary_matches_real_server() -> None:
    # Pinned against a real Paperless-ngx document's custom_fields value:
    # ISO 4217 currency code immediately followed by the amount.
    assert _format_custom_field_value("USD12.30", "monetary") == "12.30 USD"
    assert _format_custom_field_value("PLN-5.00", "monetary") == "-5.00 PLN"


def test_format_custom_field_value_monetary_falls_back_on_unknown_shape() -> None:
    assert _format_custom_field_value("???", "monetary") == "???"


def test_format_custom_field_value_date_matches_real_server() -> None:
    assert _format_custom_field_value("2026-10-04", "date") == "04.10.2026"


def test_format_custom_field_value_string_passthrough() -> None:
    assert _format_custom_field_value("2840-8002", "string") == "2840-8002"


def test_format_custom_field_value_none_is_empty_string() -> None:
    assert _format_custom_field_value(None, "string") == ""


async def test_parses_real_server_document_with_custom_fields_and_notes(
    client: PaperlessClient,
    respx_mock: respx.MockRouter,
    load_fixture: Callable[[str], dict[str, Any]],
) -> None:
    """Pinned against a real GET /api/documents/1166/ response, after the
    user set all three custom fields on a test document. Uses the real
    server's own /api/custom_fields/ field ids (1/2/3), not the synthetic
    `mock_taxonomy` fixture's ids (30/31/32)."""
    empty_page = {"count": 0, "next": None, "previous": None, "results": []}
    respx_mock.get("http://paperless.test/api/tags/").respond(json=empty_page)
    respx_mock.get("http://paperless.test/api/document_types/").respond(json=empty_page)
    respx_mock.get("http://paperless.test/api/correspondents/").respond(json=empty_page)
    respx_mock.get("http://paperless.test/api/custom_fields/").respond(
        json=load_fixture("real_custom_fields.json")
    )
    respx_mock.get("http://paperless.test/api/documents/1166/").respond(
        json=load_fixture("real_document_with_custom_fields.json")
    )

    doc = await client.get_document(1166)

    assert doc is not None
    assert doc.custom_fields == {
        "Amount": "12.30 USD",
        "Expires": "04.10.2026",
        "Policy / Doc number": "2840-8002",
    }
    assert doc.notes == "test note"
    assert doc.correspondent is None
    assert doc.document_type is None  # type id 4 not in our taxonomy fixture


async def test_find_by_custom_field_query_sends_field_name_grammar(
    client: PaperlessClient, mock_taxonomy: None, respx_mock: respx.MockRouter
) -> None:
    """Confirmed against a real server: `[field_name, op, value]`, both for
    `exists` and `range`, correctly filters by field NAME (not id)."""
    route = respx_mock.get("http://paperless.test/api/documents/").respond(
        json={"count": 0, "next": None, "previous": None, "results": []}
    )

    query = json.dumps(["Expires", "range", ["2026-01-01", "2026-12-31"]])
    await client.find_by_custom_field_query(query, limit=5)

    request = route.calls.last.request
    assert request.url.params["custom_field_query"] == query
    assert request.url.params["page_size"] == "5"


async def test_list_all_documents_pages_through_results(
    client: PaperlessClient,
    mock_taxonomy: None,
    respx_mock: respx.MockRouter,
) -> None:
    page1 = {
        "count": 2,
        "next": "http://paperless.test/api/documents/?page=2",
        "previous": None,
        "results": [{"id": 1, "title": "A", "tags": [], "custom_fields": []}],
    }
    page2 = {
        "count": 2,
        "next": None,
        "previous": None,
        "results": [{"id": 2, "title": "B", "tags": [], "custom_fields": []}],
    }
    route = respx_mock.get("http://paperless.test/api/documents/")
    route.side_effect = [httpx.Response(200, json=page1), httpx.Response(200, json=page2)]

    docs = await client.list_all_documents()

    assert [d.id for d in docs] == [1, 2]


async def test_get_document_metadata_returns_sizes(
    client: PaperlessClient, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get("http://paperless.test/api/documents/412/metadata/").respond(
        json={"original_size": 12345, "archive_size": 6789}
    )

    meta = await client.get_document_metadata(412)

    assert meta is not None
    assert meta.original_size == 12345
    assert meta.archive_size == 6789


async def test_get_document_metadata_returns_none_on_404(
    client: PaperlessClient, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get("http://paperless.test/api/documents/999/metadata/").respond(status_code=404)

    assert await client.get_document_metadata(999) is None


async def test_add_note_posts_note_body(
    client: PaperlessClient, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.post("http://paperless.test/api/documents/412/notes/").respond(json={})

    await client.add_note(412, "From #500: hello")

    request = route.calls.last.request
    assert json.loads(request.content) == {"note": "From #500: hello"}


async def test_trash_document_sends_delete(
    client: PaperlessClient, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.delete("http://paperless.test/api/documents/412/").respond(status_code=204)

    await client.trash_document(412)

    assert route.called


async def test_trash_document_raises_on_error(
    client: PaperlessClient, respx_mock: respx.MockRouter
) -> None:
    respx_mock.delete("http://paperless.test/api/documents/412/").respond(status_code=400)

    with pytest.raises(PaperlessError):
        await client.trash_document(412)


async def test_restore_from_trash_posts_restore_action(
    client: PaperlessClient, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.post("http://paperless.test/api/trash/").respond(json={})

    restored = await client.restore_from_trash(412)

    assert restored is True
    request = route.calls.last.request
    assert json.loads(request.content) == {"documents": [412], "action": "restore"}


async def test_restore_from_trash_returns_false_on_404(
    client: PaperlessClient, respx_mock: respx.MockRouter
) -> None:
    respx_mock.post("http://paperless.test/api/trash/").respond(status_code=404)

    assert await client.restore_from_trash(412) is False


async def test_restore_from_trash_raises_on_server_error(
    client: PaperlessClient, respx_mock: respx.MockRouter
) -> None:
    respx_mock.post("http://paperless.test/api/trash/").respond(status_code=500)

    with pytest.raises(PaperlessError):
        await client.restore_from_trash(412)
