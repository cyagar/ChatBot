"""Provider that requires no API key.

It does not write prose — it selects and presents the manual's own text, with
citations. That makes it strictly non-hallucinating by construction (every
sentence shown is copied verbatim from an indexed passage), which is why it is the
safe default while no provider key is configured. Its weakness is synthesis: it
cannot combine two passages into a single narrative answer or paraphrase for
brevity. Set AI_PROVIDER=anthropic|openai for that.
"""

from __future__ import annotations

import re

from app.providers.base import AIProvider, Citation, GeneratedAnswer, detect_conflict

WARNING_RE = re.compile(r"^(WARNING|CAUTION|DANGER|NOTICE|IMPORTANT)\b.*", re.IGNORECASE | re.MULTILINE)

# Gate on raw relevance, not the RRF-fused combined_score. Vector search always
# returns its k-nearest neighbors regardless of how weak the match is, so a
# fused rank-based score is never actually zero for an off-topic question —
# there's always *something* at some rank.
#
# Two signals were tried and measured against this corpus (see
# data/reports/retrieval_eval_report.md and the debug scripts used to produce
# these numbers) before picking a threshold:
#   - lexical_score > 0 is USELESS as a gate: FTS5's OR-of-all-terms means any
#     shared stopword ("the", "is", "a", "for") produces a positive BM25 score,
#     and even genuine content-word overlap on a single word ("pressure" in
#     "tire pressure" vs. "water pressure") produces scores (3.6-12.6) that
#     overlap heavily with genuinely relevant queries (6.8-19.1) — no clean
#     separating threshold exists.
#   - cosine similarity (vector_score) DOES separate, but the first pass at a
#     threshold (0.65) was fit on only 4 questions (2 relevant, 2 irrelevant)
#     and turned out to reject real, answerable questions in normal browser
#     use ("machine won't turn on" scored 0.608-0.633 depending on phrasing).
#     A wider re-survey (13 realistic relevant questions + 8 irrelevant/
#     absent-answer questions, across 3 machines, using the max vector_score
#     across the top 5 fused results — see below) found: relevant-question
#     floor 0.633, irrelevant/absent-answer ceiling 0.6165 (the ground-truth
#     "What is the machine's Bluetooth pairing code?" case). 0.62 sits in that
#     narrower but real gap.
# This is an empirical threshold for one embedding model on one corpus, not a
# universal constant — re-measure (and re-run scripts/eval_retrieval.py's
# absent-answer cases) if the embedding model, corpus, or measured gap changes.
MIN_VECTOR_SIMILARITY_FOR_ANSWER = 0.62

# The vector gate alone has a measured blind spot: short-token lookups (part
# numbers like "81-118-31", error codes) score below 0.65 even when a passage
# contains the exact string verbatim, because embeddings encode meaning, not
# identifiers — e.g. the real chunk containing "81-118-31" scored only 0.62.
# Rather than lower the global threshold (which would let genuinely off-topic
# passages through, defeating the reason 0.65 was picked), this adds a narrow,
# precise rescue: if a code-like token from the question (has a digit, len>=2)
# appears as an exact whole token in a candidate passage, that passage is
# treated as relevant regardless of its vector score. Exact tokenization (not
# substring search) matters — it is what keeps a query for "E4" from being
# "rescued" by an unrelated chunk whose content is just the single letter "E".
_CODE_TOKEN_RE = re.compile(r"^[A-Za-z]{0,4}-?\d[\dA-Za-z-]*$")


def _extract_code_tokens(text: str) -> set[str]:
    words = re.findall(r"[A-Za-z0-9-]+", text)
    return {
        w.upper()
        for w in words
        if len(w) >= 2 and any(c.isdigit() for c in w) and _CODE_TOKEN_RE.match(w)
    }


def _code_token_rescue(question: str, passages):
    """Return the first passage whose content contains an exact code-token
    match for the question, or None if no such rescue applies."""
    q_tokens = _extract_code_tokens(question)
    if not q_tokens:
        return None
    for p in passages:
        content_tokens = _extract_code_tokens(p.content)
        if q_tokens & content_tokens:
            return p
    return None


def _excerpt(content: str, limit: int = 700) -> str:
    content = content.strip()
    if len(content) <= limit:
        return content
    cut = content[:limit]
    last_break = max(cut.rfind("\n"), cut.rfind(". "))
    if last_break > limit * 0.5:
        cut = cut[: last_break + 1]
    return cut.rstrip() + " …"


