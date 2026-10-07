# Production readiness

The live release checklist. One row per gate; a gate is **Closed** only when it
names evidence for the tagged commit and the exact artifact being shipped.
Anything earlier than that is history: the previous ledger is
`docs/history/PRODUCTION_READINESS_LEDGER.md` and must not be read as current
status.

Statuses: **Closed** (verified, evidence named), **Open** (work remains),
**Owner** (needs a decision or action from the owner, not a code change),
**Unverified** (built but never exercised in the required environment).

Owner column: `dev` = builders, `owner` = ceyhun@hmwagner.com.

## Code and answer safety

| Gate | Owner | Status | Evidence / what remains |
|---|---|---|---|
| Model prose cannot state an unsupported qualitative instruction | dev | Open | Claims and steps are checked clause by clause against their own best-matching excerpt sentence: every matched word (direction/action, numbers, identifiers, and the relational words `to`/`from`/`by`) must keep the excerpt's own order, both across clauses and within one shared sentence. Warning text must be quoted verbatim and every labeled WARNING/CAUTION/DANGER sentence in the model's cited *and* retrieved passages is attached to the response regardless of what the model itself cited. Negation matching is word-boundary, not substring. Any failing claim, step or warning rejects the whole response; no-answer text is fixed server text (`app/providers/base.py`, `tests/unit/test_claim_validation.py`). This is a lexical check, not entailment: a claim can pass by selecting the excerpt's own words in a misleading way as long as it keeps their order and relational words. Fix history (bypasses found and closed across three external reviews and one live-smoke-test catch): `docs/history/ANSWER_SAFETY_VALIDATOR_HISTORY.md`. Live evaluation (Anthropic, throwaway Neon branches, 2026-09-28): 9/11, two misses -- one an unreconstructable OCR-garbled source, the other the CMA-180 retrieval-rank gap below. Still needed: a live eval re-run against the current validator, an adversarial safety set (prompt injection, inverted procedures, wrong model) against the live provider, and the eval report saved as a release artifact. Explicitly out of scope: inferring an unlabeled safety prerequisite from ordinary prose with no WARNING/CAUTION label. **CMA-180 retrieval-rank gap (open):** the correct warning passage for one eval case is approved, machine-linked and exactly matching, but doesn't reach top_k=6. Within-document chunk duplication (a repeated running header/table column-header stored once per page) was fixed 2026-10-05 and improved its rank from 41st to 13th of candidates (`_drop_repeated_boilerplate()` in `app/ingestion/chunking.py`, `CURRENT_CHUNKING_VERSION=2`) -- not enough by itself. The remaining cause: the passage's true FTS rank is 82nd of 158 matches (too generic a query term), past `CANDIDATE_POOL=50` in `app/retrieval/search.py`. Reading that code shows raising `CANDIDATE_POOL` is cheaper than first assumed -- `vector_search()` already scores every eligible embedding before the pool limit applies, and `lexical_search()`'s SQL `LIMIT` runs after `ts_rank()` ranks every match, so a larger pool mainly adds Python-side RRF/rerank work over the extra candidates, not a second full scan. Still unmeasured empirically; measure with `scripts/loadtest.py` at a larger pool before changing it. |
| Answers are tied to their own question; replays never return another turn's answer | dev | Closed | `messages.reply_to_message_id` + unique answer index; `IDEMPOTENCY_*` / `CONVERSATION_BUSY` codes; `tests/api/test_answer_attempts.py` (includes the Q1/A2 sequence). Reclaimable-attempt model, not a durable queue: a shutdown mid-provider-call loses that attempt's work, and the client resumes it. |
| Android cannot lose or mislabel a second question | dev | Closed | One pending question at a time, key persisted across process death, replies matched by key and reply link, stale reloads discarded, newest-page loading (`ChatViewModelTest`). Instrumented run on the tablet: pass. |
| One current revision per manual; rollback is atomic | dev | Closed | Migration 0005 partial unique index, `POST .../rollback`, `tests/api/test_admin.py`. Production had no violations when checked. |
| Every retrievable document and link has auditable approval | owner | Owner | Approval events are audited. The 71 grandfathered manuals need evidence recorded outside the database (`OWNER_DECISION_GATE.md` open decision 6). |
| Held-out evaluation with predeclared thresholds, including unsafe-answer rate | dev + owner | Open | `data/eval/ground_truth.json` has ten cases tuned on themselves. Needs a technician-authored held-out set across machine families and risk categories. |

## Build and artifact

| Gate | Owner | Status | Evidence / what remains |
|---|---|---|---|
| Backend CI green on the release commit | dev | Open | Record the run URL for the tag. |
| Android workflow valid and jobs run | dev | Open | `android-ci.yml` no longer references `secrets` in step conditions and `workflow-lint.yml` runs actionlint; confirm the first GitHub run creates jobs. |
| Signed release built by the release workflow and verified | owner | Owner | Needs the five signing secrets in GitHub, the keystore backed up outside the build machine, then a run of **Android release**. |
| Non-demo version, real icon | dev | Closed | `versionName 1.0.0`, `versionCode 7`, adaptive launcher icon. Increment `versionCode` for every distributed build. |
| Release record filled for the exact APK; clean and upgrade install tested on tablet and phone | owner | Owner | `RELEASE_RECORD_TEMPLATE.md`. No phone run recorded. |
| Download link access control, expiry, hash | owner | Owner | Audit the external APK link separately. |
| Dependency, secret, SAST, license and workflow scans | dev | Open | Added: actionlint, bandit, pip-audit (clean locally), ruff (`backend-ci.yml`, `backend/ruff.toml`), Dependabot. Not added: Android dependency locking/verification, CodeQL, secret scanning (enable in GitHub settings), SBOM/license inventory. |

