# Production readiness

The live release checklist. One row per gate; a gate is **Closed** only when it
names evidence for the tagged commit and the exact artifact being shipped.
Anything earlier than that is history: the previous ledger is
`docs/history/PRODUCTION_READINESS_LEDGER.md` and must not be read as current
status. Most of these gates come from the second external audit (2026-09-24).

Statuses: **Closed** (verified, evidence named), **Open** (work remains),
**Owner** (needs a decision or action from the owner, not a code change),
**Unverified** (built but never exercised in the required environment).

Owner column: `dev` = builders, `owner` = ceyhun@hmwagner.com.

## Code and answer safety

| Gate | Owner | Status | Evidence / what remains |
|---|---|---|---|
| Model prose cannot state an unsupported qualitative instruction | dev | Open | Claims and steps are checked clause by clause against their own best-matching excerpt sentence with directions, actions and negations preserved and cross-clause order enforced; any failing claim, step or warning rejects the whole response (nothing is silently dropped), and no-answer text is fixed server text (`app/providers/base.py`, `tests/unit/test_claim_validation.py`). Live evaluation (Anthropic, fresh throwaway Neon branch, 2026-09-28): 9/11 -- the 2 misses are a heavily OCR-garbled scanned source (`scanned-ocr-asq16`; wording, not grounding, is unreconstructable there) and a retrieval-ranking miss unrelated to validation (`safety-warning-cma-180uc`), neither a validator regression. This is a lexical check, not entailment. Remaining: an adversarial safety set (prompt injection, inverted procedures, wrong model) run against the live provider. |
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
| Non-demo version, real icon | dev | Closed | `versionName 1.0.0`, `versionCode 3`, adaptive launcher icon. Increment `versionCode` for every distributed build. |
| Release record filled for the exact APK; clean and upgrade install tested on tablet and phone | owner | Owner | `RELEASE_RECORD_TEMPLATE.md`. No phone run recorded. |
| Download link access control, expiry, hash | owner | Owner | Audit the external APK link separately. |
| Dependency, secret, SAST, license and workflow scans | dev | Open | Added: actionlint, bandit, pip-audit (clean locally), ruff (`backend-ci.yml`, `backend/ruff.toml`), Dependabot. Not added: Android dependency locking/verification, CodeQL, secret scanning (enable in GitHub settings), SBOM/license inventory. |

## Deployment and operations

| Gate | Owner | Status | Evidence / what remains |
|---|---|---|---|
| Deployment topology decided and matching the code | owner | Closed | Single persistent Docker host chosen (`OWNER_DECISION_GATE.md` section 14); runbook, Caddy TLS compose override and backup script in `docs/DEPLOYMENT_SINGLE_HOST.md`, `docker-compose.prod.yml`, `deploy/`. Host is provisioned and `chatbot.hmwagner.com` (Google Cloud DNS) points at it. |
| Deployed instance is current and reachable | owner | Open | `chatbot.hmwagner.com` resolves (104.239.163.59, confirmed via two independent public resolvers) but does not answer on 80/443/22 as of 2026-09-28 -- connection times out, no RST, consistent with either the instance being stopped or a firewall rule dropping the traffic. Diagnosing this needs `gcloud`, which requires an interactive `gcloud auth login` (reauthentication cannot be satisfied non-interactively) that only the owner can run. The image also has not yet been rebuilt/redeployed with this session's fixes (validator hardening, `/readyz` chunk check, `env_file:`/`TRUSTED_PROXY_IPS` permission fixes). The old Cloud Run URL (`tma-backend-873813047759.us-east4.run.app`) now returns 404 from a stale pre-`/readyz` build and should be decommissioned once the single-host instance is confirmed. |
| `/readyz` fails on an unusable corpus | dev | Closed | 503 when nothing approved, current and machine-linked is retrievable or the corpus query fails; stale corpus and stuck ingestion reported as degraded/stuck (`tests/unit/test_readyz.py`). Alerting on the body is not set up. |
| Ingestion recovers from a killed run and syncs promptly after restart | dev | Closed | Orphaned `running` rows are failed at startup under the ingestion lock; first sync runs about a minute after start when stale; an all-failed run is `failed`; permission and size skips are run errors. An expected-corpus inventory against the real Drive folder is still needed (owner). |
| Timed restore into an isolated environment | owner | Owner | Neon PITR window unconfirmed; no drill recorded. |
| Load test against the real single-host/Neon path | dev | Open | Vector search scans eligible embeddings in process and has not been measured. The page render cache is now byte-bounded and render concurrency limited. |
| Logs/metrics/alerts with correlation IDs and no raw model output | dev | Open | Rejected model responses are no longer logged by default. One structured line per request (method, path template, status, latency, correlation id -- never question text, history, excerpts, bodies or cookies) is now logged for every request, not just unhandled 500s (`app/main.py`'s `correlation_id_middleware`; `logging.basicConfig` at INFO is what makes `docker compose logs app` show anything at all, since nothing configured that before). Metrics and alerts are still not built. |

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
| Invariants enforced by the database | dev | Closed | Migrations 0005 (one current revision), 0006 (one answer per question), 0007 (CHECK constraints: role/status/rating enums), 0008 (conversation idempotency), 0009 (CHECK constraints: chunk_type, documents.doc_type, invitations.role -- file_type and source_system deliberately left open, see the migration's own comment). Applied under an advisory lock. Not yet applied to the production branch: they run when the new build starts. |

## Tested state

Backend suite and Android JVM/instrumented results are recorded in the release
record for a specific commit, not here. Device coverage to date: Galaxy Tab
A9+ only; no phone, screen reader, split-screen or offline matrix.
