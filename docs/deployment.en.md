# Docker deployment

Use a local Docker Engine with the Compose plugin and support for `up --wait`. Install Docker Desktop on Windows or macOS, or Docker Engine with the Compose plugin on Linux. You also need Python 3 to generate a secret and curl to verify HTTPS. Run these commands from the project root in a POSIX shell. [Docker installation](https://docs.docker.com/get-docker/) · [Compose installation](https://docs.docker.com/compose/install/)

## 1. Start a local HTTPS instance

Use a fresh shell with no instance-specific `FITSTUDIO_*` or `COMPOSE_*` overrides. Verify the active Docker context points to the intended host. The sample uses a fixed project name and loopback ports. If they are in use, select a distinct project name and update the paired HTTPS port settings in your environment file.

```sh
docker context show
docker version
docker compose version
umask 077
python3 - <<'PY'
from pathlib import Path
from secrets import token_hex

template = Path(".env.example").read_text(encoding="utf-8")
assert template.count("FITSTUDIO_SECRET_KEY=\n") == 1
with Path(".env").open("x", encoding="utf-8") as env:
    env.write(template.replace("FITSTUDIO_SECRET_KEY=\n", f"FITSTUDIO_SECRET_KEY={token_hex(32)}\n"))
PY

dc() {
  docker compose -p fitstudio --env-file .env "$@"
}
dc config --quiet
dc up -d --build --wait
dc exec -T web python manage.py showmigrations studio
dc exec web python manage.py bootstrap_admin studio-owner
```

The Web entrypoint applies migrations and starts Gunicorn after they succeed. Create the first administrator in an interactive terminal with a strong password. Keep `.env` private and use a unique secret for each instance.

```sh
mkdir -p work
dc cp proxy:/data/caddy/pki/authorities/local/root.crt work/fitstudio-local-ca.crt
curl --fail --noproxy '*' --cacert work/fitstudio-local-ca.crt \
  --resolve fitstudio.localhost:8443:127.0.0.1 \
  https://fitstudio.localhost:8443/healthz/
dc exec -T web python -m config.check_health
```

Expect `{"status":"ok"}`. Use a test browser that explicitly trusts your instance's CA to open `https://fitstudio.localhost:8443/admin/`. Members sign in at `/portal/login/`. Staff can use `/admin/login/?next=/` for the coach dashboard. Create an active staff account with the needed permissions. Run `configure_daily_coach --user-id <user ID>` with the Django management command to preview the permissions, then add `--apply` to grant them after review.

| Variable | Purpose |
| --- | --- |
| `FITSTUDIO_DOMAIN` | Bare hostname. Defaults to `fitstudio.localhost`. |
| `FITSTUDIO_SECRET_KEY` | Private instance secret. Deployment settings require at least 50 characters. |
| `FITSTUDIO_HTTPS_PORT`, `FITSTUDIO_HTTPS_BIND` | Public-facing HTTPS port and host binding. Keep the values aligned. |
| `FITSTUDIO_HTTP_BIND`, `FITSTUDIO_CADDYFILE` | HTTP binding and proxy configuration. Local setup disables automatic HTTP redirects. |
| `FITSTUDIO_QUICK_COURSE_TYPE_ID` | Active course type for quick purchases. |
| `FITSTUDIO_BOOKING_MAX_DAYS`, `FITSTUDIO_BOOKING_COACH_ID`, `FITSTUDIO_BOOKING_COURSE_TYPE_ID` | Advance booking horizon, active coach and active 60-minute, one-credit course. |

Create the coach and course records before setting those IDs, then run `dc up -d --wait`. SQLite uses the `studio_data` named volume. Caddy has separate `caddy_data` and `caddy_config` volumes. Keep the project name and volumes when stopping and restarting an instance.

## 2. Public HTTPS configuration

The acceptance evidence covers an isolated local instance. Before serving public traffic, verify DNS, firewall rules, TLS, proxy trust, logging and backups for the intended host. Keep the existing private project name and data volumes. In the instance environment, set `FITSTUDIO_CADDYFILE=./deploy/Caddyfile`, set `FITSTUDIO_DOMAIN` to a bare hostname you control, and use `FITSTUDIO_HTTPS_PORT=443`. Bind ports 80 and 443 according to your network policy. Publish only the proxy ports.

Run `manage.py check --deploy` and decide on HSTS, administrative access and log collection for your site. Keep instance secrets and private certificate material protected and encrypt your backups. FitStudio uses Django sessions and signing and requires no external API key. [Caddy Automatic HTTPS](https://caddyserver.com/docs/automatic-https) · [Django deployment checklist](https://docs.djangoproject.com/en/5.2/howto/deployment/checklist/)

## 3. Back up SQLite

During a maintenance window, stop writes and make an integrity-checked SQLite snapshot. Use POSIX binary redirection. If backup fails, diagnose the failure and restart the source instance with `dc up -d --wait` when appropriate.

```sh
umask 077
mkdir -p backups
dc stop proxy web
backup=$(mktemp backups/fitstudio-XXXXXXXX.sqlite3)
if ! dc run --rm --no-deps -T --entrypoint python web -c '
from contextlib import closing
from pathlib import Path
import shutil, sqlite3, sys, tempfile

with tempfile.TemporaryDirectory() as tmp:
    snap = Path(tmp) / "snapshot.sqlite3"
    with closing(sqlite3.connect("file:/data/fitstudio.sqlite3?mode=ro", uri=True)) as source:
        with closing(sqlite3.connect(snap)) as target:
            source.backup(target)
            if target.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise RuntimeError("SQLite integrity check failed")
    with snap.open("rb") as stream:
        shutil.copyfileobj(stream, sys.stdout.buffer)
' > "$backup"; then
  rm -- "$backup"
  exit 1
fi
test -s "$backup" || exit 1
dc up -d --wait
```

Store the private environment file and application version separately. The SQLite snapshot covers only the database. If a deployment needs to retain its existing Caddy TLS material, back up the private Caddy volumes separately with access controls while the related service is stopped.

## 4. Restore into an isolated instance

Use the same application version. Make a protected copy of the source instance's environment file. Keep the course and coach IDs, choose `./deploy/Caddyfile.local` and use separate loopback bindings, such as `127.0.0.1:18080` and `127.0.0.1:18443`, with `FITSTUDIO_HTTPS_PORT=18443`. Set `RESTORE_ENV` to the absolute path of that file. Use an unused project name each time. This import requires an empty data volume and refuses to overwrite an existing database.

```sh
# Set RESTORE_ENV to an absolute path to the protected instance config copy
# with separate loopback ports before running this block.
test -r "$RESTORE_ENV" && test -s "$backup" || exit 1
restore_dc() {
  docker compose -p fitstudio-restore --env-file "$RESTORE_ENV" "$@"
}

restore_dc config --quiet
restore_dc build web
# Use a brand-new project and volume; do not run up before the import.
restore_dc run --rm --no-deps -T --entrypoint python web -c '
import os, shutil, sqlite3, sys, tempfile
from pathlib import Path

dest = Path("/data/fitstudio.sqlite3")
if any(Path(str(dest) + suffix).exists() for suffix in ("", "-wal", "-shm", "-journal")):
    raise RuntimeError("Restore requires an empty data volume")
fd, staging = tempfile.mkstemp(prefix=".restore-", dir=dest.parent)
try:
    with os.fdopen(fd, "wb") as output:
        shutil.copyfileobj(sys.stdin.buffer, output)
    with sqlite3.connect(f"file:{staging}?mode=ro", uri=True) as snapshot:
        if snapshot.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise RuntimeError("SQLite integrity check failed")
        if not snapshot.execute("SELECT 1 FROM django_migrations WHERE app = ?", ("studio",)).fetchone():
            raise RuntimeError("FitStudio migration history is missing")
    os.link(staging, dest)
finally:
    os.unlink(staging)
' < "$backup" || exit 1
restore_dc up -d --wait
restore_dc exec -T web python manage.py showmigrations studio
restore_dc exec -T web python -m config.check_health
```

Export the recovery instance's own public root certificate as in step 1. Verify HTTPS, sign-in, members, purchases, bookings and balances before considering a traffic cutover.

[Architecture](architecture/system-architecture.md) · [SQLite decision](engineering/adr/0001-sqlite-single-instance.md) · [Public engineering summary](engineering/phase2-public-summary.md)
