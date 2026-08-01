# OVHcloud Production Cutover Plan — 2026-08-01

> **Planning only. No cutover action is authorized by this document.**
>
> The account owner's 1 August 2026 `GO` completed Item 13 and authorized
> preparation of this plan only. Lowering TTLs, stopping Hetzner services,
> taking the final production synchronization, changing Cloudflare DNS,
> starting the OVH scheduler, exercising live customer/provider flows, and
> rolling back all require a new, phase-specific approval.

This runbook plans Item 14 of
[OVHCLOUD_MIGRATION_SECURITY_CHECKLIST.md](./OVHCLOUD_MIGRATION_SECURITY_CHECKLIST.md).
It contains no credentials, private keys, tokens, recovery codes, customer
records, database values, or secret-bearing commands.

## Status and scope

| Field | Value |
|---|---|
| Plan status | Approved through Gate D |
| Execution status | Gate D revalidated after hotfix; 60-minute observation runs 02:25:10–03:25:10 IST |
| Maintenance window | 2026-08-01, starting 01:26 IST |
| Maximum write freeze | 30 minutes |
| Source | Hetzner web/API, worker/scheduler, and PostgreSQL/Redis hosts |
| Destination | OVHcloud Mumbai bare-metal host |
| Traffic change | `api.wishkr.com` from DNS-only Hetzner to Cloudflare-proxied OVH |
| Primary operator/approver | Account owner |
| Independent second operator | Not available; tracked in Item 16 |

The frontend remains on Vercel. This plan changes only the API origin and the
backend, scheduler, PostgreSQL, and Redis runtime described in the checklist.
Hetzner remains intact as the rollback target through Item 15.

## Authorization gates

Use a separate approval for each externally visible or write-affecting phase.
An approval for one gate does not authorize the next gate.

| Gate | Approval phrase | What it authorizes |
|---|---|---|
| A | `EXECUTE ITEM 14 TTL PREPARATION` | Announce the window and lower the current DNS-only TTL |
| B | `EXECUTE ITEM 14 FREEZE AND FINAL SYNC` | Stop Hetzner scheduler/web writes, make final recovery points, and restore OVH |
| C | `EXECUTE ITEM 14 DNS CUTOVER` | Change the API record to the proxied OVH origin |
| D | `EXECUTE ITEM 14 LIVE VALIDATION` | Run the approved live test and start exactly one OVH scheduler |
| E | `EXECUTE ITEM 14 ROLLBACK` | Perform the applicable rollback branch after its data-impact review |

Read-only preflight checks may be collected before a gate, but they must not
change DNS, services, data, configuration, customer/provider state, or external
notifications. Never paste secret values into chat or the evidence record.

## Accepted planning decisions

1. **Use a hard write freeze.** The application has no maintenance/read-only
   switch. Stop the Hetzner scheduler first and the Hetzner web backend second,
   then confirm application database sessions are gone before making the final
   dump. This creates a short, explicit maintenance period and a clean database
   boundary.
2. **Use the tested PostgreSQL 16 custom-dump path.** Stream a root-only custom
   dump over encrypted SSH, validate it with the PostgreSQL 16 restore tools,
   restore into a fresh destination database with `--single-transaction`, and
   compare schema and aggregate fingerprints before activating it.
3. **Do not treat Redis as authoritative data.** Current Redis keys are bounded
   caches, rate limits, debounce state, and short-lived guards; durable webhook
   idempotency is stored in PostgreSQL. Clear the OVH staging cache during the
   final restore and allow it to repopulate. Do not copy the Hetzner Redis volume
   or mix its persistence files with the OVH AOF.
4. **Do not bulk-copy application objects.** Both environments use the existing
   Cloudflare R2 application object store. Final object-storage synchronization
   is a non-secret configuration-equivalence check, a least-privilege canary,
   and a read of a known public catalog object. It is not a bucket-to-bucket
   copy. The encrypted backup bucket is separate and is validated by the backup
   workflow.
5. **Keep the OVH scheduler stopped through restore, private validation, and DNS
   change.** Start it only after the Hetzner worker is confirmed stopped and the
   public API is healthy through Cloudflare. Require exactly one scheduler and
   PostgreSQL advisory lock `41051`.
6. **Do not delete or repurpose Hetzner during Item 14.** Keep all three source
   hosts and their data available but application processes stopped. Retirement
   belongs to Item 15 and requires written approval.

## Preconditions

Every row must be `PASS` immediately before Gate B. A changed or unknown result
is a no-go until explained.

