"""Near-duplicate candidate scan (SPEC-dups §3). Pure business logic — no
Telegram/JobQueue glue, which lives in `paperbot.telegram.dups` (same split
as `paperbot.paperless` vs. `paperbot.telegram.reminders`).
"""

from __future__ import annotations

import logging

from paperbot.config import Settings
from paperbot.dups.db import DupsStore
from paperbot.dups.similarity import (
    content_hash,
    jaccard,
    normalize_text,
    number_tokens,
    shingles,
    text_length_ratio_ok,
)
from paperbot.paperless import Document, PaperlessClient, PaperlessError

logger = logging.getLogger(__name__)


def _is_skipped(
    doc: Document, skip_tags: set[str], min_chars: int, tag_name_by_id: dict[int, str]
) -> bool:
    """SPEC-dups §3.3: documents still waiting for OCR/classification, or
    too short to compare meaningfully, are not scanned.

    `DUPS_SKIP_TAGS`'s defaults (`gpt-ocr`, `gpt-auto`, ...) are themselves
    hidden workflow tags, so this must check against `doc.tag_ids` resolved
    through `tag_name_by_id` — `doc.tags` has already had hidden tags
    filtered out by `TaxonomyCache` and would never match them.
    """
    doc_tag_names = {tag_name_by_id.get(tag_id, "") for tag_id in doc.tag_ids}
    if doc_tag_names & skip_tags:
        return True
    return len(doc.content or "") < min_chars


async def _signature(db: DupsStore, doc: Document) -> tuple[set[str], set[str]]:
    """Shingle/number sets for one document, from the SQLite cache when the
    content hasn't changed since the last scan (SPEC-dups §3.2.6)."""
    content = doc.content or ""
    digest = content_hash(content)
    cached = await db.get_doc_cache(doc.id)
    if cached is not None and cached.content_hash == digest:
        return cached.shingles, cached.numbers
    shingle_set = shingles(normalize_text(content))
    number_set = number_tokens(content)
    await db.set_doc_cache(doc.id, digest, shingle_set, number_set)
    return shingle_set, number_set


async def scan_once(paperless: PaperlessClient, db: DupsStore, settings: Settings) -> int:
    """Run one full near-duplicate scan. Returns the number of *new*
    candidate pairs found (SPEC-dups §3.3)."""
    try:
        docs = await paperless.list_all_documents()
    except PaperlessError:
        logger.exception("dups scan: failed to list documents")
        return 0

    skip_tags = {t.lower() for t in settings.dups_skip_tags}
    all_tags = await paperless.taxonomy.all_tags()
    hidden_tags = await paperless.taxonomy.hidden_tags()
    tag_name_by_id = {t.id: t.name.lower() for t in (*all_tags, *hidden_tags)}
    new_pairs = 0

    # Exact duplicates (§3.1): `duplicate_documents` is confirmed directly on
    # the document list response (tests/fixtures/real_documents_list.json) —
    # more reliable than grouping by a `has_duplicates=true` filter result.
    seen_exact: set[tuple[int, int]] = set()
    for doc in docs:
        for other_id in doc.duplicate_documents:
            pair_key = (min(doc.id, other_id), max(doc.id, other_id))
            if pair_key in seen_exact:
                continue
            seen_exact.add(pair_key)
            if await db.upsert_pair(pair_key[0], pair_key[1], 1.0, 1.0) is not None:
                new_pairs += 1

    eligible = [
        d for d in docs if not _is_skipped(d, skip_tags, settings.dups_min_chars, tag_name_by_id)
    ]
    signatures = {doc.id: await _signature(db, doc) for doc in eligible}

    for i, doc_a in enumerate(eligible):
        shingles_a, numbers_a = signatures[doc_a.id]
        len_a = len(doc_a.content or "")
        for doc_b in eligible[i + 1 :]:
            len_b = len(doc_b.content or "")
            if not text_length_ratio_ok(len_a, len_b):
                continue
            shingles_b, numbers_b = signatures[doc_b.id]
            text_sim = jaccard(shingles_a, shingles_b)
            if text_sim < settings.dups_threshold:
                continue
            num_sim = jaccard(numbers_a, numbers_b)
            if num_sim < settings.dups_numbers_threshold:
                continue
            if await db.upsert_pair(doc_a.id, doc_b.id, text_sim, num_sim) is not None:
                new_pairs += 1

    await db.cleanup_old_sessions()
    return new_pairs
