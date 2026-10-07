# Answer-safety validator: fix history

Chronological record of findings and fixes against the claim/step/warning
grounding validator (`backend/app/providers/base.py`) and the CMA-180
retrieval-rank gap. This is historical evidence, not current status — see the
"Model prose cannot state an unsupported qualitative instruction" gate in
`docs/PRODUCTION_READINESS.md` for what's true now.

## 2026-09-28 review: order-checking and warning-backfill gaps

An external review found the clause-order check covered only action/direction
words, letting a claim keep an excerpt's own words while swapping which
subject, identifier or value went with which action (cause/effect,
before/after, terminal/wire and value/step swaps all passed). It also found
the warning-negation check's fixed character window could be outrun by a long
qualifying clause, and that nothing stopped a model from citing a passage
while leaving out the labeled WARNING/CAUTION/DANGER sentence next to it.

All three fixed: order-checking now covers every matched word including
numbers/identifiers, negation checking scans the whole enclosing sentence, and
every labeled warning in the answer's own cited passages is attached to the
response regardless of what the model's own warnings list contains
(`_extract_required_warnings`). This is still a lexical check, not entailment:
a claim can pass by selecting the excerpt's own words in a misleading way as
long as it keeps their order.

Live evaluation both before and after this fix (Anthropic, fresh throwaway
Neon branches, 2026-09-28): 9/11 both times, same two misses — confirms the
order/warning-backfill changes rejected no previously-passing case.

- `scanned-ocr-asq16`: a heavily OCR-garbled scanned source; wording, not
  grounding, is unreconstructable there.
- `safety-warning-cma-180uc`: root-caused directly against the corpus. The
  correct, exactly-matching passage exists and is approved and machine-linked
  ("WARNING: Electrical and grounding connections must comply with the
  applicable portions of..."), but it never reaches the top-6 retrieved
  chunks. See "CMA-180 retrieval-rank gap" below.

## 2026-10-05: CMA-180 retrieval-rank gap

Root cause 1 (fixed): the "four duplicate `documents` rows" framing in the
original finding was imprecise — the four rows for "180UC Svc & Parts Manual
Rev 1.03 050201.doc" (ids 13/14/15/18) are four genuinely different Drive
files (different `source_ref`, different chunk counts), not byte-identical
copies, which is why `app/ingestion/pipeline.py`'s whole-document
SHA-256/near-duplicate check correctly didn't flag them against each other.
The real bug was *within* each document: `chunk_document()`
(`app/ingestion/chunking.py`) works page by page with no whole-document view,
so a running header ("MODEL CMA-180UC PARTS MANUAL Rev. 1.20C") or a table's
own repeated column-header row was stored as its own chunk on every page it
appeared on — one document alone had the same ~40-character header as 25
separate chunk rows, and only 121 of its 184 chunks (66%) had distinct
content.

Fixed with `_drop_repeated_boilerplate()`: content recurring verbatim across
3+ distinct pages of one document is kept once, at its first occurrence
(`CURRENT_CHUNKING_VERSION` bumped to 2; `tests/unit/test_chunking.py`).
Applied retroactively to the four existing documents (112 redundant chunk
rows removed; confirmed 0 rows were referenced by `message_sources`, so
nothing in citation history was touched) rather than waiting on a
reprocessing feature that doesn't exist yet (`needs_reprocessing` is a
surfaced flag, not an automated trigger).

