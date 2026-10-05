#!/usr/bin/env bash
# Nightly backup for the single-host deployment: a database dump and the stored
# manuals. Run from the repository root (cron example in
# docs/DEPLOYMENT_SINGLE_HOST.md). Keeps the newest 14 of each.
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-/var/backups/tma}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$BACKUP_DIR"

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

pg_dump --format=custom --no-owner "$DATABASE_URL_UNPOOLED" > "$BACKUP_DIR/db-$STAMP.dump"
tar -czf "$BACKUP_DIR/object-storage-$STAMP.tgz" -C data object_storage

ls -1t "$BACKUP_DIR"/db-*.dump | tail -n +15 | xargs -r rm --
ls -1t "$BACKUP_DIR"/object-storage-*.tgz | tail -n +15 | xargs -r rm --
echo "Backup written to $BACKUP_DIR ($STAMP)"