## Deployment and operations

| Gate | Owner | Status | Evidence / what remains |
|---|---|---|---|
| Deployment topology decided and matching the code | owner | Closed | Single persistent Docker host chosen (`OWNER_DECISION_GATE.md` section 14); runbook, Caddy TLS compose override and backup script in `docs/DEPLOYMENT_SINGLE_HOST.md`, `docker-compose.prod.yml`, `deploy/`. Host is provisioned at `34.11.41.167`; `bibchatbot.com` points at it and holds a production Let's Encrypt certificate. Domain/migration history: `docs/history/DEPLOYMENT_HISTORY.md`. |
| Deployed instance is current and reachable | dev | Closed | `https://bibchatbot.com/readyz` returns 200 with a valid production TLS certificate; the Android app (`versionCode 7`) reaches it and signs in successfully, confirmed on the tablet. What remains: decommission the old Cloud Run URL (`tma-backend-873813047759.us-east4.run.app`, now a stale 404). |
| `/readyz` fails on an unusable corpus | dev | Closed | 503 when nothing approved, current and machine-linked is retrievable or the corpus query fails; stale corpus and stuck ingestion reported as degraded/stuck (`tests/unit/test_readyz.py`). Alerting on the body is not set up. |
| Ingestion recovers from a killed run and syncs promptly after restart | dev | Closed | Orphaned `running` rows are failed at startup under the ingestion lock; first sync runs about a minute after start when stale; an all-failed run is `failed`; permission and size skips are run errors. An expected-corpus inventory against the real Drive folder is still needed (owner). |
| Timed restore into an isolated environment | owner | Owner | Neon PITR window unconfirmed; no drill recorded. Checked 2026-10-07: no cron job is scheduled on `tma-host` at all (`crontab -l` is empty, despite the schedule documented in `DEPLOYMENT_SINGLE_HOST.md`), and the deployed `deploy/backup.sh` is an older revision with a since-fixed bug in how it reads `DATABASE_URL_UNPOOLED` from `backend/.env` (fragile `. backend/.env` sourcing, breaks on a BOM or certain characters). Backups are not currently happening at all, scheduled or manual. |
| Load test against the real single-host/Neon path | dev | Closed | Run 2026-10-05 against production (`scripts/loadtest.py`) at rising concurrency, with host-side `docker stats` sampled every 2s throughout. Cheap/no-LLM paths scale fine: `/readyz` and `/api/config` stay under ~1s p50 even at 50 concurrent; `/api/machines/recent` (a real Neon query) stays under ~1.1s p50 at 40 concurrent, no errors anywhere. The flagged concern is confirmed and quantified: `/api/admin/query-test` (retrieval only, no LLM -- isolates the in-process vector search) runs p50 1.0s/p95 1.2s even at 5 concurrent, degrading to p50 7.2s/p95 9.6s/max 12.4s at 60 concurrent, zero errors but real degradation throughout. The host (`tma-host`) has **2 vCPUs total**; `docker stats` showed the app container at 170-187% CPU during the heaviest burst -- pegging both cores with no headroom left for anything else on the host, confirming CPU saturation (not network or DB) as the bottleneck. Memory stayed within the container's 2GiB limit (peaked ~670MiB) but didn't fully return to baseline after the burst, worth re-checking on a longer run. Full end-to-end asks (retrieval + a real LLM call) at light concurrency (3, then 6) stayed in the 5-30s range per request with zero errors. Practical read: the host handles light, bursty load (single-digit to ~15 concurrent questions, p50 1-2s) without trouble, but has no headroom for growth or spikes. See the `CANDIDATE_POOL` note in the answer-safety gate above for what this implies for that still-open question. Not done: a sustained (multi-minute, not burst) run, and testing with the Drive sync job running concurrently. |
| Logs/metrics/alerts with correlation IDs and no raw model output | dev | Open | Rejected model responses are no longer logged by default. One structured line per request (method, path template, status, latency, correlation id -- never question text, history, excerpts, bodies or cookies) is now logged for every request, not just unhandled 500s (`app/main.py`'s `correlation_id_middleware`). A concrete gap this surfaced (2026-10-07): the Anthropic provider logs only `e.status_code` on a failed call, not `e.body` -- an account-credit-exhaustion 400 was indistinguishable from any other 400 in the logs until reproduced manually. Metrics and alerts are still not built. |

## People and policy

| Gate | Owner | Status | Evidence / what remains |
|---|---|---|---|
| One account per tester, no shared demo credential | owner | Owner | `TESTER_ONBOARDING.md`; `scripts/reset_password.py` resets any seeded password. |
| Privacy, data-flow and retention notice approved | owner | Owner | Draft in `TESTER_ONBOARDING.md` with owner decisions marked. |
| Incident owner, rollback owner, support contact, stop-testing criteria | owner | Owner | Not named. |
| Supervised UAT with target users | owner | Owner | Not started. |

## Database

| Gate | Owner | Status | Evidence / what remains |
|---|---|---|---|
| Invariants enforced by the database | dev | Closed | Migrations 0005 (one current revision), 0006 (one answer per question), 0007 (CHECK constraints: role/status/rating enums), 0008 (conversation idempotency), 0009 (CHECK constraints: chunk_type, documents.doc_type, invitations.role -- file_type and source_system deliberately left open, see the migration's own comment). Applied under an advisory lock at startup; current on production as of the 2026-10-05 redeploy. |

## Tested state

Backend suite and Android JVM/instrumented results are recorded in the release
record for a specific commit, not here. Device coverage to date: Galaxy Tab
A9+ only; no phone, screen reader, split-screen or offline matrix.
