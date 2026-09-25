# Owner Decision Gate

Recorded business and platform decisions for a native Android client backed by a
FastAPI + PostgreSQL (Neon) service. Sections 1-9 date from 2026-08-26;
sections 11-13 from later dates; "Open decisions" at the end lists what still
needs an owner. Earlier ledgers referenced here are archived in
`docs/history/`.

**Owner for every decision below: ceyhun@hmwagner.com.** Decided: 2026-08-26.

## 1. Production milestone

**Decision:** The milestone is a production-capable pilot. (The application
originally ran on SQLite; it now runs on PostgreSQL/Neon.)

## 2. Identity provider

**Decision:** Keep the existing email/password + admin-invitation login
system already implemented (`app/auth/`) — no new enterprise SSO/OIDC
integration. Judged disproportionate to a ~20-user internal tool. This is
treated as the plan's own allowed fallback ("If there is no IdP, approve a
temporary mobile token flow"); no SSO migration deadline is set at this
scale.

**Confirmed for Phase 1 (2026-08-26, asked directly):** the plan's Phase 1
deliverable "Implement Authorization Code with PKCE through the system
browser/custom tab... Android Keystore-backed token storage" is dropped.
Today's session-cookie auth stays as-is; no OAuth/PKCE work in Phase 1.

## 3. Cloud, region, and managed services

**Decision:** Google Cloud, on a near-zero-cost tier, with the reliability
tradeoffs that implies (cold starts, no SLA, a database that can pause after
inactivity) explicitly accepted for a ~20-user internal pilot.

Concrete service selections:

- **Compute:** Cloud Run (free-tier request allowance) for the backend
  container.
- **Database:** **Neon** (managed Postgres with pgvector support), not Cloud
  SQL — Cloud SQL has no perpetual free tier, so it doesn't fit the "near
  -zero-cost" decision. This specific vendor pick (Neon over Supabase) was
  an engineering recommendation made while writing this record, not put to
  the owner as its own choice — the reasoning offered was that decision #2
  keeps the existing auth system, so Supabase's bundled auth/storage would
  be unused surface area. Flagged here in case it should be confirmed
  explicitly rather than accepted by default.

  **Correction (2026-08-26):** the Neon project (`ChatBot`,
  `broad-paper-68307488`) was created directly in the Neon console in
  `aws-us-east-2` (Ohio), not `us-east4` as this section originally said.
  This project is a fact on the ground, not something this record should
  contradict — `us-east-2` is also where Neon's beta backend primitives
  (Object Storage, Functions, AI Gateway) are gated, none of which this app
  currently uses, so the region choice carries no present cost. Cloud Run
  should still target `us-east4`/Virginia per the region reasoning below;
  cross-cloud latency between Cloud Run (GCP, Virginia) and Neon (AWS, Ohio)
  is small at this scale and was accepted rather than re-provisioning Neon
  to chase an exact region match.
