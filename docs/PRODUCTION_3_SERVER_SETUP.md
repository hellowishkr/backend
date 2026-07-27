# Wishkr 3-Server Production Setup

This document covers the migration from the current single-server deployment to a 3-server production layout.

## Architecture

| Server | Hostname | Public IP | Private IP | Role | Public ports | Private service ports |
| --- | --- | --- | --- | --- | --- | --- |
| DB/Redis | `wishkr-prod-db-1` | `5.223.61.116` | `10.0.0.2` | Postgres 16 + Redis 7 + backups | SSH only | `5432` and `6379` bound only to `10.0.0.2` |
| Web/API | `wishkr-prod-web-1` | `5.223.77.195` | `10.0.0.3` | nginx + FastAPI backend | `80`, `443`, SSH | backend bound to `127.0.0.1:8000` |
| Worker/Scheduler | `wishkr-prod-worker-1` | `5.223.79.238` | `10.0.0.4` | APScheduler/background worker | SSH only | outbound to DB/Redis over private network |

All three are Hetzner CPX22 (2 vCPU, 4 GB RAM, 80 GB disk) in Singapore (`ap-southeast`), attached to a Hetzner private network (`10.0.0.0/24`).

The existing Postgres Docker volume is preserved as an external Docker volume named `wishkr_pgdata`. Do not delete, recreate, or rename this volume.

## Current State (June 2026)

The migration described below is **complete and live**:

- All three servers deployed and on the private network; DNS/webhooks cut over; public health at `https://api.wishkr.com/health`.
- Redis 7 runs as `wishkr-redis-1` on the DB server, bound to `10.0.0.2:6379`, reachable only from web/worker.
- The application actively uses Redis (commit `e8d844b`, June 2026) for cross-worker shared state: rate limiters, outbound reply dedup, ACK cooldown/suppression, and the message debouncer. Key prefixes: `wk:rl:*`, `wk:reply:*`, `wk:lastreply:*`, `wk:ack:*`, `wk:debounce:*`. With `REDIS_URL` unset (tests, local dev) or on per-call Redis errors, all of it degrades to the original in-process behaviour.
- `WEB_WORKERS=2` is live on the web server (matches the 2 vCPU box). This value lives only in `/opt/wishkr/.env` on the web server — CI never touches `.env`, so it survives deploys.
- Out-of-window WhatsApp sends use approved utility templates via `send_text_or_template()` (commits `2e0ef89`, `b0bb332`) — see `app/bot/whatsapp_templates.py` for the approved template inventory.

## Critical Network Rule

Do not bind Postgres or Redis to the public DB server IP `5.223.61.116` in production.

`DB_BIND_IP` must be one of these:

- A Hetzner private network IP assigned to the DB server
- A WireGuard IP assigned to the DB server

`DATABASE_URL` and `REDIS_URL` on web/worker must point at that same private/WireGuard DB IP, not the public DB IP.

## Phase 1 Facts (historical — pre-migration snapshot, kept for reference)

- Existing server IP: `5.223.61.116`
- Actual hostname: `wishkr-prod-1`
- Planned role name: `wishkr-prod-db-1`
- App path: `/opt/wishkr`
- Current compose path: `/opt/wishkr/docker-compose.yml`
- `/opt/wishkr` is not a Git repo
- Current DB container: `wishkr-db-1`
- Current backend container: `wishkr-backend-1`
- Existing Postgres volume: `wishkr_pgdata`
- Redis does not currently exist (since deployed — see Current State above)
- Current backend runs scheduler jobs
- Verified backup: `/opt/wishkr/backups/pre_3server_migration_20260510_122049.sql.gz`
- Backup checksum file exists and `gzip -t` passed

## Private Network Plan

Completed via Option A (Hetzner private network). Assigned IPs:

```text
DB_PRIVATE_IP=10.0.0.2
WEB_PRIVATE_IP=10.0.0.3
WORKER_PRIVATE_IP=10.0.0.4
```

Original plan kept below for rebuild/disaster-recovery reference.

Create private network connectivity before running DB/Redis compose for the 3-server setup.

### Option A: Hetzner Private Network

1. In Hetzner Cloud Console, open the same project as the three servers.
2. Create a private network in the Singapore/location-compatible project network.
3. Attach all three servers:
   - DB: `5.223.61.116`
   - Web: `5.223.77.195`
   - Worker: `5.223.79.238`
