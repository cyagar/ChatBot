# Technician Manual Assistant — Android (Day-1 Demo)

This is the first vertical slice of the native Android rewrite described in
`../Technician_Manual_Assistant_Galaxy_Tab_A9_Android_Rewrite_Plan_2026-08-24.txt`.
Scope for this build: **sign in → pick an approved machine → ask one question →
get a grounded answer → open a citation's evidence and page image**, running
live against the existing FastAPI backend in `../backend`. Nothing here is
production-ready; it exists to give technicians something real to react to.

**Device scope (updated 2026-08-25):** the plan document above was written
for the Galaxy Tab A9+ tablet fleet specifically — that's still the primary
device this build has been physically tested on. The requirement has since
widened: technicians will also use this app on their Android phones, so it
needs to work correctly and look good on any Android device, not just that
one tablet. The layout code was already written against Material 3
window-size classes rather than any device-model check, so this was mostly
already true; see "Material 3 Adaptive list-detail layout" under "Scope
decisions" below for what that means concretely and what's actually been
verified versus reasoned-through-but-unverified on real phone hardware.

## What this is NOT

This is a one-day slice, not Phase 1 of the plan. Deliberately skipped for now
(see "Scope decisions" below): Hilt, Room, DataStore, OpenAPI codegen, the new
Node.js mobile API, Postgres, the transactional outbox/durable-attempt/SSE
pipeline, and OIDC. The app talks directly to the current FastAPI backend's
existing JSON endpoints. (Material 3 Adaptive's list-detail layout *was*
added on 2026-08-24 — see "Scope decisions" below.)

## Running it

1. Start the backend from `../backend`:
   ```
   python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
   ```
2. Confirm `APP_ENV=development` in `backend/.env` — the session cookie is
   marked `Secure` otherwise and silently stops working over plain HTTP.
3. The tablet and this machine must be on the same Wi-Fi. Update
   `BASE_URL` in `app/build.gradle.kts` (debug build type) if this machine's
   LAN IP isn't `192.168.1.71` anymore, then rebuild.
4. Windows Firewall must allow inbound TCP on port 8000, or a tablet on the
   LAN can't reach the backend at all.
5. Open this `android/` folder in Android Studio, or run:
   ```
   .\gradlew.bat assembleDebug
   ```
   The APK lands at `app/build/outputs/apk/debug/app-debug.apk`. Install it
   on a physical Galaxy Tab A9+ (USB debugging enabled) via Android Studio's
   Run button, or `adb install -r app-debug.apk`.

### Demo login

A local-only technician account was seeded directly into `data/db/app.db`
for this demo (not through the invitation flow — this is dev-only seeding,
not something to do against a real deployment):

- Email: `tech.demo@hmwagner.com`
- Password: `DemoPass123!`

The existing administrator account also works and reaches the same screens
(nothing in this slice enforces the technician/administrator split yet).

## Scope decisions for the one-day demo

- **No Hilt.** A hand-rolled `ApiClient` singleton object does the wiring.
  Cheap to replace once there's more than one screen's worth of dependencies.
- **No Room/DataStore.** No offline cache, no persisted drafts. All state is
  in-memory ViewModels; force-quitting the app loses an in-progress draft.
- **No OpenAPI codegen.** Retrofit interface + hand-written `kotlinx.serialization`
  models in `network/ApiModels.kt`, kept in sync by hand with
  `backend/app/api/routes_*.py`.
