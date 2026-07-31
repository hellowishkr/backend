# Wishkr OVHcloud Bare-Metal Recovery Runbook

## Purpose and recovery objectives

Use this runbook after total loss or reinstallation of the OVHcloud Mumbai
production server. It rebuilds the Docker Compose deployment and restores
PostgreSQL, Redis, application configuration, and the active release from a
client-side encrypted restic backup.

- Backup schedule: daily at `02:15 UTC`, with up to ten minutes of random
  delay and persistent catch-up.
- Maximum accepted backup age: 30 hours.
- Backup retention: 7 daily, 5 weekly, 12 monthly, and 3 yearly snapshots.
- Recovery-point objective: no more than one scheduled backup interval
  (normally 24 hours).
- Initial recovery-time target: four hours after replacement hardware and
  provider access are available. Record the measured time after the first
  full bare-metal exercise.

This procedure is destructive only when it explicitly says to start with new
Docker volumes. Never run those steps against a surviving production host
without an approved incident decision.

## Recovery sources

Use Cloudflare R2 first because it is independent of OVHcloud. Use the
OVHcloud Backup Storage copy if R2 is unavailable or its latest snapshot
fails verification.

| Priority | Repository | Location | Encryption |
|---:|---|---|---|
| 1 | Cloudflare R2 bucket `wishkr-db-backup`, prefix `restic/ovh-prod` | APAC/off-provider | Independent restic password |
| 2 | OVHcloud Backup Storage path `wishkr/restic/ovh-prod` | BHS/OVHcloud | Independent restic password; explicit FTPS transport |

The repositories contain encrypted copies of:

- a PostgreSQL custom-format dump and checksum;
- a Redis RDB and checksum;
- `/etc/wishkr`, Nginx and WireGuard configuration;
- the active immutable application release.

## Information that must exist outside the failed server

Before declaring disaster recovery ready, confirm that two authorized people
can obtain all of the following without using the OVH host:

- OVHcloud and Cloudflare account access, MFA, and recovery codes;
- an administrator SSH private key and its passphrase;
- access to the private GitHub repository `hellowishkr/backend`;
- the R2 Access Key ID and Secret Access Key for the bucket-scoped production
  backup token;
- the R2 restic repository password;
- the OVH restic repository password;
- permission to reset the OVH Backup Storage FTP password in OVH Manager;
- Cloudflare DNS access and the documented origin-certificate material.

Never paste these values into tickets, chat, shell arguments, shell history,
or this repository. Enter them only through the hidden prompts provided by
the credential scripts.

## Phase 1: provision and secure the replacement host

1. In OVH Manager, install Ubuntu Server 24.04 LTS on both NVMe disks using
   software RAID1. Do not use JBOD or RAID0.
2. Confirm normal disk boot, RAID membership, synchronization state, and
   usable capacity:

   ```bash
   cat /proc/mdstat
   lsblk -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINTS
   sudo mdadm --detail /dev/md2
   sudo mdadm --detail /dev/md3
   ```

3. Recreate the named sudo administrator and install only approved Ed25519
   public keys. Verify a second SSH session before changing SSH policy.
4. Recreate the WireGuard administrative VPN and confirm VPN SSH. Preserve
   OVH IPMI/KVM or rescue-mode access throughout the rebuild.
5. Apply Items 3 through 6 of
   [OVHCLOUD_MIGRATION_SECURITY_CHECKLIST.md](./OVHCLOUD_MIGRATION_SECURITY_CHECKLIST.md):
   patching, unattended security updates, SSH hardening, IPv4/IPv6 UFW policy,
   OVH Edge Network Firewall rules, Fail2ban, auditd, AppArmor, SMART, mdadm,
   AIDE, and the validated sysctl/module policy.
6. Install Docker Engine and the security policy from the reviewed repository.
   Do not expose PostgreSQL, Redis, or Docker management ports.

## Phase 2: obtain reviewed recovery tooling

Clone the private repository over the administrative VPN, then check out a
known-good commit recorded in the incident log. Do not recover from an
unreviewed branch tip.

```bash
sudo install -d -o root -g root -m 0755 /opt/wishkr/releases
sudo git clone https://github.com/hellowishkr/backend.git \
  /opt/wishkr/releases/recovery-source
cd /opt/wishkr/releases/recovery-source
sudo git checkout --detach <KNOWN_GOOD_COMMIT_SHA>
sudo apt-get update
sudo apt-get install --yes restic rclone lftp
```

