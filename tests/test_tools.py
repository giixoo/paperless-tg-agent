from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import pytest

from paperbot.agent import tools
from paperbot.config import Settings
from paperbot.paperless import Document, PaperlessError


def make_doc(**overrides: Any) -> Document:
    defaults: dict[str, Any] = dict(
        id=412,
        title="Car insurance 2026",
        created=date(2026, 1, 14),
        correspondent="PZU",
        document_type="Insurance policy",
        tags=["Insurance"],
        custom_fields={"Expires": "14.12.2026"},
        snippet="...valid until 14.12.2026...",
    )
    defaults.update(overrides)
    return Document(**defaults)


@dataclass
class FakePaperless:
    """Duck-typed stand-in exposing only what tools.py calls."""

    search_results: tuple[list[Document], int] = field(default_factory=lambda: ([], 0))
    detail: Document | None = None
    custom_field_results: list[Document] = field(default_factory=list)
    taxonomy: Any = None
    raise_error: bool = False

    async def search_documents(self, query: str, **kwargs: Any) -> tuple[list[Document], int]:
        if self.raise_error:
            raise PaperlessError("boom")
        return self.search_results

    async def get_document(self, doc_id: int) -> Document | None:
        if self.raise_error:
            raise PaperlessError("boom")
        return self.detail

    async def find_by_custom_field_query(self, query_json: str, limit: int) -> list[Document]:
        return self.custom_field_results


class FakeTaxonomy:
    def __init__(self) -> None:
        self.tags = {"insurance": 1}
        self.types = {"invoice": 11}
        self.correspondents = {"pzu": 20}

    async def resolve_tag_id(self, name: str) -> int | None:
        return self.tags.get(name.lower())

    async def resolve_document_type_id(self, name: str) -> int | None:
        return self.types.get(name.lower())

    async def resolve_correspondent_id(self, name: str) -> int | None:
        return self.correspondents.get(name.lower())

    async def all_tags(self) -> list[Any]:
        return [_Named("Insurance", 3)]

    async def all_document_types(self) -> list[Any]:
        return [_Named("Invoice", 5)]

    async def all_correspondents(self) -> list[Any]:
        return [_Named("PZU", 3)]

    async def all_custom_fields(self) -> list[Any]:
        return [_FieldDef("Expires", "date")]


@dataclass
class _Named:
    name: str
    document_count: int


@dataclass
class _FieldDef:
    name: str
    data_type: str


def make_paperless(**kwargs: Any) -> FakePaperless:
    p = FakePaperless(**kwargs)
    p.taxonomy = FakeTaxonomy()
    return p


@pytest.fixture
def settings() -> Settings:
    return Settings(
        telegram_bot_token="t",  # noqa: S106
        telegram_allowed_users=[1],
        paperless_url="http://paperless.test",  # type: ignore[arg-type]
        paperless_token="pt",  # noqa: S106
        anthropic_api_key="ak",  # noqa: S106
        doc_content_max_chars=50,
    )


async def noop_send_document(doc_id: int) -> bool:
    return True


# --- search_documents ---------------------------------------------------


async def test_search_documents_returns_results(settings: Settings) -> None:
    paperless = make_paperless(search_results=([make_doc()], 1))

    result_json = await tools.dispatch_tool(
        "search_documents",
        {"query": "insurance"},
        paperless=paperless,  # type: ignore[arg-type]
        settings=settings,
        send_document_callback=noop_send_document,
    )

    data = json.loads(result_json)
    assert data["total"] == 1
    assert data["results"][0]["id"] == 412
    assert data["results"][0]["title"] == "Car insurance 2026"


async def test_search_documents_requires_query(settings: Settings) -> None:
    paperless = make_paperless()

    result_json = await tools.dispatch_tool(
        "search_documents",
        {},
        paperless=paperless,  # type: ignore[arg-type]
        settings=settings,
        send_document_callback=noop_send_document,
    )

    assert json.loads(result_json) == {"error": "query is required"}


async def test_search_documents_unknown_tag_returns_error(settings: Settings) -> None:
    paperless = make_paperless()

    result_json = await tools.dispatch_tool(
        "search_documents",
        {"query": "x", "tag": "nonexistent"},
        paperless=paperless,  # type: ignore[arg-type]
        settings=settings,
        send_document_callback=noop_send_document,
    )

    data = json.loads(result_json)
    assert "error" in data
    assert "nonexistent" in data["error"]


async def test_search_documents_paperless_error_becomes_json_error(settings: Settings) -> None:
    paperless = make_paperless(raise_error=True)

    result_json = await tools.dispatch_tool(
        "search_documents",
        {"query": "x"},
        paperless=paperless,  # type: ignore[arg-type]
        settings=settings,
        send_document_callback=noop_send_document,
    )

    assert json.loads(result_json) == {"error": "paperless request failed"}


# --- get_document --------------------------------------------------------


async def test_get_document_truncates_content(settings: Settings) -> None:
    doc = make_doc(content="x" * 100)
    paperless = make_paperless(detail=doc)

    result_json = await tools.dispatch_tool(
        "get_document",
        {"id": 412},
        paperless=paperless,  # type: ignore[arg-type]
        settings=settings,
        send_document_callback=noop_send_document,
    )

    data = json.loads(result_json)
    assert data["truncated"] is True
    assert len(data["content"]) == 50


async def test_get_document_not_found(settings: Settings) -> None:
    paperless = make_paperless(detail=None)

    result_json = await tools.dispatch_tool(
        "get_document",
        {"id": 999},
        paperless=paperless,  # type: ignore[arg-type]
        settings=settings,
        send_document_callback=noop_send_document,
    )

    assert json.loads(result_json) == {"error": "document #999 not found"}


