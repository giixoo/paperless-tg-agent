"""SQLite-backed LLM usage/cost tracking (SPEC §6)."""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


@dataclass(slots=True)
class PriceConfig:
    input_per_mtok: float
    output_per_mtok: float
    cache_write_per_mtok: float
    cache_read_per_mtok: float


@dataclass(slots=True)
class DayUsage:
    day: str
    input_tokens: int
    output_tokens: int
    cache_write_tokens: int
    cache_read_tokens: int
    cost_usd: float


def local_date_str(tz_name: str) -> str:
    return datetime.now(ZoneInfo(tz_name)).date().isoformat()


class BudgetStore:
    def __init__(self, db_path: Path, prices: PriceConfig) -> None:
        self._db_path = db_path
        self._prices = prices
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db_path)

    def _init_db(self) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS usage (
                    day TEXT PRIMARY KEY,
                    input INTEGER NOT NULL DEFAULT 0,
                    output INTEGER NOT NULL DEFAULT 0,
                    cache_write INTEGER NOT NULL DEFAULT 0,
                    cache_read INTEGER NOT NULL DEFAULT 0,
                    cost_usd REAL NOT NULL DEFAULT 0
                )
                """
            )
            conn.commit()
        finally:
            conn.close()

    def _compute_cost(
        self, input_tokens: int, output_tokens: int, cache_write_tokens: int, cache_read_tokens: int
    ) -> float:
        p = self._prices
        return (
            input_tokens / 1_000_000 * p.input_per_mtok
            + output_tokens / 1_000_000 * p.output_per_mtok
            + cache_write_tokens / 1_000_000 * p.cache_write_per_mtok
            + cache_read_tokens / 1_000_000 * p.cache_read_per_mtok
        )

    def _record_usage_sync(
        self,
        day: str,
        input_tokens: int,
        output_tokens: int,
        cache_write_tokens: int,
        cache_read_tokens: int,
    ) -> None:
        cost = self._compute_cost(
            input_tokens, output_tokens, cache_write_tokens, cache_read_tokens
        )
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO usage (day, input, output, cache_write, cache_read, cost_usd)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(day) DO UPDATE SET
                    input = input + excluded.input,
                    output = output + excluded.output,
                    cache_write = cache_write + excluded.cache_write,
                    cache_read = cache_read + excluded.cache_read,
                    cost_usd = cost_usd + excluded.cost_usd
                """,
                (day, input_tokens, output_tokens, cache_write_tokens, cache_read_tokens, cost),
            )
            conn.commit()
        finally:
            conn.close()

    async def record_usage(
        self,
        *,
        tz_name: str,
        input_tokens: int,
        output_tokens: int,
        cache_write_tokens: int,
        cache_read_tokens: int,
    ) -> None:
        day = local_date_str(tz_name)
        await asyncio.to_thread(
            self._record_usage_sync,
            day,
            input_tokens,
            output_tokens,
            cache_write_tokens,
            cache_read_tokens,
        )

    def _get_day_sync(self, day: str) -> DayUsage:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT day, input, output, cache_write, cache_read, cost_usd "
                "FROM usage WHERE day = ?",
                (day,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return DayUsage(
                day=day,
                input_tokens=0,
                output_tokens=0,
                cache_write_tokens=0,
                cache_read_tokens=0,
                cost_usd=0.0,
            )
        return DayUsage(
            day=row[0],
            input_tokens=row[1],
            output_tokens=row[2],
            cache_write_tokens=row[3],
            cache_read_tokens=row[4],
            cost_usd=row[5],
        )

    async def today_usage(self, tz_name: str) -> DayUsage:
        return await asyncio.to_thread(self._get_day_sync, local_date_str(tz_name))

    def _last_n_days_sync(self, n: int) -> list[DayUsage]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT day, input, output, cache_write, cache_read, cost_usd "
                "FROM usage ORDER BY day DESC LIMIT ?",
                (n,),
            ).fetchall()
        finally:
            conn.close()
        return [
            DayUsage(
                day=r[0],
                input_tokens=r[1],
                output_tokens=r[2],
                cache_write_tokens=r[3],
                cache_read_tokens=r[4],
                cost_usd=r[5],
            )
            for r in rows
        ]

    async def last_n_days(self, n: int) -> list[DayUsage]:
        return await asyncio.to_thread(self._last_n_days_sync, n)
