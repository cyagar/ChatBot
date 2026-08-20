# Technician Manual Assistant

A tablet-first RAG application that lets machine technicians ask natural-language
questions about commercial food-service equipment (coffee/tea/frozen-beverage
brewers, dishmachines, espresso machines, warewash controls) and get answers
grounded in — and cited to — the manufacturer's own manuals.

See `docs/ARCHITECTURE.md` for the design and the stack-substitution rationale,
and `docs/PRODUCTION_READINESS.md` for what's still outstanding before a real
production launch.

## Prerequisites

- **Python 3.12+** (developed and tested on 3.14; all dependencies ship
  Windows/Linux wheels for both).
- **Tesseract OCR** — optional, only needed to index scanned/image-only pages.
  Windows: install from https://github.com/UB-Mannheim/tesseract/wiki, then set
  `TESSERACT_CMD` in `.env` to the install path.
- **Docker** — optional, only needed for the containerized deployment path (see
  `docker-compose.yml`). Not required to run or develop locally.
- **Node.js** — not currently required. The frontend is server-rendered
  (FastAPI + Jinja2 + vanilla JS) because Node wasn't available in the initial
  development environment; see `docs/ARCHITECTURE.md` for the migration path if
  you want the plan's originally preferred Next.js frontend instead.

## Setup

```bash
cd backend
pip install -r requirements.txt
copy .env.example .env    # (or `cp` on macOS/Linux)
```

Edit `backend/.env`:

- `AI_PROVIDER` — `local_extractive` (default, no API key needed, returns
  verbatim manual passages with citations — zero hallucination risk by
  construction) or `anthropic`/`openai` (set the matching `*_API_KEY` for
  generated, synthesized answers).
- `TESSERACT_CMD` — set if you installed Tesseract and want scanned pages
  indexed.
- `LOCAL_MANUALS_DIR` — where the ingestion pipeline looks for source
  documents (defaults to `../data/manuals_incoming`).

A `SECRET_KEY` is auto-generated into `.env` on first setup in this repo's
history; if you're starting fresh, put any random 64-character string there —
**never commit `.env`** (it's gitignored).

## Indexing manuals

Drop PDF/DOC/DOCX/JPG/PNG files into `data/manuals_incoming/` (or wherever
`LOCAL_MANUALS_DIR` points), then from `backend/`:

```bash
python scripts/ingest.py
```

This is safe to re-run at any time — it's idempotent (unchanged files are
skipped) and resumable (safe to interrupt and re-run; already-indexed
documents aren't reprocessed). It prints a summary and writes full per-file
detail into the `ingestion_runs`/`ingestion_events` tables, browsable from the
admin UI (`/admin` → "Ingestion reports").

To index only new/changed files without re-embedding everything:
`python scripts/ingest.py` (embedding is incremental automatically — only
chunks without an existing embedding are processed).

## Running the app

```bash
cd backend
python -m uvicorn app.main:app --reload
```

Open `http://localhost:8000`. **The first account you register becomes the
administrator**; every account after that is a technician. Admin dashboard is
at `http://localhost:8000/admin`.

**If you change a frontend file (`app.js`, `app.css`, templates) and a tab
that already had the app open doesn't show the update:** the service worker
caches the app shell cache-first, so it can keep serving the old version even
after a normal refresh. Bump the `SHELL_CACHE` version in
`app/web/static/js/service-worker.js` (see `docs/ARCHITECTURE.md`), then in
the browser: DevTools → Application → Service Workers → **Unregister**, then
reload the page. (A plain reload after only bumping the version can still
need a second reload, since the new worker typically doesn't take over until
after the reload that triggered its install completes — unregistering is the
reliable one-step fix during development.)

## Testing

```bash
cd backend
python -m pytest
```

Covers unit (extraction, chunking, metadata, dedup), ingestion integration
(idempotency, duplicate detection, resumability — against real temp SQLite DBs
and real synthetic PDFs, not mocks), retrieval (including the machine-scoping
isolation test), and API tests (auth, chat flow, rate limiting, authorization).

### Retrieval & citation evaluation

```bash
cd backend
python scripts/eval_retrieval.py
```

Runs the ground-truth question set in `data/eval/ground_truth.json` against
the live, currently-configured pipeline end-to-end (real embeddings, real
`AI_PROVIDER`) and writes a scored report to
`data/reports/retrieval_eval_report.md`. See that file after running for
current numbers — don't take "the chatbot runs" as evidence it's accurate; this
report is the actual measurement.

## Backup

Two things to back up, both under `data/`:

- `data/db/app.db` (+ `.db-wal`/`.db-shm` if present) — all metadata, chunks,
  users, conversations, feedback. SQLite: safe to copy while the app is
  stopped; for a live backup use `sqlite3 app.db ".backup backup.db"`.
- `data/object_storage/` — the stored original manual files.

`data/manuals_incoming/` is the ingestion *inbox*; it doesn't need backing up
once files are indexed (they're copied into `object_storage`), but keeping it
costs little and lets you re-run ingestion from scratch if needed.

## Deployment

`docker-compose.yml` containers the app as built (SQLite-backed). See
`docs/ARCHITECTURE.md` for the documented (not yet implemented) path to
Postgres+pgvector for a multi-instance production deployment.

```bash
docker compose up --build
docker compose exec app python scripts/ingest.py
```

## Administrator guide

From `/admin`:

- **Manuals & metadata** — see every indexed document, its auto-detected
  manufacturer/model/doc-type/revision, and correct any of it (every edit is
  logged with who/why for audit).
- **Duplicates** — every exact-hash and near-duplicate match found during
  ingestion.
- **Ingestion reports** — trigger a re-index and inspect the full per-file
  report (indexed / duplicate / partial / failed / unsupported, with reasons).
- **Query tester** — run any question through retrieval and see the exact
  passages that would be handed to the answer generator, before generation.
- **Feedback & gaps** — technician feedback and frequently unanswered
  questions (manual-coverage gap signal).
- **Upload manual** — add a new manual; ingestion runs automatically in the
  background afterward.

## Roadmap note: Google Drive as the production document source

The ZIP used to seed this corpus was a one-time snapshot. Production intent is
a Google Drive folder that's regularly updated with new manuals. The ingestion
pipeline is already built around a `DocumentSource` interface
(`app/ingestion/sources.py`) specifically so this is a drop-in
addition later: implement a `GoogleDriveSource` (list via `files.list` scoped
to a folder id, fetch via `files.get` media download, detect changes via
`changes.list` for incremental re-sync) and register it in
`get_document_source()` — nothing in extraction, dedup, metadata, chunking, or
retrieval needs to change.
