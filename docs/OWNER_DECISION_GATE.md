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

**Concrete implication for Phase 1:** the plan's Phase 1 deliverable "Implement
Authorization Code with PKCE through the system browser/custom tab... Android
Keystore-backed token storage" is **waived by this decision**, not merely
deferred silently — Phase 1's actual scope, if pursued, excludes the
OIDC/PKCE rework and keeps today's session-cookie auth. Revisit this decision
explicitly if user count or an actual IdP later becomes available.

## 3. Cloud, region, and managed services

**Decision:** Google Cloud, on a near-zero-cost tier, with the reliability
tradeoffs that implies (cold starts, no SLA, a database that can pause after
inactivity) explicitly accepted for a ~20-user internal pilot.

Concrete service selections:

- **Compute:** Cloud Run (free-tier request allowance) for the backend
  container.
- **Database:** **Neon** (managed Postgres with pgvector support), not Cloud
  SQL — Cloud SQL has no perpetual free tier, so it doesn't fit the "near
  -zero-cost" decision. Neon was chosen over Supabase specifically because
  decision #2 keeps the existing auth system and #3 already covers object
  storage separately — Supabase's bundled auth/storage would be unused
  surface area.
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

---

**Required output per the plan** ("version-controlled architecture,
identity, data-retention, device-management, and corpus-approval decision
records with named owners") is this file. Data-retention was not one of the
plan's eight numbered items and was not decided in this pass — if it needs
its own record, that's a follow-up, not covered here.
