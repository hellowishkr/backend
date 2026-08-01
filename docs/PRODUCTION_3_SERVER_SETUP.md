# Wishkr OVH Production Setup

> Current as of 2026-08-01. This document supersedes the former Hetzner
> three-server production guide. The filename is retained so existing links do
> not break.

## Current status

- OVHcloud Mumbai is the active production runtime.
- Production health, scheduler singleton behavior, PostgreSQL, Redis, Nginx,
  TLS, monitoring, backups, and both restore paths passed post-cutover checks.
- Production DNS and the Meta, Razorpay, Vercel, Sentry, and Grafana
  integrations use the OVH-backed production hostname.
- The former Hetzner DB, web, and worker servers are powered off with deletion
  and rebuild protection enabled. They remain a rollback target through the
  seven-day stability review on 2026-08-08.
- Do not power on, alter, wipe, rebuild, cancel, or repurpose the Hetzner
  servers before the end-of-stability review. Cancellation requires separate
  written approval.

The authoritative migration status and outstanding retirement controls remain
in [OVHCLOUD_MIGRATION_SECURITY_CHECKLIST.md](./OVHCLOUD_MIGRATION_SECURITY_CHECKLIST.md).

## Architecture

Production now runs on one OVHcloud bare-metal host in Mumbai:

| Layer | Implementation | Exposure |
| --- | --- | --- |
| Public edge | Cloudflare proxy, WAF, Full (strict) TLS, Authenticated Origin Pulls | Public HTTPS |
| Origin proxy | Host Nginx | HTTPS from Cloudflare and administrative VPN only |
| API | `wishkr-backend-1` | Loopback only on `127.0.0.1:8000` |
| Scheduler | `wishkr-scheduler-1` | No host port |
| Database | `wishkr-db-1`, PostgreSQL 16 | No host port; internal Docker network only |
| Shared state/cache | `wishkr-redis-1`, Redis 7 | No host port; internal Docker network only |
| Administration | WireGuard and named SSH administrator | VPN only; public SSH closed |

```text
Internet
   |
Cloudflare proxy, WAF, TLS and origin authentication
   |
Nginx on the OVH host
   |
127.0.0.1:8000 -> FastAPI backend
                      |               |
                      +-> PostgreSQL  +-> Redis
Scheduler ----------------^---------------^

PostgreSQL and Redis exist only on the internal Docker data network.
```

### Host baseline

- Hostname: `wishkr-prod-mum-01`
- Region: OVHcloud Mumbai
- Operating system: Ubuntu Server 24.04 LTS
- Compute: 6 cores / 12 threads and 64 GiB ECC RAM
- Storage: two NVMe devices in software RAID1
- Emergency access: OVHcloud IPMI/KVM or rescue mode
- Normal administration: `ssh wishkr-ovh` after connecting WireGuard

The host is hardened with unattended security updates, SSH policy, UFW and
provider-edge firewall rules, Fail2ban, auditd, AppArmor, AIDE, SMART and mdadm
monitoring, restricted kernel/module policy, Docker daemon policy, and Trivy
image scanning. Follow the living security checklist for control-level detail.

## Docker runtime

The production definition is [docker-compose.ovh.yml](../docker-compose.ovh.yml).
It runs four services:

| Service | Container | Data/network behavior |
| --- | --- | --- |
| `db` | `wishkr-db-1` | Persistent `wishkr_pgdata`; internal data network only |
| `redis` | `wishkr-redis-1` | Persistent `wishkr_redisdata`; internal data network only |
| `backend` | `wishkr-backend-1` | Application-egress and data networks; loopback port 8000 |
| `scheduler` | `wishkr-scheduler-1` | Application-egress and data networks; no published port |

`wishkr-app` provides controlled application egress. `wishkr-data` is an
internal, non-routed network used by PostgreSQL, Redis, the backend, and the
scheduler. The database and Redis must never publish host ports.

Application containers run as a fixed non-root user with a read-only root
filesystem, all Linux capabilities dropped, `no-new-privileges`, bounded PID,
CPU and memory limits, and a small `noexec` temporary filesystem. Database and
Redis containers have equivalent least-privilege restrictions appropriate to
their fixed service users.

The backend sets `APP_ROLE=web` and `RUN_SCHEDULER=false`. The scheduler sets
`APP_ROLE=scheduler` and `RUN_SCHEDULER=true`. PostgreSQL advisory lock `41051`
is the independent singleton guard that prevents a second active scheduler.
The scheduler must never run Alembic.

## Host paths and ownership

| Path | Purpose |
| --- | --- |
| `/opt/wishkr/releases` | Immutable application releases |
| `/opt/wishkr/current` | Symlink to the active release |
| `/etc/wishkr/secrets` | Root-only production environment and credentials |
| `/var/lib/wishkr` | Root-only application and backup state |
| `/var/backups/wishkr` | Root-only local encrypted backup staging |
| `/var/log/wishkr` | Restricted operational and deployment evidence |
| `/var/log/wishkr-trivy` | Bounded image-scan reports |