Review the checked-out commit hash and run `bash -n` on the backup scripts
before entering credentials.

## Phase 3A: recover the encrypted snapshot from R2

Run the R2 helper in existing-repository mode. That mode prompts separately
for the R2 API credentials and the saved restic password, with input hidden.
It does not generate a new repository password.

```bash
cd /opt/wishkr/releases/recovery-source
sudo env WISHKR_EXISTING_RESTIC_REPOSITORY=1 \
  ./ops/backup/configure-r2-credentials.sh \
  https://e67f5178091bc01bd4c5b07371194ca0.r2.cloudflarestorage.com \
  wishkr-db-backup
sudo install -d -o root -g root -m 0700 \
  /var/backups/wishkr/bare-metal-recovery
sudo bash -c 'set -a; source /etc/wishkr/secrets/r2-backup.env; set +a; restic check --read-data'
sudo bash -c 'set -a; source /etc/wishkr/secrets/r2-backup.env; set +a; restic restore latest --target /var/backups/wishkr/bare-metal-recovery'
```

Stop if `restic check --read-data` or restore reports any error.

## Phase 3B: recover from OVH Backup Storage instead

In OVH Manager, reset the Backup Storage password and wait until the new
credential is active. Then use the helper's existing-repository mode; both
password prompts are hidden.

```bash
cd /opt/wishkr/releases/recovery-source
sudo env WISHKR_EXISTING_RESTIC_REPOSITORY=1 \
  ./ops/backup/configure-ovh-ftps-credentials.sh \
  ftpback-bhs6-105.ip-54-39-19.net \
  ns5021588.ip-148-113-6.net
sudo install -d -o root -g root -m 0700 \
  /var/backups/wishkr/ovh-repository \
  /var/backups/wishkr/bare-metal-recovery
sudo bash -c 'set -a; source /etc/wishkr/secrets/ovh-backup.env; set +a; rclone copy "$OVH_RCLONE_DESTINATION" /var/backups/wishkr/ovh-repository --checkers 1 --transfers 1'
sudo env \
  RESTIC_REPOSITORY=/var/backups/wishkr/ovh-repository \
  RESTIC_PASSWORD_FILE=/etc/wishkr/secrets/restic_ovh_password \
  restic check --read-data
sudo env \
  RESTIC_REPOSITORY=/var/backups/wishkr/ovh-repository \
  RESTIC_PASSWORD_FILE=/etc/wishkr/secrets/restic_ovh_password \
  restic restore latest --target /var/backups/wishkr/bare-metal-recovery
```

The FTPS helper converts the raw OVH hostname to its verified
`*.mybackup.ovh.ca` TLS hostname. Never enable `no_check_certificate`, plain
FTP, or anonymous access.

## Phase 4: inspect and stage restored material

1. Locate and validate the manifest, PostgreSQL dump, and Redis RDB:

   ```bash
   sudo find /var/backups/wishkr/bare-metal-recovery \
     -type f \( -name manifest.txt -o -name wishkr-postgres.dump -o -name wishkr-redis.rdb \)
   sudo docker run --rm --network none \
     --volume <ABSOLUTE_REDIS_RDB_PATH>:/recovery/wishkr-redis.rdb:ro \
     --entrypoint redis-check-rdb \
     redis:7-alpine /recovery/wishkr-redis.rdb
   ```

   Bind-mount the located Redis file at `/recovery/wishkr-redis.rdb:ro` when
   running the second command. Verify the two SHA-256 values against the
   restored `manifest.txt`; do not rely on filenames alone.
2. Compare the recovered release and configuration with the known-good Git
   commit. Prefer the reviewed Git release if they differ unexpectedly.
3. Restore only the required application secrets and certificate files from
   the staged `etc/wishkr` tree. Do not overwrite the newly entered R2/OVH
   backup credentials or restic password files.
4. Enforce `root:root 0700` on `/etc/wishkr/secrets` and `root:root 0600` on
   every secret file. Confirm that no secret is present in shell history or
   deployment logs.
5. Point `/opt/wishkr/current` to the reviewed release only after all paths
   have been inspected.

## Phase 5: restore PostgreSQL and Redis into new volumes