4. Reboot or apply network configuration if Hetzner requires it for the OS image.
5. Record the assigned private IPs:
   - `DB_PRIVATE_IP=...`
   - `WEB_PRIVATE_IP=...`
   - `WORKER_PRIVATE_IP=...`
6. From web, verify private reachability to DB private IP:

```bash
ping -c 3 DB_PRIVATE_IP
```

7. From worker, verify private reachability to DB private IP:

```bash
ping -c 3 DB_PRIVATE_IP
```

### Option B: WireGuard

Use WireGuard only if Hetzner private networking is unavailable. Assign stable VPN IPs and treat them as the private IPs:

```text
DB_PRIVATE_IP=<db WireGuard IP>
WEB_PRIVATE_IP=<web WireGuard IP>
WORKER_PRIVATE_IP=<worker WireGuard IP>
```

## Local Files Added For This Migration

- `docker-compose.prod-db.yml`
- `docker-compose.prod-web.yml`
- `docker-compose.prod-worker.yml`
- `deploy/setup-prod-common.sh`
- `deploy/setup-prod-db.sh`
- `deploy/setup-prod-web.sh`
- `deploy/setup-prod-worker.sh`
- `deploy/deploy-prod-web.sh`
- `deploy/deploy-prod-worker.sh`
- `deploy/backup-prod-db.sh`
- `.env.prod-db.example`
- `.env.prod-web.example`
- `.env.prod-worker.example`
- `app/scheduler_worker.py`

## Environment Templates

Copy the sanitized examples to each server as `/opt/wishkr/.env`. Never commit real secrets.

### DB Server

Start from `.env.prod-db.example`:

```env
POSTGRES_USER=wishkr
POSTGRES_PASSWORD=CHANGE_ME_DO_NOT_COMMIT
POSTGRES_DB=wishkr
DB_BIND_IP=DB_PRIVATE_IP
```

`DB_BIND_IP` must be a private/WireGuard IP. The setup script refuses public-looking IPs.

### Web/API Server

Start from `.env.prod-web.example`. Critical values:

```env
APP_ENV=production
APP_ROLE=web
RUN_SCHEDULER=false
DATABASE_URL=postgresql+asyncpg://wishkr:CHANGE_ME_DB_PASSWORD@DB_PRIVATE_IP:5432/wishkr
REDIS_URL=redis://DB_PRIVATE_IP:6379/0
WEB_WORKERS=1
MVP_REMINDER_ONLY=true
BOT_PRODUCT_CATALOG_ENABLED=true
GIFT_REFINEMENT_ENABLED=true
DEEPSEEK_API_KEY=CHANGE_ME_DO_NOT_COMMIT
DEEPSEEK_CLASSIFIER_MODEL=deepseek-chat
GEMINI_API_KEY=CHANGE_ME_DO_NOT_COMMIT
ENABLE_SHARED_WISHLIST=false
PREFERENCE_OUTBOUND_ENABLED=false
PREFERENCE_INTERCEPTOR_ENABLED=false
LLM_DAILY_BUDGET_USD=5
LLM_MONTHLY_BUDGET_USD=100
LLM_PER_USER_DAILY_BUDGET_USD=1
```

`WEB_WORKERS` can be raised above 1 once `REDIS_URL` is set and verified: rate limits, outbound reply dedup, ACK cooldowns, and the message debouncer are all shared via Redis (June 2026). Without `REDIS_URL`, the code falls back to process-local state — keep `WEB_WORKERS=1` in that case.

Gift refinement runs in the web/API process. Keep `MVP_REMINDER_ONLY=true`;
it does not need to be disabled for read-only Wishkr catalogue discovery.
`BOT_PRODUCT_CATALOG_ENABLED=true` makes active, in-stock Wishkr products
available to the recommendation review, while checkout and provider details
remain blocked by the MVP gates. `GIFT_REFINEMENT_ENABLED=true` enables the
DeepSeek-first, Gemini-fallback review. Both API keys must be present on the
web server; a missing or failed provider leaves the deterministic result
untouched. These keys are not required on the scheduler worker.

### Worker/Scheduler Server

Start from `.env.prod-worker.example`. Critical values:

```env
APP_ENV=production
APP_ROLE=scheduler
RUN_SCHEDULER=true
DATABASE_URL=postgresql+asyncpg://wishkr:CHANGE_ME_DB_PASSWORD@DB_PRIVATE_IP:5432/wishkr
REDIS_URL=redis://DB_PRIVATE_IP:6379/0
ENABLE_SHARED_WISHLIST=false
PREFERENCE_OUTBOUND_ENABLED=false
PREFERENCE_INTERCEPTOR_ENABLED=false
LLM_DAILY_BUDGET_USD=5
LLM_MONTHLY_BUDGET_USD=100
LLM_PER_USER_DAILY_BUDGET_USD=1
```

## Docker And UFW Caveat

Docker published ports can bypass or complicate UFW because Docker programs iptables/nftables rules directly. Do not rely on UFW alone to protect Postgres/Redis if Docker publishes them on a public interface.

Defense-in-depth requirements:

- Bind Docker-published `5432` and `6379` to `DB_PRIVATE_IP`, never `0.0.0.0` and never `5.223.61.116`.
- Add UFW rules allowing `5432` and `6379` only from `WEB_PRIVATE_IP` and `WORKER_PRIVATE_IP`.
- Verify from an unauthorized external machine that public DB ports are closed or filtered.
- Verify from web/worker that private DB ports are reachable only over private/WireGuard IPs.

## Firewall Rules

### DB Server

Allowed inbound:

```text
22/tcp from operator IPs or OpenSSH rule
5432/tcp from WEB_PRIVATE_IP only
5432/tcp from WORKER_PRIVATE_IP only
6379/tcp from WEB_PRIVATE_IP only
6379/tcp from WORKER_PRIVATE_IP only
```

Never allow `5432` or `6379` from `0.0.0.0/0`. Never allow them from the public internet.

Run DB setup with private IPs exported:

```bash
WEB_PRIVATE_IP=<web private IP> \
WORKER_PRIVATE_IP=<worker private IP> \
ssh root@5.223.61.116 'cd /opt/wishkr && WEB_PRIVATE_IP=<web private IP> WORKER_PRIVATE_IP=<worker private IP> bash deploy/setup-prod-db.sh'
```

### Web/API Server

Allowed inbound:

```text
22/tcp
80/tcp
443/tcp
```

Backend remains bound to `127.0.0.1:8000` and is reached only through nginx.

### Worker/Scheduler Server

Allowed inbound:

```text
22/tcp
```

## External Exposure Verification

Run these checks from an unauthorized machine that is not on the Hetzner private network and not connected to WireGuard.

Public DB server must not expose Postgres/Redis:

```bash
nc -vz -w 3 5.223.61.116 5432
nc -vz -w 3 5.223.61.116 6379
nmap -Pn -p 5432,6379 5.223.61.116
```

Expected result: connection refused, timed out, closed, or filtered. Any successful connection is a blocker.

From web and worker servers, private connectivity should work only to `DB_PRIVATE_IP`:

```bash
nc -vz DB_PRIVATE_IP 5432
nc -vz DB_PRIVATE_IP 6379
```

Expected result from web/worker: successful connection over private/WireGuard network.

On DB server, confirm listeners are not on the public IP:

```bash
ss -lntp | grep -E ':5432|:6379'
docker compose -f /opt/wishkr/docker-compose.prod-db.yml ps
```

Expected listener addresses should show `DB_PRIVATE_IP:5432` and `DB_PRIVATE_IP:6379`, not `0.0.0.0` and not `5.223.61.116`.

## Backup Plan

Backups are created with `deploy/backup-prod-db.sh` on the DB server. The script:

- Runs `pg_dump` inside `wishkr-db-1`
- Pipes through `gzip`
- Verifies non-empty file
- Runs `gzip -t`
- Writes `sha256sum` to a `.sha256` sidecar
- Deletes old backup/checksum files after `RETENTION_DAYS`, default `14`
- Does not store DB passwords

Manual backup command on DB server:

```bash
APP_DIR=/opt/wishkr /usr/local/bin/wishkr-backup-prod-db
```

Cron installed by `deploy/setup-prod-db.sh`:

```text
15 2 * * * root APP_DIR=/opt/wishkr /usr/local/bin/wishkr-backup-prod-db >> /var/log/wishkr-backup.log 2>&1
```

