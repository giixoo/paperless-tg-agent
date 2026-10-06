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


def search_pagination_keyboard(
    token: str, page: int, total_pages: int
) -> InlineKeyboardMarkup | None:
    if total_pages <= 1:
        return None
    buttons: list[InlineKeyboardButton] = []
    if page > 1:
        buttons.append(InlineKeyboardButton("◀", callback_data=encode_search_page(token, page - 1)))
    if page < total_pages:
        buttons.append(InlineKeyboardButton("▶", callback_data=encode_search_page(token, page + 1)))
    return InlineKeyboardMarkup([buttons]) if buttons else None