Measured effect on the exact eval query ("What electrical safety requirements
apply when installing this dishmachine?"): the target chunk's fused-and-
boosted rank improved from 41st of 70 candidates to 13th — confirmed by
rerunning `hybrid_search` directly against production before and after — but
not by itself enough to reach `top_k=6`.

Root cause 2 (still open): the correct warning chunk is short and generically
worded ("WARNING: Electrical and grounding connections must comply with the
applicable portions of the..."), so it never reaches Postgres full-text
search's own `CANDIDATE_POOL=50` — its true, unlimited rank among this
machine's 158 FTS-matching chunks is 82nd, since "electrical" alone (the only
query term it contains) is common throughout an electrical-installation
manual. It survives purely on a ~0.69 vector-cosine score plus the existing
warning-type rerank boost, which together aren't enough when several other,
more specific warning chunks from the same document (drain-screen caution,
galvanized-pipe caution) legitimately outrank it for this query and fill the
remaining slots.

**2026-10-07 correction:** raising `CANDIDATE_POOL` was deferred pending a
load-test measurement on the assumption that it would roughly double
retrieval's per-request CPU cost. Reading `search.py` shows that's wrong:
`vector_search()` scores every eligible embedding for the machine before
`CANDIDATE_POOL` is ever applied (it's a final `np.argsort(...)[:limit]`
slice over the full result), and `lexical_search()`'s SQL `LIMIT` is applied
after `ts_rank()` ranks every FTS-matching row. Raising the pool mainly adds a
small amount of Python-side RRF/rerank work over the extra candidates, not a
second full scan. Cost impact is likely low but still unmeasured empirically —
see the current gate text for what to do before changing it.

## 2026-10-05: live smoke test after redeploy

A live smoke test against the production deployment right after that day's
redeploy found a known-good eval case (`troubleshoot-axiom-heating`), which
had passed every prior run, was now being rejected. Reproducing the exact live
request locally against the same production database found two distinct
bugs, one pre-existing:

- `_warning_supported`'s negation check matched the literal substring "NOT"
  anywhere in the excerpt sentence, which also matches inside the word
  "NOTICE" — a label this corpus's own troubleshooting tables use — so any
  NOTICE-labeled passage falsely looked like it had a dropped negation. Fixed
  by requiring a word-boundary match instead of substring containment
  (`_NEGATION_WORD_PATTERNS`), which also fixed the same false positive for
  "never" inside "whenever".
- A claim that copies an excerpt's own numbered list markers verbatim ("1.
  Tank Heater failure. 2. Control Board/Thermistor failure" — exactly what the
  system prompt asks for) treated each bare "1."/"2." as its own content
  clause, which could match an unrelated stray digit elsewhere in the excerpt
  and falsely trip the clause-order check. Fixed by treating a clause left
  with nothing but digits as imposing no order, the same as an empty clause.

Both reproduced against the real failing live response before the fix and
confirmed fixed after, with regression tests added
(`test_a_warning_quoted_from_a_notice_labeled_passage_is_not_rejected_as_a_negation_drop`,
`test_a_bare_list_marker_number_in_a_claim_does_not_impose_a_bogus_order`).

## 2026-10-05: third external audit — preposition and passive-voice bypasses

Found two more validator bypasses the order check didn't catch, since
`to`/`from`/`by` were plain stopwords dropped before it ran:

- A preposition swap ("from the tank to the arm" → "to the tank from the
  arm"): entities keep their position, only which is source/destination
  flips.
- A passive-voice drop ("caused by X" → "causes X"): reverses cause and
  effect with no word actually reordered.

Fixed: these three words are now tracked through the order check instead of
discarded, and a claim that drops one of them from within the span it draws
from is rejected (`_RELATIONAL_STEMS`).

The same review found two more warning-backfill gaps:

- A labeled hazard in a retrieved-but-uncited passage was never surfaced
  (`_extract_required_warnings` only scanned `cited_passages`; now scans every
  passage retrieval gave the model for the question, still bounded at
  `top_k=6`).
- A label following a heading with no sentence-ending punctuation of its own
  ("Safety Precautions\nWARNING: ...") was missed by a check anchored only at
  the start of the whole merged excerpt unit (now also checks after each
  internal newline).

All four confirmed to fail their new regression tests before the fix and pass
after.

**Explicitly out of scope:** an unlabeled prerequisite stated as an ordinary
sentence (the audit's third warning-gap example, "Disconnect power before
servicing" with no WARNING/CAUTION label at all) — `_extract_required_warnings`'s
own doc comment states this. Reliably inferring an implicit safety
prerequisite from unlabeled prose is a materially harder problem than the four
fixed here, not attempted in this pass.
