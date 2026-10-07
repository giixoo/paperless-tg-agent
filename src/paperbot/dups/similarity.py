"""Text normalization and similarity scoring for near-duplicate detection
(SPEC-dups §3.2). Pure functions, no I/O, no LLM.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_SPACE_RE = re.compile(r"\s+")
_NUMBER_TOKEN_RE = re.compile(r"\S*\d\S*")

SHINGLE_SIZE = 3


def normalize_text(text: str) -> str:
    """Unicode NFKC, lowercase, punctuation removed, spaces collapsed."""
    normalized = unicodedata.normalize("NFKC", text).lower()
    normalized = _PUNCT_RE.sub(" ", normalized)
    return _SPACE_RE.sub(" ", normalized).strip()


def shingles(normalized_text: str, n: int = SHINGLE_SIZE) -> set[str]:
    """Word shingles of size `n` over already-normalized text."""
    words = normalized_text.split()
    if not words:
        return set()
    if len(words) < n:
        return {" ".join(words)}
    return {" ".join(words[i : i + n]) for i in range(len(words) - n + 1)}


def number_tokens(raw_text: str) -> set[str]:
    """Tokens containing at least one digit (dates, amounts, ids), extracted
    from the *original* (not punctuation-stripped) text so separators like
    `.`/`/`/`-` in dates and amounts are preserved."""
    return {tok.lower() for tok in _NUMBER_TOKEN_RE.findall(raw_text)}


def jaccard(a: set[str], b: set[str]) -> float:
    """Jaccard similarity. Two empty sets are defined as identical (1.0) —
    vacuously true, and needed so that number-less prose documents aren't
    penalized by the number-similarity gate (SPEC-dups §3.2.4)."""
    if not a and not b:
        return 1.0
    union = a | b
    if not union:
        return 1.0
    return len(a & b) / len(union)


def content_hash(text: str) -> str:
    """Cache key for `dup_doc_cache` (SPEC-dups §3.2.6): detects content
    changes between scans so shingles/numbers are only recomputed when
    needed."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def text_length_ratio_ok(len_a: int, len_b: int, *, min_ratio: float = 0.5) -> bool:
    """Prefilter (SPEC-dups §3.2.5): skip comparing two documents whose
    shorter text is below `min_ratio` of the longer one."""
    if len_a == 0 or len_b == 0:
        return len_a == len_b
    shorter, longer = sorted((len_a, len_b))
    return shorter / longer >= min_ratio
