from __future__ import annotations

from pathlib import Path

from paperbot.budget import BudgetStore, PriceConfig

TZ = "Europe/Warsaw"


def make_store(tmp_path: Path) -> BudgetStore:
    prices = PriceConfig(
        input_per_mtok=1.0,
        output_per_mtok=5.0,
        cache_write_per_mtok=1.25,
        cache_read_per_mtok=0.10,
    )
    return BudgetStore(tmp_path / "budget.sqlite3", prices)


async def test_today_usage_is_zero_initially(tmp_path: Path) -> None:
    store = make_store(tmp_path)

    usage = await store.today_usage(TZ)

    assert usage.cost_usd == 0.0
    assert usage.input_tokens == 0


async def test_record_usage_accumulates(tmp_path: Path) -> None:
    store = make_store(tmp_path)

    await store.record_usage(
        tz_name=TZ,
        input_tokens=1_000_000,
        output_tokens=0,
        cache_write_tokens=0,
        cache_read_tokens=0,
    )
    await store.record_usage(
        tz_name=TZ,
        input_tokens=0,
        output_tokens=1_000_000,
        cache_write_tokens=0,
        cache_read_tokens=0,
    )

    usage = await store.today_usage(TZ)

    assert usage.input_tokens == 1_000_000
    assert usage.output_tokens == 1_000_000
    # $1.00/Mtok input + $5.00/Mtok output
    assert usage.cost_usd == 6.0


async def test_record_usage_includes_cache_prices(tmp_path: Path) -> None:
    store = make_store(tmp_path)

    await store.record_usage(
        tz_name=TZ,
        input_tokens=0,
        output_tokens=0,
        cache_write_tokens=1_000_000,
        cache_read_tokens=1_000_000,
    )

    usage = await store.today_usage(TZ)

    assert usage.cost_usd == 1.25 + 0.10


async def test_last_n_days_orders_most_recent_first(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    await store.record_usage(
        tz_name=TZ, input_tokens=100, output_tokens=0, cache_write_tokens=0, cache_read_tokens=0
    )

    days = await store.last_n_days(7)

    assert len(days) == 1
    assert days[0].input_tokens == 100


async def test_persists_across_store_instances(tmp_path: Path) -> None:
    db_path = tmp_path / "budget.sqlite3"
    prices = PriceConfig(1.0, 5.0, 1.25, 0.10)
    store1 = BudgetStore(db_path, prices)
    await store1.record_usage(
        tz_name=TZ, input_tokens=500, output_tokens=0, cache_write_tokens=0, cache_read_tokens=0
    )

    store2 = BudgetStore(db_path, prices)
    usage = await store2.today_usage(TZ)

    assert usage.input_tokens == 500
