"""Per-chat "waiting for a reply" state (v0.2).

Lets a command ask a follow-up question ("What would you like to search
for?") instead of just showing a usage message, without retyping the
command. In-memory, process-lifetime, same pattern as `SearchStateStore`.
"""

from __future__ import annotations


class PendingInputStore:
    def __init__(self) -> None:
        self._pending: dict[int, str] = {}

    def set(self, chat_id: int, intent: str) -> None:
        self._pending[chat_id] = intent

    def pop(self, chat_id: int) -> str | None:
        return self._pending.pop(chat_id, None)

    def clear(self, chat_id: int) -> None:
        self._pending.pop(chat_id, None)


class DupsSkipStore:
    """Per-chat set of near-duplicate pair ids skipped in the current
    `/dups` review session (SPEC-dups §4/§5.1 "Skip" button) — in-memory,
    process-lifetime, same pattern as `PendingInputStore`."""

    def __init__(self) -> None:
        self._skipped: dict[int, set[int]] = {}

    def get(self, chat_id: int) -> set[int]:
        return self._skipped.get(chat_id, set())

    def add(self, chat_id: int, pair_id: int) -> None:
        self._skipped.setdefault(chat_id, set()).add(pair_id)

    def clear(self, chat_id: int) -> None:
        self._skipped.pop(chat_id, None)
