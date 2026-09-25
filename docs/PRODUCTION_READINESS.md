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
| Model prose cannot state an unsupported qualitative instruction | dev | Open | Claims and steps must be the cited excerpt's own wording with directions, actions and negations preserved, and no-answer text is fixed server text (`app/providers/base.py`, `tests/unit/test_claim_validation.py`). Live evaluation (Anthropic, production corpus copy): 10/11. This is a lexical check, not entailment. Remaining: an adversarial safety set (prompt injection, inverted procedures, wrong model) run against the live provider. |
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
| Non-demo version, real icon | dev | Closed | `versionName 1.0.0`, `versionCode 2`, adaptive launcher icon. Increment `versionCode` for every distributed build. |
| Release record filled for the exact APK; clean and upgrade install tested on tablet and phone | owner | Owner | `RELEASE_RECORD_TEMPLATE.md`. No phone run recorded. |
| Download link access control, expiry, hash | owner | Owner | Audit the external APK link separately. |
| Dependency, secret, SAST, license and workflow scans | dev | Open | Added: actionlint, bandit, pip-audit (clean locally), Dependabot. Not added: Android dependency locking/verification, CodeQL, secret scanning (enable in GitHub settings), SBOM/license inventory. |

## Deployment and operations

| Gate | Owner | Status | Evidence / what remains |
|---|---|---|---|
| Deployment topology decided and matching the code | owner | Closed | Single persistent Docker host chosen (`OWNER_DECISION_GATE.md` section 14); runbook, Caddy TLS compose override and backup script in `docs/DEPLOYMENT_SINGLE_HOST.md`, `docker-compose.prod.yml`, `deploy/`. Remaining: the host itself, its DNS name, and one deployment following the runbook. |
| Deployed instance is current (readiness endpoint, docs hidden, keep-alive) | owner | Owner | The service at the Cloud Run URL predates these changes until rebuilt and redeployed. |
| `/readyz` fails on an unusable corpus | dev | Closed | 503 when nothing approved, current and machine-linked is retrievable or the corpus query fails; stale corpus and stuck ingestion reported as degraded/stuck (`tests/unit/test_readyz.py`). Alerting on the body is not set up. |
| Ingestion recovers from a killed run and syncs promptly after restart | dev | Closed | Orphaned `running` rows are failed at startup under the ingestion lock; first sync runs about a minute after start when stale; an all-failed run is `failed`; permission and size skips are run errors. An expected-corpus inventory against the real Drive folder is still needed (owner). |
| Timed restore into an isolated environment | owner | Owner | Neon PITR window unconfirmed; no drill recorded. |
| Load test against the real Cloud Run/Neon path | dev | Open | Vector search scans eligible embeddings in process and has not been measured. The page render cache is now byte-bounded and render concurrency limited. |
| Logs/metrics/alerts with correlation IDs and no raw model output | dev | Open | Rejected model responses are no longer logged by default. Structured logging, metrics and alerts are not built. |

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
| Invariants enforced by the database | dev | Closed | Migrations 0005 (one current revision), 0006 (one answer per question), 0007 (CHECK constraints), 0008 (conversation idempotency). Applied under an advisory lock. Not yet applied to the production branch: they run when the new build starts. |

## Tested state

Backend suite and Android JVM/instrumented results are recorded in the release
record for a specific commit, not here. Device coverage to date: Galaxy Tab
A9+ only; no phone, screen reader, split-screen or offline matrix.
