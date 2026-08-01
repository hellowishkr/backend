# Wishkr Important-Log Retention

This package defines the bounded local-retention baseline used for migration
Item 12. It deliberately does not ship logs to Grafana Loki. Local retention is
the lower-risk choice until field-level filtering, access control, storage cost,
and deletion behavior are explicitly reviewed.

## Retained scopes

| Scope | Retention bound | Protection and privacy notes |
| --- | --- | --- |
| systemd journal | 30 days and 1 GiB, with 2 GiB free-space reserve | Persistent, compressed, sealed; journal files are `root:systemd-journal 0640` |
| Backend, scheduler, PostgreSQL, and Redis container output | Five compressed 10 MiB files per container | Docker storage is root-owned; application logging excludes request bodies and applies production redaction |
| Nginx access, error, and numeric monitoring logs | 14 daily rotations | `root:adm 0640`; access logs omit query strings and request bodies |
| Linux Audit | Eight 64 MiB files | `root:adm 0640`; auditd owns its size-based rotation and suspends on critical disk errors |
| `/var/log/wishkr` operational evidence | 30 days | `root:adm 0750`; cleanup is handled by systemd-tmpfiles |
| Trivy scan reports | 90 days | `root:adm 0640` files below a `0750` directory; cleanup is handled by systemd-tmpfiles |
| Sentry application exceptions | Sentry project policy | The application disables default PII, request bodies, stack locals, and scrubs Indian phone numbers before delivery |

Only `root`, the `syslog` service, and the trusted administrator should have
`adm` access. Grafana Alloy must belong only to its own group; it must not join
`adm`, `systemd-journal`, or `docker`, and its configuration must contain no
Loki components.

Never paste retained log contents into source control, tickets, or chat. Inspect
only the minimum relevant time window during an incident and redact personal
data, credentials, request identifiers, and customer content before sharing.

## Install and validate

Run from the reviewed release on the OVH host:

```bash
sudo ops/logging/install-log-retention.sh
sudo ops/logging/validate-log-retention.sh
```

The installer replaces the distribution Nginx rotation file with the reviewed
equivalent, installs the existing journald limits as source-controlled policy,
adds age-based cleanup for Wishkr operational evidence, and removes unintended
log-reader groups from Alloy. Validation reads configuration and metadata only;
it does not print log contents.

Loki can be reconsidered after cutover only if a separately reviewed pipeline
selects explicit fields, drops message bodies and secrets before transmission,
uses least-privilege credentials, defines tenant access, and sets a bounded
remote retention period.
