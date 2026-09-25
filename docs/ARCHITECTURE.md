# Architecture

## Overview

```
Google Drive folder ──▶ ingestion ──▶ PostgreSQL (Neon)  ◀──▶ FastAPI backend ◀──▶ Android app
 (service account)     extract, dedup,  documents, chunks,        auth, chat,          (technicians)
                       metadata, chunk, embeddings, users,        retrieval,
                       embed            conversations,            admin API
                                        audit events                 │
                                                                     ├──▶ Anthropic API (optional)
                                                                     └──▶ local object storage
                                                                          (manual files, page images)
                                              /admin web UI (administrators only)
```

- **Backend:** FastAPI, psycopg3, one process. Migrations in
  `backend/migrations/` are applied at startup under a PostgreSQL advisory lock.
- **Database:** PostgreSQL on Neon (pooled URL for the app, direct URL for
  migrations and dumps). Full-text search uses `tsvector`; embeddings are stored
  as `bytea` and compared in process.
- **Android:** native Jetpack Compose client using the JSON API and a session
  cookie. The checked-in `backend/openapi.json` is the API contract.
- **Admin UI:** server-rendered templates with same-origin scripts, served at
  `/admin` and `/invite`. There is no technician web UI.

## Ingestion

`app/ingestion/`: `GoogleDriveSource` lists the shared folder and caches each
file by Drive file ID (checksum-verified). Per file, under a PostgreSQL
advisory lock and a processing lease:

1. Type detection by content (magic bytes), not extension.
2. Extraction: PyMuPDF text and headings, pdfplumber tables, optional Tesseract
   OCR (bounded by page count, render-pixel budget and a timeout), python-docx,
   best-effort legacy `.doc`; `.indd` is reported unsupported.
3. Metadata from a curated manufacturer/model catalog and regexes.
4. Chunking by heading, numbered procedure, warning line and table.
5. Deduplication by SHA-256 and near-duplicate containment.
6. Embedding with a pinned `BAAI/bge-small-en-v1.5` revision baked into the image.

Runs are idempotent and resumable, recorded in `ingestion_runs` /
`ingestion_events`, and reconcile against the Drive listing: a document that
disappears from Drive is reported (`missing_from_source`), never
auto-deactivated (`docs/DRIVE_RECONCILIATION_RUNBOOK.md`). A scheduler task in
the web process (`INGESTION_SYNC_INTERVAL_MINUTES`) and the admin "Run re-index
now" button call the same path.

## Document lifecycle

A new or changed Drive file becomes a new `documents` row that is `pending`
review and not retrievable. Retrieval requires all of: `review_status =
'approved'`, `deactivated_at IS NULL`, `is_current_revision`, and an approved
`document_machines` link for the selected machine.

Promotion is one transaction under row locks: approving a document retires every
other active document at the same `source_ref` (deactivated, non-current,
`superseded_by` pointing at the replacement). `POST
/api/admin/documents/{id}/rollback` swaps back the same way; plain reactivation
of a superseded revision is refused. A partial unique index
(`documents_one_current_per_source_ref`) allows one active, approved, current
document per `source_ref`, so two current revisions cannot exist even through a
direct write. `superseded_by` and the audit trail (`document_reviewed`,
`document_superseded`, `document_rolled_back`) record the chain.

## Retrieval

`app/retrieval/search.py` fuses full-text and vector candidates with reciprocal
rank fusion, then applies small explainable boosts by question type and a
penalty for non-current revisions. Machine scoping and approval gating happen
in SQL before ranking, so a chunk from another machine or an unapproved
document cannot reach the answer generator. Vector search loads the eligible
embeddings and scores them in process; this is adequate at the current corpus
size and is a capacity risk to measure before growth (`PRODUCTION_READINESS.md`).

## Answers

`app/providers/` has two providers behind `AI_PROVIDER`:

- `local_extractive`: returns the best-matching passages verbatim with
  citations. It never generates prose.
