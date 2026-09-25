#!/usr/bin/env bash
# Nightly backup for the single-host deployment: a database dump and the stored
# manuals. Run from the repository root (cron example in
# docs/DEPLOYMENT_SINGLE_HOST.md). Keeps the newest 14 of each.
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-/var/backups/tma}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$BACKUP_DIR"

# DATABASE_URL_UNPOOLED comes from backend/.env.
set -a
. backend/.env
set +a

pg_dump --format=custom --no-owner "$DATABASE_URL_UNPOOLED" > "$BACKUP_DIR/db-$STAMP.dump"
tar -czf "$BACKUP_DIR/object-storage-$STAMP.tgz" -C data object_storage

ls -1t "$BACKUP_DIR"/db-*.dump | tail -n +15 | xargs -r rm --
ls -1t "$BACKUP_DIR"/object-storage-*.tgz | tail -n +15 | xargs -r rm --
echo "Backup written to $BACKUP_DIR ($STAMP)"
