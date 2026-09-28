# Deployment: single persistent host

The selected production topology (`OWNER_DECISION_GATE.md`, section 14): one
Linux host running Docker, one application replica, Neon for the database,
Caddy for HTTPS. One replica is what the code assumes: the ingestion scheduler
runs inside the app process, manuals and rendered pages live on local disk, and
rate limits are per process. Do not run a second replica.

## Host requirements

- Linux with Docker Engine and the Compose plugin, at least 2 CPU cores and
  4 GB RAM (the container is capped at 2 GB), 20 GB disk plus room for the
  manual corpus.
- A DNS name pointing at the host, and ports 80 and 443 open to the internet.
  The Android release build only talks to `https://` URLs.
- Outbound access to Neon, Google Drive and `api.anthropic.com`.
- `pg_dump` (PostgreSQL client 16 or newer) for backups.

## First deployment

```bash
git clone https://github.com/cyagar/ChatBot.git && cd ChatBot
git checkout <release tag>

cp backend/.env.example backend/.env       # then edit; see below
cp /path/to/service-account.json backend/  # Drive key, never committed
cat > .env <<EOF
GOOGLE_SERVICE_ACCOUNT_JSON_FILENAME=service-account.json
TMA_DOMAIN=tma.example.com
EOF

# The app container runs as a non-root user, UID/GID 10001 (see
# backend/Dockerfile). backend/.env is loaded via Compose's env_file:, so its
# host permissions don't matter -- Compose reads it as the host user and
# injects the values as real environment variables. The service-account key
# IS a bind-mounted file (the Google auth library needs a path), so it must
# be owned by 10001, or the container cannot read it and startup fails
# validate_for_startup()'s key check:
chown 10001:10001 backend/service-account.json && chmod 400 backend/service-account.json
mkdir -p data && chown -R 10001:10001 data

docker compose -f docker-compose.yml -f docker-compose.prod.yml config   # verify
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

`backend/.env` for production must set `APP_ENV=production`, a random
`SECRET_KEY` of at least 32 characters, both Neon URLs, the Drive folder and key
path (`GOOGLE_SERVICE_ACCOUNT_JSON_PATH` is set by compose), `AI_PROVIDER=anthropic`,
`ANTHROPIC_API_KEY`, and `ALLOWED_REGISTRATION_DOMAINS`. The app refuses to start
with a placeholder secret or missing settings.

Then create the first administrator, run the first sync and check readiness:

```bash
docker compose exec app python scripts/bootstrap_admin.py --email you@example.com
docker compose exec app python scripts/ingest.py
curl -fsS https://$TMA_DOMAIN/readyz
```

`/readyz` returns 503 until at least one approved, machine-linked document is
retrievable. Approve documents and machine links in `/admin` → Review queue.

Migrations are applied automatically at startup under a database lock.

## Updating and rolling back

```bash
git fetch && git checkout <new tag>
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
curl -fsS https://$TMA_DOMAIN/readyz
```

Take a backup first (below) whenever the release contains a migration.
Migrations only add columns, indexes and constraints; to roll back the code,
check out the previous tag and rebuild. The database keeps the newer columns,
which older code ignores, except that the one-current-revision index (migration
0005) makes an older build's approval flow fail with an error until you upgrade
again. For a data rollback use Neon point-in-time recovery or a branch restore.

## Monitoring

- Docker restarts the container on failure (`restart: unless-stopped`) and its
  healthcheck uses `/healthz`.
- Point an external uptime monitor at `https://<domain>/readyz` and alert on any
  non-200 response, and on `"corpus": "degraded"` or `"ingestion": "stuck"` in
  the body. `/readyz` only reports booleans and status words.
- Logs: `docker compose logs -f app`. Each response carries an `X-Correlation-ID`
  header, and error bodies include the same id.
- Disk: alert before `data/` and the Docker volume fill up.

## Backups

`deploy/backup.sh` writes a database dump and a tarball of `data/object_storage`
and keeps the newest 14 of each. Schedule it nightly and copy the output off the
host:

```cron
15 2 * * * cd /opt/ChatBot && BACKUP_DIR=/var/backups/tma ./deploy/backup.sh >> /var/log/tma-backup.log 2>&1
```

Neon also provides point-in-time recovery; confirm the retention window on the
current plan and record it in `OWNER_DECISION_GATE.md`. A backup is not proven
until restored: restore the newest dump into a scratch Neon branch
(`pg_restore --no-owner -d <scratch url> db-*.dump`), unpack the object storage
tarball into an empty directory, start the app against them with a different
`.env`, and record the elapsed time in the release record.

The Drive cache (`data/gdrive_cache`) is not a source of record.

## Security notes

- Only Caddy publishes ports; the app is reachable only through it.
- `backend/.env` and the service-account key stay on the host, outside git.
  `backend/.env` is read by Compose itself (`env_file:`), so any mode the host
  user can read is fine. The service-account key is bind-mounted into the
  container and read by the app's own UID (10001), not the host user's --
  `chown 10001:10001` it (mode 400 is enough; a host-user-only mode like 600
  would leave the container unable to read it at all).
- Apply OS security updates, and rebuild the image regularly to pick up base
  image and dependency patches (Dependabot opens the pull requests).
- Rotate `SECRET_KEY` if it is ever exposed; every session is then invalidated.