# --- find_by_custom_field -------------------------------------------------


async def test_find_by_custom_field_requires_value_unless_exists(settings: Settings) -> None:
    paperless = make_paperless()

    result_json = await tools.dispatch_tool(
        "find_by_custom_field",
        {"field": "Expires", "op": "range"},
        paperless=paperless,  # type: ignore[arg-type]
        settings=settings,
        send_document_callback=noop_send_document,
    )

    assert json.loads(result_json) == {"error": "value is required for op 'range'"}


async def test_find_by_custom_field_exists_does_not_need_value(settings: Settings) -> None:
    paperless = make_paperless(custom_field_results=[make_doc()])

    result_json = await tools.dispatch_tool(
        "find_by_custom_field",
        {"field": "Expires", "op": "exists"},
        paperless=paperless,  # type: ignore[arg-type]
        settings=settings,
        send_document_callback=noop_send_document,
    )

    data = json.loads(result_json)
    assert len(data["results"]) == 1


async def test_find_by_custom_field_rejects_invalid_op(settings: Settings) -> None:
    paperless = make_paperless()

    result_json = await tools.dispatch_tool(
        "find_by_custom_field",
        {"field": "Expires", "op": "bogus"},
        paperless=paperless,  # type: ignore[arg-type]
        settings=settings,
        send_document_callback=noop_send_document,
    )

    assert "error" in json.loads(result_json)


# --- list_taxonomy ---------------------------------------------------------


async def test_list_taxonomy_tags(settings: Settings) -> None:
    paperless = make_paperless()

    result_json = await tools.dispatch_tool(
        "list_taxonomy",
        {"kind": "tags"},
        paperless=paperless,  # type: ignore[arg-type]
        settings=settings,
        send_document_callback=noop_send_document,
    )

    data = json.loads(result_json)
    assert data["items"] == [{"name": "Insurance", "count": 3}]


async def test_list_taxonomy_invalid_kind(settings: Settings) -> None:
    paperless = make_paperless()

    result_json = await tools.dispatch_tool(
        "list_taxonomy",
        {"kind": "bogus"},
        paperless=paperless,  # type: ignore[arg-type]
        settings=settings,
        send_document_callback=noop_send_document,
    )

    assert "error" in json.loads(result_json)


# --- send_document ----------------------------------------------------------


async def test_send_document_success(settings: Settings) -> None:
    paperless = make_paperless(detail=make_doc())

    result_json = await tools.dispatch_tool(
        "send_document",
        {"id": 412},
        paperless=paperless,  # type: ignore[arg-type]
        settings=settings,
        send_document_callback=noop_send_document,
    )

    assert json.loads(result_json) == {"sent": True, "title": "Car insurance 2026"}


async def test_send_document_not_found(settings: Settings) -> None:
    paperless = make_paperless(detail=None)

    result_json = await tools.dispatch_tool(
        "send_document",
        {"id": 999},
        paperless=paperless,  # type: ignore[arg-type]
        settings=settings,
        send_document_callback=noop_send_document,
    )

    assert json.loads(result_json) == {"error": "document #999 not found"}


async def test_send_document_callback_failure_is_reported(settings: Settings) -> None:
    paperless = make_paperless(detail=make_doc())

    async def failing_callback(doc_id: int) -> bool:
        return False

    result_json = await tools.dispatch_tool(
        "send_document",
        {"id": 412},
        paperless=paperless,  # type: ignore[arg-type]
        settings=settings,
        send_document_callback=failing_callback,
    )

    assert json.loads(result_json) == {"error": "failed to send document"}


# --- dispatch_tool ------------------------------------------------------------


async def test_dispatch_unknown_tool(settings: Settings) -> None:
    paperless = make_paperless()

    result_json = await tools.dispatch_tool(
        "nonexistent_tool",
        {},
        paperless=paperless,  # type: ignore[arg-type]
        settings=settings,
        send_document_callback=noop_send_document,
    )

    assert "error" in json.loads(result_json)


async def test_dispatch_tool_never_raises_on_unexpected_exception(settings: Settings) -> None:
    class ExplodingPaperless(FakePaperless):
        async def search_documents(self, query: str, **kwargs: Any) -> tuple[list[Document], int]:
            raise RuntimeError("unexpected bug")

    paperless = ExplodingPaperless()
    paperless.taxonomy = FakeTaxonomy()

    result_json = await tools.dispatch_tool(
        "search_documents",
        {"query": "x"},
        paperless=paperless,  # type: ignore[arg-type]
        settings=settings,
        send_document_callback=noop_send_document,
    )

    assert json.loads(result_json) == {"error": "internal error running tool"}


# --- extract_touched_docs ------------------------------------------------------


def test_extract_touched_docs_from_search_results() -> None:
    result_json = json.dumps({"results": [{"id": 412, "title": "Car insurance 2026"}]})

    touched = tools.extract_touched_docs("search_documents", result_json)

    assert touched == [(412, "Car insurance 2026")]


def test_extract_touched_docs_from_get_document() -> None:
    result_json = json.dumps({"id": 412, "title": "Car insurance 2026"})

    touched = tools.extract_touched_docs("get_document", result_json)

    assert touched == [(412, "Car insurance 2026")]


def test_extract_touched_docs_handles_garbage() -> None:
    assert tools.extract_touched_docs("search_documents", "not json") == []
    assert tools.extract_touched_docs("search_documents", "null") == []
