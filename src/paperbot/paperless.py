"""Async Paperless-ngx API client, taxonomy cache, and DTOs.

Endpoint shapes are modeled on the publicly documented Paperless-ngx REST API
(https://docs.paperless-ngx.com/api/) for the v3.2.x server described in
SPEC.md §1. Some details (notably the exact JSON shape of a "monetary"
custom field value) are not nailed down in the public docs and are marked
with TODO below — flagged per CLAUDE.md rather than guessed silently.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import httpx

from paperbot.config import Settings

logger = logging.getLogger(__name__)

TAXONOMY_TTL_SECONDS = 10 * 60
_DEFAULT_TIMEOUT = 15.0
_RETRYABLE_STATUS = {500, 502, 503, 504}
_MAX_RETRIES = 2


class PaperlessError(Exception):
    """Raised for Paperless API failures (network, HTTP, or shape errors)."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(slots=True)
class Tag:
    id: int
    name: str
    is_inbox_tag: bool = False


@dataclass(slots=True)
class DocumentType:
    id: int
    name: str


@dataclass(slots=True)
class Correspondent:
    id: int
    name: str


@dataclass(slots=True)
class CustomFieldDef:
    id: int
    name: str
    data_type: str


@dataclass(slots=True)
class Document:
    id: int
    title: str
    created: date | None
    correspondent: str | None
    document_type: str | None
    tags: list[str]
    custom_fields: dict[str, str]
    original_file_name: str | None = None
    content: str | None = None
    notes: str | None = None
    page_count: int | None = None
    snippet: str | None = None


def _parse_created(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).date()
    except ValueError:
        return None


def _format_custom_field_value(value: object, data_type: str) -> str:
    if value is None:
        return ""
    if data_type == "date" and isinstance(value, str):
        parsed = _parse_created(value)
        if parsed is not None:
            return parsed.strftime("%d.%m.%Y")
        return value
    # TODO(paperless-api): verify the exact JSON shape of "monetary" values
    # against a real v3.2.x server (currency prefix? separate currency key?)
    # and adjust formatting accordingly. Falling back to str() for now.
    return str(value)


class TaxonomyCache:
    """Caches tags/document_types/correspondents/custom_fields for 10 min.

    Tags whose name starts with one of `hidden_tag_prefixes` are excluded
    from every public accessor, per CLAUDE.md.
    """

    def __init__(self, client: PaperlessClient, hidden_tag_prefixes: list[str]) -> None:
        self._client = client
        self._hidden_prefixes = tuple(p.lower() for p in hidden_tag_prefixes)
        self._tags: dict[int, Tag] = {}
        self._document_types: dict[int, DocumentType] = {}
        self._correspondents: dict[int, Correspondent] = {}
        self._custom_fields: dict[int, CustomFieldDef] = {}
        self._loaded_at: float = 0.0
        self._lock = asyncio.Lock()

    def _is_hidden(self, name: str) -> bool:
        lowered = name.lower()
        return any(lowered.startswith(p) for p in self._hidden_prefixes)

    async def refresh(self, *, force: bool = False) -> None:
        async with self._lock:
            if not force and (time.monotonic() - self._loaded_at) < TAXONOMY_TTL_SECONDS:
                return
            tags, types_, correspondents, fields_ = await asyncio.gather(
                self._client.list_tags_raw(),
                self._client.list_document_types_raw(),
                self._client.list_correspondents_raw(),
                self._client.list_custom_fields_raw(),
            )
            self._tags = {
                t["id"]: Tag(id=t["id"], name=t["name"], is_inbox_tag=bool(t.get("is_inbox_tag")))
                for t in tags
            }
            self._document_types = {
                t["id"]: DocumentType(id=t["id"], name=t["name"]) for t in types_
            }
            self._correspondents = {
                c["id"]: Correspondent(id=c["id"], name=c["name"]) for c in correspondents
            }
            self._custom_fields = {
                f["id"]: CustomFieldDef(id=f["id"], name=f["name"], data_type=f["data_type"])
                for f in fields_
            }
            self._loaded_at = time.monotonic()

    async def _ensure_loaded(self) -> None:
        if not self._loaded_at:
            await self.refresh()
        else:
            await self.refresh(force=False)

    async def tag_name(self, tag_id: int) -> str | None:
        await self._ensure_loaded()
        tag = self._tags.get(tag_id)
        if tag is None or self._is_hidden(tag.name):
            return None
        return tag.name

    async def tag_names(self, tag_ids: list[int]) -> list[str]:
        await self._ensure_loaded()
        names = []
        for tid in tag_ids:
            tag = self._tags.get(tid)
            if tag is not None and not self._is_hidden(tag.name):
                names.append(tag.name)
        return names

    async def document_type_name(self, type_id: int | None) -> str | None:
        if type_id is None:
            return None
        await self._ensure_loaded()
        dt = self._document_types.get(type_id)
        return dt.name if dt else None

    async def correspondent_name(self, correspondent_id: int | None) -> str | None:
        if correspondent_id is None:
            return None
        await self._ensure_loaded()
        c = self._correspondents.get(correspondent_id)
        return c.name if c else None

    async def custom_field_values(self, raw_fields: list[dict[str, Any]]) -> dict[str, str]:
        await self._ensure_loaded()
        result: dict[str, str] = {}
        for entry in raw_fields:
            field_id = entry.get("field")
            if field_id is None:
                continue
            field_def = self._custom_fields.get(field_id)
            if field_def is None:
                continue
            result[field_def.name] = _format_custom_field_value(
                entry.get("value"), field_def.data_type
            )
        return result

    async def resolve_tag_id(self, name: str) -> int | None:
        await self._ensure_loaded()
        lowered = name.lower()
        for tag in self._tags.values():
            if self._is_hidden(tag.name):
                continue
            if tag.name.lower() == lowered:
                return tag.id
        return None

    async def resolve_document_type_id(self, name: str) -> int | None:
        await self._ensure_loaded()
        lowered = name.lower()
        for dt in self._document_types.values():
            if dt.name.lower() == lowered:
                return dt.id
        return None

    async def resolve_correspondent_id(self, name: str) -> int | None:
        await self._ensure_loaded()
        lowered = name.lower()
        for c in self._correspondents.values():
            if c.name.lower() == lowered:
                return c.id
        return None

    async def all_tags(self) -> list[Tag]:
        await self._ensure_loaded()
        return [t for t in self._tags.values() if not self._is_hidden(t.name)]

    async def all_document_types(self) -> list[DocumentType]:
        await self._ensure_loaded()
        return list(self._document_types.values())

    async def all_correspondents(self) -> list[Correspondent]:
        await self._ensure_loaded()
        return list(self._correspondents.values())

    async def all_custom_fields(self) -> list[CustomFieldDef]:
        await self._ensure_loaded()
        return list(self._custom_fields.values())


