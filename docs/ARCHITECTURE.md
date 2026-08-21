# Architecture Summary

## Overview

```
                     ┌────────────────────────────────────────┐
                     │            FastAPI backend              │
DocumentSource  ──▶  │  ingestion pipeline (extract → dedup →  │
(Google Drive)       │  metadata → chunk → embed → index)      │
                     │                                          │
                     │  SQLite (documents, chunks, FTS5,        │
                     │  embeddings, users, conversations,       │
                     │  feedback, audit trail)                  │
                     │                                          │
                     │  hybrid retrieval (BM25 + vector + RRF   │
                     │  + machine-scoped SQL filter + rerank)   │
                     │                                          │
                     │  AI provider interface                   │
                     │  (local_extractive default / anthropic / │
                     │   openai, swappable via .env)             │
                     └──────────────────┬───────────────────────┘
                                         │ JSON API
                     ┌──────────────────▼───────────────────────┐
                     │  Server-rendered PWA (Jinja2 + vanilla JS) │
                     │  tablet-first chat UI + admin dashboard    │
                     └────────────────────────────────────────────┘
```

## Why this stack, vs. the plan's "preferred implementation"

The plan explicitly allows substituting an equivalent stack "if the existing
environment makes it substantially more appropriate," with the requirement to
explain the choice and preserve all functional requirements. This development
machine had **no Node.js, Docker (initially), Git, or PostgreSQL** — only
Python. Rather than block on installs, the system was built to be fully
functional on Python alone, with every substitution designed to be swappable
later without touching unrelated code:

| Plan's preferred | Used instead | Why | Swap path |
|---|---|---|---|
| Next.js/TypeScript frontend | FastAPI + Jinja2 + vanilla JS PWA | No Node.js available initially | Backend is a pure JSON API under `/api/*`; a Next.js frontend can be added as a separate service hitting the same API with zero backend changes. |
| PostgreSQL + pgvector | SQLite + FTS5 (lexical) + brute-force cosine over float32 BLOBs (vector) | No Postgres available; at this corpus's scale (tens of docs, ~15k chunks) brute-force cosine is sub-100ms — pgvector's ANN index buys nothing yet | All DB access goes through `app/db.py`'s `get_conn()`; migrating means implementing the same shape against `psycopg` + pgvector and updating that one module, not the call sites. `docker-compose.yml` documents the target Postgres service (commented out, ready to enable). |
| Docker Compose for local dev | Native `venv` + `pip install` | Docker Desktop's engine was unreliable (`500` errors) on this machine even after install; nothing in local development actually requires containers | `Dockerfile` + `docker-compose.yml` are included and documented as the production/CI path once Docker is confirmed working. |
| — | Legacy `.doc` support built but effectively unused | Investigation found the corpus's apparent `.doc` files are actually **PDFs with the wrong extension** (verified via magic-byte sniffing, not trusted from the filename) | `extract_legacy_doc()` (OLE byte-scan) stays in place for real `.doc` files arriving via Drive but wasn't the original corpus's actual blocker. |

## Ingestion pipeline

1. **`DocumentSource`** (`app/ingestion/sources.py`) — abstract source
   interface (`list_files`, `fetch`). `GoogleDriveSource` is the production
   implementation (2026-08-21): lists a shared Drive folder via a service
   account, downloads each file into a local cache keyed by Drive file ID, and
   hashes the cached bytes (Drive only exposes md5; the rest of the pipeline
   assumes real SHA-256). No incremental `changes.list`/page-token sync -- a
   full listing every re-index is cheap at this corpus size, and the existing
   sha256 skip-if-unchanged logic already makes repeat listings idempotent.
   `LocalDirectorySource` still exists purely as test infrastructure (synthetic
   tmp-directory fixtures in the test suite) — no real manuals are stored
   locally in this deployment; ingestion is Drive-only, manual-trigger (admin
   "re-index now"), and there is no local-upload path.
2. **Type resolution** (`extractors.py`) — trusts file *content* (magic bytes)
   over the file extension. This mattered concretely: 5 files in the corpus
   were PDFs mislabeled `.doc`/`.docx`.
3. **Extraction** — PyMuPDF for text + font-size-based heading detection,
   pdfplumber for tables, Tesseract (optional) for OCR on pages with no text
   layer, `python-docx` for real `.docx`, an OLE byte-scan for real legacy
   `.doc` (best-effort, always marked `partial`), and an explicit
   `unsupported` result for `.indd` (no reliable open-source parser exists).