PostgreSQL and Redis data live in Docker-managed volumes and are deliberately
outside the release tree. A release rollback must not replace or recreate
these volumes.

Never print, copy, commit, or paste anything from `/etc/wishkr/secrets` into a
terminal command, shell history, workflow log, ticket, or chat. Use the host
credential installers and hidden prompts described in
[ops/deployment/README.md](../ops/deployment/README.md).

## Network and edge security

- Nginx is the only public application listener and proxies to loopback.
- Cloudflare SSL mode is Full (strict), and the origin requires Authenticated
  Origin Pull verification for public requests.
- Only TLS 1.2 and TLS 1.3 are enabled; unknown TLS server names are rejected.
- The OVH Edge Network Firewall and UFW admit origin HTTPS from reviewed
  Cloudflare proxy ranges and administrative VPN traffic.
- Public SSH, HTTP, backend, PostgreSQL, Redis, and Docker management ports
  remain closed.
- The real client address is trusted only when the TCP peer is an approved
  Cloudflare proxy.
- The WhatsApp webhook has the minimum Cloudflare challenge exception needed
  for server-to-server delivery. Application-level Meta signature validation
  remains mandatory.
- Razorpay and other inbound integrations retain their application-level
  signature validation.
- Nginx access logs omit query strings and request bodies.

Source-controlled configuration and installation guidance are in
[ops/nginx/README.md](../ops/nginx/README.md).

## Administrative and CI access

Human administration uses a named sudo account over the WireGuard VPN. Do not
enable public SSH or use routine root login. Keep provider console/rescue access
available for recovery from VPN or SSH failure.

GitHub Actions uses a separate, dedicated WireGuard peer and SSH identity. It
does not reuse the administrator key. The CI account has narrowly scoped,
passwordless sudo access only to these root-owned helpers:

- `/usr/local/sbin/wishkr-deploy-release`
- `/usr/local/sbin/wishkr-cleanup-docker`
- `/usr/local/sbin/wishkr-run-migrations`

SSH host identity is pinned through the GitHub production environment; the
workflow does not use live trust-on-first-use key scanning. Required secret
names and provisioning instructions are documented in
[deploy/README.md](../deploy/README.md). Secret values must never appear in
this document or repository.

## Deployment

