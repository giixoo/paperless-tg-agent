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
import re
import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import httpx

from paperbot.config import Settings

logger = logging.getLogger(__name__)

TAXONOMY_TTL_SECONDS = 10 * 60
_DEFAULT_TIMEOUT = 15.0
_DOWNLOAD_TIMEOUT = 120.0
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
    document_count: int | None = None


@dataclass(slots=True)
class DocumentType:
    id: int
    name: str
    document_count: int | None = None


@dataclass(slots=True)
class Correspondent:
    id: int
    name: str
    document_count: int | None = None


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


@dataclass(slots=True)
class TaskResult:
    status: str
    result: str | None
    related_document: int | None


def _parse_created(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).date()
    except ValueError:
        return None


def _extract_notes(value: object) -> str | None:
    """Paperless's `notes` field is a list of {"note": "text", ...} objects
    (on the document detail endpoint; often absent from list/search results),
    not a plain string. Join their text, newest first as returned by the API.
    """
    if not isinstance(value, list) or not value:
        return None
    texts = [n["note"] for n in value if isinstance(n, dict) and isinstance(n.get("note"), str)]
    return "\n".join(texts) if texts else None


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
                t["id"]: Tag(
                    id=t["id"],
                    name=t["name"],
                    is_inbox_tag=bool(t.get("is_inbox_tag")),
                    document_count=t.get("document_count"),
                )
                for t in tags
            }
            self._document_types = {
                t["id"]: DocumentType(
                    id=t["id"], name=t["name"], document_count=t.get("document_count")
                )
                for t in types_
            }
            self._correspondents = {
                c["id"]: Correspondent(
                    id=c["id"], name=c["name"], document_count=c.get("document_count")
                )
                for c in correspondents
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

    async def inbox_tag_ids(self) -> list[int]:
        await self._ensure_loaded()
        return [t.id for t in self._tags.values() if t.is_inbox_tag and not self._is_hidden(t.name)]


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

    async def _request_get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        request_timeout: float = _DEFAULT_TIMEOUT,
    ) -> httpx.Response:
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
            return resp
        raise PaperlessError(f"request to {path} failed: {last_exc}")

    async def _get_json(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        request_timeout: float = _DEFAULT_TIMEOUT,
    ) -> dict[str, Any]:
        resp = await self._request_get(path, params, request_timeout=request_timeout)
        result: dict[str, Any] = resp.json()
        return result

    async def _get_any(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        request_timeout: float = _DEFAULT_TIMEOUT,
    ) -> Any:
        resp = await self._request_get(path, params, request_timeout=request_timeout)
        return resp.json()

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
            notes=_extract_notes(raw.get("notes")),
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

    async def find_by_custom_field_query(
        self, custom_field_query_json: str, limit: int
    ) -> list[Document]:
        """Filter documents by `custom_field_query` (SPEC §5.4/§7).

        TODO(paperless-api): the exact `custom_field_query` grammar isn't
        pinned down in the public docs beyond SPEC's own example
        (`["Expires","range",["2026-10-06","2026-12-31"]]`, i.e. field NAME
        rather than id). Implemented literally per that example; verify
        against a real v3.2.x server and adjust if it expects field ids.
        """
        data = await self._get_json(
            "/api/documents/",
            params={"custom_field_query": custom_field_query_json, "page_size": limit},
        )
        return [await self._to_document(r) for r in data.get("results", [])]

    async def list_by_tag_ids(self, tag_ids: list[int], limit: int = 50) -> list[Document]:
        """List documents carrying any of the given tag ids (OR semantics).

        TODO(paperless-api): `tags__id__in` is the standard django-filter "in"
        lookup convention; SPEC §7 only confirms `tags__id__all` (AND).
        Verify `tags__id__in` against a real server and fall back to
        per-tag queries + merge if it's not supported.
        """
        if not tag_ids:
            return []
        data = await self._get_json(
            "/api/documents/",
            params={"tags__id__in": ",".join(str(i) for i in tag_ids), "page_size": limit},
        )
        return [await self._to_document(r) for r in data.get("results", [])]

    async def bulk_remove_tags(self, document_ids: list[int], tag_ids: list[int]) -> None:
        """Remove `tag_ids` from `document_ids` via the bulk_edit endpoint.

        TODO(paperless-api): modeled on the documented bulk-edit "modify_tags"
        action (https://docs.paperless-ngx.com/api/#bulk-edit); verify the
        exact parameter shape against a real v3.2.x server.
        """
        if not document_ids or not tag_ids:
            return
        await self._post_json(
            "/api/documents/bulk_edit/",
            {
                "documents": document_ids,
                "method": "modify_tags",
                "parameters": {"add_tags": [], "remove_tags": tag_ids},
            },
        )

    async def _post_json(self, path: str, body: dict[str, Any]) -> Any:
        url = f"{str(self._settings.paperless_url).rstrip('/')}{path}"
        try:
            resp = await self._http.post(
                url, json=body, headers=self._headers(), timeout=_DEFAULT_TIMEOUT
            )
        except httpx.TransportError as exc:
            raise PaperlessError(f"network error posting to {path}: {exc}") from exc
        if resp.status_code >= 400:
            raise PaperlessError(
                f"{path} returned HTTP {resp.status_code}: {resp.text[:200]}",
                status_code=resp.status_code,
            )
        return resp.json() if resp.content else None

    async def download_document(self, doc_id: int, *, original: bool) -> tuple[bytes, str] | None:
        """Download a document's file. Returns (content, filename) or None if
        the document doesn't exist. Tries to recover the real filename from
        the `Content-Disposition` header, falling back to the metadata or a
        generic name.
        """
        params = {"original": "true"} if original else {}
        try:
            resp = await self._request_get(
                f"/api/documents/{doc_id}/download/",
                params=params,
                request_timeout=_DOWNLOAD_TIMEOUT,
            )
        except PaperlessError as exc:
            if exc.status_code == 404:
                return None
            raise
        filename = _filename_from_content_disposition(resp.headers.get("content-disposition"))
        if filename is None:
            filename = f"document_{doc_id}" + (".pdf" if not original else "")
        return resp.content, filename

    async def upload_document(
        self, content: bytes, filename: str, *, title: str | None = None
    ) -> str:
        """POST a new document for consumption. Returns the Paperless task UUID.

        TODO(paperless-api): the public docs don't pin down the exact response
        body shape for `post_document/` across v3.2.x point releases (a bare
        quoted UUID string vs. a small JSON object). Handling both here;
        verify against the real server and simplify once confirmed.
        """
        url = f"{str(self._settings.paperless_url).rstrip('/')}/api/documents/post_document/"
        files = {"document": (filename, content)}
        data = {"title": title} if title else {}
        try:
            resp = await self._http.post(
                url, files=files, data=data, headers=self._headers(), timeout=_DOWNLOAD_TIMEOUT
            )
        except httpx.TransportError as exc:
            raise PaperlessError(f"network error uploading document: {exc}") from exc
        if resp.status_code >= 400:
            raise PaperlessError(
                f"post_document returned HTTP {resp.status_code}: {resp.text[:200]}",
                status_code=resp.status_code,
            )
        parsed = resp.json()
        if isinstance(parsed, str):
            return parsed
        if isinstance(parsed, dict) and "task_id" in parsed:
            return str(parsed["task_id"])
        raise PaperlessError(f"unexpected post_document response shape: {parsed!r}")

    async def get_task(self, task_id: str) -> TaskResult | None:
        """Look up a Paperless task by id.

        TODO(paperless-api): verify against a real server whether this
        returns a bare list or a paginated {"results": [...]} envelope, and
        whether `related_document` is present on this server's version.
        """
        data = await self._get_any("/api/tasks/", params={"task_id": task_id})
        items = data if isinstance(data, list) else data.get("results", [])
        if not items:
            return None
        item = items[0]
        return TaskResult(
            status=item.get("status", ""),
            result=item.get("result"),
            related_document=item.get("related_document"),
        )


def _filename_from_content_disposition(header: str | None) -> str | None:
    if not header:
        return None
    match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";\r\n]+)"?', header)
    if match:
        return match.group(1)
    return None