| Precondition | Required result |
|---|---|
| Item 13 | Complete, with account-owner GO recorded |
| Production routing | Still targets the Hetzner origin before Gate C |
| Hetzner health | Web, worker, PostgreSQL, and Redis healthy before the freeze |
| OVH health | Backend, PostgreSQL, Redis, Nginx, monitoring, and backups healthy |
| OVH scheduler | Stopped intentionally; no live job can execute |
| Database schema | Source and destination use the reviewed release and Alembic head |
| Staging data | No scratch database or partial restore artifact remains |
| Backups | Both encrypted OVH destinations fresh; latest restore drills successful |
| Origin security | TLS hostname/date valid, Full (strict) staged, AOP required, direct origin rejected |
| Cloudflare rules | Cache bypass and signed-webhook exclusions still scoped as reviewed |
| Monitoring | Sentry uptime/error visibility and all installed Grafana rule groups operational |
| Rollback access | Hetzner, OVH, Cloudflare, and provider access confirmed without displaying credentials |
| Window | Start/end, expected impact, owner, abort criteria, and status channel recorded |

Item 16's missing independent second operator remains visible but does not
change this plan. Before execution, the account owner must explicitly confirm
that they will remain available for every approval gate and the observation
period.

## DNS preparation

The current `api.wishkr.com` record is DNS-only. At least one full current TTL
before the window—and preferably 24 hours before Gate B—record its existing
type, target, proxy state, and TTL, then lower its TTL to the lowest reviewed
value offered by the zone plan. Cloudflare currently documents a 60-second
minimum for non-Enterprise DNS-only records and a fixed `Auto` value of 300
seconds for proxied records. Once proxying is enabled, the TTL is not editable.

Do not add a second `A` or `AAAA` record for the same hostname as a migration
shortcut. Before Gate C, confirm there is one intended address record and no
stale IPv6 path. Retain the previous Hetzner target in the private change record
for rollback, but do not expose account identifiers or credentials.

## Execution sequence

### Phase 0 — Schedule and announce

This phase requires Gate A.

1. Choose a low-traffic window, preferably outside the scheduler's normal India
   recipient window of 06:30–22:00 IST.
2. Set a 30-minute write-freeze budget and a 60-minute intensive observation
   period. The account owner remains present for both.
3. Announce the API maintenance window, expected temporary unavailability, and
   the decision time for rollback. Do not promise zero downtime.
4. Lower the DNS-only TTL as described above and wait at least the previous TTL
   before relying on it.
5. Freeze unrelated deployments and schema changes until Item 14 completes or
   rolls back.

**Stop gate:** If the window, approval owner, access paths, or rollback target
is uncertain, do not begin the service freeze.

### Phase 1 — Final read-only preflight

Complete these checks no more than 30 minutes before Gate B:

1. Record UTC and IST start times and the reviewed release identifier on both
   platforms.
2. Confirm production DNS and public health still point to the working Hetzner
   path. Do not print environment files.
3. Confirm the Hetzner web process has scheduling disabled and the worker is the
   only active source scheduler.
4. Confirm OVH backend health and verify the OVH scheduler is still stopped.
5. Validate OVH Nginx, certificate validity/hostname, Cloudflare firewall
   admission, PostgreSQL/Redis hardening, RAID, free space, and zero failed
   systemd units.
6. Confirm backup timers and Grafana Alloy are operational with no failed remote
   writes. Note that the intentionally stopped OVH scheduler alert is expected.
7. Confirm the final dump destination has enough root-only storage and no stale
   `.partial` artifact.

**Stop gate:** Any unexplained health, backup, storage, TLS, firewall, schema,
or monitoring failure is a no-go.

### Phase 2 — Freeze the source

This phase requires Gate B.

1. Record `freeze_started_at` in UTC and announce maintenance start.
2. Stop the Hetzner worker/scheduler first. Confirm the container is not running
   and the scheduler advisory-lock session is absent.
3. Stop the Hetzner web backend second. Keep Nginx present so its status can be
   distinguished from an unreachable host, but do not restart the backend while
   the freeze is active.
4. Confirm no application-role sessions remain in PostgreSQL and no active
   application transaction can commit. Administrative backup sessions are the
   only permitted new sessions.
5. Wait through one short webhook/debounce interval, then recheck sessions and
   source logs for unexpected writes without displaying payloads or customer
   data.

**Abort branch:** If either process cannot be stopped cleanly or a writer
remains, do not dump. Restore the source processes in web-then-scheduler order,
verify one scheduler, announce the abort, and reschedule.

