"""Duplicate detection.

Two independent mechanisms, because the source corpus contains both kinds:
  * exact_hash — byte-identical files under different names (confirmed in the
    initial corpus: three copies of 180UC_DWT_I-O_ED4, and a Dema file that
    appears under two *different titles* despite identical bytes).
  * near_duplicate_content — same document re-exported/revised, so bytes differ
    but body text is largely the same.

Near-duplicate uses a token-shingle containment estimate over normalized text
(|A∩B| / min(|A|,|B|), not Jaccard — see NEAR_DUPLICATE_THRESHOLD below for why),
which is cheap, deterministic, and does not need embeddings.
"""

from __future__ import annotations

import re

SHINGLE_SIZE = 5
# Containment (not Jaccard): |A∩B| / min(|A|,|B|). Two revisions of the same
# manual can differ a lot in length (front matter, extra sections, appendices)
# while still sharing almost all of the shorter document's content — Jaccard
# over full-document shingle sets punishes that length gap so hard it never
# fires (measured: ~0.005 on this corpus's known revision pairs). Containment
# asks "is the smaller doc essentially contained in the bigger one", which is
# what "near-duplicate revision" actually means here.
NEAR_DUPLICATE_THRESHOLD = 0.55

_WS = re.compile(r"\s+")
_NON_ALNUM = re.compile(r"[^a-z0-9 ]+")


def normalize_text(text: str) -> str:
    text = text.lower()
    text = _NON_ALNUM.sub(" ", text)
    return _WS.sub(" ", text).strip()


def shingles(text: str, size: int = SHINGLE_SIZE) -> set[str]:
    tokens = normalize_text(text).split()
    if len(tokens) < size:
        return {" ".join(tokens)} if tokens else set()
    return {" ".join(tokens[i : i + size]) for i in range(len(tokens) - size + 1)}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    intersection = len(a & b)
    union = len(a | b)
    return intersection / union if union else 0.0


def containment(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    smaller = min(len(a), len(b))
    return len(a & b) / smaller if smaller else 0.0


def content_similarity(text_a: str, text_b: str) -> float:
    return containment(shingles(text_a), shingles(text_b))


def find_near_duplicate(
    candidate_text: str,
    existing: list[tuple[int, str]],
    threshold: float = NEAR_DUPLICATE_THRESHOLD,
) -> tuple[int, float] | None:
    """Returns (document_id, similarity) of the best near-duplicate above threshold."""
    cand = shingles(candidate_text)
    best: tuple[int, float] | None = None
    for doc_id, text in existing:
        sim = containment(cand, shingles(text))
        if sim >= threshold and (best is None or sim > best[1]):
            best = (doc_id, sim)
    return best


def find_near_duplicate_cached(
    candidate_text: str,
    existing_shingles: dict[int, set[str]],
    threshold: float = NEAR_DUPLICATE_THRESHOLD,
) -> tuple[tuple[int, float] | None, list[tuple[int, float]]]:
    """Same as find_near_duplicate but takes pre-computed shingle sets, avoiding
    re-shingling every prior document for every new one.

    Returns (best_match_above_threshold, all_scores) — all_scores is returned so the
    ingestion report can show *how close* the nearest matches were even when nothing
    crosses the threshold, rather than silently reporting 'no near-duplicates'."""
    cand = shingles(candidate_text)
    scores: list[tuple[int, float]] = []
    best: tuple[int, float] | None = None
    for doc_id, sh in existing_shingles.items():
        sim = containment(cand, sh)
        scores.append((doc_id, sim))
        if sim >= threshold and (best is None or sim > best[1]):
            best = (doc_id, sim)
    scores.sort(key=lambda x: x[1], reverse=True)
    return best, scores[:3]