**Off-site copy (added June 2026):** after the local dump, the script
uploads the backup + checksum to Cloudflare R2 (rclone remote `r2`, bucket
`wishkr-db-backup`, prefix `db/`, 30-day remote retention via
`R2_RETENTION_DAYS`). Credentials live in `/root/.config/rclone/rclone.conf`
on the DB server only (bucket-scoped R2 API token). An upload failure is
logged but never fails the local backup. Restore path verified 2026-06-12:
download from R2 → sha256 check → full restore into a scratch database.

Restore drill (run on the DB server):

```bash
rclone ls r2:wishkr-db-backup
rclone copy r2:wishkr-db-backup/db/<FILE>.sql.gz /tmp/
gunzip -c /tmp/<FILE>.sql.gz | docker exec -i wishkr-db-1 psql -U wishkr -d <target_db>
```

## Deploy Order

Do not run these until explicitly ready for the next phase.

1. Create and verify the Hetzner private network or WireGuard network first.
2. Record `DB_PRIVATE_IP`, `WEB_PRIVATE_IP`, and `WORKER_PRIVATE_IP`.
3. Common setup on new web server:

```bash
ssh root@5.223.77.195 'bash -s' < deploy/setup-prod-common.sh
```

4. Common setup on new worker server:

```bash
ssh root@5.223.79.238 'bash -s' < deploy/setup-prod-common.sh
```

5. Sync code/env templates to each new server and create `/opt/wishkr/.env` manually from the sanitized examples using private IPs.
6. Prepare DB server common tooling only if needed, taking care not to disrupt current services.
7. Deploy DB/Redis compose on DB server only after confirming the existing volume is present:

```bash
ssh root@5.223.61.116 'cd /opt/wishkr && docker volume inspect wishkr_pgdata'
ssh root@5.223.61.116 'cd /opt/wishkr && WEB_PRIVATE_IP=<web private IP> WORKER_PRIVATE_IP=<worker private IP> bash deploy/setup-prod-db.sh'
```

8. Deploy web server:

```bash
deploy/deploy-prod-web.sh 5.223.77.195
ssh root@5.223.77.195 'cd /opt/wishkr && bash deploy/setup-prod-web.sh api.wishkr.com'
```

9. Deploy worker server only after web migration succeeds:

```bash
deploy/deploy-prod-worker.sh 5.223.79.238
ssh root@5.223.79.238 'cd /opt/wishkr && bash deploy/setup-prod-worker.sh'
```

10. DNS/webhook cutover happens later and is intentionally not scripted here.

## CI/CD Deployment Flow

Production deploys are driven by `.github/workflows/deploy-backend.yml`.

### GitHub Actions Deployment Overview

The workflow follows this order:

1. Push to `main` or run `workflow_dispatch`.
2. Run backend tests.
3. Deploy the web server first.
4. Verify local web health on the web server.
5. Verify public health at `https://api.wishkr.com/health`.
6. Deploy the worker server second.
7. Verify the scheduler via Docker Compose status and logs.
8. Run a final public health check.

The DB server is not deployed from GitHub Actions.

### Trigger and order

- Trigger on push to `main` and `workflow_dispatch`.
- The backend now lives in its own repository, so every push to `main` starts this workflow:

```yaml
on:
  push:
    branches: [main]
  workflow_dispatch:
```
- Run backend tests before deploy.
- Validate that `HETZNER_SSH_KEY`, `HETZNER_WEB_HOST`, `HETZNER_WORKER_HOST`, and `HETZNER_DEPLOY_USER` are all populated before any deploy step starts.
- Run tests and resolve dependency-cache paths from the standalone repository root.
- Sync the standalone repository root to `/opt/wishkr/` on the web host and worker host separately using SSH hardening and explicit `known_hosts` setup.
- Exclude `.env`, `.git`, caches, `backups/`, `.venv/`, `venv/`, `.coverage`, `htmlcov/`, `*.log`, and `.DS_Store` from rsync so each server keeps its own local environment file and server-only artifacts.
- Use `--delete` for code drift cleanup, but protect server-only `.env` and `backups/` content.
- Preflight each server before deploy by confirming `/opt/wishkr`, `.env`, Docker, Docker Compose, and the expected compose file exist.
- Preflight environment role flags safely by checking only `APP_ROLE` and `RUN_SCHEDULER` from `.env`.
- Deploy the web host first.
- After the web deploy, verify `docker compose -f docker-compose.prod-web.yml ps`, confirm logs do not show scheduler startup, and confirm local health with `curl -f http://127.0.0.1:8000/health`.
- If local web health fails, print the last 150 backend log lines and fail.
- Verify the public API externally with `curl -f https://api.wishkr.com/health`, retrying for up to 60 seconds.
- If public health fails, print web compose status, the last 150 backend log lines, and `nginx -t` output when available.
- Deploy the worker host second.
- After the worker deploy, verify `docker compose -f docker-compose.prod-worker.yml ps`, confirm the scheduler logs show startup, confirm no Alembic migration output appeared, and confirm the scheduler container is running or healthy.
- If available, perform a DB-side advisory-lock check from the worker to confirm the scheduler lock is owned by the worker. If tooling is unavailable, the workflow prints `skipped advisory lock DB check` and continues.
- Verify the public API again after the worker deploy.
- On failure, print backend logs from web if that step ran and scheduler logs from worker if that step ran.
- Print a deployment summary on success with the GitHub SHA, web host, worker host, public health result, and compose status from both hosts.
- Never run `docker compose down` in CI.
- Never touch the DB server from CI.
- Do not enable feature flags as part of deploy automation.
- Keep Hubble in staging/safe mode until production credentials are available.

### GitHub Secrets

The workflow expects these secrets:

- `HETZNER_SSH_KEY`
- `HETZNER_WEB_HOST`
- `HETZNER_WORKER_HOST`
- `HETZNER_DEPLOY_USER`

Notes:

- `.env` files live only on servers.
- `.env` must never be committed or synced from GitHub.
- The DB server is not touched by the GitHub Actions workflow.

### SSH Deploy Key Setup

Use an `ed25519` deploy key for GitHub Actions:

1. Create a new ed25519 key pair for deploy automation.
2. Add the public key to the web server.
3. Add the public key to the worker server.
4. Store the private key in the GitHub secret `HETZNER_SSH_KEY`.
5. Do not add the deploy key to the DB server unless it is actually needed there.

### Compose commands

Web:

```bash
docker compose -f docker-compose.prod-web.yml up -d --build backend
```

Worker:

```bash
docker compose -f docker-compose.prod-worker.yml up -d --build scheduler
```

The web container startup command runs Alembic. The worker container must not run Alembic.

### Deployment Safety Rules

- Web deploy always runs before worker deploy.
- Worker must never run Alembic.
- Feature flags are not changed by CI.
- Hubble credentials are not changed by CI.
- Production `.env` files are preserved on each server.
- rsync excludes `.env`, backups, caches, logs, virtualenvs, and coverage output.
- GitHub Actions does not deploy to the DB server.
- The workflow uses `environment: production`.
- The workflow keeps `concurrency.cancel-in-progress=false` to avoid overlapping deploys.

### Health Checks

- Web local health: `curl http://127.0.0.1:8000/health`
- Public health: `curl https://api.wishkr.com/health`
- Worker does not expose a public health endpoint.
- Worker verification happens through Docker Compose status and scheduler logs.

### Failure Diagnostics

- Web backend logs are printed if the web deploy or web health check fails.
- Worker scheduler logs are printed if the worker deploy or worker verification fails.
- Public health failure may also check nginx configuration with `nginx -t` when available.
- Deployment fails if worker logs show Alembic migration activity.
- Deployment fails if web logs show scheduler startup activity.

### Runtime Safety Checks

The workflow now enforces these production checks before and after deploy:

- Web server `.env` must contain `APP_ROLE=web` and `RUN_SCHEDULER=false`.
- Worker server `.env` must contain `APP_ROLE=scheduler` and `RUN_SCHEDULER=true`.
- Web logs must not show scheduler startup signals such as `Scheduler started:` or `Scheduler advisory lock acquired`.
- Worker logs must not show `alembic upgrade` or `Running upgrade`.
- The worker scheduler lock key is `41051`, matching `app/main.py`.

### Rollback

If the post-deploy checks fail before DNS or traffic cutover, leave production traffic on the previous stable path and do not attempt any DB changes from CI.

Rollback guidance:

- Redeploy the previous commit manually if needed.
- Do not restore the DB unless explicitly required.
- Do not touch the DB server for normal app rollback.
- If the worker causes issues, stop the scheduler service on the worker.
- If the web deploy fails, keep the old image/container if available or redeploy the previous commit.
- Keep each server's local `.env` in place during rollback; it should never be resynced or rewritten by CI.