### Phase 3 — Create final recovery points and synchronize data

1. Stop the OVH backend and verify its scheduler remains stopped. Keep only
   PostgreSQL and Redis running for the backup/restore work, with no application
   process able to write.
2. Run and verify one encrypted OVH backup before overwriting the staged copy.
   Both configured destinations must succeed.
3. Create the final PostgreSQL 16 custom-format dump from the frozen Hetzner
   database. Store it root-only, calculate a checksum, and validate its archive
   list without printing data or connection strings.
4. Stream the dump over encrypted SSH into a root-only temporary file on OVH.
   Use an atomic `.partial`-to-final rename only after transfer success and
   checksum equality. Do not leave a workstation copy.
5. Record only safe aggregate evidence: archive size, checksum match result,
   public-table count, Alembic revision equality, and a hash of a sorted
   per-table row-count manifest. Do not record row values or the counts
   themselves. The known Item 13 schema has 54 public tables; any change must
   be explained by a reviewed release before proceeding.
6. Restore into a new, uniquely named empty OVH database owned by the
   non-superuser application role. Require `--no-owner`, `--no-privileges`,
   `--exit-on-error`, and `--single-transaction`.
7. Validate the restored database before activation: archive success, expected
   schema, Alembic head, row-count-manifest hash, sequence coverage, relation
   ownership, and application-role restrictions.
8. With both OVH application processes still stopped, perform the reviewed
   database-name swap. Preserve the previous Item 13 staging database under a
   clearly non-production name until Item 14 is accepted.
9. Reapply the PostgreSQL role policy and run the complete data-service
   validator.
10. Clear only the OVH Redis staging data, retain its hardened configuration and
   persistence settings, and confirm authenticated readiness. Let catalog and
   runtime caches repopulate from PostgreSQL.
11. Compare the source and OVH application-R2 endpoint/bucket/public-base
    fingerprints without printing their values. Require equality, run the
    existing random put/get/list/delete canary, and verify a known public
    catalog object. Do not copy or delete production objects.

**Stop gate:** A transfer, checksum, restore, schema, aggregate-data, sequence,
role-policy, Redis, or R2 mismatch blocks Gate C. Because the source remains
frozen and unchanged, the safe response is to stop OVH processes, resume the
Hetzner web and exactly one scheduler, and announce the aborted window.

### Phase 4 — Start and validate OVH privately

1. Confirm OVH PostgreSQL and Redis remain healthy, then start the backend only.
   Keep the scheduler stopped.
2. Require healthy containers, authenticated PostgreSQL and Redis queries,
   loopback-only backend exposure, loopback `/health` `200`, and no restart or
   OOM event.
3. Run Nginx configuration validation and the VPN/private TLS health path.
   Require the expected hostname, certificate validity, TLS 1.2/1.3, AOP request
   gate, and rejection of an unauthorized direct-origin request.
4. Run only non-mutating application checks: health, taxonomy, active products,
   pricing, one product detail, and Redis catalog-cache TTLs.
5. Require unsigned WhatsApp and Razorpay payloads to be rejected. Do not send a
   positive provider event or customer message in this phase.
6. Confirm no scratch database or partial transfer remains and that monitoring
   continues to report the expected destination state.

**Stop gate:** Do not change DNS unless all private checks pass while the OVH
scheduler remains stopped.

### Phase 5 — Switch Cloudflare traffic

This phase requires Gate C.

1. Reconfirm the source is still frozen, the OVH backend is healthy, the OVH
   scheduler is stopped, and the rollback record contains the previous DNS
   state.
2. Edit the single `api.wishkr.com` address record in place: change its target
   to the reviewed OVH origin and enable Cloudflare proxying. The proxied TTL
   becomes `Auto`; do not create parallel address records.
3. Confirm authoritative DNS returns Cloudflare anycast addresses rather than
   either origin address. Check more than one resolver, while allowing for
   cache propagation.
4. Require three consecutive external `https://api.wishkr.com/health` successes
   through Cloudflare and confirm Sentry uptime follows the new path.
5. Confirm the public response uses the expected certificate, redirects preserve
   method/path where applicable, cache is bypassed for API/webhook routes, and
   the OVH Nginx metrics log sees the requests.
6. Re-run the public read-only catalog checks and negative signature tests.

**Immediate rollback trigger:** Cloudflare `52x`, certificate/hostname failure,
AOP failure, direct-origin bypass, wrong origin, or repeated public-health
failure. Use the no-destination-write rollback branch if destination writes are
still proven absent.