class PaperlessClient:
    def __init__(self, settings: Settings, http_client: httpx.AsyncClient) -> None:
        self._settings = settings
        self._http = http_client
        self.taxonomy = TaxonomyCache(self, settings.hidden_tag_prefixes)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Token {self._settings.paperless_token}",
            "Accept": f"application/json; version={self._settings.paperless_api_version}",
        }

    async def _get_json(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        request_timeout: float = _DEFAULT_TIMEOUT,
    ) -> dict[str, Any]:
        url = f"{str(self._settings.paperless_url).rstrip('/')}{path}"
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                resp = await self._http.get(
                    url, params=params, headers=self._headers(), timeout=request_timeout
                )
            except httpx.TransportError as exc:
                last_exc = exc
                if attempt < _MAX_RETRIES:
                    logger.warning("Paperless GET %s failed (attempt %d): %s", path, attempt, exc)
                    continue
                raise PaperlessError(f"network error calling {path}: {exc}") from exc
            if resp.status_code in _RETRYABLE_STATUS and attempt < _MAX_RETRIES:
                logger.warning(
                    "Paperless GET %s returned %d (attempt %d), retrying",
                    path,
                    resp.status_code,
                    attempt,
                )
                continue
            if resp.status_code >= 400:
                raise PaperlessError(
                    f"{path} returned HTTP {resp.status_code}", status_code=resp.status_code
                )
            result: dict[str, Any] = resp.json()
            return result
        raise PaperlessError(f"request to {path} failed: {last_exc}")

    async def ping(self) -> bool:
        try:
            await self._get_json("/api/")
            return True
        except PaperlessError as exc:
            logger.warning("Paperless ping failed: %s", exc)
            return False

    async def list_tags_raw(self) -> list[dict[str, Any]]:
        return await self._list_all_raw("/api/tags/")

    async def list_document_types_raw(self) -> list[dict[str, Any]]:
        return await self._list_all_raw("/api/document_types/")

    async def list_correspondents_raw(self) -> list[dict[str, Any]]:
        return await self._list_all_raw("/api/correspondents/")

    async def list_custom_fields_raw(self) -> list[dict[str, Any]]:
        return await self._list_all_raw("/api/custom_fields/")

    async def _list_all_raw(self, path: str) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        page: int | None = 1
        while page is not None:
            data = await self._get_json(path, params={"page": page, "page_size": 100})
            results.extend(data.get("results", []))
            page = page + 1 if data.get("next") else None
        return results

    async def _to_document(self, raw: dict[str, Any]) -> Document:
        tags = await self.taxonomy.tag_names(raw.get("tags", []))
        document_type = await self.taxonomy.document_type_name(raw.get("document_type"))
        correspondent = await self.taxonomy.correspondent_name(raw.get("correspondent"))
        custom_fields = await self.taxonomy.custom_field_values(raw.get("custom_fields", []))
        search_hit = raw.get("__search_hit__") or {}
        return Document(
            id=raw["id"],
            title=raw.get("title") or f"#{raw['id']}",
            created=_parse_created(raw.get("created")),
            correspondent=correspondent,
            document_type=document_type,
            tags=tags,
            custom_fields=custom_fields,
            original_file_name=raw.get("original_file_name"),
            content=raw.get("content"),
            notes=raw.get("notes") if isinstance(raw.get("notes"), str) else None,
            page_count=raw.get("page_count"),
            snippet=search_hit.get("highlights"),
        )

    async def search_documents(
        self,
        query: str,
        *,
        page: int = 1,
        page_size: int = 5,
        extra_params: dict[str, Any] | None = None,
    ) -> tuple[list[Document], int]:
        params: dict[str, Any] = {
            "query": query,
            "page": page,
            "page_size": page_size,
        }
        if extra_params:
            params.update(extra_params)
        data = await self._get_json("/api/documents/", params=params)
        docs = [await self._to_document(r) for r in data.get("results", [])]
        return docs, data.get("count", len(docs))

    async def get_document(self, doc_id: int) -> Document | None:
        try:
            raw = await self._get_json(f"/api/documents/{doc_id}/")
        except PaperlessError as exc:
            if exc.status_code == 404:
                return None
            raise
        return await self._to_document(raw)

    async def recent_documents(self, limit: int = 10) -> list[Document]:
        data = await self._get_json(
            "/api/documents/", params={"ordering": "-created", "page_size": limit}
        )
        return [await self._to_document(r) for r in data.get("results", [])]
