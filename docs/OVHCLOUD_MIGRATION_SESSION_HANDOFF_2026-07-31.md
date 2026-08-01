# OVHcloud Migration Session Handoff — 2026-07-31

## Purpose

Continue the Wishkr migration from Hetzner to the purchased OVHcloud Mumbai
bare-metal server. Work through
[`OVHCLOUD_MIGRATION_SECURITY_CHECKLIST.md`](./OVHCLOUD_MIGRATION_SECURITY_CHECKLIST.md)
one verified item at a time and update that living checklist after every
completed control.

The current work is **Item 14: 60-minute post-cutover observation**.
Item 13 is complete. Item 12 is complete, and its two unresolved OVHcloud
provider-notification ownership controls remain intact in Item 16 at the
account owner's direction.

## Immediate next action

Gate D passed after a narrow scheduler-lock hotfix. The one controlled WhatsApp
test passed and was not repeated. The running backend was not restarted. One
OVH scheduler is healthy with seven jobs and retained lock `41051` through 149
seconds with no open transaction, restart, error, send, or PostgreSQL timeout.
The complete runtime validator passed. Hetzner web/scheduler remain stopped
with no source lock.

Continue the observation window that began at 02:25:10 IST through at least
03:25:10 IST. Keep checking public health, errors, queues, Nginx, PostgreSQL,
Redis, singleton lock/jobs, containers, system resources, TLS, Alloy, RAID, and
NVMe. Stop the scheduler immediately on a singleton failure. At the end choose
accept, hold, or rollback using the documented thresholds. Do not repeat the
WhatsApp test or begin Item 15 before Item 14 is accepted.

## Repository state

- Repository: `/Users/karan/Desktop/Wishkr/wishkr-backend`
- Branch: `main`
- Current committed HEAD: `d918b5c`
  (`test: add scheduler lock acquisition and release tests with transaction
  commit validation`).
- `origin/main` remains `1fb2305`; local `main` is six commits ahead.
- The monitoring implementation through Sentry, bounded log retention, Item
  12/16 checklist reorganization, Item 13 evidence, reminder test-fixture
  correction, the Item 14 runbook, Gate A-D evidence, scheduler-lock fix, and
  regression tests are committed locally but not pushed. Hotfix deployment and
  observation-start evidence are currently uncommitted.
- Run `git status --short`, `git diff --cached --check`, and the relevant script
  validations before committing.
- Preserve unrelated user changes. Do not reset or discard the worktree.

Important new source-controlled paths:

- `docs/OVHCLOUD_MIGRATION_CUTOVER_PLAN_2026-08-01.md`
- `ops/monitoring/config.alloy`
- `ops/monitoring/alloy.default`
- `ops/monitoring/alloy-hardening.conf`
- `ops/monitoring/configure-grafana-alloy-credentials.sh`
- `ops/monitoring/install-grafana-alloy.sh`
- `ops/monitoring/validate-grafana-alloy.sh`
- `ops/monitoring/export-nvme-smart-metrics.sh`
- `ops/monitoring/install-nvme-smart-monitoring.sh`
- `ops/monitoring/wishkr-nvme-smart-metrics.service`
- `ops/monitoring/wishkr-nvme-smart-metrics.timer`
- `ops/monitoring/grafana/wishkr-nvme-alert-rules.yml`
- `ops/monitoring/export-container-metrics.sh`
- `ops/monitoring/install-container-monitoring.sh`
- `ops/monitoring/wishkr-container-metrics.service`
- `ops/monitoring/wishkr-container-metrics.timer`
- `ops/monitoring/grafana/wishkr-container-alert-rules.yml`
- `ops/monitoring/export-datastore-metrics.sh`
- `ops/monitoring/install-datastore-monitoring.sh`
- `ops/monitoring/wishkr-datastore-metrics.service`
- `ops/monitoring/wishkr-datastore-metrics.timer`
- `ops/monitoring/grafana/wishkr-datastore-alert-rules.yml`
- `ops/monitoring/export-tls-certificate-metrics.sh`
- `ops/monitoring/install-tls-certificate-monitoring.sh`
- `ops/monitoring/wishkr-tls-certificate-metrics.service`
- `ops/monitoring/wishkr-tls-certificate-metrics.timer`
- `ops/monitoring/grafana/wishkr-tls-certificate-alert-rules.yml`
- `ops/monitoring/export-nginx-metrics.sh`
- `ops/monitoring/install-nginx-monitoring.sh`
- `ops/monitoring/wishkr-nginx-metrics.service`
- `ops/monitoring/wishkr-nginx-metrics.timer`
- `ops/monitoring/grafana/wishkr-nginx-alert-rules.yml`
- `ops/nginx/wishkr-status.conf`
- `ops/logging/10-wishkr-journald.conf`
- `ops/logging/nginx.logrotate`
- `ops/logging/wishkr-logs.tmpfiles`
- `ops/logging/install-log-retention.sh`
- `ops/logging/validate-log-retention.sh`
- `ops/logging/README.md`

