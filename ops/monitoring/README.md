# Wishkr host monitoring

This directory installs a metrics-only Grafana Alloy collector on the OVHcloud
production host. It intentionally does not collect logs, traces, application
payloads, environment files, or Docker socket data.

## Security model

- Alloy runs as the package-provided unprivileged `alloy` user.
- The Grafana Cloud token is entered at a hidden terminal prompt and stored only
  in `/etc/wishkr/secrets/grafana-alloy.env` as `root:root` mode `0600`.
- The repository contains the Grafana metrics endpoint and numeric tenant ID,
  but never the API token.
- The Alloy configuration is `root:alloy` mode `0640`.
- Alloy's HTTP UI and health endpoints listen only on `127.0.0.1:12345`.
- Anonymous usage reporting, support bundles, pprof, and remote configuration
  are disabled.
- The service receives no `docker`, `adm`, or `systemd-journal` group
  membership and no Docker socket or host-log access.
- Important logs use the bounded local policy in [`../logging`](../logging/):
  journald, Docker, Nginx, auditd, Wishkr operational evidence, and Trivy
  reports all have explicit limits. Remote log shipping remains deferred until
  field filtering, access control, cost, and retention are separately reviewed.

## Installation

First install the credential through a hidden prompt:

```bash
sudo ./configure-grafana-alloy-credentials.sh
```

Then install and start Alloy:

```bash
sudo ./install-grafana-alloy.sh
sudo ./validate-grafana-alloy.sh
```

The installer downloads the immutable Grafana Alloy `1.17.1` Debian package
from its official GitHub release and verifies its pinned SHA-256 digest before
installation. Updating Alloy requires reviewing and changing both the version
and digest in `install-grafana-alloy.sh`.

## Grafana Cloud

After the validation script passes, open the Grafana Cloud **Linux Server**
integration and select **Install** to add its maintained dashboards and alert
rules. Confirm that host `wishkr-prod-mum-01` appears and that the resource,
filesystem/inode, RAID, and systemd alerts are enabled with the intended email
notification policy.

## NVMe SMART metrics

The Linux Node integration does not expose the complete NVMe SMART health log.
Install the root-only textfile exporter after Alloy:

```bash
sudo ./install-nvme-smart-monitoring.sh
```

The timer reads only the two local NVMe devices every five minutes and writes
an atomic, non-secret Prometheus textfile to
`/var/lib/alloy/textfile/wishkr_nvme_smart.prom`. Metrics contain short device
names such as `nvme0n1`, but no serial numbers, model numbers, user data, or
SMART command output. Alloy remains unprivileged and receives only read access
to the generated metric file.

Create Grafana alerts for collection failure, overall SMART failure, a nonzero
critical-warning bitmask, temperature, spare exhaustion, endurance consumed,
and increases in media errors. RAID degradation continues to use the Linux Node
integration's maintained mdadm alerts.

Import `grafana/wishkr-nvme-alert-rules.yml` through **Alerting > Alert rules >
Import to Grafana-managed rules > Prometheus YAML file**. Select the hosted
Prometheus data source, import into a dedicated `Wishkr Hardware` folder while
paused, review the rules, and resume the `wishkr-nvme` group. The default
notification policy routes these rules to the operations contact point.

## Container health metrics

Install the root-only Docker state exporter after the Wishkr Compose stack is
healthy:

```bash
sudo ./install-container-monitoring.sh
```

The timer inspects only `wishkr-db-1`, `wishkr-redis-1`, `wishkr-backend-1`, and
`wishkr-scheduler-1` once per minute. It exports presence, running and health
state, restart count, OOM status, exit code, and start time without exporting
logs, environment variables, images, mounts, commands, or labels. The exporter
has read-only Docker socket access over `AF_UNIX`; Alloy retains no Docker socket
access.

Import `grafana/wishkr-container-alert-rules.yml` into the existing
`Wishkr Hardware` folder while paused, review the nine rules, and resume the
`wishkr-containers` group. The nine rules detect collection loss, Docker daemon
unavailability, missing/stopped or unhealthy containers, restart loops,
repeated restarts, and OOM kills.

## PostgreSQL and Redis health metrics

Install the root-only datastore exporter after both production data containers
are healthy:

```bash
sudo ./install-datastore-monitoring.sh
```

The one-minute timer runs fixed, read-only health queries inside
`wishkr-db-1` and `wishkr-redis-1` through the local Docker socket. PostgreSQL
metrics cover readiness, authenticated query success, recovery state, data
checksums, connection use, and database size. Redis metrics cover authenticated
readiness, RDB save state, AOF configuration/write/rewrite state, loading, and
persistence sizes. Only numeric state is written to
`/var/lib/alloy/textfile/wishkr_datastores.prom`; no query results, keys,
payloads, credentials, logs, or environment values are exported. Alloy receives
only read access to that textfile and retains no access to Docker or either
datastore.

Import `grafana/wishkr-datastore-alert-rules.yml` into the existing
`Wishkr Hardware` folder while paused, select the hosted Prometheus data source,
and review all 14 rules before resuming the `wishkr-datastores` group. The rules
detect collection loss, failed readiness/authenticated queries, unexpected
PostgreSQL recovery or checksum changes, connection pressure, and Redis RDB/AOF
persistence failures. Confirm every rule becomes Normal before marking the
checklist control complete.

## TLS certificate-expiry metrics

Install the root-only public-certificate exporter after the Cloudflare Origin CA
certificate is installed:

```bash
sudo ./install-tls-certificate-monitoring.sh
```

The six-hour timer reads only
`/etc/wishkr/tls/api.wishkr.com-origin.pem` and emits numeric validity-window,
hostname-match, and expiry metrics. Its systemd sandbox explicitly makes the
matching private key inaccessible, has no network access or capabilities, and
gives Alloy access only to the generated
`/var/lib/alloy/textfile/wishkr_tls_certificate.prom` file.

Import `grafana/wishkr-tls-certificate-alert-rules.yml` into the existing
`Wishkr Hardware` folder while paused, select the hosted Prometheus data source,
and review all seven rules before resuming the `wishkr-tls-certificate` group.
The rules detect collection loss, invalid dates, hostname mismatch, and 30-day
and 14-day expiry thresholds.

## Nginx traffic and error metrics

Install Nginx monitoring only after the reviewed `ops/nginx` configuration is
deployed:

```bash
sudo ./install-nginx-monitoring.sh
```

The installer creates a `127.0.0.1:8081` aggregate `stub_status` endpoint with
access logging disabled. It also enables a dedicated metrics log containing only
numeric `$msec` and `$status` fields. The one-minute root oneshot reads that
status-only log and the loopback counters, while its sandbox explicitly hides
the normal access/error logs. It exports aggregate connection counts, rolling
five-minute request/4xx/5xx/429/499 counts, and collection health without client
addresses, methods, hosts, paths, headers, request IDs, query strings, or
payloads.

Import `grafana/wishkr-nginx-alert-rules.yml` into the existing
`Wishkr Hardware` folder while paused, select the hosted Prometheus data source,
and review all nine rules before resuming the `wishkr-nginx` group. Initial
volume thresholds are deliberately conservative; record production baselines
after cutover and tune them through review rather than silently weakening the
alerts.

## Token rotation

Create a replacement Grafana Cloud token with only `set:alloy-data-write`, save
it in the approved password manager, then rerun:

```bash
sudo ./configure-grafana-alloy-credentials.sh
sudo ./validate-grafana-alloy.sh
```

Revoke the old token only after the new token has successfully delivered
metrics. Never paste a token into a command line, shell history, issue, chat,
repository, or Alloy configuration file.
