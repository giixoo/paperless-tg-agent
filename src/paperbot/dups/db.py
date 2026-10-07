"""SQLite storage for near-duplicate review (SPEC-dups §6).

A dedicated `dups.sqlite3` file under `DATA_DIR`, following the same
stdlib-`sqlite3` + `asyncio.to_thread` pattern as `paperbot.budget.BudgetStore`
(kept in a separate file rather than that one, per CLAUDE.md's "add new code
only" — this feature's tables are unrelated to LLM budget tracking).
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

SCHEMA_VERSION = 1
SESSION_TTL_HOURS = 24

STATUS_OPEN = "open"
STATUS_RESOLVED = "resolved"
STATUS_NOT_DUP = "not_dup"


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(slots=True)
class DocCache:
    doc_id: int
    content_hash: str
    shingles: set[str]
    numbers: set[str]


@dataclass(slots=True)
class DupPair:
    id: int
    a_id: int
    b_id: int
    text_sim: float
    num_sim: float
    status: str
    created_at: str


@dataclass(slots=True)
class DupSession:
    id: int
    pair_id: int
    survivor_id: int
    loser_id: int
    items: list[dict[str, object]]
    checked: list[int]
    created_at: str


@dataclass(slots=True)
class DupAction:
    id: int
    pair_id: int
    survivor_id: int
    loser_id: int
    applied: list[str]
    created_at: str


@dataclass(slots=True)
class DupStats:
    open_count: int
    resolved_count: int
    not_dup_count: int
    last_scan_at: str | None


def _pair_from_row(row: tuple[object, ...]) -> DupPair:
    return DupPair(
        id=row[0],  # type: ignore[arg-type]
        a_id=row[1],  # type: ignore[arg-type]
        b_id=row[2],  # type: ignore[arg-type]
        text_sim=row[3],  # type: ignore[arg-type]
        num_sim=row[4],  # type: ignore[arg-type]
        status=row[5],  # type: ignore[arg-type]
        created_at=row[6],  # type: ignore[arg-type]
    )


def _session_from_row(row: tuple[object, ...]) -> DupSession:
    return DupSession(
        id=row[0],  # type: ignore[arg-type]
        pair_id=row[1],  # type: ignore[arg-type]
        survivor_id=row[2],  # type: ignore[arg-type]
        loser_id=row[3],  # type: ignore[arg-type]
        items=json.loads(row[4]),  # type: ignore[arg-type]
        checked=json.loads(row[5]),  # type: ignore[arg-type]
        created_at=row[6],  # type: ignore[arg-type]
    )


def _action_from_row(row: tuple[object, ...]) -> DupAction:
    return DupAction(
        id=row[0],  # type: ignore[arg-type]
        pair_id=row[1],  # type: ignore[arg-type]
        survivor_id=row[2],  # type: ignore[arg-type]
        loser_id=row[3],  # type: ignore[arg-type]
        applied=json.loads(row[4]),  # type: ignore[arg-type]
        created_at=row[5],  # type: ignore[arg-type]
    )


class DupsStore:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db_path)

    def _init_db(self) -> None:
        conn = self._connect()
        try:
            conn.execute("CREATE TABLE IF NOT EXISTS dup_schema_version (version INTEGER)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS dup_doc_cache (
                    doc_id INTEGER PRIMARY KEY,
                    content_hash TEXT NOT NULL,
                    shingles BLOB NOT NULL,
                    numbers BLOB NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS dup_pairs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    a_id INTEGER NOT NULL,
                    b_id INTEGER NOT NULL,
                    text_sim REAL NOT NULL,
                    num_sim REAL NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(a_id, b_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS dup_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pair_id INTEGER NOT NULL,
                    survivor_id INTEGER NOT NULL,
                    loser_id INTEGER NOT NULL,
                    items_json TEXT NOT NULL,
                    checked_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS dup_actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pair_id INTEGER NOT NULL,
                    survivor_id INTEGER NOT NULL,
                    loser_id INTEGER NOT NULL,
                    applied_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS dup_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            if conn.execute("SELECT COUNT(*) FROM dup_schema_version").fetchone()[0] == 0:
                conn.execute(
                    "INSERT INTO dup_schema_version (version) VALUES (?)", (SCHEMA_VERSION,)
                )
            conn.commit()
        finally:
            conn.close()

    # --- doc cache (SPEC-dups §3.2.6) ---------------------------------------

    def _get_doc_cache_sync(self, doc_id: int) -> DocCache | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT doc_id, content_hash, shingles, numbers "
                "FROM dup_doc_cache WHERE doc_id = ?",
                (doc_id,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        return DocCache(
            doc_id=row[0],
            content_hash=row[1],
            shingles=set(json.loads(row[2])),
            numbers=set(json.loads(row[3])),
        )

    async def get_doc_cache(self, doc_id: int) -> DocCache | None:
        return await asyncio.to_thread(self._get_doc_cache_sync, doc_id)

    def _set_doc_cache_sync(
        self, doc_id: int, content_hash: str, shingles: set[str], numbers: set[str]
    ) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO dup_doc_cache (doc_id, content_hash, shingles, numbers)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(doc_id) DO UPDATE SET
                    content_hash = excluded.content_hash,
                    shingles = excluded.shingles,
                    numbers = excluded.numbers
                """,
                (
                    doc_id,
                    content_hash,
                    json.dumps(sorted(shingles)).encode("utf-8"),
                    json.dumps(sorted(numbers)).encode("utf-8"),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    async def set_doc_cache(
        self, doc_id: int, content_hash: str, shingles: set[str], numbers: set[str]
    ) -> None:
        await asyncio.to_thread(self._set_doc_cache_sync, doc_id, content_hash, shingles, numbers)

    # --- pairs (SPEC-dups §3.2.7, §4) ---------------------------------------

    def _upsert_pair_sync(
        self, a_id: int, b_id: int, text_sim: float, num_sim: float
    ) -> int | None:
        lo, hi = (a_id, b_id) if a_id < b_id else (b_id, a_id)
        conn = self._connect()
        try:
            existing = conn.execute(
                "SELECT id, status FROM dup_pairs WHERE a_id = ? AND b_id = ?", (lo, hi)
            ).fetchone()
            if existing is not None:
                return None
            cursor = conn.execute(
                """
                INSERT INTO dup_pairs (a_id, b_id, text_sim, num_sim, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (lo, hi, text_sim, num_sim, STATUS_OPEN, _now()),
            )
            conn.commit()
            return cursor.lastrowid
        finally:
            conn.close()

    async def upsert_pair(
        self, a_id: int, b_id: int, text_sim: float, num_sim: float
    ) -> int | None:
        """Insert a new open pair. Returns the new pair id, or None if a pair
        for this document pair already exists (open, resolved, or marked
        "not duplicates" — SPEC-dups §3.2.7/§8: a rescan must not resurrect a
        pair the owner already dismissed)."""
        return await asyncio.to_thread(self._upsert_pair_sync, a_id, b_id, text_sim, num_sim)

    def _get_pair_sync(self, pair_id: int) -> DupPair | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT id, a_id, b_id, text_sim, num_sim, status, created_at "
                "FROM dup_pairs WHERE id = ?",
                (pair_id,),
            ).fetchone()
        finally:
            conn.close()
        return _pair_from_row(row) if row else None

    async def get_pair(self, pair_id: int) -> DupPair | None:
        return await asyncio.to_thread(self._get_pair_sync, pair_id)

    def _next_open_pair_sync(self, exclude_ids: tuple[int, ...]) -> DupPair | None:
        conn = self._connect()
        try:
            if exclude_ids:
                placeholders = ",".join("?" for _ in exclude_ids)
                row = conn.execute(
                    "SELECT id, a_id, b_id, text_sim, num_sim, status, created_at "
                    f"FROM dup_pairs WHERE status = ? AND id NOT IN ({placeholders}) "
                    "ORDER BY text_sim DESC, id ASC LIMIT 1",
                    (STATUS_OPEN, *exclude_ids),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT id, a_id, b_id, text_sim, num_sim, status, created_at "
                    "FROM dup_pairs WHERE status = ? ORDER BY text_sim DESC, id ASC LIMIT 1",
                    (STATUS_OPEN,),
                ).fetchone()
        finally:
            conn.close()
        return _pair_from_row(row) if row else None

    async def next_open_pair(self, exclude_ids: set[int] | None = None) -> DupPair | None:
        """Highest-similarity open pair (SPEC-dups §4 `/dups`), optionally
        excluding pair ids the owner just clicked "Skip" on this session."""
        return await asyncio.to_thread(self._next_open_pair_sync, tuple(exclude_ids or ()))

    def _mark_not_dup_sync(self, pair_id: int) -> None:
        conn = self._connect()
        try:
            conn.execute("UPDATE dup_pairs SET status = ? WHERE id = ?", (STATUS_NOT_DUP, pair_id))
            conn.commit()
        finally:
            conn.close()

    async def mark_not_dup(self, pair_id: int) -> None:
        await asyncio.to_thread(self._mark_not_dup_sync, pair_id)

    def _resolve_pair_sync(self, pair_id: int, loser_id: int) -> None:
        conn = self._connect()
        try:
            conn.execute("UPDATE dup_pairs SET status = ? WHERE id = ?", (STATUS_RESOLVED, pair_id))
            # SPEC-dups §5.4.4: any other open pair that also contains the
            # (now-trashed) loser is no longer reviewable.
            conn.execute(
                "DELETE FROM dup_pairs WHERE status = ? AND id != ? AND (a_id = ? OR b_id = ?)",
                (STATUS_OPEN, pair_id, loser_id, loser_id),
            )
            conn.commit()
        finally:
            conn.close()

    async def resolve_pair(self, pair_id: int, loser_id: int) -> None:
        await asyncio.to_thread(self._resolve_pair_sync, pair_id, loser_id)

    def _stats_sync(self) -> DupStats:
        conn = self._connect()
        try:
            rows = conn.execute("SELECT status, COUNT(*) FROM dup_pairs GROUP BY status").fetchall()
            last_scan = conn.execute(
                "SELECT value FROM dup_meta WHERE key = 'last_scan_at'"
            ).fetchone()
        finally:
            conn.close()
        counts = {status: count for status, count in rows}
        return DupStats(
            open_count=counts.get(STATUS_OPEN, 0),
            resolved_count=counts.get(STATUS_RESOLVED, 0),
            not_dup_count=counts.get(STATUS_NOT_DUP, 0),
            last_scan_at=last_scan[0] if last_scan else None,
        )

    async def stats(self) -> DupStats:
        return await asyncio.to_thread(self._stats_sync)

    def _set_last_scan_at_sync(self, value: str) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO dup_meta (key, value) VALUES ('last_scan_at', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (value,),
            )
            conn.commit()
        finally:
            conn.close()

    async def set_last_scan_at(self, value: str) -> None:
        await asyncio.to_thread(self._set_last_scan_at_sync, value)

    def _add_pending_notify_sync(self, n: int) -> None:
        conn = self._connect()
        try:
            current = conn.execute(
                "SELECT value FROM dup_meta WHERE key = 'pending_notify'"
            ).fetchone()
            total = int(current[0]) if current else 0
            conn.execute(
                "INSERT INTO dup_meta (key, value) VALUES ('pending_notify', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(total + n),),
            )
            conn.commit()
        finally:
            conn.close()

    async def add_pending_notify(self, n: int) -> None:
        """Accumulate new-pair counts from automatic scans that happened
        before the owner's `DUPS_NOTIFY_TIME` (so a notification never
        arrives in the middle of the night) — see `pop_pending_notify`."""
        if n > 0:
            await asyncio.to_thread(self._add_pending_notify_sync, n)

    def _pop_pending_notify_sync(self) -> int:
        conn = self._connect()
        try:
            row = conn.execute("SELECT value FROM dup_meta WHERE key = 'pending_notify'").fetchone()
            conn.execute(
                "INSERT INTO dup_meta (key, value) VALUES ('pending_notify', '0') "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
            )
            conn.commit()
        finally:
            conn.close()
        return int(row[0]) if row else 0

    async def pop_pending_notify(self) -> int:
        """Read and reset the accumulated pending-notify count."""
        return await asyncio.to_thread(self._pop_pending_notify_sync)

    # --- sessions (SPEC-dups §5.2/§5.3) -------------------------------------

    def _save_session_sync(
        self,
        pair_id: int,
        survivor_id: int,
        loser_id: int,
        items: list[dict[str, object]],
        checked: list[int],
    ) -> None:
        conn = self._connect()
        try:
            conn.execute("DELETE FROM dup_sessions WHERE pair_id = ?", (pair_id,))
            conn.execute(
                """
                INSERT INTO dup_sessions
                    (pair_id, survivor_id, loser_id, items_json, checked_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (pair_id, survivor_id, loser_id, json.dumps(items), json.dumps(checked), _now()),
            )
            conn.commit()
        finally:
            conn.close()

    async def save_session(
        self,
        pair_id: int,
        survivor_id: int,
        loser_id: int,
        items: list[dict[str, object]],
        checked: list[int],
    ) -> None:
        await asyncio.to_thread(
            self._save_session_sync, pair_id, survivor_id, loser_id, items, checked
        )

    def _get_session_sync(self, pair_id: int) -> DupSession | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT id, pair_id, survivor_id, loser_id, items_json, checked_json, created_at "
                "FROM dup_sessions WHERE pair_id = ? ORDER BY id DESC LIMIT 1",
                (pair_id,),
            ).fetchone()
        finally:
            conn.close()
        return _session_from_row(row) if row else None

    async def get_session(self, pair_id: int) -> DupSession | None:
        return await asyncio.to_thread(self._get_session_sync, pair_id)

    def _update_checked_sync(self, pair_id: int, checked: list[int]) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE dup_sessions SET checked_json = ? WHERE pair_id = ? "
                "AND id = (SELECT MAX(id) FROM dup_sessions WHERE pair_id = ?)",
                (json.dumps(checked), pair_id, pair_id),
            )
            conn.commit()
        finally:
            conn.close()

    async def update_session_checked(self, pair_id: int, checked: list[int]) -> None:
        await asyncio.to_thread(self._update_checked_sync, pair_id, checked)

    def _delete_session_sync(self, pair_id: int) -> None:
        conn = self._connect()
        try:
            conn.execute("DELETE FROM dup_sessions WHERE pair_id = ?", (pair_id,))
            conn.commit()
        finally:
            conn.close()

    async def delete_session(self, pair_id: int) -> None:
        await asyncio.to_thread(self._delete_session_sync, pair_id)

    def _cleanup_old_sessions_sync(self) -> None:
        cutoff = (datetime.now(UTC) - timedelta(hours=SESSION_TTL_HOURS)).isoformat()
        conn = self._connect()
        try:
            conn.execute("DELETE FROM dup_sessions WHERE created_at < ?", (cutoff,))
            conn.commit()
        finally:
            conn.close()

    async def cleanup_old_sessions(self) -> None:
        await asyncio.to_thread(self._cleanup_old_sessions_sync)

    # --- actions / undo (SPEC-dups §5.4.5, §5.5) ----------------------------

    def _record_action_sync(
        self, pair_id: int, survivor_id: int, loser_id: int, applied: list[str]
    ) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO dup_actions (pair_id, survivor_id, loser_id, applied_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (pair_id, survivor_id, loser_id, json.dumps(applied), _now()),
            )
            conn.commit()
        finally:
            conn.close()

    async def record_action(
        self, pair_id: int, survivor_id: int, loser_id: int, applied: list[str]
    ) -> None:
        await asyncio.to_thread(self._record_action_sync, pair_id, survivor_id, loser_id, applied)

    def _last_action_sync(self) -> DupAction | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT id, pair_id, survivor_id, loser_id, applied_json, created_at "
                "FROM dup_actions ORDER BY id DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        return _action_from_row(row) if row else None

    async def last_action(self) -> DupAction | None:
        return await asyncio.to_thread(self._last_action_sync)
