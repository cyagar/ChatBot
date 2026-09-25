# Technician Manual Assistant

A native Android app and FastAPI backend that let machine technicians ask
questions about commercial food-service equipment and get answers cited to the
manufacturer's own manuals. An administrator web UI (`/admin`) manages the
corpus, reviews, invitations and audit trail.

- `docs/ARCHITECTURE.md` — current design and data flow.
- `docs/PRODUCTION_READINESS.md` — release gates with owner, status and evidence.
- `docs/OWNER_DECISION_GATE.md` — recorded business/platform decisions.
- `docs/DRIVE_RECONCILIATION_RUNBOOK.md`, `docs/RELEASE_RECORD_TEMPLATE.md` — operations.
- `docs/TESTER_ONBOARDING.md` — accounts, privacy notice, support and offboarding.
- `android/README.md` — Android build, test and release.
- `docs/history/` — dated review reports and ledgers. Historical evidence only,
  never current status.

## Prerequisites

- Python 3.12+ (3.14 is used for local development; the container uses 3.12).
- A PostgreSQL 16+ database with the `pgvector` extension: a Neon project
  (see `docs/OWNER_DECISION_GATE.md`) or a local instance.
- Tesseract OCR (optional; needed only to index scanned pages). Set
  `TESSERACT_CMD` in `.env`.
- Docker (optional; container deployment).

## Setup (runtime)

```bash
cd backend
pip install -r requirements.txt
cp .env.example .env
```

Edit `backend/.env`. Every variable is documented in `.env.example`. The ones
that matter for a first run:

| Variable | Purpose |
|---|---|
| `APP_ENV` | `development` for local work. **`production` for any deployed instance**: it enables secure cookies and HSTS and turns on startup validation that refuses weak secrets, a missing `ALLOWED_REGISTRATION_DOMAINS`, a missing database, missing Drive configuration, or a missing Anthropic key. |
| `SECRET_KEY` | Random secret, at least 32 characters. Generate with `python -c "import secrets; print(secrets.token_urlsafe(48))"`. |
| `DATABASE_URL`, `DATABASE_URL_UNPOOLED` | Pooled connection for the app, direct connection for migrations and dumps. Both are required. |
| `AI_PROVIDER` | `local_extractive` (no API key; returns verbatim passages) or `anthropic` (`ANTHROPIC_API_KEY`, optional `ANTHROPIC_MODEL`). |
| `GOOGLE_DRIVE_FOLDER_ID`, `GOOGLE_SERVICE_ACCOUNT_JSON_PATH` | The corpus source. Drive is the only source. |

Never commit `.env`, keystores or service-account keys.

Generated answers (`AI_PROVIDER=anthropic`) are checked lexically against
their cited excerpts: numbers and identifiers must appear verbatim, warnings
must be quoted, and each claim and step must be the excerpt's own wording
(directions, actions and negations preserved, order kept for actions). A no-answer response shows fixed server text
rather than model prose. This is a mechanical check, not proof that a statement
is entailed by its source, so technicians must verify against the cited page.

## Running locally

```bash
cd backend
python -m uvicorn app.main:app --reload
```

Migrations are applied at startup under a database advisory lock, so
concurrent instances do not race. There is no public sign-up. Create the first
administrator out-of-band (refused once any user exists):

```bash
python scripts/bootstrap_admin.py --email you@example.com
```

Sign in at `http://localhost:8000/admin` and use **Invitations** to invite
technicians. Technicians use the Android app; the backend serves no
technician web UI.

Liveness is `GET /healthz` (the process is up). Readiness is `GET /readyz`
(database reachable, storage readable, corpus status).

## Indexing manuals

Manuals live in the shared Google Drive folder. A background loop re-syncs every
`INGESTION_SYNC_INTERVAL_MINUTES` (default 360; disabled when
`GOOGLE_DRIVE_FOLDER_ID` is blank), and an administrator can trigger a run from
`/admin` → Ingestion reports → Run re-index now, or with
`python scripts/ingest.py`. Runs are idempotent and resumable. Newly ingested or
replaced manuals are not retrievable until an administrator approves the
document and its machine links. Approving a replacement retires the previous
revision atomically; `POST /api/admin/documents/{id}/rollback` restores an
earlier revision the same way. See `docs/DRIVE_RECONCILIATION_RUNBOOK.md` for
documents that disappear from Drive.

## Testing

```bash
cd backend
pip install -r requirements-dev.txt
python -m pytest
```

Tests run migrations and `TRUNCATE ... CASCADE` against the database named in
`backend/.env.test` (`DATABASE_URL`, `DATABASE_URL_UNPOOLED`). Point it at a
disposable Neon branch or local Postgres, never production; the suite refuses to
start if it matches `backend/.env`. Do not run two pytest processes at once
against one test database. CI (`.github/workflows/backend-ci.yml`) runs the
full suite against a PostgreSQL 16 service container.

Android: see `android/README.md`.

### Retrieval evaluation

```bash
cd backend
EVAL_DATABASE_URL=... EVAL_DATABASE_URL_UNPOOLED=... python scripts/eval_retrieval.py
```

Both variables must name a disposable clone of the corpus (for example a Neon
branch); the script refuses to run against production. It runs
`data/eval/ground_truth.json` through the configured pipeline and writes
`data/reports/retrieval_eval_report.md`. The ground-truth set is small and its
threshold was tuned on the same cases, so it is a regression check, not
validation for release.

## Deployment

`backend/Dockerfile` builds the service (non-root user, embedding model baked
in, `--timeout-keep-alive 650` for proxies that hold idle connections).
`docker-compose.yml` runs one container for local or single-host use:

```bash
cp backend/.env.example backend/.env       # then edit
echo GOOGLE_SERVICE_ACCOUNT_JSON_FILENAME=<key file name in backend/> > .env
docker compose config                       # verify interpolation before starting
docker compose up --build
```

Production checklist:

1. `APP_ENV=production`, a real `SECRET_KEY`, both database URLs, Drive
   settings and `ANTHROPIC_API_KEY` (when `AI_PROVIDER=anthropic`) set.
2. Object storage (`LOCAL_STORAGE_DIR`) on durable, backed-up storage; the
   Drive cache (`GDRIVE_CACHE_DIR`) may be ephemeral.
3. Interactive API docs (`/docs`, `/redoc`, `/openapi.json`) are disabled
   whenever `APP_ENV` is not `development`; the checked-in
   `backend/openapi.json` is the client contract.
4. One application replica unless durable storage, external scheduling and
   shared rate limiting are in place. See `docs/PRODUCTION_READINESS.md`
   (deployment topology gate) before scaling out.

## Backup and restore

- Database: Neon point-in-time recovery, and `pg_dump "$DATABASE_URL_UNPOOLED"`
  for manual snapshots. A timed restore drill into an isolated environment is a
  release gate; no drill is recorded yet.
- `data/object_storage/`: the stored original manuals and rendered pages.
- `data/gdrive_cache/` is a download cache, not a source of record.

## Administrator guide

From `/admin`: manuals and metadata (every correction is audited), the review
queue (approve documents and machine links), duplicates, ingestion reports,
query tester, feedback and unanswered questions, invitations and accounts.
Deactivating a manual removes it from retrieval and is audited; reactivating it
as the current revision goes through the rollback path.

## Releases

Backend CI and Android CI run on every change. Signed Android builds come from
the manually triggered `Android release` workflow, which fails when any signing
secret is missing. Record each distributed build with
`docs/RELEASE_RECORD_TEMPLATE.md`.
