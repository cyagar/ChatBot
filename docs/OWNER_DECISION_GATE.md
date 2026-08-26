# Owner Decision Gate (2026-08-26)

`UpdatedNextSteps.txt` section 5 requires these eight decisions be made and
recorded, with named owners, before Phase 1 (versioned API contract) starts.
Change set 1 (P0A-1 through P0A-6) was complete at the time these were made.

**Owner for every decision below: ceyhun@hmwagner.com.** Decided: 2026-08-26.

## 1. Production milestone

**Decision:** The next milestone is a production-capable pilot, not
continued expansion of the current FastAPI/SQLite demo.

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
`docs/PRODUCTION_READINESS.md`).

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
rows, see `docs/PRODUCTION_READINESS.md`'s "Registration is closed..." entry)
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

---

**Required output per the plan** ("version-controlled architecture,
identity, data-retention, device-management, and corpus-approval decision
records with named owners") is this file in full, including
data-retention — see section 11 above.