- **Material 3 Adaptive list-detail layout (added 2026-08-24, phone-scoped
  2026-08-25).** Uses the already-present `material3-window-size-class`
  dependency (not the newer `material3-adaptive` component suite, which is
  built around Navigation3 — a bigger retrofit than this app's plain
  `NavHost` justified) to gate on `WindowWidthSizeClass.Expanded`: landscape
  on the Tab A9+ (~1280dp) gets a fixed 360dp machine-list pane beside the
  chat pane (`AppNav.kt`'s `TwoPaneHome`); anything narrower — the Tab A9+ in
  portrait (~800dp, Medium), and any phone in either orientation, which
  essentially never reaches Expanded — keeps the original single-pane
  Machines→Chat flow (`SinglePaneHome`). This means the two-pane layout was
  always phone-safe by construction (it's gated on measured window width,
  never on device model), which is why picking up the wider "must work on
  phones too" requirement on 2026-08-25 didn't need a layout redesign — see
  the device-scope note at the top of this file. What phone support has
  actually had: reasoning through the width-driven gate, plus one spot-check
  on the Tab A9+ with `adb shell wm size`/`wm density` overridden to a
  phone-like 1080×2400 @420dpi (single-column layout, no clipping, composer
  and send button fully reachable). What it has **not** had: a real phone.
  Selection state is hoisted into a small `HomeSelectionViewModel` shared by
  both branches so it survives switching between them. Verified live on the
  Tab A9+: two-pane render, machine search/select in the left pane,
  switching between two different machines correctly swaps the right pane's
  conversation (not a stale cached `ChatViewModel`), a full send→answer
  round trip with citations in the right-hand pane with composer/send button
  not clipped, and — as of 2026-08-25 — rotating landscape (two-pane, a
  conversation selected) back to portrait correctly lands on that same
  conversation instead of dropping to the machine list (see "Handled during
  review" below for what that bug actually was; it wasn't what the first two
  fix attempts assumed).
- **Cookie-based session, not the plan's OIDC/PKCE design** (plan section 8).
  `PersistentCookieJar` still stores the backend's existing `tma_session`
  httpOnly cookie, not a refresh token from a real identity provider — that
  still needs the IdP decision (plan section 18). As of 2026-08-24 the cookie
  value itself is encrypted at rest with an AES-256-GCM key held in the
  Android Keystore (`PersistentCookieJar`'s `getOrCreateKey()`) before being
  written to `SharedPreferences`, instead of sitting there in plain text
  readable via a rooted device or `adb backup`. **Don't overstate what this
  is**: there's still no expiration/rotation policy, no server-side
  per-device revocation, and no refresh token — it closes the
  "readable-at-rest" gap for the one thing we store, nothing more. A
  pre-existing plaintext install is discarded (not migrated) on first launch
  after this change — a forced one-time re-login, not a bug.
- **Idempotency keys are implemented (2026-08-24); durable attempts/SSE are
  not** (plan section 9). Every `send()`/`retryPendingSend()` call carries an
  `Idempotency-Key` header (`ChatViewModel`'s `LocalEcho.id`, generated once
  per composed question and held steady across retries — never regenerated).
  The backend (`routes_chat.py::ask_question`, migration
  `0010_idempotency.sql`) stores it with a `UNIQUE(conversation_id,
  idempotency_key)` constraint: a duplicate key with a completed reply
  replays that reply instead of creating a second user turn; a duplicate key
  with no reply yet (the original attempt is still generating, or the server
  died mid-attempt) returns `409` rather than starting a second provider
  call. Chat is still a single synchronous `POST .../messages` call — there's
  no durable job queue, so a `409` genuinely can't be resumed, only detected
  and safely retried later with the same key. That queue/outbox (plan
  sec 5.1/9) is Phase 3, needs Postgres, and is still not built; see
  `docs/ARCHITECTURE.md`'s deviation table for the exact boundary between
  what's covered now and what still needs it.
- **minSdk 26 is a placeholder**, not a real decision — the plan requires a
  fleet inventory before setting this for real (plan section 6).
- **Debug-only cleartext HTTP exception** for the dev LAN IP, scoped to the
  debug build type only (`src/debug/res/xml/network_security_config_debug.xml`).
  The release build type has no such exception and keeps
  `usesCleartextTraffic="false"`.
- **No physical-device or emulator run happened in this environment** — no
  emulator image or connected device was available where this was built. The
  debug build compiles and packages cleanly (`assembleDebug` succeeds), but
  the actual on-device behavior (touch targets, keyboard handling, real
  network conditions, the Coil-authenticated page-image loading path) has
  **not** been verified live. Treat first real device install as the actual
  first test, not a formality.

## Known correctness gaps worth fixing before showing this beyond an internal demo

- ~~No pull-to-refresh anywhere in the app~~ **Added and verified live on the
  Tab A9+ (2026-08-25).** See "Handled during review" below. The ambiguous
  "lost connection while sending" state described in `ChatViewModel.send()`
  still has its own dedicated one-tap "Retry" affordance instead (safely
  reuses the same idempotency key) rather than being folded into the new
  pull-to-refresh -- that state is about a specific in-flight send, not "the
  list is stale," so a separate, scoped affordance stays the right fit.
- ~~A code-level accessibility pass happened (2026-08-24); a device-verified
  one hasn't~~ **Partially closed out on the Tab A9+ (2026-08-25)** — see
  "Handled during review" below. Verified via the real accessibility node
  tree (`adb shell uiautomator dump`): the citation chip does not
  double-read, and the Helpful/Incorrect/Save touch-target gap was a real,
  measured defect, now fixed. **Still genuinely open:** whether the
  error-text live regions actually get announced by a screen reader —
  `uiautomator dump` doesn't surface Compose's `liveRegion` semantics
  property at all, so this needs a human accessibility-service listening
  session to confirm, not just a node-tree inspection. This device's own
  spoken-feedback accessibility service must never be enabled
  programmatically (over adb or otherwise) to check this — that makes the
  physical device start speaking out loud unattended, which is disruptive
  and not something to trigger remotely (found the hard way, 2026-08-25).
- ~~`LoginViewModel`, `MachinesViewModel`, and `ChatViewModel` are covered by
  unit tests now; `AppNav`'s session-expiry redirect and the screens
  themselves (Compose UI) are not~~ **`AppNav`'s session-expiry redirect had
  instrumented coverage, verified live on the Tab A9+ (2026-08-25).** See
  "Instrumented (androidTest) coverage" below. Still genuinely open: the rest
  of the Compose screens (Login/Machines/History/Chat) have no UI-level
  assertions of their own, only the ViewModel unit tests underneath them —
  this closed the one item that was actually untestable at the JVM-unit-test
  layer (a real OkHttp interceptor pipeline needs a real Android Keystore),
  not the full "every screen has Compose UI tests" gap. **Caveat added during
  P0A-1 (2026-08-26):** the two tests behind that claim were since modified
  (to account for the new launch-time `/me` call, see the P0A-1 bullet below)
  and two more were added alongside them — none of the four have been run
  on-device since. The "verified live" claim above covers only the
  now-superseded pre-P0A-1 version of this test file, not its current form.
- ~~The Keystore-backed cookie encryption has not been verified on a physical
  device~~ **Verified live on the Tab A9+ (2026-08-24).** Installed this
  build directly over an existing pre-Keystore install that had an active
  plaintext session (`adb install -r`, no uninstall) — launched straight to
  the login screen as designed, no crash, plaintext session correctly not
  trusted. Logged in fresh, then `am force-stop` + relaunch went straight to
  the machines list (not login) — the encrypted cookie round-tripped through
  a real cold start via the actual Keystore, not just
  `CookieSerializationTest`'s pure-logic coverage. `logcat -d *:E` clean
  through the whole sequence.
- ~~Two-pane selection didn't survive rotation~~ **Fixed and verified live on
  the Tab A9+ (2026-08-25).** See "Handled during review" below for the full
  story — two separate bugs were involved, and the first fix attempt was
  aimed at the wrong one.
- ~~No conversation-history/resume screen.~~ **Added and verified live on the
  Tab A9+ (2026-08-25).** See "Handled during review" below. Single-pane:
  Machines -> History -> pick a past conversation -> Chat loads the right
  conversation with full history, feedback/saved-state correctly rehydrated
  (this also closes out the "cold restart into an already-marked
  conversation" rehydration gap noted in the feedback/saved-state entry
  below), and back from Chat lands on the machine list, not History. Two-pane:
  found and fixed a real bug where switching from one conversation straight
  to another in the fixed detail pane silently kept showing the *previous*
  conversation's `ChatViewModel` -- see the "two-pane History->Chat
  ViewModel-caching bug" entry below.
- ~~`submitFeedback`/`saveAnswer` fail silently on a network error~~ **Fixed
  (2026-08-25).** See "Handled during review" below. The transient
  `IOException: unexpected end of stream` errors that prompted this fix are
  now surfaced to the technician instead of hidden either way, so this fix
  stands regardless -- but their root cause turned out to be **the local dev
  server, not the app**: confirmed 2026-08-25 by restarting `uvicorn` with
  `--timeout-keep-alive 75` (default is 5s) and repeating the same tap
  sequence with 10-25s gaps that had reliably failed before -- zero failures
  across four attempts afterward, versus the near-certain failure rate
  before. Uvicorn was closing idle keep-alive connections at 5s; OkHttp
  didn't know and reused the dead socket, producing exactly this error on
  the first request after any gap longer than that. Not an OkHttp
  connection-pool bug, and not something a technician would see behind a
  real server/proxy with a normal 60-75s keep-alive -- just start the dev
  server with a longer `--timeout-keep-alive` during manual testing.
  Separately, also found and fixed a real client gap while investigating
  this: `ApiClient`'s `OkHttpClient` had `retryOnConnectionFailure(false)`,
  justified by a comment about guarding against a double-sent, non-idempotent
  `ask_question` POST -- but per-turn `Idempotency-Key` support (see
  `ChatViewModel.send` / `routes_chat.py`'s dedup on `conversation_id`+key)
  landed in an earlier slice this session, making that specific guard stale.
  The first fix attempt just flipped the flag to the OkHttp default
  (enabled) -- caught in review before committing further: that's too broad.
  `saveAnswer` is safe to retry (its `INSERT OR IGNORE` + the unique index
  from migration 0011 makes a retried POST a no-op), and `ask_question` is
  now safe too (the idempotency key), but `submitFeedback` writes a plain
  `INSERT INTO feedback` -- the table is deliberately append-only (multiple
  ratings over time are legitimate), so nothing stops a connection-failure
  retry that lands *after* the server already committed the row from writing
  a second, identical one. That's exactly the duplicate-rows bug class this
  session's first commit already fixed once for `saved_answers`; blanket
  retry would have reopened the same risk for `feedback`.

  Fixed properly instead with a custom interceptor (`getRetryInterceptor` in
  `ApiClient.kt`) that retries a request once on `IOException` only when it's
  a `GET` -- always safe to retry -- leaving every `POST` (including
  `submitFeedback`) untouched, exactly as conservative as before.
  `retryOnConnectionFailure(false)` stays explicit on the `OkHttpClient`
  itself so its own blanket retry (which can't distinguish GET from POST)
  never fires. Confirmed live 2026-08-25, both directions: reverted the dev
  server to its default 5s keep-alive (the exact condition that reliably
  failed before) and repeated the tap sequence -- GET requests (history list,
  messages) now retry transparently with no error banner; then sent a fresh
  question, tapped "Helpful" on it, and confirmed via the backend log and a
  direct `sqlite3` query that exactly one `feedback` row was written -- no
  silent duplicate from the retry path.

## Handled during review (worth knowing about)

- **Evidence sheet no longer duplicates the page image with parsed text
  underneath it (2026-08-25).** `EvidenceSheet` in `ChatScreen.kt` used to
  always show a `Card` with `evidence.content` (the extracted/OCR'd text of
  the cited excerpt) followed by the real manual page image when one was
  available (`has_page_image`) -- redundant, and occasionally a worse read
  than the actual page (imperfect OCR). Now the text card only renders when
  there's no page image to show instead; when there is one, the image alone
  is the evidence. Verified live on the Tab A9+: tapping a citation with a
  page image now shows just the title/page metadata and the real page,
  nothing else.
- **Pull-to-refresh on Machines, History, and Chat (2026-08-25).** Added
  `PullToRefreshBox` (Material3, stable in this project's compose-bom) around
  the scrollable content of all three screens. `HistoryViewModel.refresh()`
  and `ChatViewModel.refresh()` already existed (used for the initial load);
  wired the same functions to the pull gesture. `MachinesViewModel` needed a
  new `refresh()`, since neither of its existing load paths (`loadRecent()`,
  private `search()`) was reusable as-is: `refresh()` re-runs whichever list
  is currently on screen -- recents when the search box is blank, the active
  search otherwise -- rather than always reloading recents, so pulling to
  refresh mid-search doesn't silently discard the search. Uses its own
  `refreshing` state field, separate from `loading` (which stays scoped to
  the search-as-you-type spinner), so the two don't interfere. Three new
  `MachinesViewModelTest` cases (refresh with blank query, refresh with an
  active query, a failed refresh surfaces an error).

  Each screen's centered "nothing to show yet" spinner is now gated on
  `loading && list.isEmpty()` rather than `loading` alone -- otherwise every
  refresh (pull or programmatic) would blank the whole list back to a bare
  spinner instead of showing `PullToRefreshBox`'s own top indicator over the
  still-visible stale content, which is the standard pattern and what a
  technician would expect. This incidentally fixes a latent flicker in Chat:
  `loadMessages()` is also called from two in-conversation paths (the
  send-uncertainty recovery path, and after picking a clarifying machine),
  and previously either would have blanked the entire message list for the
  duration of that reload; now it doesn't, since `state.messages` is
  non-empty by then.

  Verified live on the Tab A9+ (2026-08-25): a swipe-down gesture on each of
  the three screens fired the expected fresh request (confirmed via the
  backend log) and re-rendered correctly, including a pull-to-refresh on
  Machines while a search query was active, which correctly re-ran the
  search (`GET /api/machines?q=...`) rather than switching back to recents.
- **Conversation-history/resume screen (2026-08-25).** The backend's
  `GET /conversations` (P1-3) had no Android UI at all -- tapping a machine
  always started a new conversation, with no way back to a past one for the
  same machine short of re-asking the same question. Added `HistoryScreen`/
  `HistoryViewModel` (`ui/history/`), reusing the endpoint as-is (most
  recently updated first, all machines, not filtered per-machine -- a
  single cross-machine list, closer to how a chat app's history usually
  reads than a per-machine sub-list). Entry point is a new "History" icon
  in `MachinesScreen`'s top bar (`onHistoryClick`, optional/nullable so
  existing callers are unaffected); wired into `AppNav.kt` as a real nav
  route in `SinglePaneHome` and as a plain hoisted `showHistory` toggle in
  `TwoPaneHome`'s fixed left pane, matching how `selectedId` is already
  hoisted there rather than routed. In `SinglePaneHome`, Chat's back button
  landing on the machine list rather than back through History is actually
  driven by the `key(selection.selectedId ?: -1)` remount in `HomeContent`
  (a fresh `NavController` per selection, starting straight on Chat) --
  the `popUpTo(Routes.MACHINES)` on the History->Chat nav call is close to
  a no-op given that remount, kept only in case this controller becomes
  longer-lived; see the comment at that call site for the full reasoning.
  `HistoryScreen` also has a `LaunchedEffect(Unit) { vm.refresh() }` so the
  list reloads on every composition entry, not just genuine `ViewModel`
  construction -- caught in review before this landed: `TwoPaneHome` flips
  `showHistory` between two composables in the same fixed pane with nothing
  keying that subtree, so `viewModel()` could resolve back to a retained,
  stale-data instance on toggle. `HistoryViewModel` has no `init{}` refresh
  of its own (removed after the live-testing below) -- the Composable's
  `LaunchedEffect(Unit)` is the single source of the first load; both firing
  was two redundant requests on every mount. Four `HistoryViewModelTest`
  cases (load, empty-is-not-an-error, load failure, refresh replaces rather
  than appends) against the real Retrofit/OkHttp stack via `MockWebServer`,
  same pattern as `MachinesViewModelTest`, driving `refresh()` explicitly
  since the ViewModel no longer self-starts. **Verified live on the Tab
  A9+ (2026-08-25)** in both single-pane and two-pane layouts, including a
  real backend restart mid-session (see the "unexpected end of stream" /
  `--timeout-keep-alive` note in the known-issues list above) and a genuine
  two-pane bug this testing found -- see the next entry.
- **Two-pane History->Chat `ViewModel`-caching bug, found and fixed via live
  testing (2026-08-25).** The original comment on `key(selectedId) {
  ChatScreen(...) }` in `TwoPaneHome` (`AppNav.kt`) claimed picking a
  different machine "tears down and rebuilds ChatScreen's `viewModel()` call
  site." That's wrong for the plain `viewModel(factory = ...)` overload used
  there: without an explicit `key` argument, it looks up the `ChatViewModel`
  in the `LocalViewModelStoreOwner`'s store (the Activity, here -- `TwoPaneHome`
  has no `NavBackStackEntry` to scope it per-route the way `SinglePaneHome`'s
  `NavHost` does) keyed only by class name, not by anything Compose's
  `key(...)` recomposition-scoping touches. Result: selecting a *second*
  conversation directly from History while a first was already showing in
  the two-pane detail pane silently returned the same cached `ChatViewModel`
  instance from the first selection -- its `init` never re-ran, so the pane
  kept showing the first conversation's (possibly errored) state with no new
  network call at all. Only a null-to-first-selection transition worked,
  because that's the one case where the store was genuinely empty. Found by
  instrumenting `TwoPaneHome`/`HistoryScreen` with temporary `Log.d` calls
  during live testing: recomposition demonstrably happened with the new
  `conversationId`, but zero new `okhttp` log lines followed it. Fixed in
  `ChatScreen.kt` by passing an explicit
  `viewModel(key = "chat-$conversationId", factory = ...)` -- confirmed live
  afterward, switching 94 -> 92 -> 94 in the two-pane pane, each switch
  firing its own fresh `GET .../messages` call and rendering that
  conversation's own content. This same call site is shared by the
  Machines-driven two-pane selection path, so the fix (and the bug, before
  it) applies there too, not just to History -- the original "verified live"
  claim on that comment most likely was never actually exercised
  conversation-to-conversation, only null-to-first-selection.
- **Silent feedback/save failures (2026-08-25).** `submitFeedback` and
  `saveAnswer` in `ChatViewModel.kt` used to swallow both a non-2xx response
  and a thrown exception entirely — a technician could tap Helpful or Save,
  the request could fail, and nothing on screen would say so; the button
  just stayed tappable with no explanation. Both now set the same
  `state.error` the rest of the screen already uses for load/send failures
  (bottom banner, `LiveRegionMode.Polite` so a screen reader announces it,
  never a blocking dialog — plan 10.4's "feedback must never interrupt the repair
  task" still holds, since the banner doesn't block anything). A success
  clears any stale error left over from a prior failed attempt. Four tests
  added to `ChatViewModelTest.kt` covering: a non-2xx feedback failure sets
  a feedback-specific error, a network failure on save sets a save-specific
  error and leaves `savedMessageIds` untouched, and a subsequent successful
  save clears the stale error. (One test needed `Connection: close` on the
  prior response to force a fresh connection for the disconnect test —
  otherwise OkHttp transparently retries a disconnect on a *reused* pooled
  connection regardless of `retryOnConnectionFailure(false)`, which would
  have silently masked the exact failure the test means to trigger.)
- **Duplicate feedback/saved-answers rows from re-tapping Helpful/Save
  (2026-08-25, found via live tablet testing).** `ChatViewModel`'s
  `feedbackGiven`/`savedMessageIds` were purely in-memory — a fresh
  `ChatViewModel` (rotation switching the two-pane/single-pane branch, an
  app restart, or just leaving and re-entering a conversation) always
  started both empty, so the buttons reset to unmarked even for a message
  the technician had already rated/saved. Re-tapping then silently inserted
  a second row server-side. Reproduced and confirmed directly against the
  live pilot DB (`feedback` id 4, `saved_answers` id 2 — both duplicates of
  an already-existing row for the same message/user), then removed those
  two test rows by hand. Not a regression from this session's adaptive-layout
  work — an app restart reproduces it identically with no rotation involved.
  The fix has two parts, decided independently per table:
  - **`feedback` stays exactly as it was.** `test_auth_and_chat.py`'s
    `test_concurrent_feedback_submission_does_not_crash_or_corrupt` already
    documents, deliberately, that multiple feedback rows per message are
    allowed by design (a technician reconsidering "helpful" to "incorrect"
    is a real case) — that precedent settled the question rather than being
    overridden. `MessageOut` now carries `feedback_rating`, the *most
    recent* rating for the requesting user (`ORDER BY created_at DESC, id
    DESC LIMIT 1` in `_hydrate_message`), so a client can show "already
    marked" without needing the table itself to be deduplicated.
  - **`saved_answers` gets a real `UNIQUE(user_id, message_id)` constraint**
    (migration `0011_saved_answers_unique.sql`, applied to the live pilot
    DB) — a duplicate save carries no "reconsideration" meaning the way a
    changed rating does, so `save_answer` now does `INSERT OR IGNORE`
    against that index, making a repeat save a no-op instead of a second
    row. `MessageOut.is_saved` reports the boolean.
  - `ChatViewModel.loadMessages()` now rebuilds `feedbackGiven`/
    `savedMessageIds` from these two fields on every reload instead of
    always starting empty.
  - Backend: three new tests in `test_auth_and_chat.py`
    (`test_get_messages_reports_the_current_users_feedback_and_saved_state`,
    `test_get_messages_reports_the_most_recent_feedback_rating`,
    `test_save_answer_twice_is_idempotent`), all passing alongside the full
    274-test suite.
  - Android: verified live that `feedback_rating`/`is_saved` round-trip
    correctly (raw JSON confirmed via a temporary `HttpLoggingInterceptor`
    bump to `BODY`, reverted before this build) and that a fresh
    `ChatViewModel` (via the single-pane/two-pane branch switch) renders
    "Marked helpful"/"Saved" without any tap. At the time, there was no way
    to reopen a specific past conversation from the UI, so the "cold app
    restart back into an already-marked conversation" path -- the one that
    most needs this fix -- couldn't be exercised live, only verified by
    code inspection and the backend tests. The History screen added below
    is what makes that path reachable; it hasn't been used to close out
    this specific verification gap yet (see its own entry for why).
- **Accessibility node-tree verification, not audio (2026-08-25).** There's
  no way to make this session literally listen to a screen reader's speech
  output through adb/screenshots. What's used instead is
  `adb shell uiautomator dump`, which prints the actual
  `AccessibilityNodeInfo` tree the platform hands to any screen reader —
  strong, direct evidence for anything that's a property of that tree
  (node merging, `contentDescription` values, touch-target bounds), but it
  does **not** surface Compose's `liveRegion` semantics property, so it
  can't confirm or deny whether an announcement actually gets spoken. Two
  of the three open questions from the 2026-08-24 accessibility pass were
  resolved this way; the third genuinely needs a real accessibility-service
  listening session:
  - **Citation chip does not double-read.** Dumped the tree with a real
    citation chip on screen (`AssistChip` in `ChatScreen.kt`'s
    `MessageBubble`): the chip's own `clickable`/`focusable` node has empty
    text and `content-desc`, while its descendants (the label `Text` and
    the chip's own Button-role node) are both `focusable="false"` —
    confirming Compose's semantics merging collapses them into one
    accessibility focus stop, and the label's overridden `contentDescription`
    (e.g. "Citation 1, page 1, AJ/AJX SERIES") *replaces* rather than
    supplements the raw "[1] p.1" glyph text for accessibility purposes.
    One announcement, not two.
  - **Helpful/Incorrect/Save's touch-target gap was a real, measured
    defect — now fixed.** The dumped bounds showed each button already met
    the 48dp minimum touch-target *height* on its own, but the gap between
    adjacent clickable bounds was exactly 4dp (matching the `spacedBy(4.dp)`
    in `ChatScreen.kt`) — half of the ~8dp Material Design recommends
    between adjacent targets, and reproduced identically across two
    separate messages/screens. Bumped to `spacedBy(8.dp)`; re-dumped and
    confirmed the gap is now exactly 8dp. This is exactly the kind of
    mis-tap plan section 13.5's gloves concern is about, made worse by
    Helpful/Incorrect being opposite-meaning actions next to each other.
  - **Error-text live regions: still unverified.** `liveRegion =
    LiveRegionMode.Polite` is set in code (`ChatScreen.kt`, `LoginScreen.kt`,
    `MachinesScreen.kt`) and the Compose→platform mapping for this property
    is a direct, well-established one, so there's reasonable confidence
    it's wired correctly — but "reasonable confidence from reading the
    code" is exactly the standard this whole pass was trying to raise past.
    Confirming it actually gets announced needs a real accessibility-service
    listening session, which a future physical-device session should do.
- **Two-pane rotation: a crash, then a real state bug, both found only by
  running on the tablet (2026-08-25).** Illustrates why "builds and unit
  tests pass" was never treated as equivalent to "works" for this feature —
  neither issue below was visible from `assembleDebug`/`testDebugUnitTest`.
  - **Crash on every launch.** `HomeSelectionViewModel` (`AppNav.kt`) was
    declared `private class`. Android's default `ViewModelProvider` factory
    instantiates ViewModel classes via reflection, which needs at least
    package visibility — `private` produced `IllegalAccessException:
    ...HomeSelectionViewModel is not accessible from
    ...JvmViewModelProviders` on every cold start, caught live via `adb
    logcat -s AndroidRuntime:E` after the app started immediately crashing
    with "keeps stopping." Fixed by dropping the `private` modifier.
  - **The actual rotation bug wasn't a state-persistence problem.** The
    first fix attempt (hoisting the selection into `rememberSaveable`, then
    into a `ViewModel`) assumed the selected-conversation state itself was
    getting lost across the Activity recreate a rotation triggers. Temporary
    logging (`Log.d` on `HomeContent`'s recomposition) proved that wrong:
    the selection survived correctly the whole time, with either approach.
    The real bug was in `SinglePaneHome`: its `rememberNavController()` gets
    reused — along with whatever back stack it had the *last* time
    `SinglePaneHome` was on screen — across the plain `if (isExpanded) {
    TwoPaneHome } else { SinglePaneHome }` branch in `HomeContent`, because
    Compose preserves `remember`-level identity across a branch leaving and
    re-entering the same composition, not just across true Activity
    recreates. So `startDestination` was silently ignored on every re-entry
    after the first: `NavHost` only consults it when the controller has no
    existing destination, and this one already had one (`Routes.MACHINES`,
    from the very first cold-launch composition of `SinglePaneHome`, before
    a machine was ever picked). Fixed by wrapping `SinglePaneHome`'s call
    site in `key(selection.selectedId ?: -1)`, which forces a genuinely
    fresh `NavController` — and correct `startDestination` evaluation —
    whenever the selection differs from what it was the last time that
    branch was shown, while leaving in-branch nav state alone (e.g. a
    mid-draft composer) when the selection hasn't actually changed.
  - Verified live end-to-end afterward: select a machine in two-pane
    landscape, rotate to portrait, land directly on that same conversation's
    Chat screen (not the machine list) with the back arrow correctly shown
    and correctly clearing the selection back to the machine list.
- **401 recovery**: an OkHttp interceptor (`ApiClient.kt`) clears the session
  and routes back to the login screen on any 401 other than a failed login
  attempt. Without this, a session expiring mid-demo (or sitting idle over a
  break) would leave every screen showing a raw "code 401" with no way back
  in short of force-reinstalling.
- **P0A-1: account boundary, logout, and launch-time session validation.**
  Three related gaps, all fixed together since they share one mechanism:
  - There was no visible way to sign out. `ui/common/LogoutAction.kt` adds
    one `IconButton` used in every signed-in screen's `TopAppBar`
    (Machines/History/Chat) that calls the new `ApiClient.logout()` — a
    best-effort server-side `POST /api/auth/logout` followed by an
    *unconditional* local session clear, so a technician can always sign out
    of this device even if the server is unreachable.
  - `AppNav`'s 401 redirect (`sessionExpired`) cleared the NavHost back stack
    but never touched `HomeSelectionViewModel` — the selected
    conversation id/label, which is Activity-scoped, not tied to the `HOME`
    back-stack entry the way the Machines/History/Chat ViewModels are. A
    later login could silently reopen the *previous account's* conversation.
    Fixed by clearing `selection.selectedId`/`selectedLabel` in the same
    `LaunchedEffect(sessionExpired)` block that does the redirect.
    `logout()` reuses this exact same flag/handling deliberately — a
    deliberate sign-out and a 401 need identical "drop every account-scoped
    screen" treatment, not two implementations that can drift apart.
  - A stored session cookie used to be treated as proof of a still-valid
    signed-in user (`ApiClient.hasSession()` only checks that something is
    saved). `AppNav` now gates its very first frame on a real `GET
    /api/auth/me` call (bounded to 5s, well under the client's real 15s
    connect / 90s read timeouts, so a dead-zone launch fails open instead of
    leaving a "Checking your session…" spinner on screen for up to 90s)
    before deciding whether to start on Home or Login, so a revoked/expired
    cookie from a previous run lands on Login immediately instead of
    flashing Home and then bouncing back. **Owner-confirmed policy
    (2026-08-26):** a network exception or timeout during that check (as
    opposed to an explicit 401) fails open onto the cached session rather
    than forcing Login while merely offline — connectivity is required for
    every real action regardless (plan §2.3 — "Internet is required for AI
    answers"), so this only affects whether Home renders while briefly
    offline, not what's trusted for anything that matters.
  - Covered by `AppNavSessionExpiryTest` (see "Instrumented (androidTest)
    coverage" below): a session expiry (simulated via a direct `logout()`
    call, which shares AppNav's handling with a real 401) after actually
    opening a conversation through UI clicks, followed by a real re-login
    through `LoginScreen`, lands back on the machine list rather than
    straight into the old conversation; and a logout with the server shut
    down still clears the local session. **Run for real on the Tab A9+
    (2026-08-26)** — and it found a genuine, on-device-only bug: the
    session-expiry effect used to clear `selection.selectedId`/
    `selectedLabel` directly, which changes the `key(selection.selectedId ?:
    -1)` wrapping `SinglePaneHome` (see `HomeContent`) while `Home` might
    still be transiently composed, recomposing a brand-new
    `MachinesScreen`/`MachinesViewModel` (and firing its own
    `recentMachines()` call) in the gap before `navController.navigate
    (Routes.LOGIN)`'s own backstack change actually removed `Home` from the
    tree. That stray, then-cancelled request desynced this test's
    MockWebServer response queue, making the subsequent re-login
    deserialize the wrong enqueued body and silently fail to navigate —
    invisible to any JVM test, since none of them drive a real
    `NavController`/Compose recomposition scheduler. Reordering
    (`navigate()` before the clear) did **not** fix it, confirmed by
    rerunning the same test: Compose batches snapshot-state writes made
    without an intervening suspension into the same recomposition pass
    regardless of source order. The real fix moves the clearing into
    `HomeContent`'s own `DisposableEffect(Unit) { onDispose { ... } }`, so
    it only happens as an actual consequence of `Home` leaving composition,
    not a reactive side effect racing against it. Re-verified: all 5
    `AppNavSessionExpiryTest` cases now pass on-device.
  - Deliberately NOT changed: the admin account still works in this app.
    Section 5/P0A-1 of the plan requires blocking admin accounts "unless the
    owner explicitly approves admins using it" — the owner did, so this is a
    recorded decision, not an oversight.
- **P0A-2: an unresolved 409 no longer silently drops the "still processing"
  status.** `ChatViewModel.loadMessages()` used to unconditionally clear
  `pendingEcho`/`pendingEchoUncertain` on *any* successful `GET`, including
  when the reload's own last message was still just the user's own turn (the
  duplicate-`Idempotency-Key` `409` case, see the `performSend` comment above
  — the original attempt is still generating, or the server crashed
  mid-attempt) or when nothing had been persisted yet at all. A question
  could look silently resolved — the pending bubble and its "Retry" affordance
  just disappeared — while no assistant answer actually existed. It now reads
  the reload's own last message: resolved only if it's the assistant's reply.
  Two distinct unresolved states are tracked, not one collapsed "uncertain"
  flag — a 409 reload whose last message IS this user's turn means the
  server *definitely* accepted the question and is still working on it
  (`pendingEchoStillProcessing`, a new, calmer "Still generating an answer
  for this…" bubble with a "Check again" button — the old "Connection lost"
  warning styling would have been simply wrong here, the opposite of what
  actually happened), while an empty/unrelated reload is a genuine "no idea
  if this was even received" (`pendingEchoUncertain`, the existing warning
  styling). In both cases `pendingEcho` stays visible (dropping a trailing
  persisted duplicate of the same user turn from `messages` so it isn't
  rendered twice) and `error` explains a path forward. A concurrent
  `refresh()`/pull-to-refresh landing while the *original* send is still
  genuinely in flight (a real answer can take 20-30s, plenty of time for an
  impatient pull — `ChatScreen`'s `PullToRefreshBox` has no guard against
  this) is a separate hazard this also closes: `loadMessages()` now leaves
  `pendingEcho`/its status/`error` completely untouched whenever `sending` is
  still true, so it can't race the in-flight `performSend()` coroutine that
  alone owns that question's outcome, and can't paint a false "still
  waiting" alarm over a send that hasn't even had a chance to fail yet. This
  is still the interim fix the plan calls for (§4, P0A-2) — the real fix
  needs Phase 2's durable answer-attempt resource with server-authoritative
  status instead of inferring state from adjacent message rows. Covered by
  three new `ChatViewModelTest` cases (13 total now, see below) — the first
  two were confirmed to actually fail against the pre-fix `loadMessages()`
  before being kept, matching this repo's existing regression-test norm; all
  three pass in `testDebugUnitTest` (JVM, no device needed for this one).
  **Known pre-existing gap, not addressed here** (found while designing this
  fix, outside P0A-2's interim scope): `send()` only guards on `sending`, not
  on whether a `pendingEcho` is already uncertain/still-processing — a
  technician who sends a second question while an earlier one is still
  unresolved overwrites `pendingEcho` (and its idempotency key) with the new
  one, making the first question's outcome unrecoverable. This is squarely
  the kind of "silently disappears" case the plan's P0A-2 exit gate warns
  against; it needs its own fix (likely: disable sending a new question
  while one is genuinely uncertain/still-processing, not just while
  `sending`).
- **P0A-3: removed three Android request-order/partial-success races.**
  - `MachinesViewModel` used to launch a brand new, uncancelled coroutine on
    every keystroke in the search box -- an older, slower response could
    land after a newer one and silently overwrite its results with stale
    data. `onQueryChange` now debounces (300ms, `SEARCH_DEBOUNCE_MS`),
    cancels the previous `searchJob` before starting a new one, and
    `search()`/`refresh()` both apply their response only if
    `_state.value.query` still equals the query they were sent for -- the
    debounce/cancellation are the first line of defense, the query-match
    check is the actual backstop if a response was already in flight when a
    newer keystroke landed.
  - `selectMachine()` used to await `touchMachine()` *inside* the same try
    block as `createConversation()`, before calling `onCreated` -- a network
    exception there (a real one; confirmed via a throwaway diagnostic that
    `Response<Unit>`'s converter never actually parses the body, so this
    needs a genuine connection failure, not a bad response) reported total
    failure even though the conversation was already committed server-side:
    it never opened, and retrying could create a second, empty conversation
    for the same machine. `onCreated` now fires immediately once
    `createConversation` succeeds; `touchMachine` runs in its own detached,
    best-effort coroutine (`touchMachineBestEffort`) that can never block
    navigation or turn an already-committed conversation into a reported
    error -- it's a recency/favorites convenience, not part of the
    conversation itself. Its failure is logged (`Log.w`, tag
    `MachinesViewModel`) rather than silently swallowed, so it's
    independently observable rather than just "best-effort" in name only;
    this needed `testOptions.unitTests.isReturnDefaultValues = true` in
    `app/build.gradle.kts` so the unmocked `android.util.Log` call doesn't
    throw in plain JVM unit tests.
  - Chat refresh racing an in-flight send was already closed as part of
    P0A-2 above (`loadMessages()`'s `sendInFlight` guard) -- this is the
    same race P0A-3 names separately, not new work.
  - Review caught a bug the four tests above didn't: `refresh()` cancelling a
    still-debouncing search job (needed so a pull-to-refresh doesn't race a
    stale search) could cancel that job *before* it ever reached `search()`
    -- and `search()` was the only place that ever cleared `loading`, so the
    spinner could get stranded on-screen forever even though the refresh
    itself completed normally. Fixed with a `finally` block around the
    debounced search that clears `loading` on cancellation too, guarded so a
    newer keystroke's own `loading = true` is never clobbered by an older
    job's cleanup.
  - Covered by 5 new `MachinesViewModelTest` cases (13 total now, see below),
    all confirmed to actually fail against the pre-fix code before being
    kept. Two race tests are deterministic via a blocking `Dispatcher`/
    `CountDownLatch`, the same pattern as `ChatViewModelTest`'s clarifying
    -machine race test, not timing luck. The debounce itself needed an
    explicit `TestCoroutineScheduler` passed to `UnconfinedTestDispatcher` in
    the test class (`awaitState`/`awaitRequestCount` now call
    `advanceUntilIdle()`) -- a bare `delay()` never resumes under the
    default scheduler in a plain JUnit test with no `runTest {}` driving it.
- **P0A-4: fixed navigation and evidence failure handling.**
  - `Routes.chat()` used to interpolate the machine label directly into the
    route string (`"chat/$id?label=$label"`) with no encoding at all. A "/"
    in a real model label would split it into extra path segments and break
    route matching outright; "&", "?", or "%" would corrupt the query value.
    Fixed with `android.net.Uri.encode` on the write side -- deliberately
    not `java.net.URLEncoder`, which form-encodes spaces as `+` rather than
    `%20`. Confirmed by reading androidx.navigation 2.9.8's own source
    (`NavDeepLink.kt`'s query-argument branch, backed by `NavUri`, a
    straight `typealias` for `android.net.Uri` on this platform) that
    Navigation's route matching already decodes query arguments via
    `Uri.getQueryParameters` before handing them to the composable -- so the
    read side (`backStackEntry.arguments?.getString("label")`) needed no
    change at all; adding a manual decode there would have double-decoded
    and corrupted any label containing a literal `%`. Covered by a new
    instrumented test (`aMachineLabelWithReservedUriCharactersNavigatesAndDisplaysCorrectly`
    in `AppNavSessionExpiryTest`) driving a real `NavController` with a
    label containing `/`, `&`, `%`, and a space. **Run for real on the Tab
    A9+ (2026-08-26) — passes**, confirming the `Uri.encode`/no-manual
    -decode reasoning above holds against Navigation's actual matching, not
    just against how this file's own analysis expected it to behave. A JVM
    test of the encoding alone was deliberately not written: it would only
    prove the encoder agrees with itself, not that it round-trips through
    Navigation's actual (Android-only) matching, so it would read as
    coverage without being real coverage.
  - Citation evidence requests used to check neither `isSuccessful` on the
    response nor catch anything useful on exception -- a non-2xx response
    left `evidence` null via `resp.body()` returning null, and an exception
    was swallowed outright. Since `ChatScreen` only shows the evidence sheet
    for `evidenceLoading || evidence != null`, any failure (401, 403, 404,
    500, a dropped connection, a timeout) closed the sheet completely
    silently, with nothing to retry. `ChatUiState` now has `evidenceError`
    and `evidenceCitation` (the last-requested citation, kept so `Retry` can
    redrive the identical request); `openCitation`/`retryEvidence` both
    route through a shared `loadEvidence` that checks `isSuccessful` and
    sets a visible, retryable error either way. `dismissEvidence` now clears
    `evidenceError`/`evidenceCitation` too, not just `evidence`, so an error
    sheet actually closes on dismiss instead of a stale error flashing back.
    Covered by 4 new `ChatViewModelTest` cases (17 total now, see below),
    confirmed to genuinely fail against the pre-fix code (a compile error,
    not just a failing assertion, since `evidenceError`/`retryEvidence`
    are new API surface the pre-fix `ChatViewModel` doesn't have). A real
    `SocketTimeoutException` is caught by the same generic
    `catch (Exception)` a plain connection disconnect is -- the same
    equivalence this file's other "network failure" tests already rely on
    -- so no separate timeout-specific test was needed.
  - Not fixed here, deferred as its own separate open item (not part of
    P0A-5, which is layout overflow/focus order, not Coil error states):
    image-load failure for the evidence page image itself (`AsyncImage` in
    `EvidenceSheet`) still renders nothing on a failed load, rather than
    falling back to the text excerpt underneath. That needs either a real
    device or a Robolectric/screenshot harness to verify at all -- there's
    no way to confirm a Coil error-state fallback actually renders
    correctly with only JVM unit tests, so it wasn't
    worth adding unverified UI churn to this otherwise fully-tested commit.
- **P0A-5 (partial): the three layout sites the plan names by name no
  longer overflow.** `MessageBubble`'s clarifying-options row, citation
  -chip row, and Helpful/Incorrect/Save row were all plain `Row`s, which
  don't wrap. Switched to `FlowRow` (both `horizontalArrangement` and
  `verticalArrangement` set explicitly -- omitting the vertical one leaves
  wrapped lines jammed together with no gap). Also dropped the
  `Modifier.padding(start = 12.dp, end = 8.dp)` tuned for the
  "Marked helpful"/"Marked incorrect" label's position in a horizontal
  `Row`; inside a `FlowRow` that text can land at the start of a wrapped
  line, where the same padding would read as a stray indent.
  - **The real pre-fix failure mode, found by dumping the actual on-device
    semantics tree (2026-08-26), was not what it looks like from reading
    the code.** A plain `Row` given a bounded max-width constraint (from
    the `Card`'s `widthIn(max = 560.dp)` and the screen's own width) does
    NOT let its total children width exceed that bound by rendering extra
    chips off-screen to the right. Instead, once the first long chip
    consumes nearly all the available width, every *subsequent* chip in
    the same `Row` gets measured with essentially zero space left and
    **collapses to a literal zero-width placement** (`Rect.fromLTRB(504.0,
    209.0, 504.0, ...)` -- left equals right) rather than rendering at
    all: still present in the semantics tree, completely invisible and
    untappable in practice. A first draft of the new instrumented test
    below asserted only that each chip's right edge stayed within the
    container -- which trivially passes for a zero-width chip -- and
    didn't actually catch the pre-fix bug on the first attempt; caught by
    following this repo's own verify-tests-actually-fail discipline, not
    by inspection.
  - Covered by a new instrumented test,
    `ChatScreenLayoutTest.clarifyingOptionChipsWrapInsteadOfCollapsingAt360dpWidth`
    (`android/app/src/androidTest/.../ui/chat/`): renders `ChatScreen`
    inside a `Box(Modifier.width(360.dp))` -- the narrowest width the
    plan's device matrix names -- with four deliberately long clarifying
    -option labels, and asserts every chip has a genuinely positive width
    as well as staying within the container's bounds. **Run for real on
    the Tab A9+ (2026-08-26)**, confirmed to fail against the pre-fix
    `Row` (once the assertion was corrected to catch the actual collapse
    failure mode, not just an out-of-bounds one) and pass with `FlowRow`.
    Only the clarifying-options site got its own test; the citation-chip
    and Helpful/Incorrect/Save sites share the exact same `Row`-collapse
    mechanism and the exact same `FlowRow` fix, so a second and third copy
    of the same test would prove the same thing again, not add real
    coverage.
  - **What this does NOT close out**, per the plan's own P0A-5 exit gate:
    the wider device/config matrix (411dp, Medium/Expanded tablet,
    landscape, split-screen, 200% font scale, display scaling, keyboard
    open, long localized-length strings beyond the one case tested above),
    and a human accessibility-service listening session for focus order --
    none of these were run. This tablet's own physical screen is 800dp
    wide (1200x1920px @ 240dpi), so even on-device, only the
    width-constrained instrumented test above exercises a genuinely narrow
    layout; the rest of the matrix still needs to be run by hand.
- **The clarifying-machine flow is now reachable**: "Not sure which machine?"
  on the machine picker starts a conversation with no machine selected, so
  asking a question exercises the server's real clarify-instead-of-guess path
  (`_resolve_machine_mention` in `routes_chat.py`) — worth demoing, since it's
  one of the invariants the plan explicitly calls out as must-not-regress.
- **No-answer gets its own visual treatment.** Live testing against the real
  corpus showed `is_no_answer=true` is a common outcome, not an edge case —
  two of three test questions came back that way. It's now a distinct muted
  card with an explicit label instead of rendering identically to a real
  answer, so it reads as intentional honesty rather than a broken app.
- **Confirming a clarifying machine no longer races its own reload.**
  `selectClarifyingMachine()` used to call `refresh()`, which launches a
  detached child coroutine and returns immediately — so its `finally` cleared
  `sending` (re-enabling the composer) before that child coroutine had
  actually reloaded the conversation. Fixed by awaiting the reload directly
  in the same coroutine (`ChatViewModel.loadMessages()`). Covered by
  `ChatViewModelTest`.
- **A dropped connection mid-send no longer looks identical to a normal
  message.** If `send()` throws (network lost, not an HTTP error), the
  technician's own question stayed on screen with no spinner and no
  explanation once `sending` flipped back to false — indistinguishable from
  an already-sent message. `ChatUiState.pendingEchoUncertain` now flags this
  case explicitly and `ChatScreen` labels it "Connection lost — unknown if
  this was received" instead of silently dropping the ambiguity.
- **Feedback/save buttons now show whether they landed.** They used to swallow
  both success and failure silently, so tapping "Helpful" or "Save" gave no
  visible confirmation the server got it. On a successful call the buttons
  are replaced with "Marked helpful"/"Marked incorrect" and "Saved"; on
  failure they're deliberately left tappable again rather than showing a
  false confirmation (still fire-and-forget per plan 10.4 — feedback must
  never block or interrupt the repair task).
- **Question submission is now idempotent (plan sec 9/16/17).** Every send
  carries an `Idempotency-Key` header (`LocalEcho.id`, generated once per
  composed question, never regenerated on retry). A dropped connection or a
  `409` ("this key is already being processed") now shows a "Retry" button on
  the pending bubble that resends with the *same* key and text — never a
  fresh key, never whatever's currently in the composer — so it's always safe
  to tap even if the original attempt actually landed. See
  `docs/ARCHITECTURE.md`'s deviation table for what this does and doesn't
  cover versus the plan's full durable-attempt design.
- **The session cookie is no longer stored in plain text (2026-08-24).**
  `PersistentCookieJar` previously wrote `tma_session` straight into
  `SharedPreferences` — readable on a rooted device or via `adb backup`. It's
  now encrypted with an AES-256-GCM key generated inside the Android Keystore
  (never exported) before being written. Explicitly *not* claimed: this is
  not the plan's OIDC/PKCE token vault (still no refresh token, rotation, or
  server-side per-device revocation — that needs the identity-provider
  decision in plan section 18); it only removes the plaintext-at-rest gap.
  **Verified live on the Tab A9+ (2026-08-24)** — see the "Known correctness
  gaps" entry above for the full sequence (upgrade install over a plaintext
  session, fresh login, force-stop/relaunch survives via the real Keystore).
- **Accessibility pass (2026-08-24) — code-auditable parts fixed, screen
  -reader behavior unverified.** What changed, split by how it was verified:
  - **Verified by build + code audit:**
    - Dark mode was broken for warnings/errors. `Theme.kt` wires up
      `isSystemInDarkTheme()`, but every warning/error color in
      `ChatScreen`/`LoginScreen`/`MachinesScreen` was a single hardcoded hex
      value never re-checked against `DarkColors` — worst case, the error
      card (`Color(0xFFFFEBEE)`, a near-white pink) rendering as a bright
      slab in a dark UI. All of it now goes through
      `MaterialTheme.colorScheme.error`/`errorContainer`/`onErrorContainer`
      or a new `warningColor` (`ui/theme/Theme.kt`), each with an explicit
      light and dark value.
    - `conflict_note` (a revision-conflict notice — plan section 2's
      must-not-regress list) was color-only status: amber text, no icon, no
      label, the exact "color-independent status" gap plan section 6 names.
      It now gets the same icon + explicit "Revision conflict:" label prefix
      the safety-warnings block already had.
    - Audited for fixed-height text containers and hardcoded `fontSize`/
      `maxLines` overrides that would clip text under a larger system font
      size — **none found** across the four screens; Compose's default
      scalable typography is used throughout.
  - **Standard fix applied, behavior not confirmed on-device:**
    - Login/Machines/Chat error text now sets
      `Modifier.semantics { liveRegion = LiveRegionMode.Polite }` so a
      screen reader should announce an error as soon as it appears, instead
      of a screen-reader user needing to manually explore the screen to
      discover it landed.
    - The Login "Sign in" button sets an explicit `contentDescription`
      ("Signing in") while loading, since the button's content becomes a
      bare spinner with no text for a screen reader to read otherwise.
    - Citation chips (`[1] p.5`) get a fuller `contentDescription`
      ("Citation 1, page 5, `<title>`") set on the label `Text` specifically
      (not the chip's own modifier), to avoid touching the chip's own
      click/Button-role semantics. **Needs a real screen-reader listening
      session**: if this double-announces (chip role + both the literal
      glyphs and the override), the fix is different from what's here.
  - **Flagged, not fixed — needs a physical device to even evaluate:**
    Helpful/Incorrect/Save render as three `TextButton`s in a
    `Row(Arrangement.spacedBy(4.dp))`. Material3 enforces a 48dp minimum
    interactive size per button by default (not hand-added here), but
    whether three adjacent 4dp-gapped targets are actually mis-tap-prone
    with gloves (plan section 13.5) is a usability question, not something
    resolvable by reading code.

## Tests

The 2026-08-24 accessibility pass added no new tests — Compose semantics/
screen-reader behavior needs Robolectric or an instrumented test to verify
meaningfully, and a shaky Robolectric semantics harness would manufacture
confidence this doesn't actually have. It's verified by `assembleDebug` and
code audit only; see the accessibility bullet above for exactly which parts
that does and doesn't cover.

Four ViewModels have unit test coverage, all driven against a real
`MockWebServer` rather than a mocked `ApiService` (via
`ApiClient.overrideServiceForTest`, a test-only seam — `init(context)` needs
a real Android `Context` a JVM unit test doesn't have):

- `CookieSerializationTest` (3 tests): the pure domain+Set-Cookie-header
  serialization round-trips, and garbage/blank input fails to parse instead
  of throwing. This is deliberately the *only* part of `PersistentCookieJar`
  that's unit-tested — the Android Keystore isn't available in a JVM unit
  test, so the AES-256-GCM encrypt/decrypt path (`getOrCreateKey()`,
  `storeCookie()`, `loadStoredCookie()`) is verified only by installing on a
  physical device: confirm a session survives an app restart, and confirm
  installing this build over a pre-encryption install forces a clean
  re-login instead of crashing on the old plaintext data.
- `ChatViewModelTest` (17 tests): correct user-then-assistant message
  ordering, the uncertain-pending-echo state, the clarifying-machine reload
  race (verified to actually fail against the pre-fix code before being
  kept), the feedback/save success+failure paths, `retryPendingSend()`
  reusing the original `Idempotency-Key` header rather than a fresh one, a
  `409` triggering an automatic refresh that resolves the pending echo,
  (P0A-2, all three confirmed to actually fail against the pre-fix
  `loadMessages()`) three more: a `409` whose reload finds only the
  persisted user turn keeps the question visibly pending and marks it
  `pendingEchoStillProcessing` rather than the generic uncertain state; a
  bare `refresh()` call (what `ChatScreen`'s pull-to-refresh calls — now with
  a direct test of its own, not just indirect coverage via every other
  test's `init{}`) can't clear an in-flight pending echo when nothing has
  been persisted server-side yet; and a `refresh()` landing while the
  *original* `send()` is still genuinely in flight can't misreport or clear
  that send's `pendingEcho` (a real concurrency scenario, driven with the
  same blocking-`Dispatcher`/`CountDownLatch` pattern as the
  clarifying-machine reload race test above, not just a sequential
  approximation of one); and (P0A-4, all four confirmed to fail against the
  pre-fix code -- a compile error, since `evidenceError`/`retryEvidence`
  are new API surface) four evidence-loading tests: a non-2xx response
  (looped over 401/403/404/500) surfaces a visible, retryable error instead
  of silently closing the sheet; a network failure (standing in for a real
  timeout, caught by the same generic exception branch) does the same;
  `retryEvidence()` redrives the identical citation and can recover after a
  prior failure; and `dismissEvidence()` actually clears a standing error,
  not just the evidence itself.
- `LoginViewModelTest` (4 tests): blank-credential validation short-circuits
  before any network call, successful login, wrong-password message, lost
  connection.
- `MachinesViewModelTest` (13 tests): recent-machines load, search, selecting
  a machine (conversation created + touched + label reported), starting
  without a machine (null label reported), a failed conversation creation,
  pull-to-refresh (blank query reloads recents, an active query re-searches
  instead of reloading recents, and a failed refresh surfaces an error), and
  (P0A-3, all five confirmed to actually fail against the pre-fix code) five
  more: search genuinely waits out its debounce before contacting the server
  (via `takeRequest`'s own timeout, not an immediate `requestCount` read
  racing the same call it's ruling out); rapid typing before the debounce
  elapses fires only one request, for the final query; a slow response for
  an older query can't overwrite a newer one's results (deterministic via a
  blocking `Dispatcher`, same pattern as `ChatViewModelTest`'s clarifying
  -machine race test); a `touchMachine` failure doesn't block navigation
  or report the conversation as failed (needed `Connection: close` on the
  preceding response to force a fresh connection -- a throwaway diagnostic
  confirmed `Response<Unit>`'s converter never actually parses the body at
  all, so only a real connection-level failure, not a malformed body, can
  exercise this path; the same `Connection: close` need is already
  documented on `ChatViewModelTest`'s network-failure-on-save test); and
  (caught in review, not by the other four) `refresh()` cancelling a
  still-debouncing search doesn't leave `loading` stuck true forever.
- `HistoryViewModelTest` (4 tests): past conversations load on `refresh()`
  (no longer on `init{}` — `HistoryScreen`'s own `LaunchedEffect(Unit)` drives
  the first load so a retained instance still refreshes on re-entry), an
  empty history isn't treated as an error, a failed load surfaces one, and a
  second `refresh()` replaces the list rather than appending to it.

Run with:
```
.\gradlew.bat testDebugUnitTest
```

### Instrumented (androidTest) coverage

One thing the JVM unit tests above structurally can't reach: `AppNav`'s
`sessionExpired` redirect (`AppNav.kt`, the `LaunchedEffect(sessionExpired)`
block) is driven by `ApiClient`'s `authExpiryInterceptor`, a real OkHttp
interceptor that only exists once `ApiClient.init`/`initForTest` has built the
actual client pipeline — every ViewModel test above swaps in its own bare
Retrofit+`MockWebServer` client via `overrideServiceForTest` instead and never
wires that interceptor up at all, so this redirect had never actually been
exercised by any test, only verified by hand on the tablet.

`AppNavSessionExpiryTest` (5 tests, `android/app/src/androidTest/...`) closes
that gap by running for real on-device: it logs in against a real
`MockWebServer` instance (so `PersistentCookieJar` stores a real
Keystore-encrypted session cookie, same as production), composes `AppNav`
directly, then asserts that a 401 on the first authenticated request
(`MachinesViewModel.init{}`'s `recentMachines()` call) redirects to the login
screen and clears the session, while a 200 does not. Two more tests (added
for P0A-1, see above) open a real conversation through UI clicks, force a
session expiry, and assert a real re-login through `LoginScreen` lands back
on the machine list rather than the old conversation; and that `logout()`
still clears the local session with the server shut down. A fifth (P0A-4)
selects a machine whose server-returned `machine_label` contains `/`, `&`,
`%`, and a space, and asserts `ChatScreen` still navigates to it and
displays the exact original string — the real route-matching/decoding path
`Routes.chat()`'s `Uri.encode` fix depends on, which no JVM test can
exercise since `NavUri` is a straight `android.net.Uri` typealias on this
platform. This needed two small production-code additions, both scoped
narrowly to test support:

- `ApiClient.initForTest(context, baseUrl)` — `init(context)` is a one-shot
  guarded by `::service.isInitialized`, and by the time a test runs,
  `TechManualApp`'s real `Application.onCreate` has already called it against
  the real `BuildConfig.BASE_URL`. `initForTest` rebuilds the whole pipeline
  (fresh cookie jar included) against a test-supplied base URL instead.
- `localhost`/`127.0.0.1` added to `network_security_config_debug.xml`'s
  cleartext allowlist, alongside the existing dev-machine LAN IP and emulator
  loopback alias — the on-device `MockWebServer` instance this test talks to
  binds to loopback on the tablet itself, not the dev machine's LAN IP.

**All 5 tests run for real on the Tab A9+ (2026-08-26)**, the first time
this file was ever actually executed rather than just compiled -- and it
paid for itself immediately: the re-login test failed on the first real run,
surfacing a genuine race in `AppNav.kt`'s session-expiry handling that no
JVM test could ever have caught (see the P0A-1 bullet above for the full
mechanism and fix). All 5 pass after that fix.

`ChatScreenLayoutTest` (1 test, `android/app/src/androidTest/.../ui/chat/`,
added for P0A-5, also run for real on the Tab A9+ 2026-08-26) closes a
different structural gap: whether a Compose layout actually wraps instead
of overflowing/collapsing at a given width is a real measurement-pass
question no JVM/Robolectric-free unit test can answer honestly. It renders
`ChatScreen` inside a `Box(Modifier.width(360.dp))` with several long
clarifying-option labels and inspects the real semantics tree's laid-out
bounds. See the P0A-5 bullet above for why the first draft's assertion
(right edge in bounds) didn't actually catch the pre-fix bug, and what the
real failure mode turned out to be.

Requires a connected device or running emulator. Run with:
```
.\gradlew.bat connectedDebugAndroidTest
```
Not covered yet: the Compose screens themselves beyond the session-expiry
redirect above (no other UI/instrumented tests — everything else above is
ViewModel-level).

## Things I did that you should know about

- **The demo technician account was seeded by inserting directly into
  `data/db/app.db`**, bypassing the invitation flow — reasonable for local
  dev seeding, not something to do against a real deployment. Its password
  is in this file, in plaintext, above. If this repo (or just this file) ever
  goes somewhere more shared than your own machine, rotate that password or
  strip it out first.
- **`data/app.db` (0 bytes, shows as untracked in `git status`) is stale** —
  the real database is at `data/db/app.db` per `backend/.env`'s `DB_PATH`. I
  didn't delete it since I wasn't sure if it's leftover from something else;
  worth confirming it's safe to remove.
- **The backend I ran to verify this end-to-end is not persistent** — it was
  started in a background shell for testing and will not survive past this
  session. Before the demo, start it fresh from `backend/`:
  `python -m uvicorn app.main:app --host 0.0.0.0 --port 8000`
- **Windows Firewall**: I could not add the inbound rule for TCP 8000 myself
  (access denied — needs an elevated shell). Run this once, in an elevated
  PowerShell, before the demo:
  ```
  New-NetFirewallRule -DisplayName "TechManualAssistant-dev-8000" -Direction Inbound -Protocol TCP -LocalPort 8000 -Action Allow -Profile Private
  ```