No secrets are stored in these files.

## OVHcloud server

| Field | Value |
|---|---|
| Hostname | `wishkr-prod-mum-01` |
| SSH alias | `ssh wishkr-ovh` |
| Administrative path | WireGuard only, server address `10.77.0.1` |
| Public IPv4 | `148.113.6.76` |
| Region | Mumbai, `ap-south-mum` |
| OS | Ubuntu Server 24.04 LTS |
| CPU | Intel Xeon E-2386G, 6 cores / 12 threads |
| RAM | 64 GB ECC |
| Storage | 2 × 512 GB NVMe, software RAID1 |

WireGuard must be active before using `ssh wishkr-ovh`. Public SSH is not the
normal administration route.

The production Compose stack currently has four healthy containers:

- `wishkr-db-1`
- `wishkr-redis-1`
- `wishkr-backend-1`
- `wishkr-scheduler-1`

PostgreSQL and Redis expose no host ports. Nginx is the host reverse proxy.

## Completed migration work

The main checklist contains full evidence. High-level status:

- Item 2: Ubuntu and RAID1 installation verified, including reboot testing.
- Item 3: WireGuard-only administrative SSH verified.
- Item 4: OS patching and base hardening completed.
- Item 5: host and OVH edge firewalls configured; an external UDP scan remains
  an open verification item.
- Item 6: AppArmor, auditd, Fail2ban, AIDE, Lynis, SMART, mdadm, firmware and
  unattended security controls completed.
- Item 7: Docker installed and secured; compatibility suite passed with
  `797 passed, 3 skipped`.
- Item 8: OVH application deployment completed. Production environment data was
  transferred directly over SSH and was not stored locally or committed.
  PostgreSQL and Redis use new host-generated internal passwords.
- Item 9: Nginx, Cloudflare Origin CA TLS, strict TLS staging, Authenticated
  Origin Pulls, cache bypass, signed-webhook exclusions, and Cloudflare-only
  origin HTTPS allowlisting completed. Production DNS cutover is not complete.
- Item 10: PostgreSQL 16 and Redis 7 hardening/tuning completed and validated.
- Item 11: encrypted dual-destination backup and full restore drills completed.
  Backups use Cloudflare R2 and OVH FTPS; the bare-metal recovery runbook is in
  the private repository.

## Monitoring completed in Item 12

### External uptime

- Sentry project: `wishkr-prod-1`
- Monitor: `Wishkr Production API Downtime`
- Request: `GET https://api.wishkr.com/health`
- Interval: 1 minute
- Timeout: 10 seconds
- Fire after 3 consecutive failures; resolve after 2 successes.
- Email notification was tested successfully.
- The hostname still follows the current Cloudflare/DNS target, which remained
  Hetzner at the time of this handoff.

### Grafana Alloy

- Grafana Alloy `1.17.1` is installed from a checksum-pinned official package.
- Service runs as unprivileged user/group `alloy`.
- UI is loopback-only at `127.0.0.1:12345`.
- pprof, support bundles, anonymous reporting, remote configuration and Loki log
  collection are disabled.