### Phase 6 — Live integration and scheduler transition

This phase requires Gate D. It must name the one permitted test identity and
expected external effect without placing that identity in this document.

1. Confirm the Hetzner worker remains stopped and its scheduler lock is absent.
2. Run one account-owner-approved WhatsApp test from a controlled test number.
   Verify signed inbound processing, one expected response, durable idempotency,
   and Sentry visibility. Do not replay a payment event or use customer data.
3. Start only `wishkr-scheduler-1` on OVH.
4. Require one scheduler container, healthy readiness, the expected seven MVP
   jobs, acquisition of advisory lock `41051`, and no second holder. Run
   `ops/deployment/validate-runtime.sh` only after this point because that
   validator intentionally requires the scheduler.
5. Inspect scheduler logs for startup errors and unexpected immediate sends.
   Do not manually invoke a job. Confirm scheduled next-run times are sensible.
6. Confirm the Hetzner web and scheduler remain stopped. Keep its database and
   Redis available only as the rollback source.

**Immediate rollback trigger:** More than one scheduler, missing singleton
lock, duplicate live action, unexpected outbound action, failed signature
validation, or a provider flow that writes inconsistent state.

### Phase 7 — Observe and close Item 14

Observe continuously for at least 60 minutes before declaring Item 14 complete:

- Sentry uptime and application errors;
- Nginx 5xx/429/499 counts, request volume, and active connections;
- PostgreSQL readiness, recovery mode, connection use, and slow/error logs;
- Redis readiness, RDB/AOF status, loading, memory, and evictions;
- container health, restarts, OOM events, CPU, memory, disk, I/O, RAID, and NVMe;
- scheduler singleton/lock, job errors, next-run times, and unexpected sends;
- Cloudflare origin/TLS events and webhook delivery behaviour;
- payment/order reconciliation state using safe aggregate checks only.

At 60 minutes, choose exactly one result:

- **Accept Item 14:** all gates pass; leave Hetzner stopped but retained and
  begin the Item 15 stability period.
- **Rollback:** a threshold below is met; follow the correct data-divergence
  branch.
- **Hold:** service is safe but evidence is inconclusive; keep OVH serving,
  keep Hetzner stopped, and extend observation. Do not alternate DNS targets.

## Abort and rollback criteria

### Immediate abort or rollback

- a source writer remains after the freeze;
- final backup, transfer, checksum, restore, schema, aggregate-data, or sequence
  validation fails;
- PostgreSQL or Redis is not ready, Redis persistence reports failure, or
  PostgreSQL unexpectedly enters recovery;
- origin TLS, hostname validation, Full (strict), AOP, or Cloudflare-only access
  fails;
- unsigned Meta or Razorpay input is accepted;
- public health is continuously unavailable for two minutes or fails three
  consecutive one-minute probes;
- more than one scheduler is active, the OVH scheduler lacks advisory lock
  `41051`, or any duplicate/unexpected live action is observed;
- a secret or customer record appears in logs, terminal output, chat, or the
  repository;
- data integrity is uncertain.

### Five-minute rollback thresholds

- at least five Nginx 5xx responses and a 5xx ratio above 5% for five minutes,
  matching the installed critical Grafana rule;
- p95 API latency above 1 second or above twice the comparable pre-cutover
  baseline for five minutes, excluding a confirmed provider-side delay;
- PostgreSQL connection use above 80% with errors or blocked work, Redis
  instability, repeated container restarts, or sustained resource pressure;
- webhook/provider failures that cause a growing retry/backlog condition or
  prevent safe order/message processing.

High traffic alone (`>9,000` requests in five minutes), more than 100 rate-limit
responses in five minutes, or more than 50 client aborts in five minutes is an
investigation warning. It becomes a rollback condition only when correlated
with customer impact, origin instability, or resource exhaustion.

## Rollback branches

### Branch 1 — Before Gate C

No public traffic reached OVH. Stop any OVH application process, leave its
scheduler stopped, preserve failure evidence, start the Hetzner web backend,
then start exactly one Hetzner scheduler and verify its lock. Production DNS is
unchanged.

### Branch 2 — After Gate C with no OVH write

Stop the OVH backend and scheduler, confirm no destination write occurred,
start the Hetzner web backend and verify it locally, restore the previous DNS
target/proxy state, wait for authoritative/public health on Hetzner, then start
exactly one Hetzner scheduler. Keep the failed OVH copy for investigation.

