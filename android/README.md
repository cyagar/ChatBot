# Technician Manual Assistant — Android

The native client: sign in, pick an approved machine, ask a question, read a
cited answer, open a citation's evidence and page image. It talks directly to the
FastAPI backend in `../backend` over its JSON API. The chronological review
diary that used to live here is archived in
`../docs/history/ANDROID_REVIEW_DIARY.md`; it is history, not current status.

## Prerequisites

- Android Studio with AGP 9.3.2, Kotlin 2.4.10 and `compileSdk`/`targetSdk` 37,
  or the command-line SDK with `platforms;android-37.0` and `build-tools;36.0.0`.
- Do not install Gradle or a JDK for it. Use the wrapper (`./gradlew`); the
  Gradle daemon JDK (25) is provisioned automatically from
  `gradle/gradle-daemon-jvm.properties`.
- `minSdk` 26.

## Backend endpoint

`BASE_URL` is resolved at build time from an environment variable, then
`local.properties` (gitignored; see `local.properties.example`):

| Build | Property / environment variable | Default |
|---|---|---|
| Debug | `techManual.baseUrl.debug` / `TECHMANUAL_BASE_URL_DEBUG` | `http://10.0.2.2:8000/` (emulator loopback) |
| Release | `techManual.baseUrl.release` / `TECHMANUAL_BASE_URL_RELEASE` | none; `assembleRelease` fails unless it is an `https://` URL |

A physical device on your LAN needs the debug URL set to the development
machine's address. Only debug builds permit cleartext traffic.

Debug and release use different application IDs, so both can be installed.

## Build and test

```bash
./gradlew testDebugUnitTest            # JVM unit tests
./gradlew assembleDebug                # debug APK
./gradlew lintRelease                  # lint
./gradlew connectedDebugAndroidTest    # instrumented tests on a connected, unlocked device or emulator
```

`connectedDebugAndroidTest` uninstalls the debug app when it finishes. The device
screen must be on and unlocked or the tests fail with "No compose hierarchies
found". CI runs `testDebugUnitTest` and `assembleDebug`; connected tests are
not in CI, so record their result in the release record.

A local development technician account can be created with
`backend/scripts/seed_dev_technician.py` (refuses to run unless
`APP_ENV=development`). Never use it against a deployed backend.

## Signing and release

Signed builds come from the manually triggered **Android release** GitHub
workflow, which fails if any of these secrets is missing:
`TECHMANUAL_BASE_URL_RELEASE`, `TECHMANUAL_RELEASE_STORE_FILE_BASE64`,
`TECHMANUAL_RELEASE_STORE_PASSWORD`, `TECHMANUAL_RELEASE_KEY_ALIAS`,
`TECHMANUAL_RELEASE_KEY_PASSWORD`. It runs unit tests and lint, builds
`assembleRelease`, verifies the signature (`apksigner verify --print-certs`),
prints the package metadata and SHA-256, and uploads the APK.

To build locally, set the same values as environment variables or
`techManual.release.*` properties in `local.properties` and run
`./gradlew assembleRelease`.

Before distributing a build:

1. Fill in `../docs/RELEASE_RECORD_TEMPLATE.md` for the exact file: commit,
   `versionCode`/`versionName`, backend URL, APK SHA-256, certificate
   fingerprint, keystore custody, devices and results.
2. Increment `versionCode` in `app/build.gradle.kts` for every distributed
   build so it installs over the previous one; it is the upgrade key.
3. Test a clean install and an upgrade install over the previous release.
4. The keystore must be backed up outside the build machine; losing it means
   users must uninstall to update.

## Behaviour worth knowing

- Sessions use the backend's cookie, stored encrypted (Android Keystore). A 401
  clears it and returns to login. Logout is local; a copied cookie stays valid
  until it expires or an administrator disables the account.
- One question at a time. A question with no answer yet stays pending under
  its Idempotency-Key, persisted so it survives process death. Retry resends it
  under the same key and the server either replays the answer, resumes the
  attempt, or refuses with a specific code (`CONVERSATION_BUSY`,
  `IDEMPOTENCY_IN_PROGRESS`, `IDEMPOTENCY_PAYLOAD_MISMATCH`,
  `IDEMPOTENCY_SUPERSEDED`). The client never treats an HTTP 409 alone as
  proof the question was accepted.
- A conversation loads its newest 200 messages; "Load earlier messages" pages
  backward.
- The app fetches `/api/config` once at startup with a five-second timeout and
  fails open (maintenance mode and minimum version are best-effort). Minimum
  version compares dotted numeric parts of `versionName`.

## Known limitations

- No CI coverage for instrumented tests; no phone, screen-reader, split-screen or
  offline/reconnect matrix has been recorded (only a Galaxy Tab A9+).
- A citation's page image that fails to load shows an error state rather than a
  text-first fallback.
- Timestamps use the device locale and time zone.
