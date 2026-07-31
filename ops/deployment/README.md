# Wishkr OVH Production Deployment

This directory contains the host-preparation and validation controls for the
single-server OVH deployment. It contains no production credentials.

## Host layout

| Path | Owner/mode | Purpose |
| --- | --- | --- |
| `/opt/wishkr/releases` | `root:root 0755` | Immutable application releases |
| `/opt/wishkr/current` | symlink | Release selected for Compose operations |
| `/etc/wishkr/secrets` | `root:root 0700` | Production environment and secret files |
| `/var/lib/wishkr` | `root:root 0700` | Reserved Wishkr host state |
| `/var/backups/wishkr` | `root:root 0700` | Local encrypted backup staging |
| `/var/log/wishkr` | `root:adm 0750` | Security/deployment audit output |

PostgreSQL and Redis use the Docker-managed volumes `wishkr_pgdata` and
`wishkr_redisdata`. They are deliberately not bind-mounted into the release
tree. Application containers log to stdout/stderr using Docker's rotating
`json-file` driver.

## Required secret files

All files below are created directly on the host, owned by `root:root`, and
mode `0600`:

- `/etc/wishkr/secrets/production.env`
- `/etc/wishkr/secrets/postgres_password`
- `/etc/wishkr/secrets/redis_password`

The two generated internal passwords are synchronized into the root-only
`production.env` file for Compose interpolation. Standalone Compose implements
file-based secrets as bind mounts, which preserve the host's `root:root 0600`
ownership and therefore cannot be read by the fixed non-root PostgreSQL and
Redis accounts. Docker access on this host is root-only. Redis writes its
credential into a mode-`0600` tmpfs configuration file and unsets the shell
variable before replacing the shell with `redis-server`.

`production.env` contains application credentials plus these deployment
settings:

```dotenv
APP_ENV=production
APP_ROLE=web
RUN_SCHEDULER=false
WEB_WORKERS=1
ALLOWED_HOSTS=api.wishkr.com,wishkr.com,www.wishkr.com,127.0.0.1,localhost
GIFT_REFINEMENT_ENABLED=true
POSTGRES_USER=wishkr
POSTGRES_DB=wishkr
POSTGRES_PASSWORD=<generated>
REDIS_PASSWORD=<generated>
DATABASE_URL=postgresql+asyncpg://wishkr:<generated>@db:5432/wishkr
REDIS_URL=redis://:<generated>@redis:6379/0
WISHKR_ENV_FILE=/etc/wishkr/secrets/production.env
WISHKR_SECRETS_DIR=/etc/wishkr/secrets
```

Never paste real values into this repository, terminal history, tickets, or
chat. Create or transfer the files through the VPN and verify permissions
before invoking Compose.

## Release procedure

1. Run `prepare-host.sh` as root.
2. Create the archive on macOS with metadata disabled, then secret-scan it:

   ```bash
   git ls-files --cached --others --exclude-standard -z |
     COPYFILE_DISABLE=1 tar --no-mac-metadata --no-xattrs \
       --null -T - -cf release.tar
   ```

   Confirm that the archive and built image contain no `._*` AppleDouble
   files. Extract the reviewed source into a new directory below
   `/opt/wishkr/releases`.
3. Point `/opt/wishkr/current` at that release.
4. From `/opt/wishkr/current`, render and validate Compose without printing the
   rendered environment.
5. Build and vulnerability-scan the backend and PostgreSQL images.
6. Start the stack with:

   ```bash
   sudo docker compose \
     --env-file /etc/wishkr/secrets/production.env \
     -f /opt/wishkr/current/docker-compose.ovh.yml \
     up -d --remove-orphans
   ```

7. Run `validate-runtime.sh`.
8. Stream `validate-r2.py` into the running backend container. It creates and
   deletes one random canary object and fails if unrelated buckets are visible.

The web process always sets `RUN_SCHEDULER=false`. Only
`wishkr-scheduler-1` sets it to `true`; PostgreSQL advisory lock `41051`
provides an independent singleton guard.

## Rollback

Point `/opt/wishkr/current` back to the previous reviewed release and repeat
the Compose command. Database migrations require their own reviewed rollback
decision; never downgrade the database automatically.