### Branch 3 — After any OVH write

Do not simply point DNS back: that would lose or split new production data.

1. Stop the OVH scheduler and backend to establish a second hard freeze.
2. Preserve an encrypted OVH backup and a root-only custom dump.
3. Compare safe aggregate database fingerprints and determine whether OVH
   accepted authoritative writes.
4. Obtain Gate E approval for reverse synchronization.
5. Restore the complete frozen OVH database into a fresh Hetzner database,
   validate it with the same schema/aggregate/sequence checks, and activate it
   while both application stacks remain stopped.
6. Because application R2 is shared, do not reverse-copy objects. Validate its
   configuration and referenced object availability.
7. Start Hetzner web and verify it locally, restore the previous DNS target,
   verify Hetzner public health, then start exactly one Hetzner scheduler and
   verify its lock.

If it is unclear which database is authoritative, keep both schedulers and both
web backends stopped, preserve both datasets, and treat the event as a data
integrity incident. Do not flap DNS or merge rows manually during the outage.

## Evidence record

Record only outcomes and non-sensitive identifiers.

| Evidence | Value |
|---|---|
| Window start/end (UTC and IST) | Start: 2026-08-01 01:26 IST / 2026-07-31 19:56 UTC; write freeze ended 01:52 IST |
| Gate approver and timestamps | Gate A: account owner, 01:33 IST; Gate B: before 01:39 IST; Gate C: before 01:52 IST; Gate D: before 01:57 IST |
| Source/destination reviewed release IDs | `[TBD]` |
| Source freeze timestamp | 2026-08-01 01:39:33 IST / 2026-07-31 20:09:33 UTC |
| Hetzner web/scheduler stopped | Pass; no client writer and no scheduler lock |
| Final encrypted backup result | Pass; both configured destinations |
| Dump archive/checksum/list validation | Pass; root-owned mode `0600`, 531,237 bytes |
| Schema/Alembic/aggregate fingerprint match | Pass; 54 tables, row and sequence fingerprints match |
| PostgreSQL role policy and datastore validation | Pass |
| R2 equivalence/canary/public-object validation | Configuration and canary pass; shared store, no bulk copy |
| Private OVH validation | Pass; backend healthy, scheduler stopped |
| DNS origin record changed/proxied | Pass; Cloudflare anycast only, TTL `300` |
| External TLS/health/catalog/signature validation | TLS, health, catalog and unsigned-signature rejection pass |
| WhatsApp controlled test result | Pass: one signed inbound, one successful reply, one durable `done` record, zero processing/rate-limit/backpressure errors |
| Singleton scheduler/lock validation | Pass after hotfix: one healthy scheduler, seven MVP jobs, one idle lock holder with no open transaction through 149 seconds, zero errors/sends, full runtime validator pass |
| 60-minute monitoring result | In progress from 02:25:10 through 03:25:10 IST; baseline passed |
| Item 14 outcome | Gate D passed; acceptance pending completion of the observation window |

Gate A evidence (2026-08-01): the account owner selected an immediate window
starting at 01:26 IST and authorized TTL preparation. At 01:33 IST, Cloudflare
authoritative DNS, `1.1.1.1`, and `8.8.8.8` each returned the unchanged Hetzner
address with a 60-second TTL. The origin target and DNS-only proxy state were
unchanged, and public health remained `200`. The previous cache lifetime must
be allowed to expire through 01:39 IST before Gate B can begin.

Gate B evidence (2026-08-01): the freeze began at 01:39:33 IST. The Hetzner
scheduler and web backend are stopped; PostgreSQL and Redis remain healthy with
no client writer or scheduler lock. A final encrypted OVH pre-overwrite backup
succeeded. The frozen PostgreSQL 16 custom archive was validated, streamed over
encrypted SSH, checksum-matched, and restored into a fresh OVH database. All 54
tables, Alembic revision, per-table row-count fingerprint, and sequence-state
fingerprint matched. The final database was activated while the prior Item 13
database was preserved with connections disabled. PostgreSQL role policy,
Redis reset, datastore hardening, R2 configuration/canary, private health,
catalog/cache, signature rejection, exposure, Alloy, backup freshness, and host
health passed. At 01:49:18 IST, DNS still targeted Hetzner and the OVH scheduler
remained stopped. Gate C is not yet authorized.