- Alloy is not in the Docker group and cannot access the Docker socket.
- Root-only token file: `/etc/wishkr/secrets/grafana-alloy.env`, mode `0600`.
- Never print or copy that credential into chat, commands, logs, or the repo.
- Hosted Prometheus data source: `grafanacloud-hugemint2487-prom`.
- Production labels include `environment=production`, `provider=ovhcloud`,
  `region=ap-south-mum`, and `cluster=wishkr-production`.
- Linux Node integration installed seven dashboards and 24 maintained resource,
  filesystem/inode, systemd and RAID rules.

### Grafana notifications

- Contact point: `Wishkr Operations Email`.
- Test email received successfully.
- Default notification policy sends all unmatched alerts to that contact point.
- Group by: `grafana_folder`, `alertname`.
- Timing: group wait 30 seconds, group interval 5 minutes, repeat 4 hours.
- A temporary always-firing alert was delivered end to end in 31 seconds and was
  then deleted.

### NVMe SMART monitoring

- Timer: `wishkr-nvme-smart-metrics.timer`, every five minutes.
- Metrics file: `/var/lib/alloy/textfile/wishkr_nvme_smart.prom`,
  `root:alloy` mode `0640`.
- Exporter publishes device names and numeric health counters only; no serials,
  model identifiers, command output or disk data.
- Baseline at validation:
  - two devices detected;
  - SMART passed on both;
  - critical warning `0` on both;
  - temperatures 30°C and 31°C;
  - available spare 100% on both;
  - endurance used 20% and 22%;
  - media errors and error-log entries `0`.
- Folder/group: `Wishkr Hardware/wishkr-nvme-alert-rules.yml` /
  `wishkr-nvme`.
- Twelve imported Grafana-managed alerts are active and confirmed Normal.

### Container monitoring

- Timer: `wishkr-container-metrics.timer`, every minute.
- Metrics file: `/var/lib/alloy/textfile/wishkr_containers.prom`,
  `root:alloy` mode `0640`.
- A capability-free root oneshot connects only to the local Docker socket and
  inspects only the four fixed Wishkr container names.
- It does not export logs, environment variables, commands, images, mounts or
  Docker labels.
- Neither `karan` nor `alloy` belongs to the Docker group.
- Server-side validation reported Docker available and all four containers
  present, running and healthy, with zero restarts, restart loops or OOM kills.
- Folder/group: `Wishkr Hardware/wishkr-container-alert-rules.yml` /
  `wishkr-containers`.
- All nine imported rules are active and were confirmed Normal by the account
  owner after evaluation. The final server-side preflight also reported fresh
  metrics, all four containers healthy, zero restarts/restart loops/OOM kills,
  and zero failed Alloy remote-write samples.

### PostgreSQL and Redis monitoring

- Timer: `wishkr-datastore-metrics.timer`, every minute, active and enabled.
- Metrics file: `/var/lib/alloy/textfile/wishkr_datastores.prom`, `root:alloy`
  mode `0640`.
- A capability-free root oneshot runs only fixed authenticated health queries in
  `wishkr-db-1` and `wishkr-redis-1`; it never exports credentials, environment
  values, keys, payloads, logs, or query results.
- PostgreSQL validation reported readiness/query success, recovery mode `0`,
  data checksums enabled, and 11 of 100 connections in use.
- Redis validation reported authenticated readiness, loading `0`, successful RDB
  save/AOF write/AOF rewrite state, and AOF enabled.
- The service result is `success`, systemd exposure is `3.9 OK`, remote-write
  failures remain zero, and no recent Alloy authentication errors were found.
- All 14 rules in `Wishkr Hardware/wishkr-datastores` are active and were
  confirmed Normal by the account owner after evaluation.

### TLS certificate monitoring

- Timer: `wishkr-tls-certificate-metrics.timer`, every six hours, active and
  enabled.
- Metrics file: `/var/lib/alloy/textfile/wishkr_tls_certificate.prom`,
  `root:alloy` mode `0640`.