1. Prepare host directories and the external Docker networks:

   ```bash
   cd /opt/wishkr/current
   sudo ./ops/host-security/docker/ensure-wishkr-networks
   sudo ./ops/deployment/prepare-host.sh
   sudo docker network inspect wishkr-app >/dev/null
   sudo docker network inspect wishkr-data >/dev/null
   ```

2. Validate Compose without printing the rendered environment. Build and scan
   the PostgreSQL and backend images using the Item 8 release gate.
3. Start only PostgreSQL first; do not start the web or scheduler processes:

   ```bash
   sudo docker compose \
     --env-file /etc/wishkr/secrets/production.env \
     -f /opt/wishkr/current/docker-compose.ovh.yml \
     up -d db
   sudo docker inspect --format '{{.State.Health.Status}}' wishkr-db-1
   ```

4. Restore the custom-format dump into the new empty `wishkr` database using
   the non-superuser application role. The exact restore command used by the
   tested drill is maintained in `ops/backup/test-r2-restore.sh`. Require
   `--no-owner`, `--no-privileges`, and `--exit-on-error`.
5. Before starting Redis, place the validated `wishkr-redis.rdb` into the new
   `wishkr_redisdata` Docker volume as `/data/dump.rdb`, mode `0600`, owned by
   UID `999` and GID `1000`. Never copy into a running Redis volume.
6. Start Redis and require healthy authenticated `PING`, successful RDB load,
   and successful AOF initialization.
7. Run `ops/database/apply-postgres-role-policy.sh` and
   `ops/database/validate-data-services.sh`. Confirm that all application
   relations are owned by `wishkr`, the application role is not privileged,
   and the expected public-table count is present.

## Phase 6: start and validate the application

1. Start the backend and scheduler from `/opt/wishkr/current` with the
   reviewed Compose file.
2. Run `ops/deployment/validate-runtime.sh` and the R2 application-canary
   validation. Confirm exactly one scheduler and its PostgreSQL advisory lock.
3. Install the reviewed Nginx configuration and Cloudflare origin certificate.
   Require Full (strict) TLS and Authenticated Origin Pulls. Keep direct-origin
   access restricted to Cloudflare ranges.
4. Validate the loopback application and Nginx configuration before DNS
   changes. Authenticated Origin Pulls intentionally prevent an ordinary
   direct-origin HTTPS request from succeeding without Cloudflare's client
   certificate:

   ```bash
   curl --fail --silent --show-error http://127.0.0.1:8000/health
   sudo nginx -t
   openssl s_client -connect 127.0.0.1:443 -servername api.wishkr.com \
     -CAfile /etc/wishkr/tls/api.wishkr.com-origin.pem </dev/null
   ```

5. Verify webhook signature handling, one read-only application workflow, and
   one controlled asynchronous/scheduler workflow. Do not send production
   customer messages during the recovery test.
6. Change the proxied Cloudflare DNS record only after all local health gates
   pass, then immediately require a successful external
   `https://api.wishkr.com/health` request through Cloudflare. Monitor errors
   and latency continuously during the cutover. Preserve the previous origin
   until rollback is no longer required.

## Phase 7: reinstate backups and close the incident

Install and enable the backup and hourly monitor timers:

```bash
cd /opt/wishkr/current
sudo ./ops/backup/install-backup-service.sh
sudo systemctl start wishkr-backup.service
sudo /usr/local/sbin/wishkr-backup-status
sudo /usr/local/sbin/wishkr-test-backup-restore r2
sudo /usr/local/sbin/wishkr-test-backup-restore ovh
sudo systemctl list-timers \
  wishkr-backup.timer wishkr-backup-monitor.timer --no-pager
```

Record the selected snapshot ID and time, table count, checksum results,
recovery duration, validation evidence, DNS change, and incident approver.
Delete plaintext staging data only after the restored production system and
both encrypted destinations have passed verification.

## Abort and escalation conditions

Stop the recovery and escalate if any of these occur:

- RAID1 is degraded or synchronizing with unexpected errors;
- the selected restic repository fails full data verification;
- artifact checksums do not match the restored manifest;
- PostgreSQL restore exits non-zero or the public-table count differs;
- Redis validation fails or starts with persistence errors;
- a secret appears in terminal output, logs, shell history, or the repository;
- more than one scheduler becomes active;
- origin TLS, Authenticated Origin Pulls, or Cloudflare-only access fails;
- required business validation cannot be completed without risking customer
  data or sending unintended messages.
