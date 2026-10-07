"""Inline keyboards and short-token callback_data encoding.

Telegram caps `callback_data` at 64 bytes, so long-lived state (the search
query text, extra filters) is kept server-side in `SearchStateStore` and
referenced by a short token, per SPEC §4.2.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import count
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

CALLBACK_SEARCH_PAGE = "sp"  # sp:<token>:<page>
CALLBACK_DOWNLOAD = "dl"  # dl:<doc_id>
CALLBACK_INBOX_DONE = "ib"  # ib:<doc_id>
CALLBACK_SEARCH_EXPAND = "xd"  # xd:<doc_id>
CALLBACK_SEARCH_COLLAPSE = "cd"  # cd:<doc_id>
CALLBACK_PREVIEW = "pv"  # pv:<doc_id>
CALLBACK_CONTENT = "pc"  # pc:<doc_id>


@dataclass(slots=True)
class SearchState:
    query: str
    extra_params: dict[str, Any] = field(default_factory=dict)


class SearchStateStore:
    """In-memory, process-lifetime store of search tokens -> query state."""

    def __init__(self) -> None:
        self._states: dict[str, SearchState] = {}
        self._counter = count(1)

    def put(self, state: SearchState) -> str:
        token = format(next(self._counter), "x")
        self._states[token] = state
        return token

    def get(self, token: str) -> SearchState | None:
        return self._states.get(token)


def encode_search_page(token: str, page: int) -> str:
    data = f"{CALLBACK_SEARCH_PAGE}:{token}:{page}"
    assert len(data.encode()) <= 64, "callback_data exceeds 64 bytes"
    return data


def decode_search_page(data: str) -> tuple[str, int] | None:
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != CALLBACK_SEARCH_PAGE:
        return None
    try:
        return parts[1], int(parts[2])
    except ValueError:
        return None


def encode_download(doc_id: int) -> str:
    data = f"{CALLBACK_DOWNLOAD}:{doc_id}"
    assert len(data.encode()) <= 64, "callback_data exceeds 64 bytes"
    return data


def decode_download(data: str) -> int | None:
    parts = data.split(":")
    if len(parts) != 2 or parts[0] != CALLBACK_DOWNLOAD:
        return None
    try:
        return int(parts[1])
    except ValueError:
        return None


def download_button(doc_id: int) -> InlineKeyboardButton:
    return InlineKeyboardButton("📄", callback_data=encode_download(doc_id))


def doc_card_keyboard(doc_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[download_button(doc_id)]])


def search_page_nav_keyboard(
    token: str, page: int, total_pages: int
) -> InlineKeyboardMarkup | None:
    """◀ ▶ row for the pagination-control message sent after a batch of
    per-document search result cards (SPEC §4.2, v0.2 redesign)."""
    nav: list[InlineKeyboardButton] = []
    if page > 1:
        nav.append(InlineKeyboardButton("◀", callback_data=encode_search_page(token, page - 1)))
    if page < total_pages:
        nav.append(InlineKeyboardButton("▶", callback_data=encode_search_page(token, page + 1)))
    return InlineKeyboardMarkup([nav]) if nav else None


def encode_search_expand(doc_id: int) -> str:
    data = f"{CALLBACK_SEARCH_EXPAND}:{doc_id}"
    assert len(data.encode()) <= 64, "callback_data exceeds 64 bytes"
    return data


def decode_search_expand(data: str) -> int | None:
    parts = data.split(":")
    if len(parts) != 2 or parts[0] != CALLBACK_SEARCH_EXPAND:
        return None
    try:
        return int(parts[1])
    except ValueError:
        return None


def encode_search_collapse(doc_id: int) -> str:
    data = f"{CALLBACK_SEARCH_COLLAPSE}:{doc_id}"
    assert len(data.encode()) <= 64, "callback_data exceeds 64 bytes"
    return data


def decode_search_collapse(data: str) -> int | None:
    parts = data.split(":")
    if len(parts) != 2 or parts[0] != CALLBACK_SEARCH_COLLAPSE:
        return None
    try:
        return int(parts[1])
    except ValueError:
        return None


def encode_content(doc_id: int) -> str:
    data = f"{CALLBACK_CONTENT}:{doc_id}"
    assert len(data.encode()) <= 64, "callback_data exceeds 64 bytes"
    return data


def decode_content(data: str) -> int | None:
    parts = data.split(":")
    if len(parts) != 2 or parts[0] != CALLBACK_CONTENT:
        return None
    try:
        return int(parts[1])
    except ValueError:
        return None


def content_button(doc_id: int) -> InlineKeyboardButton:
    return InlineKeyboardButton("📝", callback_data=encode_content(doc_id))


def search_card_keyboard(doc_id: int, *, expanded: bool) -> InlineKeyboardMarkup:
    """[📄 Download] + [▼ Details] / [▲ Collapse] + [📝 Content] for one
    search result card."""
    detail_button = (
        InlineKeyboardButton("▲ Collapse", callback_data=encode_search_collapse(doc_id))
        if expanded
        else InlineKeyboardButton("▼ Details", callback_data=encode_search_expand(doc_id))
    )
    return InlineKeyboardMarkup([[download_button(doc_id), detail_button, content_button(doc_id)]])


def encode_inbox_done(doc_id: int) -> str:
    data = f"{CALLBACK_INBOX_DONE}:{doc_id}"
    assert len(data.encode()) <= 64, "callback_data exceeds 64 bytes"
    return data


def decode_inbox_done(data: str) -> int | None:
    parts = data.split(":")
    if len(parts) != 2 or parts[0] != CALLBACK_INBOX_DONE:
        return None
    try:
        return int(parts[1])
    except ValueError:
        return None


def inbox_done_keyboard(doc_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("✅ Done", callback_data=encode_inbox_done(doc_id))]]
    )


def encode_preview(doc_id: int) -> str:
    data = f"{CALLBACK_PREVIEW}:{doc_id}"
    assert len(data.encode()) <= 64, "callback_data exceeds 64 bytes"
    return data


def decode_preview(data: str) -> int | None:
    parts = data.split(":")
    if len(parts) != 2 or parts[0] != CALLBACK_PREVIEW:
        return None
    try:
        return int(parts[1])
    except ValueError:
        return None


def agent_doc_actions_keyboard(doc_ids: list[int]) -> InlineKeyboardMarkup | None:
    """One row per document the agent's reply cites: [📄 #id] [👁 #id]
    [📝 #id] (download / metadata preview / text content), so a plain-text
    agent answer still gets the same file access as /search and /doc."""
    if not doc_ids:
        return None
    rows = [
        [
            InlineKeyboardButton(f"📄 #{doc_id}", callback_data=encode_download(doc_id)),
            InlineKeyboardButton(f"👁 #{doc_id}", callback_data=encode_preview(doc_id)),
            InlineKeyboardButton(f"📝 #{doc_id}", callback_data=encode_content(doc_id)),
        ]
        for doc_id in doc_ids
    ]
    return InlineKeyboardMarkup(rows)