- The capability-free oneshot reads only the public Origin CA certificate; the
  private key is explicitly inaccessible and the service has no network access.
- Validation reported parse success, current validity, hostname match, and
  `94587078` seconds until expiry. The unit exposure score is `3.9 OK`.
- Seven rules from
  `ops/monitoring/grafana/wishkr-tls-certificate-alert-rules.yml` were imported
  and resumed. The account owner confirmed the complete
  `wishkr-tls-certificate` group active and Normal after evaluation.

### Nginx monitoring

- Timer: `wishkr-nginx-metrics.timer`, every minute, active and enabled.
- Metrics file: `/var/lib/alloy/textfile/wishkr_nginx.prom`, `root:alloy` mode
  `0640`.
- Aggregate Nginx status is available only at `127.0.0.1:8081`, with access
  logging disabled and no wildcard/public listener.
- The dedicated metrics log stores numeric timestamp/status pairs only. The
  exporter sandbox hides the normal access/error logs and emits only aggregate
  connection, five-minute request, 4xx, 5xx, 429, and 499 counts.
- Initial validation passed with zero parse errors and zero 4xx/5xx/429/499
  responses; the unit exposure score is `4.3 OK`.
- Nine rules from `ops/monitoring/grafana/wishkr-nginx-alert-rules.yml` were
  imported and resumed. The account owner confirmed the complete
  `wishkr-nginx` group active and Normal after evaluation.

### Sentry application-error verification

- The production SDK preflight passed without displaying the DSN: the backend
  was healthy, the DSN was configured, and the SDK was installed.
- One isolated, PII-free synthetic exception was submitted directly from a
  disposable process in `wishkr-backend-1`. It did not call an application
  route or exercise customer, webhook, scheduler, WhatsApp, or payment code.
- Sentry initialized, accepted the capture locally, flushed its queue, and
  assigned event ID `9fe67efb6d7f4852bcdeb1609c91adbc`. On 1 August 2026, the
  account owner confirmed that the event and log were visible in
  `wishkr-prod-1`, completing the end-to-end delivery gate.
- The preflight discovered that the temporary `/sentry-debug` feature flag was
  still enabled. It was disabled in the root-only production environment and
  only the backend was recreated. The replacement is healthy with zero
  restarts; `/health` returns `200`, `/sentry-debug` returns `404`, and the
  environment file remains `root:root 0600`.

### Important-log retention

- The bounded local baseline retains journald for 30 days within 1 GiB, Nginx
  for 14 daily rotations, each container within five compressed 10 MiB files,
  auditd within eight 64 MiB files, Wishkr operational evidence for 30 days,
  and Trivy reports for 90 days.
- No log contents were printed during inventory or validation. Nginx access
  records omit query strings and bodies; application Sentry events retain their
  existing PII scrubber. Loki remains disabled pending a separate remote-log
  design review.
- The inventory exposed and corrected two least-privilege gaps: Alloy no longer
  belongs to `adm` or `systemd-journal`, and all three Wishkr Nginx logs are now
  `root:adm 0640`. The source installers and validators enforce both controls.
- The production validator passed configuration checksums, journald integrity,
  logrotate parsing, directory/file permissions, Docker and auditd limits, and
  Alloy isolation. All four containers remained healthy with zero restarts, the
  Nginx metrics exporter returned `success`, and the host had zero failed units.

### OVHcloud provider monitoring

- The account owner confirmed the dedicated server service status is `Normal`
  and Monitoring is `Enabled with proactive intervention`. OVHcloud documents
  this mode as sending an alert email and initiating a technician check when the
  server stops responding.
- The Network Security Dashboard showed no suspicious activity for the selected
  one-year period. OVHcloud documents an empty activity log as normal when no
  attack has targeted the public IP.
- The account owner confirmed the administrative, technical, and billing
  contacts currently resolve to the same OVHcloud account and that no second
  trusted OVHcloud operator is currently available. No account identifier was
  recorded. The contacts remain unchanged. This leaves a single provider
  notification and management path; no acceptance of that residual risk has
  been recorded yet.