- `anthropic`: requests a JSON answer of separate `claims`, `steps` and
  `warnings`, each citing excerpts. `parse_and_validate` accepts a response only
  if: numbers, identifiers, units and signs in each claim appear verbatim in its
  cited excerpts; warnings are quoted verbatim without a trimmed negation; each
  claim and step is the cited excerpt's own wording, lightly trimmed: direction,
  action and modal words (remove, disconnect, before, must, ...) must appear in
  the excerpt and keep its order, other words may differ only by a small
  allowance for reworded labels, and no negation or restriction is added or
  dropped. A claim that fails is dropped; a failing step or warning, or a
  number or identifier the excerpt lacks, rejects the response. A rejection
  triggers one repair attempt that names the failing items, then a fixed "could
  not verify" answer. The technician-visible text is
  assembled server-side from validated lines with inline `[n]` citation markers;
  a no-answer response shows fixed server text, never model prose. Revision
  conflict notes are computed from document metadata, never from the model.

These checks are lexical. They do not prove entailment, and a live-provider,
live-corpus adversarial evaluation has not been run (`PRODUCTION_READINESS.md`).

## Chat operations and idempotency

`POST /api/conversations/{id}/messages` accepts an `Idempotency-Key`. Each
assistant message stores `reply_to_message_id` (the user message it answers),
and a partial unique index allows one real answer per question.

- Same key, same question: the stored reply is returned. If the question was
  accepted but never answered (its worker died) and it is still the newest turn,
  the request claims the conversation lease and resumes it.
- Same key, different question: `409 IDEMPOTENCY_PAYLOAD_MISMATCH`.
- Attempt still running under a live lease: `409 IDEMPOTENCY_IN_PROGRESS`
  (retryable). Another question running: `409 CONVERSATION_BUSY`. A newer turn
  exists after an unanswered one: `409 IDEMPOTENCY_SUPERSEDED`.
- One question per conversation at a time, enforced by a lease
  (`processing_attempt_id`, 120 s) with fencing: a worker whose lease expired
  cannot write its answer.

Replaying the same key is the status poll. This is a reclaimable-attempt model,
not a durable queue: the provider call runs inside the request, so a
process shutdown during the call loses that attempt's progress (but not its
question, which the client resumes).

## Security

- bcrypt password hashing; JWT session cookie (httponly, `SameSite=Lax`,
  secure outside development); `token_version` lets disabling an account
  invalidate its tokens. Roles: technician, administrator.
- No public sign-up: registration needs an administrator-issued, email-bound,
  single-use invitation limited to `ALLOWED_REGISTRATION_DOMAINS`.
- Origin check on state-changing requests, strict CSP (`script-src 'self'`,
  `style-src 'self'`), HSTS outside development, interactive API docs disabled
  outside development.
- Rate limiting with SlowAPI is process-local.
- Secrets live in the environment or mounted files, never in the repository.

## Deployment topology

The owner decision selects Cloud Run, Neon, Secret Manager and object storage
(`OWNER_DECISION_GATE.md`). The current code assumes a long-lived single
process: the ingestion scheduler and post-response reindex run in-process,
source manuals and rendered pages use the local filesystem, and rate limits are
per process. Running it as documented on Cloud Run therefore needs durable
storage, an external scheduler and shared rate limiting first; a single
persistent host avoids those. The topology is an open release gate in
`PRODUCTION_READINESS.md`.

## Process-local caches

| Cache | Bound | Invalidation |
|---|---|---|
| AI provider object | 1 | process restart |
| Embedding model | 1 (baked into the image) | process restart |
| Rendered page PNGs (`routes_manuals._render_page_png`) | 256 entries (not byte-bounded) | process restart |
| Drive download cache (`GDRIVE_CACHE_DIR`) | disk | checksum mismatch re-downloads |
| Rate limiter | in memory | process restart |

Each instance has its own copy of every cache.