The production workflow is
[Test and Deploy Backend to OVH](https://github.com/hellowishkr/backend/actions/workflows/deploy-backend.yml).

### Trigger behavior

- A push to `main` runs tests only. The production deployment job is skipped.
- To deploy after tests, open the workflow, choose **Run workflow**, select the
  `main` branch, choose `DEPLOY_OVH_PRODUCTION`, and confirm the run.
- The GitHub `production` environment and the shared production concurrency
  lock prevent accidental overlap with maintenance workflows.
- Never initiate a second production workflow while one is active.

### Deployment sequence

1. Check out the selected commit and run migrations against the isolated test
   PostgreSQL service.
2. Run the complete backend test suite.
3. Establish the dedicated CI WireGuard peer and verify the pinned SSH host.
4. Run the remote deployment preflight.
5. Build a Git archive and calculate its SHA-256 checksum.
6. Upload it to the approved temporary namespace.
7. The root-owned helper validates the archive, release identifier, checksum,
   and path layout.
8. Build and Trivy-scan the application image; re-scan the reviewed PostgreSQL
   image without rebuilding it.
9. Activate the immutable release, wait for all four containers, and run the
   complete runtime validator.
10. Verify the public health endpoint and remove runner credentials/VPN state.

The deploy helper serializes changes and retains prior immutable releases for
application rollback. Routine application deployment must not rebuild or
restart PostgreSQL merely because the application release changed.

The backend container's startup gate applies `alembic upgrade head` before
starting Uvicorn. Migration compatibility must therefore be reviewed before
dispatching a release. The scheduler starts only through
`python -m app.scheduler_worker` and never applies migrations.

## Manual maintenance workflows

Both maintenance workflows are manual-only, require an explicit confirmation,
use the dedicated CI identity, and share the production concurrency lock.

### Stale container cleanup

[OVH Cleanup Stale Docker Containers](https://github.com/hellowishkr/backend/actions/workflows/ovh-docker-cleanup.yml)
removes only stopped containers older than seven days. It does not prune
images, volumes, networks, build cache, running services, or rollback releases.

### Latest database migrations

[OVH Run Latest Database Migrations](https://github.com/hellowishkr/backend/actions/workflows/ovh-run-migrations.yml)
requires the selected commit to match the active release. It verifies runtime,
creates and validates a fresh encrypted backup, runs Alembic from that active
release, compares database and release migration heads, and revalidates public
health. It cannot upload or run migrations from an inactive release.

## Routine read-only checks

Run these only over the administrative VPN. They do not print environment or
credential values.

```bash
# Active release and containers
readlink -f /opt/wishkr/current
cd /opt/wishkr/current
sudo docker compose --env-file /etc/wishkr/secrets/production.env \
  -f docker-compose.ovh.yml ps

# Complete runtime validation
sudo /opt/wishkr/current/ops/deployment/validate-runtime.sh

# Failed host units
systemctl --failed --no-pager

# RAID and filesystem capacity
cat /proc/mdstat
df -h / /var/lib/docker
df -i / /var/lib/docker

# Backup freshness
sudo /usr/local/sbin/wishkr-backup-status

# Public health from an external operator workstation
curl --fail --silent --show-error https://api.wishkr.com/health >/dev/null
```

Do not use `docker compose config` without redaction: rendered Compose output
can include interpolated credentials.

## Backups and recovery

The host creates daily, client-side encrypted restic backups. Cloudflare R2 is
the off-provider primary recovery repository; OVHcloud Backup Storage over
certificate-verified explicit FTPS is the secondary destination.

Each backup includes:

- a PostgreSQL custom-format logical dump validated by `pg_restore --list`;
- a Redis RDB created after successful `BGSAVE`;
- Wishkr configuration, Nginx and WireGuard configuration;
- the active immutable application release.

The timer runs daily at 02:15 UTC with persistent catch-up and up to ten
minutes of random delay. Retention is 7 daily, 5 weekly, 12 monthly, and 3
yearly snapshots. Backup freshness must remain within 30 hours.

The post-cutover restoration test passed independently from both repositories.
Each drill verified the encrypted repository, artifact checksums, an isolated
Redis load, and a PostgreSQL scratch restore, then removed all temporary data.
Production volumes were never overwritten.

Safe operational commands and restore-drill behavior are in
[ops/backup/README.md](../ops/backup/README.md). Total host-loss recovery is in
[OVHCLOUD_BARE_METAL_RECOVERY_RUNBOOK.md](./OVHCLOUD_BARE_METAL_RECOVERY_RUNBOOK.md).

## Monitoring and alerting

Grafana Alloy sends metrics only. It has no Docker socket, production secret,
application log, system journal, or database access. Root-only oneshot
exporters produce narrowly scoped numeric textfiles for privileged checks.

Active monitoring includes:

- Grafana Cloud Linux host, filesystem, inode, RAID, and systemd monitoring;
- NVMe SMART collection and alerts;
- all four container presence, health, restart, OOM, and exit-state alerts;
- PostgreSQL readiness, authentication, recovery, checksum, connection, and
  size alerts;
- Redis readiness, RDB, AOF, loading, and persistence alerts;
- origin TLS validity, hostname, and expiry alerts;
- Nginx collection, connection, request-volume, 4xx, 5xx, 429, and 499 alerts;
- Sentry application exceptions with PII collection disabled and scrubbing;
- external Sentry uptime monitoring;
- OVHcloud service monitoring with proactive intervention.

The imported Wishkr Grafana alert groups are active and Normal. Details,
installers, and source YAML are in
[ops/monitoring/README.md](../ops/monitoring/README.md).

## Log retention

Logs remain local with bounded retention. Grafana Loki is intentionally not
enabled. Current scopes include bounded journald, rotating Docker logs, 14
daily Nginx rotations, bounded audit logs, 30-day Wishkr operational evidence,
and 90-day Trivy reports.

Grafana Alloy must not join the `docker`, `adm`, or `systemd-journal` groups.
Inspect only the minimum incident window and redact personal data, credentials,
request identifiers, and customer content before exporting evidence. See
[ops/logging/README.md](../ops/logging/README.md).

## Rollback and incident response

### Application release rollback

Point `/opt/wishkr/current` to the previous reviewed immutable release and run
the normal Compose activation and runtime validation. Do not automatically
downgrade PostgreSQL. Any database rollback or restore requires its own
reviewed incident decision and a verified backup.

### Scheduler containment

If duplicate or unsafe scheduler activity is suspected, stop only the
scheduler service, preserve logs, and verify the PostgreSQL advisory-lock
holder before attempting a restart. Do not start the former Hetzner worker as
a diagnostic step.

### Host loss

Use the bare-metal recovery runbook. Prefer the off-provider R2 repository,
then the OVH Backup Storage copy. Restore into new volumes and validate before
changing traffic. Never run destructive recovery steps against surviving
production volumes.

### Temporary Hetzner rollback target

The legacy three-server Compose and deployment helpers exist only for the
protected stability-period rollback target. They are not the current
production architecture and must not be used for routine OVH deployment.
Rollback to Hetzner requires an explicit incident decision and a coordinated
DNS/integration plan; powering on an old server alone is not a rollback.

## Remaining migration work

Item 15 remains in progress until the 2026-08-08 end-of-stability review.
Pending controls include evidence export, safe credential rotation, secure
destruction of old data, cancellation after written approval, and final
incident-response closeout. Item 16 remains open for independent OVHcloud
notification ownership and escalation arrangements.

Do not mark either item complete from this architecture document. Record every
decision and evidence update in the migration security checklist.
