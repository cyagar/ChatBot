#!/usr/bin/env bash
# Nightly backup for the single-host deployment: a database dump and the stored
# manuals. Run from the repository root (cron example in
# docs/DEPLOYMENT_SINGLE_HOST.md). Keeps the newest 14 of each.
#
# Consistency boundary: the DB dump and the object-storage archive are two
# separate point-in-time snapshots, taken back-to-back but NOT inside one
# atomic operation -- pg_dump finishes, then tar starts. A manual uploaded
# (written under data/object_storage AND committed to the documents table)
# in the gap between the two can land in the object-storage archive with no
# matching database row in the dump (harmless: an orphaned file, cleaned up
# or re-ingested on restore) or, if ingestion itself is mid-write at that
# exact moment, a database row with no matching file yet (the restore runbook
# in docs/DEPLOYMENT_SINGLE_HOST.md checks for this explicitly). This is an
# accepted skew, not a bug: achieving true cross-store atomicity would need a
# consistent snapshot mechanism (e.g. a filesystem/LVM snapshot) this
# single-host deployment does not have, and the gap in practice is seconds on
# a corpus this size -- an ingestion run already holds the cross-process
# advisory lock (app.ingestion.pipeline) for its whole duration, so this
# script should not normally overlap with one at all.
set -euo pipefail

# Backup dumps are a full, uncompressed-equivalent copy of production data
# (every user's data, every manual) -- group/world-readable files here would
# be a real exposure on any multi-user host. Applies to every file this
# script creates from this point on, including the temp files below.
umask 077

BACKUP_DIR="${BACKUP_DIR:-/var/backups/tma}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$BACKUP_DIR"

