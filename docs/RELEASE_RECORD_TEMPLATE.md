# Release Record

One copy of this filled in per pilot drop (each time a signed APK is handed
to technicians). Keep completed records somewhere durable outside this repo
(they may contain or reference secrets-adjacent identifiers) — a private
Drive doc, a password manager's notes field, or an internal wiki page all
work; this file is the template, not the record itself.

Why this exists: `android/app/build.gradle.kts`'s `versionCode`/`versionName`
and Gradle's own version pin are reproducible from git history, but the
*signed artifact* that actually reached a device — which exact keystore
signed it, which backend URL it was built against, which commit and
transitive dependency versions were resolved that day — is not otherwise
recoverable after the fact. Without this, "is the APK on this tablet the one
we think it is" has no answer.

---

## Drop info

- **Date:**
- **Filled in by:**
- **Git commit built from:** (full SHA, not just a branch name)
- **`versionCode` / `versionName`:**
- **Backend URL this build points at** (`TECHMANUAL_BASE_URL_RELEASE`):
- **Distribution method** (direct APK, internal app-sharing link, etc.) and
  who it was sent to:

## Artifact identity

- **APK SHA-256:** (`sha256sum app-release.apk` or the CI job's own output)
- **Signing certificate fingerprint** (`apksigner verify --print-certs`,
  the SHA-256 line):
- **Built by:** CI run (link to the `build-release-candidate` job) / local
  machine (note whose)

## Signing key custody

- **Keystore location:** (where the `.jks` file actually lives — password
  manager, secret store, etc. — never "on my laptop" as the only copy)
- **Backup exists:** yes/no — where
- **Who holds the store/key passwords:** (names or roles, not the
  passwords themselves — see `backend/local.properties` handling: those
  values must never be pasted into this record or any chat/ticket)
- **Restore tested:** yes/no — date last verified someone besides the
  original builder can produce an identically-signed build from the backed
  -up keystore

## Dependency reproducibility

- **Gradle wrapper version:** (from `gradle/gradle-wrapper.properties`,
  should match repo state at the built commit)
- **Transitive dependency lock:** none as of this template's writing — the
  effective resolved dependency tree is not hash-locked. If this drop needs
  bit-for-bit reproducibility guarantees, that's a gap to close first
  (Gradle dependency locking / verification-metadata.xml), not something
  this record can paper over.
- **Docker base image tag** (backend, if this drop paired with a backend
  deploy): (exact tag, not just "latest")
- **Embedding model revision** (backend `EMBEDDING_MODEL_REVISION`, if
  changed this drop):

## Verification performed before handing this off

- [ ] `apksigner verify` passed
- [ ] `aapt dump badging` package id / version match what was intended
- [ ] Installed and smoke-tested on at least one real device
- [ ] If replacing a prior release: confirmed it installs *over* the
      previous same-certificate build without requiring uninstall first

## Notes / deviations from the normal process