class ExtractiveProvider(AIProvider):
    name = "local_extractive"

    def generate(self, question, machine_label, passages, history=None) -> GeneratedAnswer:
        # `history` is unused: this provider only ever quotes the current
        # question's retrieved passages verbatim, so there is no synthesis step
        # that could use prior turns even if they were provided.
        if not passages:
            return GeneratedAnswer(
                answer=(
                    "I could not find anything in the indexed manuals that answers this "
                    f"{'for ' + machine_label if machine_label else ''}.\n\n"
                    "**What to verify next:**\n"
                    "1. Confirm the exact model and serial number on the machine's data plate — "
                    "the manual set may index it under a different model name.\n"
                    "2. Check whether the machine's manual is in the library at all "
                    "(an administrator can confirm from the ingestion report).\n"
                    "3. If the manual exists but is a scanned copy, its text may not be searchable yet."
                ),
                is_no_answer=True,
                provider=self.name,
            )

        top = passages[0]
        # Gate on the best vector score among the top candidates, not just the
        # #1 fused/reranked result: RRF can rank a passage first purely on
        # lexical evidence (e.g. an exact phrase match), leaving its
        # vector_score at 0 because it never appeared in the vector search's
        # own candidate list -- found via a live survey where "what parts are
        # needed for a routine service" had top.vector_score == 0.0 even
        # though a genuinely relevant passage sat at vector_score 0.727 a few
        # ranks down. Gating on passages[0] alone would refuse to answer a
        # clearly answerable question.
        candidates = passages[:5]
        is_relevant = top.vector_score >= MIN_VECTOR_SIMILARITY_FOR_ANSWER
        if not is_relevant:
            # passages[0] itself didn't clear the bar, but something else in the
            # top few did (the fusion-bug case above). Display THAT passage, not
            # passages[0] -- otherwise a technically-correct "yes, relevant"
            # verdict still shows the lexical/rerank winner, which can be
            # tangential (e.g. a generic caution notice that merely shares a
            # word) instead of the passage that's actually semantically on-topic.
            best_by_vector = max(candidates, key=lambda p: p.vector_score, default=None)
            if best_by_vector is not None and best_by_vector.vector_score >= MIN_VECTOR_SIMILARITY_FOR_ANSWER:
                top = best_by_vector
                is_relevant = True
        if not is_relevant:
            rescued = _code_token_rescue(question, candidates)
            if rescued is not None:
                top = rescued
                is_relevant = True
        if not is_relevant:
            return GeneratedAnswer(
                answer=(
                    "I found some possibly related manual content, but nothing that clearly "
                    "answers this question. Rather than guess, here is the closest material — "
                    "please verify it applies before acting on it."
                ),
                citations=self._citations(passages[:3]),
                is_no_answer=True,
                provider=self.name,
            )

        lines = []
        header = f"From the manual for **{machine_label}**:" if machine_label else "From the indexed manuals:"
        lines.append(header)
        lines.append("")
        lines.append(_excerpt(top.content, 900))

        # `top` may not be passages[0] (see above), so exclude it by identity
        # rather than by position, and rank the rest by actual relevance.
        supporting = sorted(
            (p for p in passages if p.chunk_id != top.chunk_id and p.vector_score >= MIN_VECTOR_SIMILARITY_FOR_ANSWER),
            key=lambda p: p.vector_score,
            reverse=True,
        )[:3]
        if supporting:
            lines.append("")
            lines.append("**Additional relevant passages:**")
            for p in supporting:
                loc = f"{p.original_filename}" + (f", p.{p.page_number}" if p.page_number else "")
                lines.append(f"\n*{loc}*")
                lines.append(_excerpt(p.content, 400))

        warnings = self._collect_warnings(passages)
        conflict = detect_conflict(passages)

        # citations[0] must be `top` -- the passage the answer text actually
        # quotes -- not just passages[0], which can differ (see above).
        ordered = [top] + [p for p in passages if p.chunk_id != top.chunk_id]
        return GeneratedAnswer(
            answer="\n".join(lines),
            citations=self._citations(ordered[:5]),
            safety_warnings=warnings,
            conflict_note=conflict,
            provider=self.name,
        )

    @staticmethod
    def _collect_warnings(passages) -> list[str]:
        found: list[str] = []
        for p in passages:
            if p.chunk_type == "warning":
                found.append(_excerpt(p.content, 300))
                continue
            for m in WARNING_RE.finditer(p.content):
                text = m.group(0).strip()
                if len(text) > 15 and text not in found:
                    found.append(text[:300])
        return found[:5]

    @staticmethod
    def _citations(passages) -> list[Citation]:
        return [
            Citation(
                chunk_id=p.chunk_id,
                document_id=p.document_id,
                filename=p.original_filename,
                title=p.title,
                page_number=p.page_number,
                section_heading=p.section_heading,
                revision=p.revision,
                excerpt=_excerpt(p.content, 500),
            )
            for p in passages
        ]
