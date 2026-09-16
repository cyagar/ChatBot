"""Hybrid retrieval: BM25/FTS5 lexical + dense vector, fused and reranked, with
hard machine-scoped filtering.

The machine filter is the safety-critical part. Plan requirement 11 ("Prevents
information from a similarly named but different machine from being presented as
if it applies to the selected model") is enforced structurally, not by prompt
instruction: when a machine is selected, candidate chunks are restricted at the
SQL level to documents linked to that machine. A passage from a different model
cannot reach the answer generator at all.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import numpy as np

from app.config import get_settings
from app.db import get_conn
from app.retrieval.embeddings import blob_to_vector, embed_query

logger = logging.getLogger(__name__)

RRF_K = 60          # reciprocal-rank-fusion damping constant
CANDIDATE_POOL = 50
MIN_CONTENT_CHARS_FOR_RESULT = 4  # excludes near-empty extraction-noise chunks; see hybrid_search


@dataclass
class RetrievedChunk:
    chunk_id: int
    document_id: int
    content: str
    page_number: int | None
    section_heading: str | None
    chunk_type: str
    original_filename: str
    title: str | None
    doc_type: str | None
    revision: str | None
    manufacturer: str | None
    is_current_revision: bool
    lexical_score: float = 0.0
    vector_score: float = 0.0
    combined_score: float = 0.0


_FTS_SPECIAL = re.compile(r'["\'\(\)\*\:\^\-&|!<>]')


def _sanitize_fts_query(q: str) -> str:
    """Postgres to_tsquery() has its own query syntax (&, |, !, (), :, <->) --
    user text must be neutralized to avoid both syntax errors and unintended
    operators, same reasoning as the old FTS5 sanitizer this replaced (that
    version stripped FTS5's own special-character set; this one strips
    to_tsquery's instead, a superset that also covers tsquery's boolean
    operators). Each term is OR'd together -- broad recall across whatever
    words the question actually contains, same as before."""
    cleaned = _FTS_SPECIAL.sub(" ", q)
    terms = [t for t in cleaned.split() if t.strip()]
    if not terms:
        return ""
    return " | ".join(terms)


def _machine_filter_sql(machine_id: int | None) -> tuple[str, list]:
    if machine_id is None:
        return "", []
    return (
        " AND d.id IN (SELECT document_id FROM document_machines "
        "WHERE machine_id = %s AND review_status = 'approved') ",
        [machine_id],
    )


def _revision_filter_sql(include_superseded: bool) -> str:
    """Superseded revisions are EXCLUDED from retrieval by default, not merely
    rank-penalized (independent follow-up review P1-11: "is_current_revision
    receives only a ranking penalty").

    A superseded manual is not a weaker answer, it's a wrong one -- a
    technician acting on a withdrawn torque spec or an obsolete wiring
    diagram is the exact harm this system exists to prevent, and a -0.20
    rerank boost only makes that outcome less likely, not impossible.
    `include_superseded=True` exists for the admin query tester, where seeing
    what WOULD have matched is the point."""
    return "" if include_superseded else " AND d.is_current_revision = true "


def lexical_search(query: str, machine_id: int | None, limit: int = CANDIDATE_POOL,
                    include_superseded: bool = False) -> list[tuple[int, float]]:
    fts_query = _sanitize_fts_query(query)
    if not fts_query:
        return []
    filter_sql, filter_params = _machine_filter_sql(machine_id)
    revision_sql = _revision_filter_sql(include_superseded)
    # Ported from SQLite's FTS5 virtual table (chunks_fts + bm25()) to
    # Postgres full-text search: chunks.content_tsv (a GENERATED tsvector
    # column, GIN-indexed -- see backend/migrations/0001_initial_schema.sql)
    # plus ts_rank(), matched with @@ against a to_tsquery() built from the
    # same OR-of-terms this always sent FTS5.
    sql = f"""
        SELECT c.id AS chunk_id, ts_rank(c.content_tsv, to_tsquery('english', %s)) AS score
        FROM chunks c
        JOIN documents d ON d.id = c.document_id
        WHERE c.content_tsv @@ to_tsquery('english', %s)
          AND d.status IN ('indexed','partial')
          AND d.deactivated_at IS NULL
          AND d.review_status = 'approved'
          {revision_sql}
          {filter_sql}
        ORDER BY score DESC
        LIMIT %s
    """
    with get_conn() as conn:
        rows = conn.execute(sql, [fts_query, fts_query, *filter_params, limit]).fetchall()
    # ts_rank() already returns higher = better, unlike FTS5's bm25() (lower
    # = better) -- no negation needed here, unlike the old return.
    return [(r["chunk_id"], r["score"]) for r in rows]


def vector_search(query: str, machine_id: int | None, limit: int = CANDIDATE_POOL,
                   include_superseded: bool = False) -> list[tuple[int, float]]:
    filter_sql, filter_params = _machine_filter_sql(machine_id)
    revision_sql = _revision_filter_sql(include_superseded)
    sql = f"""
        SELECT e.chunk_id, e.dim, e.vector
        FROM embeddings e
        JOIN chunks c ON c.id = e.chunk_id
        JOIN documents d ON d.id = c.document_id
        WHERE d.status IN ('indexed','partial')
          AND d.deactivated_at IS NULL
          AND d.review_status = 'approved'
          {revision_sql}
          {filter_sql}
    """
    with get_conn() as conn:
        rows = conn.execute(sql, filter_params).fetchall()

    if not rows:
        # No eligible chunks (empty corpus, or none for this machine) -- skip
        # the embedding call entirely rather than loading the model just to
        # discover there's nothing to compare against. Also means a query
        # with no matching documents never depends on model/network
        # availability at all (independent review P1-5/P2-1).
        return []

    try:
        qvec = embed_query(query)
    except Exception:
        # The embedding model failing to load (misconfigured/offline deployment;
        # see get_model()'s docstring) must degrade search, not break it. Lexical
        # (FTS) search has no model dependency at all, so hybrid_search() can
        # still return real, citable results from it -- refusing outright here
        # would throw away a working result set over an unrelated component
        # being down (independent follow-up review P1-5, same precedent as the
        # P1-2 fix's "labeled lexical-only degraded mode"). routes_chat.py's
        # honest-failure-message path remains the backstop for when even
        # lexical_search / the fusion below can't produce anything.
        logger.exception("Embedding model unavailable; falling back to lexical-only search")
        return []

    dim = rows[0]["dim"]
    matrix = np.vstack([blob_to_vector(r["vector"], r["dim"]) for r in rows])
    # Vectors are stored L2-normalized, so dot product == cosine similarity.
    scores = matrix @ qvec
    chunk_ids = [r["chunk_id"] for r in rows]
    top_idx = np.argsort(scores)[::-1][:limit]
    return [(chunk_ids[i], float(scores[i])) for i in top_idx]


def reciprocal_rank_fusion(
    lexical: list[tuple[int, float]],
    vector: list[tuple[int, float]],
) -> dict[int, float]:
    """RRF is used instead of raw score blending because BM25 and cosine live on
    incomparable scales; rank-based fusion needs no per-corpus normalization."""
    fused: dict[int, float] = {}
    for rank, (chunk_id, _) in enumerate(lexical):
        fused[chunk_id] = fused.get(chunk_id, 0.0) + 1.0 / (RRF_K + rank + 1)
    for rank, (chunk_id, _) in enumerate(vector):
        fused[chunk_id] = fused.get(chunk_id, 0.0) + 1.0 / (RRF_K + rank + 1)
    return fused


def _hydrate(chunk_ids: list[int]) -> dict[int, RetrievedChunk]:
    if not chunk_ids:
        return {}
    placeholders = ",".join("%s" for _ in chunk_ids)
    sql = f"""
        SELECT c.id AS chunk_id, c.document_id, c.content, c.page_number,
               c.section_heading, c.chunk_type,
               d.original_filename, d.title, d.doc_type, d.revision,
               d.is_current_revision, m.name AS manufacturer
        FROM chunks c
        JOIN documents d ON d.id = c.document_id
        LEFT JOIN manufacturers m ON m.id = d.manufacturer_id
        WHERE c.id IN ({placeholders})
    """
    with get_conn() as conn:
        rows = conn.execute(sql, chunk_ids).fetchall()
    return {
        r["chunk_id"]: RetrievedChunk(
            chunk_id=r["chunk_id"],
            document_id=r["document_id"],
            content=r["content"],
            page_number=r["page_number"],
            section_heading=r["section_heading"],
            chunk_type=r["chunk_type"],
            original_filename=r["original_filename"],
            title=r["title"],
            doc_type=r["doc_type"],
            revision=r["revision"],
            manufacturer=r["manufacturer"],
            is_current_revision=bool(r["is_current_revision"]),
        )
        for r in rows
    }


def _rerank_boost(chunk: RetrievedChunk, query: str) -> float:
    """Light, explainable reranking signals. Deliberately not a neural reranker:
    these boosts are auditable in the admin retrieval inspector, which matters more
    here than a marginal ranking gain."""
    boost = 0.0
    q = query.lower()

    # Prefer the current revision when several revisions of a manual exist.
    if not chunk.is_current_revision:
        boost -= 0.20

    # Question-type affinity: route to the chunk type that actually answers it.
    if re.search(r"error|code|fault|e-?\d{1,3}\b", q) and chunk.chunk_type == "error_code":
        boost += 0.35
    if re.search(r"part|number|p/n|replace|kit", q) and chunk.chunk_type == "table":
        boost += 0.25
    if re.search(r"how do i|how to|step|procedure|install|replace|adjust", q) and chunk.chunk_type == "procedure":
        boost += 0.25
    if re.search(r"volt|amp|psi|temperature|degrees|spec|rating|dimension", q) and chunk.chunk_type in ("table", "spec"):
        boost += 0.25
    if re.search(r"safe|warning|caution|danger|lockout|shock", q) and chunk.chunk_type == "warning":
        boost += 0.30

    # Prefer service/repair docs for troubleshooting language.
    if re.search(r"not (heating|working|brewing)|won'?t|fail|troubleshoot|diagnos", q):
        if chunk.doc_type == "service_repair":
            boost += 0.20

    return boost


def hybrid_search(
    query: str,
    machine_id: int | None = None,
    top_k: int = 6,
    include_superseded: bool = False,
) -> list[RetrievedChunk]:
    """`include_superseded` defaults to False: superseded revisions are
    excluded from technician-facing retrieval outright (P1-11), not just
    rank-penalized. Only the admin query tester passes True, where the point
    is to inspect what would otherwise have matched."""
    lexical = lexical_search(query, machine_id, include_superseded=include_superseded)
    vector = vector_search(query, machine_id, include_superseded=include_superseded)

    fused = reciprocal_rank_fusion(lexical, vector)
    if not fused:
        return []

    lexical_map = dict(lexical)
    vector_map = dict(vector)
    hydrated = _hydrate(list(fused.keys()))

    results: list[RetrievedChunk] = []
    for chunk_id, fused_score in fused.items():
        chunk = hydrated.get(chunk_id)
        if chunk is None:
            continue
        # Near-empty chunks (single stray characters/digits from OCR or table-cell
        # extraction noise) can score an artificially high cosine similarity against
        # short queries despite carrying no real content — found via a bare-code-query
        # probe where a chunk whose entire content was "E" scored 0.75 against "E4",
        # clearing the relevance gate. They carry nothing citable, so exclude them here
        # rather than expensively re-chunking/re-embedding the whole corpus.
        if len(chunk.content.strip()) < MIN_CONTENT_CHARS_FOR_RESULT:
            continue
        chunk.lexical_score = lexical_map.get(chunk_id, 0.0)
        chunk.vector_score = vector_map.get(chunk_id, 0.0)
        chunk.combined_score = fused_score + _rerank_boost(chunk, query)
        results.append(chunk)

    results.sort(key=lambda c: c.combined_score, reverse=True)
    return results[:top_k]