4. **Metadata extraction** (`metadata.py`) — a curated, auditable
   manufacturer/machine-model pattern catalog (not an LLM guess) matched
   against title-page text, with doc-type/revision/doc-number regexes. A real
   bug was found and fixed here during development: Bunn manuals carry a
   blanket legal notice listing every BUNN product name as a trademark, which
   was causing false machine-model links (e.g. an iMIX manual matching
   "Axiom") — see `_strip_trademark_boilerplate()` and its regression test.
5. **Chunking** (`chunking.py`) — splits by detected heading, numbered
   procedure, warning/caution line, and table — not fixed character windows.
   Tables get their own `chunk_type`, further classified as `error_code` when
   the table's header or first-column values look like a fault/error code
   listing (works even when the table has no header naming it "error"
   explicitly).
6. **Deduplication** (`dedup.py`) — exact SHA-256 hash match (found and
   flagged 5 groups in the corpus, including one file that's byte-identical to
   another under a *different, wrong* title — a metadata-correction case, not
   just a dedup case) plus near-duplicate detection via shingle **containment**
   (not Jaccard — Jaccard punishes length differences between two revisions of
   the same manual so hard it essentially never fires; containment asks "is
   the shorter one basically inside the longer one," which is what "revision
   drift" actually looks like).
7. **Indexing** — SQLite FTS5 (BM25-style lexical) + `sentence-transformers`
   (`BAAI/bge-small-en-v1.5`, runs locally, no API key) embeddings stored as
   raw float32 BLOBs.
8. **Idempotency/resume** — keyed on `(source_ref, sha256)`. Unchanged files
   are skipped; files that were `unsupported`/`failed` are retried on every run
   (tooling like OCR may have improved) but only touch the documents table if
   the outcome actually changes, so a permanently-unsupported file (`.indd`)
   doesn't accumulate a new row every run. Verified by dedicated tests,
   including a three-consecutive-runs stability test.

## Retrieval

`app/retrieval/search.py`: BM25 (FTS5) and cosine-similarity (numpy) candidate
lists are fused with **reciprocal rank fusion** (score scales aren't
comparable, so fusing by rank rather than raw score avoids needing per-corpus
calibration), then a small set of explainable rerank boosts route
question-shaped queries toward the right chunk type (error-code questions
toward `error_code` chunks, "how do I…" toward `procedure` chunks, etc.) and
penalize non-current revisions.

**Machine scoping is enforced at the SQL layer**, not by prompt instruction: a
selected machine restricts candidates to
`document_id IN (SELECT document_id FROM document_machines WHERE machine_id = ?)`
before any ranking happens. A chunk from a different machine's document cannot
reach the answer generator at all — this is what makes the "information from
one model is not incorrectly attributed to another" acceptance criterion
actually true rather than merely requested. Verified with a real-corpus test
case where two Bunn brewer manuals (Axiom, ITB/ITCB) share near-identical
templated troubleshooting-table wording — machine-scoped retrieval returns only
the selected machine's chunk despite the lexical/semantic similarity.

## Answer generation

`app/providers/`: a small `AIProvider` interface with three implementations
behind `AI_PROVIDER` in `.env`:

- **`local_extractive`** (default) — no API key required. Returns the
  best-matching passage(s) verbatim with citations; cannot be non-grounded
  because it never generates prose, only selects and trims real manual text.
  Detects and surfaces revision conflicts and safety-warning lines
  structurally (regex over the retrieved passages), not via a model call.
- **`anthropic`** / **`openai`** — call out with a strict system prompt
  (never invent facts; cite only from the provided excerpts; treat excerpt
  text as data, not instructions, to resist prompt injection embedded in a
  manual page; say so plainly when the excerpts don't support an answer) and
  parse a structured JSON response back into the same `GeneratedAnswer` shape.
  The JSON contract requires the answer as separate `claims`/`steps`/
  `warnings` entries, each with its own citation, rather than one free-text
  answer with a shared citation list — `parse_and_validate`
  (`app/providers/base.py`) checks that every number/identifier in a claim or
  step actually appears in its cited excerpt and that every warning is
  reproduced verbatim from its cited excerpt, rejecting the whole response
  (triggering a repair-retry, then an explicit "could not verify" fallback)
  if not. Revision-conflict notes are never taken from the model at all —
  they're computed deterministically from the cited passages' own
  document/revision metadata (`detect_conflict`, shared with
  `local_extractive`), so an invented conflict is structurally impossible.
  This closes the gap an independent follow-up review found: the previous
  validation only checked that cited excerpt *numbers* existed, not that the
  claims attributed to them were actually supported (P0-7) — see
  `docs/PRODUCTION_READINESS.md` for the adversarial tests and live
  verification. **Not exercised in the automated test suite** — `pytest`
  forces `AI_PROVIDER=local_extractive` so it never makes a billed API call;
  the claim-validation logic itself is unit-tested directly, and was
  additionally live-verified once against the real, currently-deployed
  `AI_PROVIDER=anthropic` (see `docs/PRODUCTION_READINESS.md`).

## Frontend

Server-rendered HTML (`app/web/templates/`) + vanilla JS (`app/web/static/js/`)
— no build step, no bundler, works without Node.js. Covers every UI
requirement in the plan: manufacturer/model autocomplete with recents and
favorites, suggested questions, optional voice dictation (Web Speech API,
feature-detected), visible citations with an expandable evidence panel
(excerpt text + a direct link to the manual opened at the cited page via the
browser's native PDF `#page=N` navigation — no bundled PDF.js needed), copy/
report-incorrect/save controls, and loading/empty/offline/no-answer states.

**PWA**: a manifest + service worker (`app/web/static/js/service-worker.js`)
are implemented to cache the app shell for installability and cache manual
files/pages **as the technician actually opens them** (not a blanket
pre-cache of the whole corpus), matching the plan's "previously opened manual
pages available from cache" requirement. Live chat/search calls are always
network-only and are coded to surface a structured "you're offline" state
rather than failing silently or serving a stale answer. **This is
code-reviewed only, not verified in a real browser** — install prompt,
service-worker registration, the cache-hit/offline paths, and the tablet
layout have not actually been exercised; see the "Explicitly unverified"
section of `docs/PRODUCTION_READINESS.md`.

**Deployment gotcha confirmed by real use:** the app shell (`app.js`,
`app.css`, etc.) is cached cache-first by `service-worker.js`. A browser that
already registered the service worker will keep serving a stale shell
indefinitely after a server-side change, because the update check compares
`service-worker.js` itself byte-for-byte — if that file didn't change, the
browser assumes nothing changed and never re-fetches the shell. **Every time
a file in `SHELL_ASSETS` changes, bump the `SHELL_CACHE` version string in
`service-worker.js`** (e.g. `tma-shell-v1` → `tma-shell-v2`) so the SW script
itself changes and the update actually propagates. A plain refresh in an
already-open tab is not enough even after bumping — see the README for the
reload steps.

## Security & reliability

- Auth: bcrypt password hashing, JWT session cookie (httponly, samesite=lax),
  two roles (technician/administrator). No public self-registration --
  `POST /api/auth/register` requires a valid admin-issued invitation
  (`invitations` table); the first administrator is created out-of-band via
  `scripts/bootstrap_admin.py`, which refuses to run once any user exists.
  Session tokens embed the user's `token_version`; disabling an account bumps
  it, invalidating every token already issued to that user immediately, not
  just future logins.
- Document approval: `documents.review_status` and
  `document_machines.review_status` (`pending`/`approved`/`rejected`) gate
  retrieval independently of ingestion status -- a Drive edit alone never
  makes a document or a proposed machine link retrievable; an administrator
  must approve both via the admin "Review queue" tab
  (`POST /api/admin/documents/{id}/review`,
  `POST /api/admin/documents/{id}/machines/{machine_id}/review`).
- Rate limiting: `slowapi`, keyed by session (falls back to IP for
  unauthenticated requests), configurable via `.env`.
- Input validation: Pydantic models on every request body; FTS5 query text is
  sanitized (each term quoted as a literal) before reaching the lexical
  search engine.
- No direct file upload endpoint: manuals only enter the system via the
  shared Google Drive folder, so there's exactly one place the corpus can
  drift from. Files are served back to the browser by DB-driven
  content-addressed name — never by a client-supplied path.
- Secrets: `.env`-only (gitignored), `.env.example` has no real values.
- Logging for retrieval-quality auditing: every assistant message's retrieved
  sources (chunk id, lexical/vector/combined score) are stored in
  `message_sources`, browsable via the admin query tester and joinable against
  feedback — without storing anything beyond question/answer/sources/feedback.