# A *.partial left behind by a hard kill (SIGKILL bypasses the trap below
# entirely) is harmless to a later run -- it never matches the *.dump/*.tgz
# globs rotation uses -- but is debris nonetheless; swept here so it doesn't
# accumulate silently forever.
rm -f "$BACKUP_DIR"/*.partial 2>/dev/null || true

# DATABASE_URL_UNPOOLED comes from backend/.env, extracted directly here --
# not sourced as shell (`. backend/.env` runs the file's own content as
# shell code). Confirmed two different ways that actually breaks under this
# script's own `set -euo pipefail`: a leading UTF-8 BOM (some editors write
# one) makes bash try to run the BOM bytes plus the file's first "#" comment
# as a command and abort before pg_dump ever runs; separately, any OTHER
# variable's value containing a shell metacharacter a plain source can't
# tolerate (an unquoted space in a local path, for example) does the same.
# Reading just the one line this script needs, with a leading BOM on line 1
# stripped first, can't be broken by any other line or value in the file --
# and fails loudly below rather than silently running pg_dump against an
# empty connection string. `|| true` on the grep: with `set -e`+pipefail
# above, a legitimate "no match" (grep's normal exit 1 when the variable is
# missing) would otherwise abort the script right here, before the explicit
# check below ever runs -- silencing it here is what lets that check be the
# one that actually reports the problem.
raw="$(sed '1s/^\xef\xbb\xbf//' backend/.env | grep '^DATABASE_URL_UNPOOLED=' | tail -n 1 || true)"
DATABASE_URL_UNPOOLED="${raw#DATABASE_URL_UNPOOLED=}"
DATABASE_URL_UNPOOLED="${DATABASE_URL_UNPOOLED%\"}"
DATABASE_URL_UNPOOLED="${DATABASE_URL_UNPOOLED#\"}"
if [ -z "$DATABASE_URL_UNPOOLED" ]; then
    echo "backup.sh: DATABASE_URL_UNPOOLED is not set in backend/.env -- refusing to run pg_dump with an empty connection string" >&2
    exit 1
fi

DB_DUMP="$BACKUP_DIR/db-$STAMP.dump"
OBJECT_TGZ="$BACKUP_DIR/object-storage-$STAMP.tgz"
MANIFEST="$BACKUP_DIR/manifest-$STAMP.json"

# Both backup files are written to a temp name in the SAME directory (so the
# rename below is a same-filesystem, atomic rename, not a cross-filesystem
# copy) and only renamed into their real, final name once pg_dump/tar have
# actually exited 0. Without this, a dump interrupted partway through (a
# killed process, a full disk, a reboot) leaves a truncated file sitting at
# the real backup name -- indistinguishable from a complete one to anything
# that just globs db-*.dump -- and the retention rotation further down could
# then delete the last good backup while keeping that truncated one as the
# newest. `trap` cleans up a leftover temp file on any exit path (including
# a failure partway through this script), so a failed run never leaves
# stray partial files for the next run's glob to trip over either.
DB_DUMP_TMP="$DB_DUMP.partial"
OBJECT_TGZ_TMP="$OBJECT_TGZ.partial"
trap 'rm -f "$DB_DUMP_TMP" "$OBJECT_TGZ_TMP"' EXIT

pg_dump --format=custom --no-owner "$DATABASE_URL_UNPOOLED" > "$DB_DUMP_TMP"
mv -f "$DB_DUMP_TMP" "$DB_DUMP"

tar -czf "$OBJECT_TGZ_TMP" -C data object_storage
mv -f "$OBJECT_TGZ_TMP" "$OBJECT_TGZ"

# A manifest per backup, independent of trusting file mtimes/sizes alone to
# tell a good backup apart from a bad one later -- the restore runbook
# (docs/DEPLOYMENT_SINGLE_HOST.md) checksums against this before trusting a
# backup it's about to restore from.
# The deployed host has no .git checkout at all (see
# docs/DEPLOYMENT_SINGLE_HOST.md) -- VERSION is the file that procedure
# scp's up alongside the rest of the release, and is this script's primary
# source for which commit is actually running. git rev-parse is only a
# fallback, for running this script directly from a real checkout (a local
# dry run, this script's own tests).
if [ -f VERSION ]; then
    GIT_COMMIT="$(cat VERSION)"
# `if ! VAR=$(cmd)` rather than `VAR=$(cmd || echo unknown)`: on a checkout
# with no commits yet, some git versions write the unresolved ref name
# itself ("HEAD") to STDOUT before failing on stderr -- folding the fallback
# into the same command substitution would capture that leaked text instead
# of a clean "unknown". Checking the exit status separately, after
# capturing, discards whatever (if anything) git put on stdout on failure.
elif ! GIT_COMMIT="$(git rev-parse HEAD 2>/dev/null)"; then
    GIT_COMMIT="unknown"
fi
# Checksummed from inside $BACKUP_DIR (via a subshell `cd`) so the recorded
# filename is bare ("db-<stamp>.dump"), not a full path -- that's what makes
# the sidecar below directly usable as `sha256sum -c` input run from this
# same directory.
DB_SHA256_LINE="$(cd "$BACKUP_DIR" && sha256sum "$(basename "$DB_DUMP")")"
OBJECT_SHA256_LINE="$(cd "$BACKUP_DIR" && sha256sum "$(basename "$OBJECT_TGZ")")"
DB_SHA256="${DB_SHA256_LINE%% *}"
OBJECT_SHA256="${OBJECT_SHA256_LINE%% *}"
echo "$DB_SHA256_LINE" > "$BACKUP_DIR/db-$STAMP.dump.sha256"
echo "$OBJECT_SHA256_LINE" > "$BACKUP_DIR/object-storage-$STAMP.tgz.sha256"
cat > "$MANIFEST" <<EOF
{
  "stamp": "$STAMP",
  "git_commit": "$GIT_COMMIT",
  "db_dump": {"file": "$(basename "$DB_DUMP")", "sha256": "$DB_SHA256", "bytes": $(stat -c%s "$DB_DUMP")},
  "object_storage": {"file": "$(basename "$OBJECT_TGZ")", "sha256": "$OBJECT_SHA256", "bytes": $(stat -c%s "$OBJECT_TGZ")}
}
EOF

# Rotation only ever removes a COMPLETE, renamed backup -- the glob below
# can never match a still-in-progress *.partial file -- so an interrupted
# run never costs this script a good prior backup.
ls -1t "$BACKUP_DIR"/db-*.dump | tail -n +15 | xargs -r rm --
ls -1t "$BACKUP_DIR"/db-*.dump.sha256 | tail -n +15 | xargs -r rm --
ls -1t "$BACKUP_DIR"/object-storage-*.tgz | tail -n +15 | xargs -r rm --
ls -1t "$BACKUP_DIR"/object-storage-*.tgz.sha256 | tail -n +15 | xargs -r rm --
ls -1t "$BACKUP_DIR"/manifest-*.json | tail -n +15 | xargs -r rm --
echo "Backup written to $BACKUP_DIR ($STAMP)"