Gate C evidence (2026-08-01): the account owner changed the existing API
address record in place to the reviewed OVH origin and enabled proxying.
Cloudflare authoritative DNS, `1.1.1.1`, and `8.8.8.8` returned only Cloudflare
anycast addresses with TTL `300`. Public Cloudflare health passed three
consecutive probes; health, taxonomy, products, pricing, and product detail
returned `200`; the cache status was `DYNAMIC`; unsigned WhatsApp returned
`403`; HTTP redirected with `308`; and unauthorized direct-origin access
remained blocked. OVH Nginx recorded zero 5xx responses with successful status
and metrics-log collection, Alloy was ready, and the host had zero failed
units. The write freeze ended at approximately 01:52 IST after about 13 minutes.
Hetzner web/scheduler remain stopped and the OVH scheduler remains stopped
pending Gate D.

Gate D evidence (2026-08-01): after the account owner's authorization, the
Hetzner worker was reconfirmed exited with zero advisory-lock holders. One
controlled `hi` reached the OVH backend as exactly one signed inbound, produced
exactly one successful Meta send, and left exactly one durable processed-message
record in `done`; no webhook, rate-limit, or backpressure error was recorded.
The OVH scheduler started at 02:03:28 IST with one healthy container, one lock
holder, seven MVP jobs, zero startup errors, and zero unexpected sends. The
complete runtime validator passed and Hetzner web/scheduler remained stopped.

At 02:06:02 IST, the first observation checkpoint found the scheduler still
healthy but lock `41051` no longer held. No job error or outbound send occurred.
The scheduler was stopped at 02:06:30 IST. PostgreSQL recorded one
idle-in-transaction timeout: SQLAlchemy's lock-acquisition `SELECT` had
autobegun a transaction, and the hardened one-minute
`idle_in_transaction_session_timeout` terminated that session. The safe code
remediation commits immediately after acquiring the session-level advisory
lock (and after explicit release); its focused regression suite passes `4/4`
locally. The passed WhatsApp test was not repeated.

The account owner authorized the narrow hotfix deployment and Gate D scheduler
retry. A new release was copied from the deployed Item 8 release with exactly
one changed file, checksum-matched to committed `app/main.py`, secret-scanned,
and built into a vulnerability-scanned image. The previous release link and
image remain available for rollback. The running backend was not recreated.
The OVH scheduler restarted at 02:22:13 IST from the hotfix image. It registered
seven MVP jobs, became healthy with zero restarts, and retained exactly one
advisory-lock holder through checkpoints at 49, 74, 99, 124, and 149 seconds.
The lock session was `idle` with no open transaction; PostgreSQL recorded no
idle-in-transaction termination. There were zero scheduler errors or outbound
sends. The full runtime validator passed, while Hetzner web/scheduler remained
stopped with no lock. The official 60-minute observation began at 02:25:10 IST;
its baseline passed public health, containers, PostgreSQL, Redis, Nginx, TLS,
Alloy, RAID, NVMe, queue, and aggregate payment/order safety checks.

Do not remove final dump/backup artifacts, the preserved destination staging
database, or any source host during Item 14. Cleanup and retirement decisions
belong to Item 15.

## Communication templates

### Planned maintenance

> Wishkr API maintenance is planned for `[window]`. API and WhatsApp responses
> may be temporarily unavailable for up to 30 minutes while production data is
> moved to the Mumbai environment. The rollback decision point is `[time]`.

### Cutover accepted

> Wishkr API migration completed at `[time]`. Health, TLS, data, webhook, and
> scheduler checks passed. Enhanced monitoring remains active during the
> stability period.

### Cutover rolled back

> The migration was rolled back at `[time]` after a predefined safety condition
> was met. The previous production environment is serving traffic. Data and
> logs are preserved for review; no retirement action will occur.

## Source-controlled references

- [Migration security checklist](./OVHCLOUD_MIGRATION_SECURITY_CHECKLIST.md)
- [Migration session handoff](./OVHCLOUD_MIGRATION_SESSION_HANDOFF_2026-07-31.md)
- [Bare-metal recovery runbook](./OVHCLOUD_BARE_METAL_RECOVERY_RUNBOOK.md)
- [OVH deployment procedure](../ops/deployment/README.md)
- [Runtime validator](../ops/deployment/validate-runtime.sh)
- [Backup procedure](../ops/backup/README.md)
- [Nginx and Cloudflare origin procedure](../ops/nginx/README.md)
- [Cloudflare DNS TTL reference](https://developers.cloudflare.com/dns/manage-dns-records/reference/ttl/)
- [Cloudflare proxy-status reference](https://developers.cloudflare.com/dns/proxy-status/)
