"""Agent tool schemas and implementations (SPEC §5.4).

Tool implementations never raise into the agent loop (CLAUDE.md): every
error path returns a compact `{"error": ...}` JSON string instead.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from anthropic.types import ToolParam

from paperbot.config import Settings
from paperbot.paperless import Document, PaperlessClient, PaperlessError

logger = logging.getLogger(__name__)

_VALID_OPS = {"exists", "exact", "lt", "lte", "gt", "gte", "range"}

TOOL_SCHEMAS: list[ToolParam] = [
    {
        "name": "search_documents",
        "description": (
            "Full-text search over the document archive. Accepts optional filters. "
            "Returns compact JSON with id, title, created, correspondent, document_type, "
            "tags, custom_fields, and a snippet for each match."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Full-text search query"},
                "tag": {"type": "string", "description": "Tag name (case-insensitive)"},
                "document_type": {"type": "string"},
                "correspondent": {"type": "string"},
                "created_after": {"type": "string", "description": "YYYY-MM-DD"},
                "created_before": {"type": "string", "description": "YYYY-MM-DD"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 15, "default": 8},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_document",
        "description": ("Fetch full metadata and (truncated) content for one document by id."),
        "input_schema": {
            "type": "object",
            "properties": {"id": {"type": "integer"}},
            "required": ["id"],
        },
    },
    {
        "name": "find_by_custom_field",
        "description": (
            "Find documents by a custom field value, e.g. an `Expires` date range. "
            "op is one of exists/exact/lt/lte/gt/gte/range; value is a string, number, "
            "or [a, b] pair for range."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "field": {"type": "string", "description": "Custom field name"},
                "op": {"type": "string", "enum": sorted(_VALID_OPS)},
                "value": {},
                "limit": {"type": "integer", "default": 15},
            },
            "required": ["field", "op"],
        },
    },
    {
        "name": "list_taxonomy",
        "description": "List known tags, document types, correspondents, or custom fields.",
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["tags", "document_types", "correspondents", "custom_fields"],
                }
            },
            "required": ["kind"],
        },
    },
    {
        "name": "send_document",
        "description": (
            "Send the original file of a document to the user's current chat. "
            "Use this when the user asks for a scan/file/copy of a document."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"id": {"type": "integer"}},
            "required": ["id"],
        },
        "cache_control": {"type": "ephemeral"},
    },
]

SendDocumentCallback = Callable[[int], Awaitable[bool]]


def _doc_summary(doc: Document) -> dict[str, Any]:
    return {
        "id": doc.id,
        "title": doc.title,
        "created": doc.created.isoformat() if doc.created else None,
        "correspondent": doc.correspondent,
        "document_type": doc.document_type,
        "tags": doc.tags,
        "custom_fields": doc.custom_fields,
        "snippet": doc.snippet,
    }


async def _search_documents(paperless: PaperlessClient, args: dict[str, Any]) -> dict[str, Any]:
    query = args.get("query")
    if not isinstance(query, str) or not query.strip():
        return {"error": "query is required"}

    extra_params: dict[str, Any] = {}

    tag = args.get("tag")
    if tag:
        tag_id = await paperless.taxonomy.resolve_tag_id(str(tag))
        if tag_id is None:
            return {"error": f"unknown tag '{tag}'; call list_taxonomy to see valid names"}
        extra_params["tags__id__all"] = tag_id

    document_type = args.get("document_type")
    if document_type:
        type_id = await paperless.taxonomy.resolve_document_type_id(str(document_type))
        if type_id is None:
            return {
                "error": f"unknown document_type '{document_type}'; "
                "call list_taxonomy to see valid names"
            }
        extra_params["document_type__id"] = type_id

    correspondent = args.get("correspondent")
    if correspondent:
        corr_id = await paperless.taxonomy.resolve_correspondent_id(str(correspondent))
        if corr_id is None:
            return {
                "error": f"unknown correspondent '{correspondent}'; "
                "call list_taxonomy to see valid names"
            }
        extra_params["correspondent__id"] = corr_id

    if args.get("created_after"):
        extra_params["created__date__gte"] = args["created_after"]
    if args.get("created_before"):
        extra_params["created__date__lte"] = args["created_before"]

    limit = args.get("limit", 8)
    try:
        limit = max(1, min(15, int(limit)))
    except (TypeError, ValueError):
        limit = 8

    docs, total = await paperless.search_documents(
        query, page=1, page_size=limit, extra_params=extra_params
    )
    return {"total": total, "results": [_doc_summary(d) for d in docs]}


async def _get_document(
    paperless: PaperlessClient, settings: Settings, args: dict[str, Any]
) -> dict[str, Any]:
    doc_id = args.get("id")
    if not isinstance(doc_id, int):
        return {"error": "id must be an integer"}
    doc = await paperless.get_document(doc_id)
    if doc is None:
        return {"error": f"document #{doc_id} not found"}
    content = doc.content or ""
    max_chars = settings.doc_content_max_chars
    truncated = len(content) > max_chars
    return {
        **_doc_summary(doc),
        "notes": doc.notes,
        "content": content[:max_chars],
        "truncated": truncated,
        "page_count": doc.page_count,
    }


async def _find_by_custom_field(paperless: PaperlessClient, args: dict[str, Any]) -> dict[str, Any]:
    field = args.get("field")
    op = args.get("op")
    if not isinstance(field, str) or not field:
        return {"error": "field is required"}
    if op not in _VALID_OPS:
        return {"error": f"op must be one of {sorted(_VALID_OPS)}"}

    if op == "exists":
        query: list[Any] = [field, "exists", True]
    else:
        value = args.get("value")
        if value is None:
            return {"error": f"value is required for op '{op}'"}
        query = [field, op, value]

    limit = args.get("limit", 15)
    try:
        limit = max(1, min(50, int(limit)))
    except (TypeError, ValueError):
        limit = 15

    docs = await paperless.find_by_custom_field_query(json.dumps(query), limit)
    return {"results": [_doc_summary(d) for d in docs]}


async def _list_taxonomy(paperless: PaperlessClient, args: dict[str, Any]) -> dict[str, Any]:
    kind = args.get("kind")
    if kind == "tags":
        tags = await paperless.taxonomy.all_tags()
        return {"items": [{"name": t.name, "count": t.document_count} for t in tags]}
    if kind == "document_types":
        doc_types = await paperless.taxonomy.all_document_types()
        return {"items": [{"name": t.name, "count": t.document_count} for t in doc_types]}
    if kind == "correspondents":
        correspondents = await paperless.taxonomy.all_correspondents()
        return {"items": [{"name": c.name, "count": c.document_count} for c in correspondents]}
    if kind == "custom_fields":
        fields = await paperless.taxonomy.all_custom_fields()
        return {"items": [{"name": f.name, "data_type": f.data_type} for f in fields]}
    return {"error": "kind must be one of tags/document_types/correspondents/custom_fields"}


async def _send_document(
    paperless: PaperlessClient, send_callback: SendDocumentCallback, args: dict[str, Any]
) -> dict[str, Any]:
    doc_id = args.get("id")
    if not isinstance(doc_id, int):
        return {"error": "id must be an integer"}
    doc = await paperless.get_document(doc_id)
    if doc is None:
        return {"error": f"document #{doc_id} not found"}
    sent = await send_callback(doc_id)
    if not sent:
        return {"error": "failed to send document"}
    return {"sent": True, "title": doc.title}


async def dispatch_tool(
    name: str,
    tool_input: dict[str, Any],
    *,
    paperless: PaperlessClient,
    settings: Settings,
    send_document_callback: SendDocumentCallback,
) -> str:
    """Run one tool call and return a compact JSON string result."""
    try:
        if name == "search_documents":
            result = await _search_documents(paperless, tool_input)
        elif name == "get_document":
            result = await _get_document(paperless, settings, tool_input)
        elif name == "find_by_custom_field":
            result = await _find_by_custom_field(paperless, tool_input)
        elif name == "list_taxonomy":
            result = await _list_taxonomy(paperless, tool_input)
        elif name == "send_document":
            result = await _send_document(paperless, send_document_callback, tool_input)
        else:
            result = {"error": f"unknown tool '{name}'"}
    except PaperlessError as exc:
        logger.warning("tool %s: paperless error: %s", name, exc)
        result = {"error": "paperless request failed"}
    except Exception:
        logger.exception("tool %s failed unexpectedly", name)
        result = {"error": "internal error running tool"}
    return json.dumps(result, ensure_ascii=False)


def extract_touched_docs(tool_name: str, result_json: str) -> list[tuple[int, str]]:
    """Pull (id, title) pairs out of a tool result, for the recent_docs memory."""
    try:
        data = json.loads(result_json)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, dict):
        return []
    touched: list[tuple[int, str]] = []
    if tool_name in ("search_documents", "find_by_custom_field"):
        for item in data.get("results", []):
            if isinstance(item, dict) and isinstance(item.get("id"), int):
                touched.append((item["id"], str(item.get("title", ""))))
    elif tool_name == "get_document":
        if isinstance(data.get("id"), int):
            touched.append((data["id"], str(data.get("title", ""))))
    return touched