- **Object storage:** Google Cloud Storage (free-tier allowance).
- **Secret manager:** Google Secret Manager.
- **Telemetry:** Google Cloud Logging/Monitoring.
- **Queue / Redis equivalent:** not selected yet. At this scale and with no
  durable-attempt/outbox implementation built yet (that's Phase 2 work),
  there's no concrete requirement to point a decision at. Revisit once
  Phase 2's actual queue needs are known.
- **Region:** `us-east4` (Ashburn, Virginia) — closest Google Cloud region to
  the technicians' location (Maryland).
- **Backup policy: still open.** Not decided in this pass. Needs a follow-up
  decision on acceptable data-loss window and whether Neon's free-tier
  backup/retention behavior is sufficient or a paid tier is needed for that
  alone.

## 4. Manual source of truth

**Decision:** Google Drive remains the sole source of truth, no change from
the current implementation (`GoogleDriveSource`, see
`docs/history/PRODUCTION_READINESS_LEDGER.md`).

## 5. BYOD vs. company-managed devices

**Decision:** Company-managed only. All devices — the existing Galaxy Tab A9+
fleet and the phones being added — are company-issued, not personal. No BYOD
policy (work-profile, personal-device screenshot/copy/share controls) is
needed; standard company-device management controls apply uniformly.

## 6. Fleet inventory and minSdk

**Decision:** The work phones are recent devices, similar generation to the
Tab A9+ (Android 13+ era), not a mixed fleet reaching back to old OS
versions.

**What this does NOT close out:** an exact model/OS-version inventory
hasn't been done — this decision confirms the *category* (modern fleet), not
a specific minimum API level. `android/app/build.gradle.kts`'s `minSdk = 26`
remains an explicit, now-consciously-chosen interim floor rather than a
forgotten placeholder, pending the actual device list. Tighten it once real
models/versions are known, rather than inventing a specific number now.

## 7. PWA fallback

**Decision:** Not needed. No PWA fallback path will be built or tested.

## 8. Grandfathered manual review (71 documents)

**Decision:** The 71 documents grandfathered to `approved` status are
understood to have already been reviewed individually, outside this system,
before ever being ingested. No further remediation action or reviewer
assignment is needed.

**Factual note for anyone reading this later:** the migration's own audit
trail (`review_note` on the grandfathered `documents`/`document_machines`
rows, see `docs/history/PRODUCTION_READINESS_LEDGER.md`'s "Registration is closed..." entry)
explicitly states that no individual *re-review happened at migration time
within this system*. That is not in conflict with this decision — the claim
here is that the real review happened upstream of that migration (before the
files were ever placed in the Drive folder), not that the migration itself
constituted the review. No documentation of who performed that upstream
review, or when, exists in this repository; if audit evidence is ever
needed, consider recording that separately.

## 9. Phase 1 scope (2026-08-26, decided after the gate above)

Phase 1 as originally written also calls for two more significant pieces
of work beyond the versioned-contract items. Both were put to the owner
directly, given they're the same class of scale-vs-plan judgment call as
decisions #2 and #3:

- **No separate Node.js/TypeScript public API service.** Phase 1 stays on
  the existing FastAPI app — version and harden its routes instead of
  bootstrapping a new service to sit in front of it.
- **No Hilt / repository-layer / generated-API-client refactor of the
  Android app.** The current hand-rolled `ApiClient` singleton and
  ViewModel structure stays as-is.

**What Phase 1 means at this scale, as a result:** the remaining
deliverables from the plan's Phase 1 section — `/config` and `/me`
endpoints, a standardized safe-error envelope (code, display message,
correlation ID, retryability, field errors, HTTP status), cursor
pagination for machines/history/messages/saved answers, UTC ISO-8601
timestamps with offsets, and publishing an OpenAPI contract for the
existing FastAPI routes — done against the current app, not a new one.

**All five items above are implemented (2026-08-26, commits `826811b`,
`6fc65f3`, and the OpenAPI-contract commit that follows this record's own
update)** — see the "Phase 1" bullet in `docs/history/PRODUCTION_READINESS_LEDGER.md`
for the detailed accounting. Phase 1 (narrowed scope) is done.

## 11. Backup and data-retention policy (2026-08-26)

**Decision:** Rely on Neon's built-in backup/point-in-time-recovery on its
free tier (typically a few days of restore window) rather than a separate
backup mechanism. No separate data-retention policy for conversation/
message content — kept indefinitely, since there's no compliance driver
requiring deletion at this scale.

**What this does NOT cover:** an actual account-offboarding data policy
(what happens to a departed technician's conversation history) was not
asked about separately and isn't decided here. Neon's exact free-tier
retention window (days of point-in-time recovery) should be confirmed
against their current published limits before this is treated as a firm
guarantee, not assumed from this record.

## 12. Accessibility listening session — waived (2026-08-26)

**Decision:** The plan's P0A-5 requirement to "perform a human TalkBack
session to verify error live-region announcements and logical focus order"
(UpdatedNextSteps.txt line 266) will **not** be performed. Waived by the
owner, consistent with the standing instruction that this fleet's devices
are not to be made to speak aloud.

**What this means, stated plainly rather than quietly dropped:** P0A-5's
exit gate explicitly says "accessibility-node inspection alone is
insufficient," so this item is closed as **deliberately unverified**, not
as verified. What *is* known: the Compose `semantics`/`liveRegion` markers
are present and correct by code inspection; the real on-device
accessibility node tree was checked with `adb shell uiautomator dump`,
which found and fixed a genuine touch-target defect and confirmed the
citation chip doesn't double-read. What is **not** known: whether a screen
reader actually voices the error live regions, and whether focus order is
logical when driven by an accessibility service. If this app is ever put in
front of a technician who relies on a screen reader, that gap is real and
should be closed first.

**Standing constraint, unchanged by this waiver:** no spoken-feedback
accessibility service may be enabled programmatically (adb or otherwise) on
these devices. The waiver removes the obligation to test; it does not
authorize turning the service on.

## 13. Issues.txt follow-up decisions (2026-09-16)

Answered directly, in response to the independent builder-review backlog
(`docs/history/Issues_2026-08.txt`, reviewed commit `2105488`):

- **Concurrent questions in one conversation: not supported.** A second
  question must not be enterable while one is being answered — the
  technician must wait for it to finish or stop it first. Enforced
  server-side (`conversations.is_processing`,
  `app/api/routes_chat.py`'s `_claim_conversation_processing`/
  `_release_conversation_processing`), covering `ask_question`,
  `retry_answer`, and the pending-clarification resume path in
  `set_conversation_machine` — not just a disabled client button.
- **Technician PWA: removed entirely.** Android is the only technician
  client in production. `index.html`, `app.js`, `service-worker.js`, the
  manifest, and their routes in `app/main.py` are deleted; the admin web UI
  (`admin.html`/`admin.js`, sharing `app.css`) stays.
- **AI provider: Anthropic**, confirming section 3's Neon/GCP direction —
  already wired and configured (`app/providers/anthropic_provider.py`,
  `backend/.env`'s `AI_PROVIDER=anthropic` + `ANTHROPIC_API_KEY`).
- **Answer confidence: self-disclosed in the response text, not gated by a
  strict threshold.** The provider's JSON contract gained an optional
  `confidence` field (`"high"`/`"low"`); a low-confidence answer gets a
  visible caveat prepended to the displayed text. The verbatim-evidence
  check every claim/step/warning must pass is unchanged either way —
  confidence never relaxes what the model is allowed to assert, only
  whether a caveat is shown.
- **Saved answers (and the existing favorite-machine feature): keep and
  extend, not remove.** The backend capability continues development —
  Android UI for it is in scope going forward, superseding any reading of
  "no need to add favorites" as a request to hide the feature.
- **Document/revision approval, conflict resolution, deletion review,
  emergency withdrawal: owned entirely by ceyhun@hmwagner.com.** Not a
  role the app needs to model differently from today's single-admin
  account.
- **Google Drive reconfirmed as the sole data source** (reaffirms section
  4). Whether the real corpus uses nested folders/shortcuts/Google-native
  files (`Issues.txt` P1-20) was not asked separately and remains open.
- **The historical demo-credential exposure in git history (P0-5): a
  non-issue.** Everything runs local-only; no rotation/deletion action was
  requested.

---

## 14. Deployment topology and answer provider (2026-09-25)

**Decision:** production runs on a single persistent Docker host (one
application replica, Neon for the database, Caddy for HTTPS), not Cloud Run;
see `docs/DEPLOYMENT_SINGLE_HOST.md`. Testers and production use the
Anthropic provider (`AI_PROVIDER=anthropic`). Live evaluation against the
production corpus copy: 10 of 11 cases passing with the current answer
validation (one answered from a different excerpt than the ground truth
expects); the ground-truth set is small and is a regression check, not
release validation.

## Open decisions (second audit, 2026-09-24)

Each needs an owner (ceyhun@hmwagner.com unless reassigned) before a broad
pilot; none is settled by the code.

1. **Deployment topology.** Cloud Run (section 3) versus one persistent Docker
   host. The application currently assumes a long-lived single process; see the
   "Deployment topology" gate in `docs/PRODUCTION_READINESS.md`.
2. **Answer provider for testers.** `local_extractive` (verbatim passages) or
   `anthropic` (generated, lexically validated, not entailment-checked).
3. **Anthropic data terms.** Retention setting, region, contractual position,
   and tester consent wording (`docs/TESTER_ONBOARDING.md`).
4. **Neon recovery window and restore drill.** Confirm the current plan's
   point-in-time recovery window and record a timed restore.
5. **Retention, offboarding and deletion.** Section 11 keeps conversations
   indefinitely; decide log retention and what happens to a departed
   technician's history.
6. **Evidence for the 71 grandfathered manuals.** Section 8 accepts them;
   record who reviewed which source and when, outside the mutable database
   rows, and require explicit review for new, revised or high-risk manuals.
7. **Support ownership.** Incident owner, rollback owner, support contact and
   stop-testing criteria.

---

**Required output per the plan** ("version-controlled architecture,
identity, data-retention, device-management, and corpus-approval decision
records with named owners") is this file in full, including
data-retention — see section 11 above.