### Post-Deploy Verification Checklist

Use this after the workflow reports success and before any wider traffic changes:

- Public health is OK.
- Web backend is healthy.
- Worker scheduler is healthy.
- No duplicate scheduler is running.
- Feature flags are still false unless changed manually on the server.
- Sentry is clean or only shows expected noise from the deploy window.
- WhatsApp smoke test passes.
- `llm_usage_events` has a new row after an AI interaction.
- Redis reachable from the web container and `wk:*` keys appear under live
  traffic (`docker exec wishkr-redis-1 redis-cli keys "wk:*"` on the DB server).
- Web `.env` still has the operator-set `WEB_WORKERS` value (CI must never
  rewrite it) and `docker compose logs backend` shows that many
  "Started server process" lines.

## Capacity And Upgrade Path (June 2026)

Measured at WEB_WORKERS=2 rollout: all three boxes are far below capacity
(worker load average 0.00; DB using ~730 MB of 4 GB). The throughput levers,
in the order they matter:

### Web requests

- Webhook turns are I/O-bound (Gemini + Meta calls); 2 async uvicorn workers
  handle hundreds of requests/sec on light paths. Gemini latency/quota
  throttles real bot traffic long before this box does.
- Scale signal: web load average consistently above ~1.5, or p95 webhook
  latency rising without a matching LLM slowdown.
- Scale action: resize CPX22 → CPX32 (4 vCPU / 8 GB) in Hetzner, then set
  `WEB_WORKERS=4` in `/opt/wishkr/.env` on the web server and
  `docker compose -f docker-compose.prod-web.yml up -d --force-recreate backend`.
  All cross-worker state is already in Redis, so raising workers is safe.

### Reminder/cron throughput (worker)

Env-only knobs in the worker's `/opt/wishkr/.env` — no code or hardware:

```text
REMINDER_CONCURRENCY=15   # parallel Meta sends; 15 ≈ 50 sends/sec ≈ 3,000/min
REMINDER_BATCH_SIZE=300   # reminders claimed per tick
REMINDER_TICK_MINUTES=5   # cron frequency
REMINDER_WINDOW_START_HOUR_IST=6            # global India operational opening
REMINDER_WINDOW_START_MINUTE_IST=30
REMINDER_WINDOW_END_HOUR_IST=22             # global India operational close
REMINDER_RECIPIENT_WINDOW_START_HOUR=6      # recipient-local quiet-hours opening
REMINDER_RECIPIENT_WINDOW_START_MINUTE=30
REMINDER_RECIPIENT_WINDOW_END_HOUR=22       # half-open: local time < 22:00
```

The real ceiling is Meta's, not ours: business-initiated messaging tiers
(1K → 10K → 100K unique recipients/day, grows automatically with volume +
quality) and ~80 msgs/sec API throughput. Even at the 10K tier, current
settings clear a full day's reminders in under 4 minutes.

### DB/Redis

- Scale signal: Postgres connection pile-ups, or available RAM persistently
  under ~500 MB on the DB box.
- Scale action: resize the DB server (RAM helps Postgres more than CPU).
  Redis footprint is a few MB and is never the reason to resize.

## Cutover Plan

1. Confirm latest backup exists, `gzip -t` passes, and checksum is saved.
2. Confirm DB server binds Postgres/Redis only to `DB_PRIVATE_IP`.
3. Confirm unauthorized external scans cannot reach public `5432/6379`.
4. Confirm DB server accepts Postgres/Redis only from `WEB_PRIVATE_IP` and `WORKER_PRIVATE_IP`.
5. Confirm Redis health on DB server.
6. Confirm web server can run migrations and passes `/health` locally.
7. Confirm worker scheduler starts and acquires the advisory lock.
8. Confirm web server has `RUN_SCHEDULER=false` in runtime env.
9. Move traffic only after separate approval. Do not change DNS or webhook URLs in this phase.
10. After traffic is confirmed on the new web server, stop old backend in a separate approved phase.

## Rollback Plan

Before DNS/webhook cutover, rollback is simply to leave traffic on the existing single server.

After cutover, rollback steps are:

1. Point traffic back to the old single-server endpoint or reverse proxy target using the previously approved DNS/proxy process.
2. Stop the new worker scheduler to prevent duplicate jobs.
3. Keep DB volume `wishkr_pgdata` untouched.
4. If data corruption is suspected, do not restore immediately. First take another backup, preserve logs, and decide whether to restore from `/opt/wishkr/backups/pre_3server_migration_20260510_122049.sql.gz` or a newer verified dump.

## Verification Commands

DB server:

```bash
cd /opt/wishkr
docker volume inspect wishkr_pgdata
docker compose -f docker-compose.prod-db.yml ps
docker compose -f docker-compose.prod-db.yml exec -T db pg_isready -U wishkr -d wishkr
docker compose -f docker-compose.prod-db.yml exec -T redis redis-cli ping
ufw status verbose
ss -lntp | grep -E ':5432|:6379'
```

Web server:

```bash
cd /opt/wishkr
docker compose -f docker-compose.prod-web.yml ps
docker compose -f docker-compose.prod-web.yml logs --tail=150 backend
curl -fsS http://127.0.0.1:8000/health
nc -vz DB_PRIVATE_IP 5432
nc -vz DB_PRIVATE_IP 6379
nginx -t
```

Worker server:

```bash
cd /opt/wishkr
docker compose -f docker-compose.prod-worker.yml ps
docker compose -f docker-compose.prod-worker.yml logs --tail=150 scheduler
nc -vz DB_PRIVATE_IP 5432
nc -vz DB_PRIVATE_IP 6379
docker compose -f docker-compose.prod-worker.yml exec -T scheduler python - <<'PY'
import asyncio
from sqlalchemy import text
from app.db.session import engine
LOCK_KEY = 41051
async def main():
    async with engine.connect() as conn:
        acquired = bool(await conn.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": LOCK_KEY}))
        if acquired:
            await conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": LOCK_KEY})
            raise SystemExit("scheduler advisory lock is not held by the worker")
        print("scheduler advisory lock is held by the worker")
asyncio.run(main())
PY
```

Unauthorized external machine:

```bash
nc -vz -w 3 5.223.61.116 5432
nc -vz -w 3 5.223.61.116 6379
nmap -Pn -p 5432,6379 5.223.61.116
```

## Migration/Missing Tables

The Phase 1 sanity check showed these tables are missing on production before migration:

- `llm_usage_events`
- `recipient_preferences`
- `preference_requests`

This is expected before the new migrations run. `docker-compose.prod-web.yml` runs `alembic upgrade head` during web deploy, and that should create the missing tables. The worker server must not run migrations.

## Worker Service Shape

There is no `app.worker` module today, so `docker-compose.prod-worker.yml` intentionally includes only the scheduler service. Adding a separate background worker service later should be done only after a real `app.worker` entrypoint exists.

The worker deploy intentionally does not run the web backend, and the workflow now checks the worker compose status to ensure only the scheduler service is present.

## Known Risks From Phase 1

- The current backend runs APScheduler jobs. Duplicate schedulers can send duplicate reminders/outbound messages. The generated files set `RUN_SCHEDULER=false` on web and `RUN_SCHEDULER=true` only on the scheduler service.
- The previous app code did not honor `RUN_SCHEDULER`; this migration adds an explicit guard in `app/main.py`.
- `/opt/wishkr` is not a Git repo on the DB server. All generated deploy scripts use rsync and do not rely on remote Git.
- Redis is new. It must be bound to `DB_PRIVATE_IP` and protected by UFW rules allowing only `WEB_PRIVATE_IP` and `WORKER_PRIVATE_IP`.
- The DB backup is valid but small. Verify this matches expected production data before cutover.
- Several newer tables are missing on production before migration; web deploy runs `alembic upgrade head`, worker deploy does not.
- ~~Some WhatsApp dedup logic is process-local. Keep `WEB_WORKERS=1` until shared dedup/rate-limit storage is implemented.~~ Resolved June 2026: rate limits, reply dedup, ACK state, and the debouncer are Redis-backed; `WEB_WORKERS=2` is live.
- Docker published ports can bypass or complicate UFW. Treat private-IP binding plus external scans as mandatory, not optional.
- The deploy workflow will fail early if its required secrets are missing, rather than discovering that during SSH or rsync.
- The workflow intentionally preserves `.env` and backups on each host; do not add GitHub-side syncing for either.
