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
- `pg_dump` at a version **equal to or newer than** the Neon project's server
  version (`pg_dump` refuses to dump from a newer server than itself) --
  check the server version against what's installable from the distro's
  default repos before assuming it's covered; Ubuntu 24.04 only ships
  `postgresql-client-16`, so a newer server needs the PGDG apt repo
  (`apt.postgresql.org`) for a matching `postgresql-client-NN` package.

## First deployment

Only `backend/` (plus the compose files and `deploy/`) needs to exist on the
host -- there is no `.git` checkout there, and the rest of the repo (docs,
android, top-level README) is never deployed or kept in sync. Transfer it as
a tarball, not a clone:

```bash
# Create the destination directory on the host FIRST -- scp has no parent
# directory to land in otherwise on a fresh host, and the three scp calls
# right after this one all target paths under it.
gcloud compute ssh <host> --zone=<zone> -- sudo mkdir -p /opt/ChatBot && sudo chown $(whoami) /opt/ChatBot

# From a local checkout, at the commit/tag to deploy:
git archive --format=tar.gz -o /tmp/backend.tar.gz HEAD:backend
gcloud compute scp /tmp/backend.tar.gz <host>:/tmp/ --zone=<zone>
gcloud compute scp docker-compose.yml docker-compose.prod.yml <host>:/opt/ChatBot/ --zone=<zone>
gcloud compute scp -r deploy <host>:/opt/ChatBot/ --zone=<zone>
# There is no .git checkout on the host (see above), so this is the only
# record of which commit is actually running there -- deploy/backup.sh reads
# it into each backup's manifest.
git rev-parse HEAD > /tmp/VERSION
gcloud compute scp /tmp/VERSION <host>:/opt/ChatBot/ --zone=<zone>

# On the host:
sudo mkdir -p /opt/ChatBot/backend
sudo tar -xzf /tmp/backend.tar.gz -C /opt/ChatBot/backend
cd /opt/ChatBot

cp backend/.env.example backend/.env       # then edit; see below
chmod 600 backend/.env                     # restrictive regardless of what Compose itself requires -- see Security notes
cp /path/to/service-account.json backend/  # Drive key, never committed
cat > .env <<EOF
GOOGLE_SERVICE_ACCOUNT_JSON_FILENAME=service-account.json
TMA_DOMAIN=tma.example.com
EOF
# .env's TMA_DOMAIN is read by Compose alone (env_file:), not by this shell --
# export it separately for the curl checks later in this same session:
export TMA_DOMAIN=tma.example.com

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
# From a local checkout, at the commit/tag to deploy:
git archive --format=tar.gz -o /tmp/backend.tar.gz HEAD:backend
gcloud compute scp /tmp/backend.tar.gz <host>:/tmp/ --zone=<zone>
git rev-parse HEAD > /tmp/VERSION
gcloud compute scp /tmp/VERSION <host>:/opt/ChatBot/ --zone=<zone>

# On the host:
sudo tar -xzf /tmp/backend.tar.gz -C /opt/ChatBot/backend
cd /opt/ChatBot
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build app
curl -fsS https://$TMA_DOMAIN/readyz
```

`docker compose restart` does not reread `backend/.env` -- an env-only change
needs `up -d` (which recreates the container), not `restart`.

Never `rm -rf` the host's `backend/` directory before extracting: the Drive
service-account key lives there too (bind-mounted into the container,
outside git, see First deployment above) and isn't in the archive, so wiping
the directory first deletes it -- the container then crash-loops on a
missing key, and Docker will silently create an empty *directory* at that
path to satisfy the bind mount, which then has to be removed by hand before
the real key file can go back. `tar -xzf ... -C backend` alone overwrites
every file the archive contains and leaves everything else (the key,
`.env`) untouched; the only cost is a file deleted from the repo lingering
on the host until someone removes it by hand, which is the safer failure
mode.

Take a backup first (below) whenever the release contains a migration.
Migrations only add columns, indexes and constraints; to roll back the code,
extract the previous tag's `backend/` the same way and rebuild. The database
keeps the newer columns, which older code ignores, except that the
one-current-revision index (migration 0005) makes an older build's approval
flow fail with an error until you upgrade again. For a data rollback use Neon
point-in-time recovery or a branch restore.

## Monitoring

- Docker restarts the container on failure (`restart: unless-stopped`) and its
  healthcheck uses `/healthz`.
- Point an external uptime monitor at `https://<domain>/readyz` and alert on any
  non-200 response, and on `"corpus": "degraded"` or `"ingestion": "stuck"` in
  the body. `/readyz` only reports booleans and status words.
- Logs: `docker compose logs -f app`. One line per request (method, path,
  status, latency, correlation id) plus a full traceback on any unhandled
  exception. Each response also carries an `X-Correlation-ID` header, and
  error bodies include the same id, so an id a technician reports (or one an
  Android crash report shows) is directly greppable in these logs.
- Disk: alert before `data/` and the Docker volume fill up.

## Backups

`deploy/backup.sh` writes a database dump and a tarball of `data/object_storage`,
plus a `.sha256` sidecar for each (`sha256sum -c`-ready) and a
`manifest-<stamp>.json` recording both checksums and the git commit the
backup was taken at (read from the `VERSION` file the deploy procedure above
writes to the host -- there is no `.git` checkout there to ask), and keeps
the newest 14 of each. The dump and tarball are both written to a temp name
and only renamed into their final `db-<stamp>.dump` / `object-storage-<stamp>.tgz`
name once their own command exits 0, so an interrupted run (killed process,
full disk) never leaves a truncated file at a name retention's rotation
would otherwise treat as a complete backup. Schedule it nightly and copy the
output off the host:

```cron
15 2 * * * cd /opt/ChatBot && BACKUP_DIR=/var/backups/tma ./deploy/backup.sh >> /var/log/tma-backup.log 2>&1
```

The database dump and the object-storage tarball are two separate
point-in-time snapshots, not one atomic one -- see the consistency-skew
comment at the top of `backup.sh` for exactly what that can and cannot cause.

Neon also provides point-in-time recovery; confirm the retention window on the
current plan and record it in `OWNER_DECISION_GATE.md`. A backup is not proven
until restored:

1. Before restoring, verify the backup is actually intact: from inside
   `$BACKUP_DIR`, run `sha256sum -c db-<stamp>.dump.sha256` and
   `sha256sum -c object-storage-<stamp>.tgz.sha256` for the stamp you're
   restoring. A checksum mismatch means something corrupted the file after
   backup.sh wrote it (copying it off-host, storage media) -- restoring from
   it anyway would silently fail later in a way that looks like missing
   data, not a bad backup.
2. Restore the newest dump into a scratch Neon branch
   (`pg_restore --no-owner -d <scratch url> db-*.dump`) and unpack the object
   storage tarball into an empty directory.
3. Start the app against them with a different `.env`, confirm `/readyz`
   returns 200 (proving at least one restored document is still approved,
   current and machine-linked -- a restore that silently lost review state
   would otherwise look successful right up to that check).
4. Record the elapsed time, the manifest's git commit, and the checksum
   verification result in the release record.

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