- The provider-side monitoring state is confirmed. Primary recipient delivery
  and the missing independent escalation path remain the notification gaps
  unless an explicit temporary risk exception is recorded; no DDoS or
  hardware-failure simulation will be performed.

## Item 13 staging progress

Item 13 is complete. The account owner's `GO` authorizes Item 14 planning only,
not any live cutover action.

- Production DNS still resolves to the Hetzner origin; no DNS setting changed.
  OVH runtime and datastore validators passed before restore. Installed Alloy,
  monitoring, and backup services were active with zero failed remote-write
  samples and zero failed systemd units.
- A fresh encrypted dual-destination OVH backup completed successfully before
  the restore window. The Hetzner PostgreSQL 16 custom dump was streamed over
  encrypted SSH into a `root:root 0600` OVH artifact, without a workstation
  copy or record output, and passed `pg_restore --list` validation.
- The OVH backend and scheduler were stopped before the dump was restored in a
  single transaction. Only staging Redis was flushed. PostgreSQL role-policy
  application and the complete datastore hardening validator passed afterward.
- Only `wishkr-backend-1` was restarted. It is healthy and the loopback health,
  taxonomy, product list, pricing, product-detail, and Redis catalog-cache
  checks pass. `wishkr-scheduler-1` remains intentionally stopped to prevent
  restored production jobs from producing live actions.
- The non-live application suite passes with `797 passed, 3 skipped`. A
  timezone-boundary defect in one reminder test fixture was corrected by
  constructing its offset from the user's calendar day; application behavior
  was not changed.
- A non-sending scheduler probe registered all seven expected MVP jobs and
  proved the PostgreSQL singleton lock blocks a concurrent holder and can be
  reacquired after release. No job executed, and the real scheduler remains
  stopped.
- An isolated migrated scratch database supported one synthetic inbound
  WhatsApp `hi` through the real Gemini path while Meta delivery was
  intercepted. The deployed route separately returned `403` for an unsigned
  request. Scratch database and synthetic cache state were removed.
- Bounded private health/catalog load tests completed with zero errors at about
  530.8 and 205.2 requests/s respectively. Post-load host, container,
  datastore, authentication, signature, host-header, production-debug, and
  exposure checks passed.
- A fresh encrypted backup of the restored production copy succeeded. Both R2
  and OVH Backup Storage restore drills recovered 539 files/directories,
  validated checksums and Redis, and restored all 54 PostgreSQL tables into
  isolated scratch databases that were removed afterward.
- The current release symlink predates the newer source-tree monitoring
  validator path. Installed-service checks passed, but the next reviewed
  release must include the newer validator files.

## Deferred Item 16 controls

- Verify OVHcloud DDoS and hardware notification delivery.
- Document alert ownership, secondary escalation contacts, and response paths.

## Security constraints for continued work

- Never request or paste passwords, API tokens, private keys, recovery codes,
  database credentials, backup passwords or certificate private keys.
- Keep secrets under `/etc/wishkr/secrets/` with restrictive ownership/modes and
  outside Git.
- Do not add `karan` or `alloy` to the Docker group.
- Do not grant Alloy direct access to the Docker socket, NVMe devices, production
  environment files, databases or application logs.
- Use short root-only oneshots to generate narrowly scoped numeric textfile
  metrics for privileged checks.
- Do not change production DNS, trigger live WhatsApp/cron actions, or retire
  Hetzner without the explicit checklist gate and go/no-go approval.
- Do not log or retain contents read from the Hetzner production environment.
- Preserve Cloudflare webhook exceptions and application-level Meta/Razorpay
  signature validation.

## Recommended continuation order

1. Continue the active observation window through at least 03:25:10 IST.
2. Keep exactly one OVH scheduler and lock; keep Hetzner web/scheduler stopped.
3. At 60 minutes, apply the documented accept/hold/rollback thresholds and
   update Item 14. Do not repeat the passed WhatsApp test.
4. Complete the deferred OVHcloud notification ownership and escalation work
   under Item 16 when a legitimate second-operator arrangement is available.
