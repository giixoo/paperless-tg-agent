from __future__ import annotations

from pathlib import Path

from paperbot.dups.db import STATUS_NOT_DUP, STATUS_OPEN, STATUS_RESOLVED, DupsStore


def make_store(tmp_path: Path) -> DupsStore:
    return DupsStore(tmp_path / "dups.sqlite3")


# --- doc cache ----------------------------------------------------------------


async def test_doc_cache_roundtrip(tmp_path: Path) -> None:
    store = make_store(tmp_path)

    await store.set_doc_cache(1, "hash1", {"a b c"}, {"123"})
    cached = await store.get_doc_cache(1)

    assert cached is not None
    assert cached.content_hash == "hash1"
    assert cached.shingles == {"a b c"}
    assert cached.numbers == {"123"}


async def test_doc_cache_miss_returns_none(tmp_path: Path) -> None:
    store = make_store(tmp_path)

    assert await store.get_doc_cache(999) is None


async def test_doc_cache_upsert_overwrites(tmp_path: Path) -> None:
    store = make_store(tmp_path)

    await store.set_doc_cache(1, "hash1", {"a"}, {"1"})
    await store.set_doc_cache(1, "hash2", {"b"}, {"2"})
    cached = await store.get_doc_cache(1)

    assert cached is not None
    assert cached.content_hash == "hash2"
    assert cached.shingles == {"b"}


# --- pairs ----------------------------------------------------------------


async def test_upsert_pair_creates_open_pair(tmp_path: Path) -> None:
    store = make_store(tmp_path)

    pair_id = await store.upsert_pair(2, 1, 0.9, 0.8)

    assert pair_id is not None
    pair = await store.get_pair(pair_id)
    assert pair is not None
    assert (pair.a_id, pair.b_id) == (1, 2)  # stored with a_id < b_id
    assert pair.status == STATUS_OPEN


async def test_upsert_pair_is_idempotent(tmp_path: Path) -> None:
    store = make_store(tmp_path)

    first = await store.upsert_pair(1, 2, 0.9, 0.8)
    second = await store.upsert_pair(2, 1, 0.9, 0.8)

    assert first is not None
    assert second is None


async def test_not_duplicate_pair_does_not_come_back_after_rescan(tmp_path: Path) -> None:
    """SPEC-dups §8: a rescan must not resurrect a pair marked "not
    duplicates"."""
    store = make_store(tmp_path)
    pair_id = await store.upsert_pair(1, 2, 0.9, 0.8)
    assert pair_id is not None
    await store.mark_not_dup(pair_id)

    resurrected = await store.upsert_pair(1, 2, 0.95, 0.85)

    assert resurrected is None
    pair = await store.get_pair(pair_id)
    assert pair is not None
    assert pair.status == STATUS_NOT_DUP


async def test_next_open_pair_orders_by_similarity_desc(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    await store.upsert_pair(1, 2, 0.81, 0.9)
    high_id = await store.upsert_pair(3, 4, 0.95, 0.9)

    pair = await store.next_open_pair()

    assert pair is not None
    assert pair.id == high_id


async def test_next_open_pair_excludes_skipped_ids(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    low_id = await store.upsert_pair(1, 2, 0.81, 0.9)
    high_id = await store.upsert_pair(3, 4, 0.95, 0.9)
    assert high_id is not None

    pair = await store.next_open_pair({high_id})

    assert pair is not None
    assert pair.id == low_id


async def test_next_open_pair_none_when_no_open_pairs(tmp_path: Path) -> None:
    store = make_store(tmp_path)

    assert await store.next_open_pair() is None


async def test_resolve_pair_removes_other_open_pairs_with_loser(tmp_path: Path) -> None:
    """SPEC-dups §5.4.4: pairs that contain a trashed document are removed."""
    store = make_store(tmp_path)
    resolved_id = await store.upsert_pair(1, 2, 0.9, 0.9)
    other_id = await store.upsert_pair(2, 3, 0.9, 0.9)
    unrelated_id = await store.upsert_pair(4, 5, 0.9, 0.9)
    assert resolved_id is not None and other_id is not None and unrelated_id is not None

    await store.resolve_pair(resolved_id, loser_id=2)

    assert (await store.get_pair(resolved_id)).status == STATUS_RESOLVED  # type: ignore[union-attr]
    assert await store.get_pair(other_id) is None
    assert await store.get_pair(unrelated_id) is not None


async def test_stats_counts_by_status(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    open_id = await store.upsert_pair(1, 2, 0.9, 0.9)
    not_dup_id = await store.upsert_pair(3, 4, 0.9, 0.9)
    resolved_id = await store.upsert_pair(5, 6, 0.9, 0.9)
    assert open_id is not None and not_dup_id is not None and resolved_id is not None
    await store.mark_not_dup(not_dup_id)
    await store.resolve_pair(resolved_id, loser_id=6)

    stats = await store.stats()

    assert stats.open_count == 1
    assert stats.not_dup_count == 1
    assert stats.resolved_count == 1
    assert stats.last_scan_at is None


async def test_set_last_scan_at_reflected_in_stats(tmp_path: Path) -> None:
    store = make_store(tmp_path)

    await store.set_last_scan_at("2026-10-07T03:30:00+00:00")
    stats = await store.stats()

    assert stats.last_scan_at == "2026-10-07T03:30:00+00:00"


# --- sessions ----------------------------------------------------------------


async def test_session_roundtrip(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    items: list[dict[str, object]] = [{"kind": "tag", "label": "tag: a"}]

    await store.save_session(1, survivor_id=10, loser_id=20, items=items, checked=[])
    session = await store.get_session(1)

    assert session is not None
    assert session.survivor_id == 10
    assert session.loser_id == 20
    assert session.items == items
    assert session.checked == []


async def test_save_session_replaces_previous_session_for_pair(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    await store.save_session(1, survivor_id=10, loser_id=20, items=[], checked=[0])

    await store.save_session(1, survivor_id=10, loser_id=20, items=[], checked=[])

    session = await store.get_session(1)
    assert session is not None
    assert session.checked == []


async def test_update_session_checked(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    await store.save_session(1, survivor_id=10, loser_id=20, items=[], checked=[])

    await store.update_session_checked(1, [0, 2])

    session = await store.get_session(1)
    assert session is not None
    assert session.checked == [0, 2]


async def test_delete_session(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    await store.save_session(1, survivor_id=10, loser_id=20, items=[], checked=[])

    await store.delete_session(1)

    assert await store.get_session(1) is None


# --- actions / undo ------------------------------------------------------------


async def test_last_action_none_when_empty(tmp_path: Path) -> None:
    store = make_store(tmp_path)

    assert await store.last_action() is None


async def test_last_action_returns_most_recent(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    await store.record_action(1, survivor_id=10, loser_id=20, applied=["tag: a"])
    await store.record_action(2, survivor_id=30, loser_id=40, applied=[])

    action = await store.last_action()

    assert action is not None
    assert action.pair_id == 2
    assert action.survivor_id == 30
    assert action.loser_id == 40
    assert action.applied == []


async def test_persists_across_store_instances(tmp_path: Path) -> None:
    db_path = tmp_path / "dups.sqlite3"
    store1 = DupsStore(db_path)
    await store1.upsert_pair(1, 2, 0.9, 0.9)

    store2 = DupsStore(db_path)
    stats = await store2.stats()

    assert stats.open_count == 1
