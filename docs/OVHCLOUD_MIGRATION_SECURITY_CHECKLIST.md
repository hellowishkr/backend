# OVHcloud Mumbai Migration and Security Checklist

> Living runbook for migrating Wishkr from Hetzner to the OVHcloud Mumbai
> bare-metal server. Update this document after every completed task.

## Document control

| Field | Value |
|---|---|
| Status | In progress |
| Current gate | Item 14 — Production cutover planning only |
| Last updated | 2026-08-01 |
| Target environment | OVHcloud Mumbai bare metal |
| Source architecture | [PRODUCTION_3_SERVER_SETUP.md](./PRODUCTION_3_SERVER_SETUP.md) |

### Status legend

- `[ ]` Not started
- `[~]` In progress
- `[x]` Completed and verified
- `[!]` Blocked or requires a decision

### Execution rule

Complete and verify one numbered item before starting the next one. Do not mark an
item complete merely because configuration was attempted. Record the verification
evidence and any operational decisions in the change log.

Never place passwords, private keys, recovery codes, tokens, database credentials,
or other secret values in this document.

## Target server

| Component | Specification |
|---|---|
| Plan | OVHcloud RISE-1 |
| Location | Mumbai, India |
| CPU | Intel Xeon E-2386G, 6 cores / 12 threads |
| Memory | 64 GB DDR4 ECC 3200 MHz |
| Storage | 2 × 512 GB enterprise/datacenter NVMe SSD |
| Storage layout | Software RAID1 |
| Expected usable storage | Approximately 477 GiB before filesystem/system overhead |
| Public network | 1 Gbps, 25 TB monthly traffic |
| Private network | 1 Gbps guaranteed/unmetered, where supported by the service |
| Operating system | Ubuntu Server 24.04 LTS minimal |

RAID1 protects against a single disk failure. It does not protect against deletion,
filesystem corruption, ransomware, server failure, or a datacenter outage.

## Planned deployment model

- Nginx on the host as the public reverse proxy.
- Docker Compose for the application services.
- PostgreSQL 16 and Redis 7 on private Docker networks with no public ports.
- FastAPI web containers reachable only through Nginx.
- A separate APScheduler container/process with exactly one active scheduler.
- Cloudflare-proxied `api.wishkr.com` using Full (strict) TLS.
- Cloudflare R2 plus an additional backup destination for recovery.
- The existing frontend remains on Vercel unless a separate migration is approved.

## Security responsibility boundary

| Area | Owner | Responsibility |
|---|---|---|
| Datacenter and physical hardware | OVHcloud | Power, physical security, network infrastructure, and failed-hardware intervention |
| Network DDoS mitigation | OVHcloud | Default network analysis and attack scrubbing |
| Edge Network Firewall | Shared | OVHcloud provides the facility; Wishkr defines, tests, and maintains the rules |
| KVM/IPMI and rescue mode | OVHcloud | Out-of-band and recovery access |
| Software RAID | Shared | OVHcloud supplies the disks/install option; Wishkr selects RAID1 and monitors its health |
| Operating system | Wishkr | Installation, patching, kernel maintenance, services, and reboot policy |
| SSH and administrator access | Wishkr | Keys, accounts, sudo permissions, restrictions, and access revocation |
| Host and Docker firewalls | Wishkr | Inbound rules, forwarding policy, Docker rules, IPv4, and IPv6 |
| Containers and applications | Wishkr | Images, permissions, secrets, resource limits, and vulnerability management |
| PostgreSQL and Redis | Wishkr | Authentication, network isolation, maintenance, backup, and restoration |
| Backups | Wishkr | Jobs, encryption, retention, monitoring, off-site copies, and restore testing |
| Monitoring and incident response | Wishkr | Alerts, logs, escalation, containment, and recovery |
| Cloudflare | Shared | Cloudflare protects proxied hostnames; Wishkr must proxy and secure the API origin correctly |

### Provider references

- [OVHcloud Network Security Dashboard and anti-DDoS](https://help.ovhcloud.com/csm/en-au-network-security-dashboard?id=kb_article_view&sysparm_article=KB0060692)
- [OVHcloud dedicated-server security](https://help.ovhcloud.com/csm/en-ie-dedicated-servers-securing-server?id=kb_article_view&sysparm_article=KB0043971)
- [OVHcloud software RAID](https://help.ovhcloud.com/csm/en-ca-dedicated-servers-raid-soft?id=kb_article_view&sysparm_article=KB0043936)
- [Cloudflare proxied DNS records](https://developers.cloudflare.com/learning-paths/prevent-ddos-attacks/baseline/proxy-dns-records/)
- [Cloudflare Authenticated Origin Pulls](https://developers.cloudflare.com/ssl/origin-configuration/authenticated-origin-pull/)

## Cloudflare protection requirements

Cloudflare protection for the frontend does not automatically protect
`api.wishkr.com`. The API hostname must also be orange-cloud proxied.

- [ ] Proxy `api.wishkr.com` through Cloudflare.
- [ ] Use Full (strict) TLS.
- [ ] Install and automatically renew a valid origin certificate.
- [ ] Configure Authenticated Origin Pulls.
- [ ] Restrict origin HTTPS access to Cloudflare IP ranges where operationally safe.
- [ ] Trust `CF-Connecting-IP` only when the request came from a Cloudflare address.
- [ ] Disable caching for API and webhook routes.
- [ ] Exempt `/webhooks/whatsapp` from browser challenges and incompatible bot rules.
- [ ] Retain application-level Meta webhook signature validation.
- [ ] Rate-limit sensitive endpoints without disrupting legitimate webhook traffic.
- [ ] Reject direct-origin requests that do not pass the selected origin-authentication control.

References:

- [Cloudflare IP address ranges](https://developers.cloudflare.com/fundamentals/concepts/cloudflare-ip-addresses/)
- [Cloudflare WAF skip rules](https://developers.cloudflare.com/waf/custom-rules/skip/)
- [Cloudflare rate-limiting rules](https://developers.cloudflare.com/waf/rate-limiting-rules/)

## Master checklist

### 1. Secure provider accounts

**Status:** `[~]` In progress

#### OVHcloud

- [ ] Use a unique password stored in an approved password manager.
- [x] Enable TOTP or hardware security-key two-factor authentication.
- [ ] Store recovery codes securely offline.
- [ ] Confirm that the recovery email does not depend solely on Wishkr infrastructure.
- [ ] Review OVHcloud users, IAM permissions, contacts, and API credentials.
- [ ] Remove unknown, unused, or unnecessarily privileged access.
- [ ] Use delegated accounts instead of sharing the owner login.
- [ ] Restrict Control Panel access by IP only when a stable office or VPN IP exists.
- [ ] Confirm that the server KVM/IPMI console is available.
- [ ] Confirm that rescue mode is available.
- [ ] Confirm that security and hardware alerts reach at least two trusted people.
- [ ] Confirm that the Network Security Dashboard is accessible.

#### Cloudflare

- [ ] Enable two-factor authentication for every administrator.
- [ ] Review account members and remove unnecessary access.
- [ ] Review and revoke unused or overly broad API tokens.
- [ ] Confirm that the Global API Key will not be placed on the OVH server.
- [ ] Plan a narrowly scoped DNS API token for certificate automation.

#### Completion gate

- [ ] A second authorized person can recover both provider accounts.
- [ ] KVM/IPMI access has been opened successfully without changing server state.
- [ ] No shared, unknown, or stale privileged account remains.

Reference:
[OVHcloud Control Panel IP restrictions](https://help.ovhcloud.com/csm/en-sg-customer-ovhcloud-control-panel-ip-restriction?id=kb_article_view&sysparm_article=KB0043039)

### 2. Install the operating system and configure storage

**Status:** `[x]` Completed and verified

- [x] Install Ubuntu Server 24.04 LTS minimal.
- [x] Use UEFI boot if supported by the OVH installation profile.
- [x] Configure both NVMe drives as software RAID1.
- [x] Confirm that the server boots normally from the installed system.
- [x] Confirm that both disks participate in the RAID array.
- [x] Wait for initial RAID synchronization or document its progress.
- [x] Verify the expected filesystem capacity.
- [x] Verify recovery access before hardening SSH.
- [x] Record non-sensitive disk, partition, filesystem, and RAID information.

#### Completion gate

- [x] `cat /proc/mdstat` reports the expected healthy RAID1 devices.
- [x] `lsblk` shows the intended disk and filesystem layout.
- [x] The host survives a normal reboot and returns to a healthy state.

### 3. Establish secure administrative access

**Status:** `[x]` Completed and verified

- [x] Create a named sudo administrator.
- [x] Create a separate deployment/service user.
- [x] Install Ed25519 SSH public keys.
- [x] Verify key-based access in a second SSH session.
- [x] Keep the working session open during SSH configuration changes.
- [x] Validate SSH configuration with `sshd -t` before reloading it.
- [x] Disable SSH password authentication.
- [x] Disable direct root SSH login.
- [x] Restrict SSH access by trusted IP or administrative VPN.
- [x] Define and test the emergency-access procedure.

SSH source restriction is intentionally completed with the firewall/VPN work in
Item 5. Until then, SSH remains public-key-only with root and password login
disabled. Emergency access is through OVHcloud IPMI/KVM or rescue boot; restore or
disable `/etc/ssh/sshd_config.d/10-wishkr-hardening.conf`, validate with `sshd -t`,
then reboot or reload SSH as appropriate.

Reference:
[Ubuntu OpenSSH server guidance](https://documentation.ubuntu.com/server/how-to/security/openssh-server/)

### 4. Patch and harden the base operating system

**Status:** `[x]` Completed and verified

- [x] Install all available operating-system and security updates.
- [x] Enable and verify `unattended-upgrades`.
- [x] Install and configure `needrestart`.
- [x] Establish controlled reboot notifications and a maintenance process.
- [x] Configure reliable time synchronization.
- [x] Use UTC for host and container logs.
- [x] Remove or disable unused packages and network services.
- [x] Configure bounded journald storage and log rotation.
- [x] Apply conservative, documented kernel and network hardening.
- [x] Decide explicitly whether IPv6 will be supported or disabled.

References:

- [Ubuntu security suggestions](https://documentation.ubuntu.com/server/explanation/security/security_suggestions/)
- [Ubuntu automatic updates](https://documentation.ubuntu.com/server/how-to/software/automatic-updates/)

### 5. Configure network firewalls

**Status:** `[~]` In progress

- [x] Configure the OVHcloud Edge Network Firewall.
- [x] Configure a default-deny host inbound policy.
- [x] Apply equivalent rules for IPv4 and IPv6.
- [x] Permit SSH only from approved administrative sources.
- [x] Permit only required HTTP/HTTPS traffic.
- [x] Document every allowed inbound port and its purpose.
- [x] Add an explicit Docker `DOCKER-USER` forwarding policy after Docker installation.
- [x] Confirm that PostgreSQL, Redis, and Docker management ports are not public.
- [x] Run an external TCP port scan.
- [ ] Run an external UDP port scan.
- [x] Verify KVM/rescue access after firewall activation.

Current allowed inbound traffic:

- Public IPv4/IPv6: UDP `51820` for the WireGuard administrative VPN.
- `wg-admin` interface only: TCP `22` for SSH.
- Public TCP `22`, `80`, `443`, `5432`, `6379`, and `8000`: blocked.
- HTTP/HTTPS remain closed until the reverse-proxy deployment gate.

OVHcloud Edge Network Firewall constraints:

- The service is stateless and currently protects IPv4 only; UFW remains the
  authoritative IPv4/IPv6 host firewall.
- Saved Edge Network Firewall rules can be forced active automatically during
  DDoS mitigation, even if the user-facing firewall switch is off.
- UDP fragments are dropped at the OVHcloud edge. The administrative
  WireGuard tunnel therefore uses the verified `1280` MTU.
- Configure required allow rules before the final deny rule and test the VPN
  after every material rule change.

Active IPv4 edge rules:

| Priority | Action | Protocol | Source | Source port | Destination port | TCP status |
|---:|---|---|---|---:|---:|---|
| `0` | Authorise | TCP | All | Any | Any | Established |
| `1` | Authorise | UDP | All | Any | `51820` | N/A |
| `2` | Authorise | UDP | All | `53` | Any | N/A |
| `3` | Authorise | UDP | All | `123` | Any | N/A |
| `4` | Authorise | ICMP | All | N/A | N/A | N/A |
| `19` | Refuse | IPv4 | All | Any | Any | N/A |

Post-activation validation passed: a fresh SSH session connected through
WireGuard, DNS resolution succeeded, outbound HTTPS returned HTTP `200`, NTP
remained synchronized, the WireGuard peer had a live handshake, and the UFW
policy remained active. A subsequent external connection attempt to public TCP
`22` timed out while VPN SSH remained healthy.

References:

- [OVHcloud Edge Network Firewall guidance](https://docs.ovhcloud.com/en/guides/bare-metal-cloud/dedicated-servers/firewall-network)
- [OVHcloud iptables firewall guidance](https://help.ovhcloud.com/csm/en-dedicated-servers-firewall-iptables?id=kb_article_view&sysparm_article=KB0043436)

### 6. Install host security and hardware monitoring

**Status:** `[x]` Completed and verified

- [x] Install and configure Fail2ban for SSH.
- [x] Verify that AppArmor is installed, enabled, and enforcing expected profiles.
- [x] Install and configure `auditd`.
- [x] Install and configure `smartmontools`.
- [x] Configure `mdadm` RAID monitoring and alerts.
- [x] Run and review an initial Lynis security audit.
- [x] Review privileged files, users, groups, and sudo policy.
- [x] Establish a recurring security-audit schedule.
- [x] Install and baseline file-integrity/package-integrity monitoring.
- [x] Apply the accepted Lynis kernel and uncommon-protocol hardening.
- [x] Resolve or explicitly disposition failed security-maintenance units.

CrowdSec or Wazuh may be evaluated later. Do not install overlapping blocking
agents without defining ownership and response procedures.

Fail2ban SSH policy:

- Ubuntu package `1.0.2-3ubuntu0.1`, enabled at boot and active.
- Systemd journal backend with the distribution's native nftables action.
- Five failures within 10 minutes triggers an initial one-hour ban.
- Repeat-offender bans increase up to 24 hours.
- A test ban/unban using reserved address `192.0.2.1` passed.
- Source-controlled configuration:
  `ops/host-security/fail2ban/fail2ban.local` and
  `ops/host-security/fail2ban/jail.d/wishkr-sshd.local`.

Linux Audit policy:

- Ubuntu `auditd` and `audispd-plugins` packages are enabled and active.
- Kernel boot includes `audit=1 audit_backlog_limit=16384`; the loaded runtime
  backlog is `8192`, with `lost 0` after the verified reboot.
- Twenty-six targeted rules cover identities, sudo, SSH, WireGuard, UFW,
  Fail2ban, audit configuration, systemd, cron, security-sensitive commands,
  kernel modules, mounts, time, and host-identity changes.
- Audit log rotation is bounded to eight 64 MiB files (512 MiB maximum).
- A keyed `sudo` execution event was generated and successfully retrieved with
  `ausearch --input-logs -k privileged_command -i`.
- Rules remain mutable (`-e 1`) during migration. Consider immutable mode
  (`-e 2`) only after production policy stabilises because unlocking it
  requires a reboot.
- Source-controlled configuration:
  `ops/host-security/auditd/auditd.conf`,
  `ops/host-security/auditd/rules.d/50-wishkr.rules`, and
  `ops/host-security/auditd/40-wishkr-audit.cfg`.

NVMe and temperature monitoring:

- `smartmontools` `7.4`, `nvme-cli` `2.8`, and `lm-sensors` are installed.
- `smartmontools.service` is enabled and actively monitors both Samsung
  `MZVL2512HCJQ-00B07` 512 GB NVMe controllers.
- SMART warnings are sent to journald/syslog; temperature-change reporting is
  enabled with informational/critical thresholds of 70 C/80 C.
- Initial health for both devices: SMART `PASSED`, critical warning `0x00`,
  100% available spare, zero media/data-integrity errors, and zero error-log
  entries.
- Baseline wear is 20% used on `/dev/nvme0` and 22% used on `/dev/nvme1`;
  this is not a current fault but must be trended.
- Non-destructive short device self-tests completed without error on both
  drives.
- Source-controlled configuration:
  `ops/host-security/smartmontools/smartd.conf` and
  `ops/host-security/smartmontools/smartmontools.default`.

Software RAID monitoring:

- Ubuntu `mdadm` `4.3-1ubuntu2.1` monitors `/dev/md2` and `/dev/md3`.
- Both RAID1 arrays have two active/working devices, zero failed devices, and
  internal write-intent bitmaps.
- `mdmonitor.service`, its daily one-shot timer, and the periodic
  `mdcheck_start`/`mdcheck_continue` consistency-check timers are active.
- Events are delivered to journald through
  `/usr/local/sbin/wishkr-mdadm-alert`; no unconfigured local mail relay is
  required.
- Built-in `TestMessage` alerts were received for both arrays.
- The current array definitions were written to initramfs.
- Source-controlled configuration:
  `ops/host-security/mdadm/mdadm.conf` and
  `ops/host-security/mdadm/wishkr-mdadm-alert`.

Initial Lynis baseline:

- Lynis `3.0.9` ran 259 tests with no warnings and a hardening index of `71`.
- Firewall, Fail2ban, AppArmor, audit rules, audit configuration, package
  security repositories, automatic updates, and time synchronization passed.
- Suggestions intentionally not applied:
  changing SSH port `22` (SSH is reachable only inside WireGuard), disabling
  local SSH forwarding (needed for controlled administrative tunnels), GRUB
  password/Secure Boot changes (would complicate provider-console recovery),
  repartitioning `/home`, `/tmp`, and `/var` after installation, and disabling
  USB storage (could interfere with OVHcloud virtual-media recovery).
- Fail2ban's request for a full `jail.local` is a false positive for this
  deployment; source-controlled `.local` drop-ins already protect the SSH
  jail from package upgrades.
- Follow-ups accepted for this item: file-integrity monitoring, selected safe
  kernel settings, SSH session-limit tightening, privileged-access review,
  and a recurring audit schedule.
- Off-host log archival will be implemented with the production observability
  stack rather than by exposing a new logging listener now.
- Reports remain root-readable on the host at `/var/log/lynis.log` and
  `/var/log/lynis-report.dat`.

Final Lynis verification:

- A post-hardening audit ran 265 tests with zero warnings and improved the
  hardening index from `71` to `77`.
- The remaining generic kernel differences are intentional:
  `kernel.perf_event_paranoid=4` is stricter than Lynis's expected `3`;
  `kernel.unprivileged_bpf_disabled=2` disables unprivileged BPF while
  remaining recoverable by an administrator; `kernel.modules_disabled=0`
  permits the upcoming Docker installation; restricted `kernel.sysrq=176`
  preserves OVHcloud console recovery; and loose reverse-path filtering
  (`rp_filter=2`) supports WireGuard and future Docker routing.
- Lynis continues to print generic review prompts for DCCP, SCTP, RDS, and
  TIPC, but each module is explicitly blocked and `modprobe -n` resolves to
  `/bin/false`.
- AIDE's `Checksums = H` compound group includes SHA-256 and SHA-512; the
  generic suggestion that those hashes are absent is therefore not applicable.

Privilege-boundary review:

- `root` is the only UID `0` identity and direct root SSH remains disabled.
- `karan` is the only active SSH administrator. The verified Ed25519 key
  fingerprint is
  `SHA256:P/vYBSOluci1rzCgWjbTdE6GZrDKeJfuQTymU0xXURk`.
- `ubuntu` is retained only as an inactive filesystem identity: password
  locked, shell `/usr/sbin/nologin`, no supplemental groups, no sudo, and no
  active `authorized_keys` file.
- `wishkr` remains a locked non-login service identity with no sudo access.
- The root and `ubuntu` bootstrap key files plus the cloud-init sudo file were
  renamed with `.disabled-20260731`; remove these recovery copies after final
  migration acceptance.
- `karan` intentionally has non-interactive full sudo because its local
  password is locked; access requires both the WireGuard and SSH private keys,
  while OVHcloud IPMI/rescue provides emergency recovery.
- SSH now permits only `karan`, limits multiplexed sessions to two, disables
  protocol TCP keepalives, and retains local-only forwarding for controlled
  administrative tunnels.
- Sudoers and SSH configuration validation passed, a fresh login succeeded,
  the 20 setuid/setgid files matched expected system packages/paths, and no
  unsafe world-writable files or directories were found on the root
  filesystem.
- Source-controlled configuration:
  `ops/host-security/ssh/10-wishkr-hardening.conf` and
  `ops/host-security/sudoers/90-wishkr-admin`.

Recurring audit schedule:

- Ubuntu's `lynis.timer` is enabled and active with a daily schedule and a
  randomized delay of up to 30 minutes.
- A source-controlled systemd drop-in sets `Persistent=true`, ensuring a missed
  audit runs after the next boot.
- Source-controlled configuration:
  `ops/host-security/lynis/wishkr-timer.conf`.

File and package integrity:

- Ubuntu `aide` `0.18.6` and `debsums` are installed.
- AIDE has a full-root cryptographic baseline containing 103,033 entries.
- A clean independent comparison returned `AIDE_RC=0` with no filesystem
  differences; `debsums --silent` also returned `0`.
- The vendor `dailyaidecheck.timer` is enabled, active, and persistent.
- Ephemeral per-user runtime state and the Landscape status cache are excluded.
  Audit and sysstat logs remain protected with append-aware rules.
- AIDE's package dependency installed Postfix, but AIDE sends reports to
  journald rather than email. Postfix is therefore stopped and masked, with no
  port `25` listener. Its retained fallback configuration is loopback-only.
- Source-controlled configuration:
  `ops/host-security/aide/aide.default`,
  `ops/host-security/aide/99_wishkr_volatile`, and
  `ops/host-security/aide/postfix-local-only.cf`.

Kernel, core-dump, and protocol hardening:

- TTY line-discipline autoloading is disabled, FIFO/regular-file link
  protections are strict, setuid core dumps are disabled, and login-session
  core limits are zero.
- BPF JIT hardening applies to all users, impossible IPv4 source addresses are
  logged, unsafe IPv4/IPv6 redirects and source routing are rejected, and
  SYN-cookie protection remains enabled.
- Unused DCCP, SCTP, RDS, and TIPC protocol modules are blocked using both
  `install ... /bin/false` and `blacklist` directives.
- Source-controlled configuration:
  `ops/host-security/sysctl/99-wishkr-hardening.conf`,
  `ops/host-security/modprobe/60-wishkr-uncommon-protocols.conf`, and
  `ops/host-security/security-limits/60-wishkr-no-coredumps.conf`.

Firmware metadata maintenance:

- The initially failed `fwupd-refresh.service` was a transient first-refresh
  failure. Its next scheduled run and a controlled retry both succeeded.
- LVFS metadata is current. The host exposes three firmware-updatable
  components, but none has a supported update in the enabled remotes; no
  firmware was flashed.
- `fwupd-refresh.timer` remains enabled and active, and the host has zero failed
  systemd units.

Final Item 6 verification:

- Fresh administrative SSH arrived through WireGuard from `10.77.0.2`; the
  session core limit is zero.
- AppArmor, auditd, Fail2ban, SMART monitoring, mdadm monitoring, SSH, and UFW
  are active; all AIDE, Lynis, mdadm, RAID-check, and fwupd timers are active.
- Ubuntu's `ssh.socket` is enabled and active under `sockets.target`, ensuring
  SSH activation persists across reboot even though `ssh.service` itself is
  socket-triggered.
- Audit has 26 rules and `lost 0`; Fail2ban has no current failures or bans.
- `/dev/md2` and `/dev/md3` are both `[UU]`; both NVMe devices report SMART
  `PASSED`.
- UFW exposes only WireGuard UDP `51820` publicly and permits SSH TCP `22` only
  on `wg-admin`. No SMTP listener exists.
- There are zero pending package upgrades, no reboot requirement, and zero
  failed systemd units.

References:

- [Ubuntu AppArmor](https://documentation.ubuntu.com/server/how-to/security/apparmor/)
- [Linux kernel filesystem protection controls](https://docs.kernel.org/admin-guide/sysctl/fs.html)
- [Linux kernel BPF JIT hardening](https://docs.kernel.org/admin-guide/sysctl/net.html)
- [Linux kernel reverse-path filtering](https://docs.kernel.org/networking/ip-sysctl.html)
- [Linux PAM resource limits](https://man7.org/linux/man-pages/man5/limits.conf.5.html)

### 7. Install and secure Docker

**Status:** `[x]` Completed on 2026-07-31

- [x] Install Docker Engine from Docker's official apt repository.
- [x] Do not expose the Docker remote API.
- [x] Limit membership of the root-equivalent `docker` group.
- [x] Configure container log size and file rotation.
- [x] Enable Docker live restore if compatible with the deployment.
- [x] Create isolated Docker networks.
- [x] Do not publish PostgreSQL or Redis ports.
- [x] Bind the FastAPI service to localhost or an internal Docker network.
- [x] Apply `no-new-privileges` where supported.
- [x] Drop unnecessary Linux capabilities.
- [x] Configure container health checks.
- [x] Configure CPU and memory limits/reservations.
- [x] Scan application images with Trivy before deployment.

Docker-published ports can bypass normal UFW expectations. Docker forwarding rules
must be tested separately.

Implementation and verification:

- Installed Docker Engine `29.7.0`, Compose `5.3.1`, Buildx `0.36.0`, and
  containerd `2.2.6` from Docker's signed Ubuntu `noble` repository. The
  repository key fingerprint was verified as
  `9DC8 5822 9FC7 DD38 854A E2D8 8D81 803C 0EBF CD88`.
- Docker listens only on its root-owned Unix socket. No TCP `2375` or `2376`
  listener exists, `karan` is not in the root-equivalent `docker` group, and
  the group has no members.
- `/etc/docker/daemon.json` enables live restore, AppArmor/seccomp/cgroup
  isolation, global `no-new-privileges`, compressed JSON logs capped at
  `5 x 10 MiB`, and a dedicated `172.28.0.0/16` address pool. The userland
  proxy is disabled.
- Persistent IPv4 and IPv6 `DOCKER-USER` rules accept established replies,
  drop new forwarded traffic arriving on public interface `enp3s0f0`, and
  return all other traffic for normal Docker processing. The service is
  enabled and active; UFW remains default-deny and OVH Edge Firewall remains
  the outer public filter.
- Created `wishkr-app` (`172.28.10.0/24`) and internal-only `wishkr-data`
  (`172.28.20.0/24`) networks with deterministic bridge names.
- Added `docker-compose.ovh.yml` for the single-server architecture. All four
  services use fixed non-root UIDs, read-only root filesystems,
  `no-new-privileges`, `cap_drop: ALL`, PID/CPU/memory bounds, and health
  checks. PostgreSQL and Redis publish no host ports; FastAPI binds only to
  `127.0.0.1:8000`. The rendered Compose model passed the source-controlled
  `validate-compose-security.py` policy.
- Pinned the Python base and Redis image by immutable digest. PostgreSQL uses a
  two-line derivative of the pinned official `16.14` Alpine image: it runs
  directly as UID/GID `70` and removes the unused `gosu` helper. PostgreSQL and
  Redis both initialized and accepted live queries in disposable containers
  while non-root, read-only, capability-free, resource-limited, and attached
  only to an internal network.
- Installed Trivy `0.72.0` from its immutable GitHub release after verifying
  the release checksum manifest. Scans run as locked user `trivy-scan`, which
  is not in the Docker group; the root wrapper exports an image to a protected
  temporary archive and retains root/`adm` reports with mode `0640`.
- The first backend scan correctly failed on 10 fixable HIGH Python findings.
  FastAPI/Starlette, PyJWT, python-multipart, setuptools/wheel, and Razorpay
  were updated to compatible fixed versions. The final backend image then
  passed with zero HIGH/CRITICAL findings and the full application suite
  reported `797 passed, 3 skipped`.
- The official PostgreSQL image scan found 14 HIGH and 1 CRITICAL findings in
  startup-only `gosu`; the final non-root derivative has zero HIGH/CRITICAL
  findings. Redis has zero HIGH/CRITICAL findings. Final reports are
  `/var/log/wishkr-trivy/scan-20260731T084115Z.log` (backend),
  `scan-20260731T084207Z.log` (Redis), and
  `scan-20260731T084426Z.log` (PostgreSQL).
- No production application container was started in this item. After
  disposable tests, the host had zero containers and no listeners on
  `2375`, `2376`, `5432`, `6379`, or `8000`. VPN SSH still originated from
  `10.77.0.2`; there were zero failed units, pending upgrades, or reboot
  requirements.
- Rebuilt the AIDE post-Docker baseline while excluding mutable Docker,
  containerd, and Trivy cache stores. The independent comparison found no
  differences across `103,881` entries, and `debsums` remained clean.

References:

- [Docker Engine installation on Ubuntu](https://docs.docker.com/engine/install/ubuntu/)
- [Docker with iptables](https://docs.docker.com/engine/network/firewall-iptables/)
- [Docker port publishing](https://docs.docker.com/engine/network/port-publishing/)
- [Docker JSON log rotation](https://docs.docker.com/engine/logging/drivers/json-file/)
- [Docker live restore](https://docs.docker.com/engine/daemon/live-restore/)
- [Trivy release history](https://github.com/aquasecurity/trivy/releases)
- [Trivy 2026 supply-chain incident advisory](https://github.com/aquasecurity/trivy/security/advisories/GHSA-69fq-xp46-6x23)

### 8. Prepare the application deployment

**Status:** `[x]` Completed on 31 July 2026

- [x] Create documented application, data, backup, and log directories.
- [x] Deploy PostgreSQL 16.
- [x] Deploy Redis 7 with required persistence and authentication.
- [x] Deploy the FastAPI web service.
- [x] Deploy APScheduler as a separate service.
- [x] Ensure that exactly one scheduler instance is active.
- [x] Mount only required persistent directories.
- [x] Keep secrets outside the Git repository and container images.
- [x] Set secret-file ownership and permissions to `0600`.
- [x] Configure graceful shutdown and restart behaviour.
- [x] Add readiness and health checks.
- [x] Confirm Cloudflare R2 access with a least-privilege credential.

Progress notes (31 July 2026):

- Added repository ignore rules for production environment files, local
  mirrors of `/etc/wishkr/secrets`, SSH/WireGuard private material, API
  credential files, recovery-code files, and certificate private keys.
  Public SSH keys and public certificate files remain eligible for version
  control.
- Verified that no already tracked file is newly matched by these secret
  ignore rules. The content-level secret scan was subsequently completed as
  recorded below.

Completion evidence:

- Gitleaks `8.30.1` was downloaded from the official release, verified
  against SHA-256
  `b40ab0ae55c505963e365f271a8d3846efbc170aa17f2607f13df610a9aeb6a5`,
  and run with full redaction. Ten Git commits, the proposed repository
  contents, and each release candidate returned zero findings.
- Created root-owned host paths for immutable releases, secrets, reserved
  application state, local backup staging, and audit logs. The layout and
  release/rollback procedure are documented in `ops/deployment/README.md`.
- Production application credentials were streamed directly from
  `/opt/wishkr/.env` on the Hetzner web host to OVH through SSH. No local copy
  was created and no value was printed. Deployment-specific database, Redis,
  role, host, and runtime fields were replaced on OVH.
- Generated independent 256-bit PostgreSQL and Redis passwords on OVH.
  `/etc/wishkr/secrets/production.env`, `postgres_password`, and
  `redis_password` are all `root:root 0600`. Docker CLI access remains
  root-only.
- Deployed PostgreSQL `16.14` with data checksums enabled, SCRAM-SHA-256
  password encryption, a 90-second stop grace period, a non-root UID, and the
  labeled `wishkr_pgdata` volume. This is a freshly initialized database;
  Hetzner production data has not yet been imported or cut over.
- Deployed authenticated Redis 7 with AOF enabled,
  `appendfsync everysec`, successful AOF status, a non-root UID, and the
  labeled `wishkr_redisdata` volume.
- Deployed `wishkr-backend-1` on `127.0.0.1:8000` only and
  `wishkr-scheduler-1` without a published port. PostgreSQL and Redis have no
  host ports. All four services have read-only root filesystems where
  applicable, `no-new-privileges`, dropped capabilities, PID/CPU/memory
  limits, rotating logs, and health checks.
- Confirmed exactly one scheduler container and acquisition of PostgreSQL
  advisory lock `41051`. A controlled SIGTERM restart showed the API shutdown
  hook running and the scheduler lock being released and reacquired; both
  services returned healthy.
- The first production start exposed two packaging/deployment defects before
  public traffic existed: standalone Compose file-secret ownership was
  incompatible with fixed non-root service UIDs, and Sentry's Starlette
  integration required the missing `jinja2==3.1.6` dependency. Both were
  corrected without weakening host secret permissions or container UIDs.
- Removed macOS AppleDouble metadata from the release process and excluded
  `._*`/`.DS_Store` from Git and Docker contexts. The final archive and image
  contain zero AppleDouble files, and Alembic loaded the complete revision
  graph successfully.
- The test suite completed with `797 passed`, `3 skipped`, and two warnings.
  The final backend, custom PostgreSQL, and Redis images each passed Trivy
  with zero HIGH/CRITICAL findings.
- An R2 canary verified put, get, prefix-list, integrity, and delete access.
  The credential could not see an unrelated bucket. The random canary object
  was deleted during the test.
- `ops/deployment/validate-runtime.sh` passed every service, singleton,
  authenticated-query, health, loopback exposure, and Compose check.
  Nginx/Cloudflare public routing remains intentionally unavailable until
  Item 9.

### 9. Configure Nginx, TLS, and Cloudflare

**Status:** `[~]` Staging complete — DNS cutover and live Cloudflare request
validation are intentionally deferred to Items 13–14

- [x] Install and harden Nginx.
- [x] Configure TLS 1.2 and TLS 1.3.
- [ ] Proxy `api.wishkr.com` through Cloudflare.
- [x] Stage Cloudflare Full (strict) for `api.wishkr.com` with a hostname
  configuration rule.
- [x] Configure Authenticated Origin Pulls globally and require a valid
  Cloudflare client certificate for non-VPN origin requests.
- [x] Trust forwarded client IPs only from current Cloudflare ranges.
- [x] Disable caching for API and webhook routes.
- [x] Configure safe request-body and timeout limits.
- [x] Disable unnecessary server-version disclosure.
- [x] Add appropriate security headers.
- [x] Add endpoint-specific rate limits.
- [ ] Verify the WhatsApp webhook without Cloudflare browser challenges.
- [x] Verify that unauthorized direct-origin requests are rejected.

Item 9 staging evidence (2026-07-31):

- The existing Hetzner origin was inspected read-only. It proxies
  `api.wishkr.com` to loopback port 8000, but the zone record is currently
  DNS-only to `5.223.77.195`; no DNS change was made.
- Nginx `1.24.0-2ubuntu7.15` is enabled on OVH and proxies only to
  `127.0.0.1:8000`. Nginx listens on public IPv4 port 443, but UFW and the
  OVHcloud Edge Network Firewall admit origin HTTPS only from the 15 published
  Cloudflare IPv4 ranges; WireGuard administration remains independently
  available.
- The origin ECDSA P-256 private key was generated on OVH with mode `0600`; it
  never left `/etc/wishkr/tls`. The matching Cloudflare Origin CA certificate
  for `api.wishkr.com` is installed and expires on 30 July 2029.
- Cloudflare dashboard staging is complete: hostname-scoped Full (strict),
  global Authenticated Origin Pulls, API cache bypass, a signed-webhook WAF
  skip rule for incompatible rate-limit/managed/bot checks, and a method-safe
  HTTP-to-HTTPS `308` redirect that preserves paths and query strings.
- TLS 1.2 and 1.3 handshakes succeeded over WireGuard; TLS 1.0 and unknown SNI
  were rejected. Public `148.113.6.76:443` timed out as intended.
- `/health` returned 200 through Nginx, a synthetic invalid Meta signature
  returned 403 from the application, and the admin-login limiter returned 429
  after its configured burst.
- A forged `CF-Connecting-IP` header sent over WireGuard was ignored. Access
  logs retained the VPN peer address and omitted query strings and synthetic
  sensitive canaries.
- Final revalidation passed: `nginx -t`, active service state, VPN `/health`
  HTTP 200, certificate expiry, loopback-only backend listener, and all 15 UFW
  Cloudflare-origin rules. Production DNS still resolves to Hetzner
  `5.223.77.195`; proxy activation, Cloudflare-to-origin mTLS, the `308`
  redirect, and live WhatsApp webhook delivery must be verified during the
  controlled cutover.

### 10. Secure and tune PostgreSQL and Redis

**Status:** `[x]` Complete

- [x] Use strong, independent credentials.
- [x] Bind both services only to internal networks.
- [x] Configure PostgreSQL connection and memory limits for the 64 GB host.
- [x] Configure Redis memory, persistence, and eviction policy.
- [x] Enable useful PostgreSQL error and slow-query logging.
- [x] Restrict administrative database access.
- [x] Establish schema migration and rollback procedures.
- [x] Verify that application containers can connect without exposing public ports.

Completion evidence (31 July 2026):

- PostgreSQL `16.14` and Redis `7.4.10` run as non-root, read-only containers
  exclusively on the internal `wishkr-data` network. Neither publishes a host
  port; only FastAPI remains published on host loopback.
- Independent 256-bit PostgreSQL application, PostgreSQL administrator, and
  Redis passwords were generated directly on OVH. Their files and the merged
  production environment remain `root:root 0600`; no credential value was
  printed, copied into the repository, or retained in deployment output.
- All local and Docker-network PostgreSQL connections require SCRAM-SHA-256.
  The immutable bootstrap identity is now `wishkr_admin` with a three-session
  limit; the new `wishkr` application owner has no superuser, role-creation,
  database-creation, replication, or row-security-bypass privilege and has a
  90-session limit. A privilege probe confirmed it cannot create roles.
- The application database and all 57 application relations are owned by
  `wishkr`; zero non-system application relations remain owned by
  `wishkr_admin`. Protected PostgreSQL system objects and the `plpgsql`
  extension remain with the bootstrap administrator.
- PostgreSQL is explicitly configured for at most 100 connections, 4 GiB
  shared buffers, 12 GiB effective cache, 8 MiB work memory, bounded
  maintenance/autovacuum memory, NVMe-aware I/O costs, 4 GiB maximum WAL,
  60-second idle-transaction termination, lock/checkpoint/autovacuum logging,
  and one-second slow-query logging with bind-parameter values suppressed.
- Redis has a 1 GiB dataset ceiling inside its 2 GiB container limit,
  `allkeys-lru` eviction, protected mode, RDB snapshots, AOF persistence with
  `everysec` fsync, successful background-save/rewrite status, and healthy
  authenticated queries.
- The replacement PostgreSQL image passed disposable fresh-database and
  idempotent role-migration tests, rejected passwordless authentication,
  rejected application role creation, and passed the Trivy release gate with
  zero vulnerabilities.
- `/var/backups/wishkr/item10-prechange-20260731.dump` is a readable,
  root-only custom-format recovery point. Alembic upgrades run before web
  startup; rollback requires an explicit reviewed migration or restoration
  decision and is never automatic.
- Both data services and both application processes returned healthy after the
  controlled restart. Runtime validation confirmed the singleton scheduler,
  advisory lock, authenticated PostgreSQL/Redis queries, loopback API health,
  private port exposure, valid Compose model, and Nginx `/health` HTTP 200.

### 11. Configure backups and disaster recovery

**Status:** `[x]` Completed and verified (2026-07-31)

- [x] Activate and validate encrypted backups to the included OVHcloud Backup Storage (500 GB, hosted in BHS; OVH source `148.113.6.76/32`; explicit FTPS only).
- [x] Configure encrypted PostgreSQL backups.
- [x] Configure Redis/application-state backups where required.
- [x] Keep an off-provider backup in Cloudflare R2.
- [x] Define retention and deletion policies.
- [x] Monitor backup completion, size, and age.
- [x] Perform and document a complete restoration test.
- [x] Document bare-metal rebuild and recovery steps.
- [x] Store recovery documentation outside the OVH server.

Progress evidence (2026-07-31):

- OVHcloud included Backup Storage is active (500 GB, BHS) and restricted to
  the OVH server address.
- A new account-level Cloudflare R2 token is restricted to
  `wishkr-db-backup` with Object Read & Write permissions and the OVH public
  source addresses.
- The OVH host stores the R2 credentials and an independent 256-bit restic
  repository password in root-owned mode-`0600` files.
- R2 authentication succeeded and the isolated encrypted repository at prefix
  `restic/ovh-prod` was initialized without modifying the legacy Hetzner
  backup objects.
- `wishkr-backup.timer` runs daily at 02:15 UTC with up to 10 minutes of
  random delay and persistent catch-up. Retention is 7 daily, 5 weekly,
  12 monthly, and 3 yearly snapshots.
- The first encrypted R2 backup completed successfully at
  `2026-07-31T12:00:33Z`; PostgreSQL and Redis artifacts passed pre-upload
  validation and the freshness monitor passed.
- The isolated R2 restoration drill verified all repository data, restored
  534 files/directories with matching artifact checksums, validated the Redis
  RDB in a network-isolated disposable container, and restored all 54 public
  PostgreSQL tables into a uniquely named scratch database. The scratch
  database and temporary restore directory were both confirmed absent after
  cleanup.
- The first dual-destination backup completed successfully at
  `2026-07-31T12:44:11Z`. The secondary repository is client-side encrypted
  locally, transferred using explicit FTPS with certificate verification, and
  synchronized to `wishkr/restic/ovh-prod` on the included OVH storage.
- The independent OVH restoration drill downloaded the repository from BHS,
  passed restic's full cryptographic data check, restored 537 files/directories
  with matching artifact checksums, validated Redis, and restored all 54
  public PostgreSQL tables. No scratch database or restored temporary
  directory remained afterward.
- A fresh post-monitoring backup completed at `2026-07-31T12:56:42Z` in 125
  seconds. The R2 upload, encrypted local OVH repository update, FTPS sync, and
  downloaded remote-content comparison all passed. Recorded metrics were a
  141,110-byte PostgreSQL dump, 89-byte Redis RDB, and 1,692,834-byte encrypted
  OVH restic repository.
- `wishkr-backup-monitor.timer` runs hourly with a randomized delay and fails
  if the backup is older than 30 hours, the daily timer is inactive, the last
  service failed, or a recorded PostgreSQL, Redis, or encrypted-repository
  size is invalid. Its first production execution returned status `0` and all
  checks passed. External notification delivery belongs to Item 12.
- The total-loss procedure is documented in
  [`OVHCLOUD_BARE_METAL_RECOVERY_RUNBOOK.md`](./OVHCLOUD_BARE_METAL_RECOVERY_RUNBOOK.md),
  including R2-first recovery, OVH fallback, secure existing-password entry,
  database/Redis restore order, validation, cutover, and abort criteria. The
  runbook and non-secret backup tooling are stored outside OVHcloud in the
  private GitHub repository at commit `39b49c2`.

### 12. Configure monitoring and alerting

**Status:** `[x]` Completed and verified (2026-08-01)

- [x] Configure external uptime monitoring.
- [x] Alert on CPU, memory, load, filesystem, and inode exhaustion.
- [x] Alert on RAID degradation and NVMe health.
- [x] Alert on container restart loops and failed health checks.
- [x] Monitor PostgreSQL and Redis health.
- [x] Monitor TLS certificate expiration.
- [x] Monitor Nginx error rates and unusual request volume.
- [x] Verify Sentry application error reporting.
- [x] Securely retain or centralize important logs.

At the account owner's direction on 1 August 2026, the two unresolved
provider-notification ownership controls were transferred intact to Item 16.
This closes the implemented monitoring scope in Item 12 without hiding or
waiving the remaining provider escalation work.

An uptime monitor running only on this server cannot report a complete server
outage. At least one check must run externally.

Progress evidence (2026-07-31):

- Sentry Uptime monitor `Wishkr Production API Downtime` checks
  `GET https://api.wishkr.com/health` externally every minute in project
  `wishkr-prod-1`, environment `production`, with a 10-second timeout.
- Three consecutive failures create an issue; two consecutive successes
  resolve it. The monitor is assigned to the Wishkr team and has the documented
  Cloudflare, VPN, Nginx, container, backup, and disaster-recovery triage steps.
- Connected email alert `Notify Karan Dumbre` is active, and successful monitor
  check-ins and notification delivery were confirmed by the account owner.
- An independent external request returned HTTP `200` with `{"status":"ok"}`
  in approximately 197 ms. The current DNS target remains Hetzner
  (`5.223.77.195`); the same hostname monitor will follow the Cloudflare-proxied
  OVH origin during cutover.
- Grafana Alloy `1.17.1` is installed from its checksum-pinned official release
  as the unprivileged `alloy` user. Its UI is loopback-only, pprof and support
  bundles are disabled, the unit is systemd-hardened, and it has no Docker
  socket access or Docker group membership.
- The root-only Grafana Cloud write token is stored outside the repository in
  `/etc/wishkr/secrets/grafana-alloy.env` with mode `0600`. Alloy collects host
  and systemd metrics only; Loki log collection and remote configuration are
  not configured.
- Production validation reported `422` remote-write samples sent and zero
  failed after the final restart. The current process had no collector or
  authentication errors, no systemd unit was failed, and both RAID1 arrays
  remained healthy `[UU]`.
- Grafana Cloud's Linux Node integration is collecting production telemetry for
  `wishkr-prod-mum-01`. Its seven dashboards and 24 resource alert rules cover
  CPU, memory, load, filesystem capacity, inode exhaustion, disk I/O, network,
  systemd, textfile metrics, and RAID degradation.
- The default Grafana notification policy delivers alerts to the tested
  `Wishkr Operations Email` contact point, grouped by `grafana_folder` and
  `alertname` with a 30-second group wait, five-minute group interval, and
  four-hour repeat interval. An always-firing `node_uname_info` test alert was
  evaluated and delivered end to end in 31 seconds, then deleted.
- A hardened root oneshot exports non-secret SMART health metrics for both NVMe
  devices to Alloy's textfile directory every five minutes. The production
  baseline is SMART passed, critical-warning bitmask zero, 30/31 degrees
  Celsius, 100 percent available spare, 20/22 percent endurance used, and zero
  media or error-log entries.
- Twelve imported Grafana-managed rules in `Wishkr Hardware/wishkr-nvme` cover
  missing/stale collection, missing devices, collection failure, SMART failure,
  critical warnings, temperature, spare threshold, endurance, media errors,
  error-log entries, and new unsafe shutdowns. All rules were resumed and
  confirmed Normal; the two temporary manually-created duplicates were removed.
- A capability-free, root-only container exporter reads only the four fixed
  Wishkr container names over the local Docker socket and publishes non-secret
  state metrics once per minute. All four containers were present, running,
  healthy, not restarting or OOM-killed, and had zero restarts during server-side
  validation. Neither `karan` nor `alloy` belongs to the Docker group.
- Nine Grafana-managed container rules were imported into
  `Wishkr Hardware/wishkr-containers`, resumed, and confirmed active and Normal
  by the account owner after evaluation. The final server-side preflight found
  fresh metrics, all four expected containers present/running/healthy, zero
  restarts/restart loops/OOM kills, `194846` remote-write samples sent, zero
  failed samples, and no recent Alloy authentication or remote-write errors.
- A hardened root-only oneshot now runs fixed authenticated health queries inside
  `wishkr-db-1` and `wishkr-redis-1` once per minute over the local Docker
  socket. It exports numeric readiness, PostgreSQL recovery/checksum/connection
  state, and Redis RDB/AOF state to an atomic `root:alloy` mode-`0640` textfile;
  Alloy retains no Docker, datastore, environment-file, or credential access.
- Production datastore validation passed: PostgreSQL was ready, accepted the
  fixed query, was not in recovery, had data checksums enabled, and used 11 of
  100 connections. Redis answered authenticated PING, was not loading, and
  reported successful RDB save, AOF write, and AOF rewrite state with AOF
  enabled. The timer is active/enabled, the oneshot result is `success`, its
  systemd exposure score is `3.9 OK`, Alloy had sent `286748` samples with zero
  failures, and there were no recent authentication or remote-write errors.
- Fourteen Grafana-managed rules from
  `ops/monitoring/grafana/wishkr-datastore-alert-rules.yml` were imported into
  `Wishkr Hardware/wishkr-datastores`, resumed, and confirmed active and Normal
  by the account owner after evaluation.
- A six-hour capability-free TLS oneshot now reads only the public
  `api.wishkr.com` Origin CA certificate and publishes numeric validity and
  expiry state. Its sandbox makes the matching private key inaccessible and has
  no network access. Production validation reported parse success, current
  validity, hostname match, and `94587078` seconds remaining; the timer is
  active/enabled and the service exposure score is `3.9 OK`.
- Nginx now exposes aggregate `stub_status` counters only on
  `127.0.0.1:8081` with access logging disabled. A separate metrics log contains
  only numeric timestamp and HTTP status; the exporter sandbox cannot read the
  normal access/error logs. The one-minute exporter reported successful status
  and log parsing, zero parse errors, and no 4xx/5xx/429/499 responses in its
  initial safe health-check window. Its timer is active/enabled and exposure is
  `4.3 OK`.
- Alloy validation after both deployments reported `348045` remote-write
  samples sent, zero failed, and no recent authentication or non-recoverable
  errors. Installed exporter/unit/Nginx configuration checksums match the
  reviewed repository files.
- Seven TLS rules and nine Nginx rules were imported from
  `ops/monitoring/grafana/wishkr-tls-certificate-alert-rules.yml` and
  `ops/monitoring/grafana/wishkr-nginx-alert-rules.yml`. The account owner
  resumed both groups and confirmed all 16 rules active and Normal after
  evaluation.
- A PII-free synthetic exception tagged `wishkr_verification=item12_controlled_test`
  and `synthetic=true` was submitted directly through the initialized production
  SDK without calling an HTTP, webhook, customer, scheduler, WhatsApp, or payment
  path. Sentry assigned event ID `9fe67efb6d7f4852bcdeb1609c91adbc`.
  On 1 August 2026, the account owner confirmed that the event and log were
  visible in project `wishkr-prod-1`, completing end-to-end delivery
  verification.
- The preflight found the temporary Sentry debug-route flag still enabled. It
  was set to false in the root-only production environment and only the backend
  was recreated. The container returned healthy with zero restarts, `/health`
  returned `200`, `/sentry-debug` returned `404`, the DSN remained configured,
  and the environment file remained `root:root 0600`.
- A source-controlled, bounded local-retention baseline now covers the systemd
  journal (30 days/1 GiB), Nginx (14 daily rotations), all four container logs
  (five compressed 10 MiB files each), Linux Audit (eight 64 MiB files), Wishkr
  operational evidence (30 days), and Trivy reports (90 days). Loki remains
  intentionally disabled pending a separate field-filtering, access-control,
  cost, and remote-retention review.
- The metadata-only inventory found that the package-provided `alloy` account
  had inherited `adm` and `systemd-journal` memberships and that the three new
  Wishkr Nginx logs were initially mode `0644`. Alloy was removed from both log
  groups and restarted, and the Nginx files were corrected to `root:adm 0640`.
  The installer and validator now enforce these boundaries on every deployment.
- Production validation passed configuration checksum, journald integrity,
  logrotate parsing, directory/file permissions, Docker rotation, auditd
  rotation, service, and metrics-only Alloy checks. All four containers remained
  healthy with zero restarts, the Nginx metrics oneshot returned `success`,
  Alloy remote-write failures were zero, and the host had zero failed units.
- On 1 August 2026, the account owner confirmed the dedicated server service
  status is `Normal` and OVHcloud Monitoring is `Enabled with proactive
  intervention`. The Network Security Dashboard showed no suspicious activity
  for the selected one-year period, which OVHcloud documents as the normal
  result when no attack has targeted the service. Provider monitoring is active;
  recipient/contact delivery confirmation remains pending under Item 16.
- On 1 August 2026, the account owner confirmed the administrative, technical,
  and billing contacts all resolve to the same OVHcloud account and that no
  second trusted OVHcloud operator is currently available. No account
  identifier was recorded, and the contacts remain unchanged. This is one
  notification and management path rather than independent escalation
  coverage. No acceptance of the residual single-operator risk has been
  recorded; the two controls remain visible and in progress under Item 16.

### 13. Stage and validate the migration

**Status:** `[x]` Completed and verified (2026-08-01)

- [x] Build the OVH environment without changing production DNS.
- [x] Restore a recent production database copy.
- [x] Validate core backend and catalog functions.
- [x] Validate Redis-dependent behaviour.
- [x] Validate the scheduler without producing duplicate live actions.
- [x] Validate incoming WhatsApp webhook processing.
- [x] Validate outbound WhatsApp behaviour in a safe test scope.
- [x] Run application, load, backup, recovery, and security smoke tests.
- [x] Record performance baselines.
- [x] Obtain explicit go/no-go approval.

Progress evidence (2026-08-01):

- The private OVH environment built in Items 8–12 passed its runtime and
  PostgreSQL/Redis hardening validators before the restore. All monitoring and
  backup timers were active, Alloy was ready with zero failed remote-write
  samples, the host had zero failed units, and production DNS still resolved to
  the Hetzner origin. No DNS setting changed.
- A fresh encrypted dual-destination OVH pre-change backup completed with
  service result `success` and exit status `0` before any application process
  was stopped.
- Read-only Hetzner preflight found PostgreSQL `16.13` and 54 public tables. An
  ephemeral official PostgreSQL 16 client validated dump connectivity without
  displaying the database URL or records. The custom-format dump was streamed
  over encrypted SSH into OVH without a workstation file, validated with
  `pg_restore --list`, and retained as a `root:root 0600` recovery artifact.
- The OVH scheduler and backend were stopped before import. The dump restored
  atomically with `pg_restore --single-transaction`; only staging Redis was
  flushed. The existing PostgreSQL role policy was reapplied and the complete
  PostgreSQL/Redis hardening validator passed.
- Only the private backend was restarted. It returned healthy and loopback
  `/health` returned `200`; the scheduler remains intentionally stopped so no
  restored job can send a live action. The restored database contains the
  expected 54-table schema and non-sensitive validation confirmed production
  data is present.
- Private read-only checks returned `200` for health, catalog taxonomy, active
  products, pricing, and one product detail. Response structures were valid,
  active catalog data was present, and the taxonomy/product caches were created
  in authenticated staging Redis with valid TTLs.
- The non-live application suite initially exposed a timezone-boundary fixture
  defect in the 23 July reminder regression test. The fixture now builds an exact user-local occasion calendar day; application behavior is unchanged.
  The targeted regression and full suite then passed with `797 passed, 3 skipped`.
- The current release symlink predates the newer source-tree monitoring
  validator path, so that one path-based invocation was unavailable. Installed
  service checks independently confirmed Alloy and every monitoring/backup
  timer active; the newer validator must ship with the next reviewed release.
- A non-sending scheduler probe registered all seven expected MVP jobs while
  replacing `scheduler.start()` with a no-op, so no job executed. Two live
  PostgreSQL sessions proved advisory lock `41051` permits one holder, rejects
  a concurrent holder, and can be reacquired after release. The real scheduler
  container remains stopped against the restored data.
- A temporary migrated scratch database isolated the WhatsApp smoke from the
  restored production copy. One synthetic `hi` traversed the real webhook and
  Gemini path; Meta signature validation was bypassed only inside that test
  process and outbound Meta delivery was intercepted. The deployed endpoint
  separately rejected an unsigned payload with `403`. The scratch database and
  synthetic staging Redis state were removed, and catalog caches were
  repopulated.
- A bounded loopback-only read load test completed with zero errors. `/health`
  served 1,000 requests at concurrency 20 at approximately 530.8 requests/s
  (`p50` 31.2 ms, `p95` 34.0 ms, `p99` 196.2 ms). The cached product catalog
  served 300 requests at concurrency 10 at approximately 205.2 requests/s
  (`p50` 37.2 ms, `p95` 192.2 ms, `p99` 203.3 ms).
- Post-load security assertions passed: hostile Host rejected with `400`,
  production docs and the Sentry debug route returned `404`, unauthenticated
  admin access and unsigned WhatsApp returned `403`, no Wishkr container had a
  wildcard-published port, the backend remained healthy with zero restarts,
  datastore hardening passed, and the host had zero failed units.
- A fresh encrypted dual-destination backup of the restored production copy
  completed successfully. Isolated restore drills from both R2 and OVH Backup
  Storage each restored 539 files/directories with matching checksums, validated
  Redis, and restored all 54 PostgreSQL tables; both scratch restores cleaned
  up successfully.
- During this pre-cutover validation window, `wishkr-scheduler-1` remains
  intentionally stopped. Container-health alerts for that service may fire and
  are expected maintenance signals, not permission to restart it before the
  Hetzner scheduler is paused and final synchronization is approved.
- On 1 August 2026, the account owner gave `GO` to begin Item 14 cutover
  planning only. The decision did not authorize a TTL change, service stop,
  final synchronization, DNS change, scheduler start, live provider/customer
  action, or retirement of Hetzner.

### 14. Cut over production

**Status:** `[~]` Gate D passed; observation in progress through 03:25:10 IST

- [x] Prepare the planning-only cutover, validation, and rollback runbook.
- [x] Lower relevant DNS TTLs before the migration window.
- [x] Announce the change window and rollback criteria.
- [x] Pause writes or perform the approved final synchronization procedure.
- [x] Take and verify a final pre-cutover backup.
- [x] Apply final database and object-storage synchronization.
- [x] Change API DNS to the Cloudflare-proxied OVH origin.
- [x] Verify TLS, API, catalog, scheduler, and WhatsApp flows.
- [ ] Monitor errors, latency, queues, scheduled jobs, and system resources.
- [ ] Roll back if a predefined rollback condition is met.

Planning evidence (2026-08-01):

- The phase-specific approval gates, hard write freeze, final PostgreSQL
  synchronization, shared-R2 validation, Cloudflare DNS change, singleton
  scheduler transition, monitoring thresholds, and pre-/post-write rollback
  branches are documented in
  [`OVHCLOUD_MIGRATION_CUTOVER_PLAN_2026-08-01.md`](./OVHCLOUD_MIGRATION_CUTOVER_PLAN_2026-08-01.md).
- The plan leaves every post-TTL Item 14 control unchecked. The maintenance
  window was selected as 1 August 2026 starting at 01:26 IST with a 30-minute
  maximum write freeze. Execution requires a new approval at each externally
  visible or write-affecting gate.
- Gate A was authorized and completed on 1 August 2026. At 01:33 IST,
  Cloudflare authoritative DNS and two independent public resolvers returned
  the unchanged Hetzner address with a 60-second TTL. The record remained
  DNS-only and public health remained `200`; no service, origin target, proxy
  state, or scheduler changed. Gate B may not begin before 01:39 IST and still
  requires explicit approval.
- Gate B began at 01:39:33 IST. Hetzner web and scheduler are stopped with no
  client writer or scheduler lock, while its PostgreSQL and Redis remain
  healthy. The final encrypted backup passed both destinations; the frozen
  PostgreSQL 16 archive transferred with matching checksum and restored into a
  fresh OVH database. All 54 tables, Alembic revision, row-count fingerprint,
  and sequence-state fingerprint matched before activation. Role policy,
  datastore hardening, shared-R2 equivalence/canary, private backend/catalog,
  Redis cache, security-negative, exposure, monitoring, and backup checks pass.
  The OVH scheduler remains stopped and DNS remains on Hetzner pending Gate C.
- Gate C completed at approximately 01:52 IST. Authoritative DNS and two public
  resolvers returned only Cloudflare anycast addresses at TTL `300`. Public
  Cloudflare TLS/health, taxonomy, products, pricing, product detail, cache
  bypass, unsigned WhatsApp rejection, `308` redirect, and direct-origin block
  checks passed. Three consecutive health probes returned `200`; OVH Nginx
  reported zero 5xx responses, both collection paths passed, Alloy was ready,
  and the host had zero failed units. The write freeze lasted about 13 minutes.
  Hetzner web/scheduler remain stopped and the OVH scheduler remains stopped
  pending Gate D.
- Gate D's single controlled WhatsApp test passed: exactly one signed inbound,
  one successful reply, and one durable `done` record were observed with zero
  webhook, rate-limit, or backpressure errors. The OVH scheduler then started
  healthy with one lock holder and all seven MVP jobs; the complete runtime
  validator passed and no unexpected send occurred.
- The first observation checkpoint detected that lock `41051` had disappeared
  while the scheduler container remained healthy. PostgreSQL confirmed its
  one-minute idle-in-transaction timeout terminated the lock session. The OVH
  scheduler was stopped at 02:06:30 IST; Hetzner web/scheduler remain stopped,
  while the OVH backend continues serving public traffic. A local fix commits
  the lock-acquisition transaction without releasing the session-level lock;
  focused regression tests pass `4/4`.
- The account owner authorized the narrow hotfix and scheduler-only Gate D
  retry. The reviewed release differs from the deployed base by only
  `app/main.py`; its checksum, secret scan, image vulnerability scan, rollback
  image, and rollback release link passed. The running backend was not
  restarted. The scheduler registered seven jobs and retained one idle lock
  holder with no open transaction through 149 seconds, with zero restarts,
  errors, outbound sends, or database timeout terminations. The full runtime
  validator and Hetzner stopped/no-lock checks passed. The fresh observation
  window began at 02:25:10 IST and its complete baseline was healthy.

### 15. Stabilize and retire Hetzner

**Status:** `[ ]` Not started

- [ ] Keep Hetzner available as a rollback target during the stability period.
- [ ] Confirm that no production DNS or integrations still target Hetzner.
- [ ] Confirm successful backups and a post-cutover restoration test.
- [ ] Rotate credentials that were present on the old servers.
- [ ] Export required logs and audit evidence.
- [ ] Securely destroy remaining sensitive data.
- [ ] Cancel Hetzner resources only after written approval.
- [ ] Update architecture, operations, and incident-response documentation.

### 16. Complete OVHcloud notification ownership and escalation

**Status:** `[~]` In progress (2026-08-01)

- [ ] Verify OVHcloud DDoS and hardware notifications.
- [ ] Document alert ownership and escalation contacts.

Provider monitoring is active, but all three OVHcloud contact roles currently
resolve to one account and no second trusted operator is available. Keep the
contacts unchanged; do not create a duplicate/shared account or simulate DDoS
or hardware failure. Complete this item when legitimate notification-delivery
evidence and an approved ownership/escalation arrangement are available.

## Security controls intentionally deferred unless required

- CrowdSec or Wazuh, after alert ownership and response procedures are defined.
- Ubuntu Pro/USG CIS auditing, after baseline hardening and compatibility testing.
- ClamAV, if the server begins accepting or processing untrusted file uploads.
- Cloudflare Tunnel, if reducing direct origin exposure justifies the added dependency.
- A second compute node, when higher availability is required.

## Change log

| Date | Item | Change | Verification/evidence | Operator |
|---|---|---|---|---|
| 2026-07-31 | Document | Created migration and security checklist | File created; Item 1 marked in progress | Codex |
| 2026-07-31 | Access bootstrap | Generated temporary Ed25519 migration key outside the repository | Public-key fingerprint: `SHA256:uz+Ap8U998IrUqSau/3WbyXubwe325gmQmm2B7+wjFY` | Codex |
| 2026-07-31 | Access bootstrap | User generated a permanent, passphrase-protected Ed25519 key for OVH administration | Public-key fingerprint: `SHA256:P/vYBSOluci1rzCgWjbTdE6GZrDKeJfuQTymU0xXURk`; key comment: `karan-wishkr-ovh` | Karan |
| 2026-07-31 | 1 — Provider accounts | Enabled OVHcloud two-factor authentication | Confirmed by account owner before OS installation | Karan |
| 2026-07-31 | 2 — OS and storage | Approved Ubuntu 24.04 LTS installation using two NVMe disks and software RAID1 | Installer summary reviewed before confirmation | Karan / Codex |
| 2026-07-31 | 2 — OS and storage | OVHcloud reported successful installation and normal service status | Ubuntu Server 24.04 LTS; boot mode set to disk | Karan / OVHcloud |
| 2026-07-31 | 2 — OS and storage | Recorded first-seen SSH ED25519 host-key fingerprint before login | `SHA256:r8/Lea6hoGEpwrIAfvslO7fsS0sxIk0JfIxzKg8lgHs` | Codex |
| 2026-07-31 | 2 — OS and storage | Verified UEFI boot, RAID1, filesystems, swap, and both EFI system partitions | `/dev/md2` and `/dev/md3` healthy `[UU]`; root ext4 approximately 467 GiB; matching EFI boot files on both NVMe drives | Codex |
| 2026-07-31 | 2 — OS and storage | Completed controlled reboot test | Boot ID changed from `77b6f0b9-a7d9-4af2-b5cf-a1946b2bc9c1` to `2f243cbb-9838-4d4e-a5b5-8aa946372376`; RAID remained `[UU]` | Codex |
| 2026-07-31 | 3 — Administrative access | Created named `karan` administrator and installed the verified personal Ed25519 key | Fresh login and non-interactive sudo test passed; key fingerprint `SHA256:P/vYBSOluci1rzCgWjbTdE6GZrDKeJfuQTymU0xXURk` | Codex |
| 2026-07-31 | 3 — Administrative access | Installed and validated SSH hardening policy | `sshd -t` passed; public-key-only authentication; root SSH rejected; X11 and agent forwarding disabled; TCP forwarding limited to local | Codex |
| 2026-07-31 | 3 — Administrative access | Removed `ubuntu` from root-equivalent LXD group and created locked `wishkr` service identity | `ubuntu` no longer belongs to `lxd`; `wishkr` uses `/usr/sbin/nologin` with no sudo privileges | Codex |
| 2026-07-31 | 3 — Administrative access | Verified direct user access through the `wishkr-ovh` SSH alias | Confirmed by account owner after host-key fingerprint verification | Karan |
| 2026-07-31 | 4 — Base OS hardening | Applied all current Ubuntu updates and configured unattended security maintenance | No pending upgrades; automatic reboots disabled; `needrestart` reports current kernel and microcode | Codex |
| 2026-07-31 | 4 — Base OS hardening | Installed persistent bounded journald, disabled coredump storage, and applied conservative sysctl hardening | Journal verification passed; 1 GiB/30-day cap; redirects/source routing disabled for IPv4 and IPv6 | Codex |
| 2026-07-31 | 4 — Base OS hardening | Disabled unused generic OVH image services while retaining patched packages | VM tools, modem, multipath/iSCSI, LXD installer, Snap, UDisks, and ZFS services inactive; `ubuntu-server` metapackage retained | Codex |
| 2026-07-31 | 4 — Base OS hardening | Verified final host health after deferred service restart | UTC/NTP synchronized; no failed units; no reboot required; fresh SSH login passed | Codex |
| 2026-07-31 | 5 — Network firewalls | Installed WireGuard administrative VPN for the named Mac peer | Server `10.77.0.1/24`; client `10.77.0.2/32`; live handshake and encrypted traffic verified | Karan / Codex |
| 2026-07-31 | 5 — Network firewalls | Set WireGuard MTU to `1280` after path-MTU testing | Packets around 1.35 KB succeeded while larger packets were lost; VPN SSH succeeded after applying MTU `1280` to both peers | Codex |
| 2026-07-31 | 5 — Network firewalls | Enabled UFW default-deny for IPv4/IPv6 and removed public SSH | Public UDP `51820` only; TCP `22` permitted only on `wg-admin`; firewall logging set to low | Codex |
| 2026-07-31 | 5 — Network firewalls | Performed external TCP exposure test | Public TCP `22`, `80`, `443`, `5432`, `6379`, and `8000` blocked; VPN SSH remained available | Codex |
| 2026-07-31 | 5 — Network firewalls | Switched the `wishkr-ovh` SSH alias to the VPN address | Direct `ssh wishkr-ovh` login succeeded through `10.77.0.1`; server recorded client source `10.77.0.2` | Karan |
| 2026-07-31 | 5 — Network firewalls | Enabled and verified the OVHcloud Edge Network Firewall for public IPv4 | Authorised TCP-established responses, WireGuard UDP `51820`, UDP DNS/NTP responses, and ICMP; refused all remaining IPv4; fresh VPN SSH, DNS, HTTPS, NTP, WireGuard, and UFW checks passed | Karan / Codex |
| 2026-07-31 | 6 — Host security | Verified AppArmor enforcement on Ubuntu 24.04 | AppArmor `4.0.1` service/module active; 117 profiles loaded; no profiled process unexpectedly unconfined | Codex |
| 2026-07-31 | 6 — Host security | Installed and verified Fail2ban for SSH | Journald backend and nftables action active; 5 retries/10 minutes; one-hour escalating ban; reserved-IP ban/unban test passed | Codex |
| 2026-07-31 | 6 — Host security | Installed and verified Linux Audit | Early-boot audit enabled; 26 targeted rules; 512 MiB bounded retention; keyed sudo query passed; post-reboot `lost 0`; RAID and all security services healthy | Codex |
| 2026-07-31 | 6 — Hardware monitoring | Installed and verified NVMe SMART and temperature monitoring | Both drives SMART passed with zero media/errors; 20%/22% wear baseline; short self-tests completed without error; journal alerts active | Codex |
| 2026-07-31 | 6 — Hardware monitoring | Configured and tested mdadm RAID monitoring | Both RAID1 arrays healthy; daily monitor and consistency timers active; journal alert-hook tests passed for `/dev/md2` and `/dev/md3`; initramfs updated | Codex |
| 2026-07-31 | 6 — Host security | Ran and reviewed the initial Lynis audit | 259 tests; no warnings; hardening index 71; architecture-specific exceptions and accepted follow-ups documented | Codex |
| 2026-07-31 | 6 — Privileged access | Retired duplicate bootstrap access and reviewed privilege boundaries | `karan` is sole active SSH/sudo administrator; root SSH disabled; `ubuntu` locked/non-login/no-sudo; `wishkr` non-login/no-sudo; no unsafe world-writable paths | Codex |
| 2026-07-31 | 6 — Security auditing | Enabled persistent recurring Lynis audits | Vendor daily timer active with up to 30-minute jitter; persistent catch-up enabled by managed systemd drop-in | Codex |
| 2026-07-31 | 6 — File integrity | Installed and baselined AIDE and debsums | Final full-root baseline contains 103,033 entries; independent AIDE comparison found no differences; all installed package checksums passed; daily persistent timer active | Codex |
| 2026-07-31 | 6 — Kernel hardening | Applied architecture-safe sysctl, core-dump, and uncommon-protocol controls | Accepted Lynis controls active; Docker/WireGuard/OVH recovery exceptions retained; DCCP, SCTP, RDS, and TIPC blocked; fresh VPN SSH passed | Codex |
| 2026-07-31 | 6 — Security verification | Re-ran Lynis and completed end-to-end host health checks | Hardening index 77 with zero warnings; no failed units, pending upgrades, or reboot; RAID, SMART, audit, firewall, VPN, services, and timers healthy | Codex |
| 2026-07-31 | 7 — Docker runtime | Installed and hardened Docker Engine from its verified official repository | Engine `29.7.0`; Compose `5.3.1`; Unix socket only; live restore/AppArmor/seccomp/cgroup v2 active; bounded compressed logs; Docker group has no members | Codex |
| 2026-07-31 | 7 — Container networking | Installed persistent IPv4/IPv6 forwarding controls and isolated networks | Public-interface traffic dropped in `DOCKER-USER`; `wishkr-app` and internal `wishkr-data` created; VPN SSH remained healthy; no forbidden service listeners | Codex |
| 2026-07-31 | 7 — Deployment definition | Added and policy-validated the OVH Compose architecture | All services non-root/read-only/capability-free/resource-limited/health-checked; backend binds localhost; PostgreSQL and Redis publish no ports | Codex |
| 2026-07-31 | 7 — Image security | Installed isolated Trivy `0.72.0` scanner and remediated all release-gating findings | Backend initially had 10 HIGH findings and official PostgreSQL `gosu` had 14 HIGH/1 CRITICAL; final backend, PostgreSQL derivative, and Redis scans contain zero HIGH/CRITICAL findings | Codex |
| 2026-07-31 | 7 — Compatibility verification | Ran the complete backend suite against the exact final container image | `797 passed, 3 skipped`; non-root PostgreSQL/Redis live query test passed; all disposable Docker objects were removed | Codex |
| 2026-07-31 | 7 — Integrity verification | Rebuilt and independently checked the post-Docker AIDE baseline | No differences across 103,881 entries; `debsums` clean; zero failed units, pending upgrades, or reboot requirement | Codex |
| 2026-07-31 | 9 — Nginx and origin TLS | Installed and hardened the OVH reverse proxy and Cloudflare Origin CA certificate | Nginx configuration valid and active; TLS 1.2/1.3 only; VPN health returned 200; certificate valid through 30 July 2029 | Karan / Codex |
| 2026-07-31 | 9 — Cloudflare staging | Staged strict TLS, Authenticated Origin Pulls, API cache bypass, signed-webhook exclusions, and method-preserving HTTPS redirect | Dashboard changes confirmed by account owner; origin HTTPS restricted to all 15 current Cloudflare IPv4 ranges; production DNS remains on Hetzner pending cutover | Karan / Codex |
| 2026-07-31 | 10 — PostgreSQL security and tuning | Introduced SCRAM-only application/admin role separation and explicit resource, WAL, timeout, and privacy-aware slow-query settings | Disposable migration tests passed; `wishkr` is non-superuser owner of 57 application relations; PostgreSQL `16.14` healthy after controlled restart | Codex |
| 2026-07-31 | 10 — Redis security and tuning | Applied bounded memory, LRU eviction, protected mode, RDB snapshots, and AOF `everysec` persistence | Redis `7.4.10` authenticated and healthy; persistence status clean; PostgreSQL/Redis publish no host ports | Codex |
| 2026-07-31 | 10 — Recovery and runtime verification | Captured and verified a root-only pre-change PostgreSQL dump, then ran complete data-service and application validation | Database dump readable; all four containers healthy; singleton scheduler/advisory lock, authenticated queries, loopback API, Compose, Nginx, and exposure checks passed | Codex |
| 2026-07-31 | 11 — Backups and disaster recovery | Implemented and verified encrypted dual-destination backups, hourly health monitoring, complete restore drills, and the off-host bare-metal recovery runbook | R2 and OVH FTPS copies verified; PostgreSQL/Redis restoration passed from both destinations; recovery controls stored in private GitHub commit `39b49c2` | Karan / Codex |
| 2026-07-31 | 12 — External uptime | Created and verified a one-minute Sentry Uptime monitor and connected email alert for the public API health endpoint | Successful check-ins and notification delivery confirmed; independent external request returned HTTP `200` and `{"status":"ok"}` | Karan / Codex |
| 2026-07-31 | 12 — Host metrics ingestion | Installed a metrics-only, non-root, systemd-hardened Grafana Alloy collector using a checksum-pinned release and root-only token file | Loopback-only health passed; 422 samples sent, zero failed; no collector/authentication errors or failed units; RAID remained `[UU]` | Karan / Codex |
| 2026-07-31 | 12 — Host resource alerts | Installed the Grafana Linux Node dashboards and alert rules and verified the default email notification path end to end | 24 integration rules cover CPU, memory, load, filesystems, inodes, disk I/O, network, systemd, textfile metrics, and RAID; a temporary firing alert reached the operations mailbox in 31 seconds and was deleted | Karan / Codex |
| 2026-07-31 | 12 — NVMe alerts | Installed a hardened five-minute SMART textfile exporter and imported 12 editable Grafana-managed alert rules | Both devices report healthy SMART, cool temperatures, full spare capacity, no media errors, and all collection, health, wear, temperature, and error rules are active and Normal | Karan / Codex |
| 2026-07-31 | 12 — Container alerts | Installed a least-privilege one-minute container-state exporter and imported nine editable Grafana-managed alert rules | All four expected containers were healthy with zero restarts, restart loops, or OOM kills; all nine rules were confirmed active and Normal after evaluation | Karan / Codex |
| 2026-07-31 | 12 — Datastore metrics | Installed a hardened one-minute PostgreSQL/Redis readiness and persistence exporter and staged 14 editable Grafana-managed alert rules | Fixed authenticated queries passed; PostgreSQL recovery/checksum/connection state and Redis RDB/AOF state were healthy; timer active, unit exposure `3.9 OK`, and Alloy remote write had zero failed samples | Codex |
| 2026-07-31 | 12 — Datastore alerts | Imported and resumed 14 PostgreSQL/Redis readiness, connection-pressure, recovery, checksum, RDB, and AOF alert rules | All 14 `wishkr-datastores` rules were confirmed active and Normal after evaluation | Karan / Codex |
| 2026-07-31 | 12 — TLS metrics | Installed a hardened six-hour public-certificate validity/expiry exporter and staged seven Grafana-managed rules | Certificate parse, validity, hostname, permissions, timer, sandbox, Alloy delivery, and installed-file checksums passed; private key is inaccessible to the oneshot | Codex |
| 2026-07-31 | 12 — TLS alerts | Imported and resumed seven public-certificate parse, validity, hostname, and expiry alert rules | All seven `wishkr-tls-certificate` rules were confirmed active and Normal after evaluation | Karan / Codex |
| 2026-07-31 | 12 — Nginx metrics | Added a loopback-only aggregate status endpoint, timestamp/status-only metrics log, hardened one-minute exporter, and nine staged Grafana-managed rules | Nginx config/reload, loopback bind, privacy-safe parsing, timer, sandbox, Alloy delivery, and installed-file checksums passed; initial 5xx/429/499 counts were zero | Codex |
| 2026-07-31 | 12 — Nginx alerts | Imported and resumed nine availability, collection, parse, connection, request-volume, and response-status alert rules | All nine `wishkr-nginx` rules were confirmed active and Normal after evaluation | Karan / Codex |
| 2026-08-01 | 12 — Sentry error reporting | Submitted one isolated, PII-free synthetic exception through the production SDK and removed the temporary debug route | Event `9fe67efb6d7f4852bcdeb1609c91adbc` was confirmed visible in `wishkr-prod-1`; backend remained healthy and `/sentry-debug` returned `404` | Karan / Codex |
| 2026-08-01 | 12 — Important-log retention | Installed and validated a source-controlled bounded local-retention policy and removed unintended Alloy log access | Journald, Nginx, Docker, auditd, Wishkr operations, and Trivy scopes have explicit limits; sensitive Nginx logs are `root:adm 0640`; Alloy has no log-reader groups or Loki configuration; all services remained healthy | Codex |
| 2026-08-01 | 12 / 16 — Provider notifications | Closed Item 12's implemented monitoring scope and transferred its two unresolved provider-notification ownership controls intact to new Item 16 | Provider monitoring is active; all contact roles still resolve to one account, no second trusted operator is available, and neither remaining control was waived or hidden | Karan / Codex |
| 2026-08-01 | 13 — Staging database restore | Took a fresh encrypted OVH backup, streamed a validated production PostgreSQL dump from Hetzner, and restored it atomically with application processes stopped | Root-only archive validated; role policy and datastore hardening passed; backend restarted healthy while scheduler remains intentionally stopped | Codex |
| 2026-08-01 | 13 — Private application validation | Ran restored-data health, catalog, pricing, product-detail, Redis-cache, and complete non-live application tests | Private HTTP structures and Redis TTLs passed; reminder fixture timezone boundary corrected; `797 passed, 3 skipped` | Codex |
| 2026-08-01 | 13 — Scheduler and WhatsApp safety | Validated scheduler registration/locking without executing jobs and ran an isolated webhook/Gemini smoke with outbound Meta delivery intercepted | Seven MVP jobs registered; singleton lock lifecycle passed; unsigned deployed webhook rejected; scratch database and synthetic cache state removed | Codex |
| 2026-08-01 | 13 — Load, security, backup and recovery | Ran bounded private load/security checks, backed up the restored production copy, and restored it from both encrypted destinations | Zero load-test errors; security assertions and health passed; R2 and OVH drills each restored 539 files/directories and 54 PostgreSQL tables with valid checksums | Codex |
| 2026-08-01 | 13 / 14 — Go decision and cutover planning | Account owner gave GO for Item 14 planning only; prepared the source-controlled cutover and rollback runbook | Item 13 closed; all Item 14 live actions remain unchecked and require phase-specific approval | Karan / Codex |
| 2026-08-01 | 14 — TTL preparation | Selected the immediate maintenance window, announced the 30-minute write-freeze limit and rollback criteria, and lowered the existing DNS-only API TTL to 60 seconds | Authoritative DNS, `1.1.1.1`, and `8.8.8.8` returned the unchanged Hetzner target at TTL 60; DNS remained unproxied and health returned `200` | Karan / Codex |
| 2026-08-01 | 14 — Write freeze and final synchronization | Stopped Hetzner scheduler/web, backed up OVH staging, streamed and restored the final frozen PostgreSQL archive, activated the validated database, reset staging Redis, and validated shared R2 | No source writer/lock; checksums, 54-table schema, Alembic, row/sequence fingerprints, role policy, datastore hardening, private API/catalog/cache, R2 canary, monitoring, and backup checks passed; DNS unchanged and OVH scheduler stopped | Karan / Codex |
| 2026-08-01 | 14 — Cloudflare DNS cutover | Changed the existing API record to the reviewed OVH origin and enabled Cloudflare proxying | Authoritative and public DNS returned Cloudflare anycast; public TLS/health/catalog/signature/redirect and direct-origin controls passed; Nginx 5xx was zero; write freeze ended after about 13 minutes | Karan / Codex |
| 2026-08-01 | 14 — Gate D live validation hold | Completed one controlled WhatsApp test, started one OVH scheduler, detected loss of its singleton lock, and stopped it safely | WhatsApp and initial runtime checks passed; zero unexpected sends; PostgreSQL's one-minute idle-in-transaction timeout terminated the uncommitted lock session; local regression fix passes `4/4` but is not deployed | Karan / Codex |
| 2026-08-01 | 14 — Gate D scheduler hotfix | Deployed a one-file transaction-finalization hotfix from the existing production base and repeated only scheduler validation | Secret and vulnerability scans passed; backend untouched; one healthy scheduler retained one idle lock with no open transaction beyond 149 seconds; seven jobs and complete runtime validation passed with zero errors or sends | Karan / Codex |

## Decisions and exceptions

Record material decisions, accepted risks, and temporary exceptions here.

| Date | Item | Decision or exception | Reason | Review date |
|---|---|---|---|---|
| 2026-07-31 | 3 — Administrative access | Temporarily retain `ubuntu` as a public-key-only break-glass account (superseded later the same day) | Reduced lockout risk until VPN access and provider recovery were validated | Completed in Item 6 |
| 2026-07-31 | 6 — Administrative access | Retire active access for the temporary `ubuntu` bootstrap account | WireGuard SSH and OVHcloud recovery paths were validated; only `karan` now requires active SSH/sudo access | At migration acceptance, remove dated disabled recovery copies |
| 2026-07-31 | 3 / 5 — Administrative network access | Use WireGuard instead of a source-IP allowlist | Supports roaming from dynamic networks while public SSH remains closed | Review annually |
| 2026-07-31 | 4 / 5 — IPv6 | Retain IPv6 and apply equivalent host-firewall policy | Avoids hidden unfiltered exposure and preserves future dual-stack operation | Item 5 |
| 2026-07-31 | 6 — Kernel hardening | Retain `kernel.modules_disabled=0`, restricted `kernel.sysrq=176`, and loose `rp_filter=2` | Required for upcoming Docker modules, OVHcloud console recovery, and WireGuard/asymmetric container routing respectively | After Docker deployment |
| 2026-07-31 | 7 — Docker administration | Keep human administrators out of the `docker` group | Docker socket access is root-equivalent; `karan` will use audited `sudo docker` commands | Review annually |
| 2026-07-31 | 7 — Trivy installation | Pin and checksum-verify Trivy `0.72.0` directly from its immutable release | Avoided affected `0.69.4` artifacts and mutable package-repository resolution after the 2026 supply-chain incident | On each scanner upgrade |
| 2026-07-31 | 7 — PostgreSQL image | Run a minimal derivative of the pinned official PostgreSQL image as UID/GID `70` and remove `gosu` | Upstream `gosu` was unused in a non-root start but contained 14 HIGH/1 CRITICAL fixable Go findings | On every PostgreSQL image refresh |
| 2026-07-31 | 7 — Deployment boundary | Do not start production application services during runtime hardening | Item 7 proves the engine, policy, images, and disposable data-service behavior; secrets/data restoration and live deployment belong to Item 8 | Item 8 |
| 2026-08-01 | 12 / 16 — Provider notification ownership | Transfer the two incomplete OVHcloud notification/ownership controls from completed Item 12 to new Item 16 without waiving them | No second trusted OVHcloud operator is currently available; staging validation can continue while the gap remains visible | Item 16 |
| 2026-08-01 | 13 — Scheduler maintenance state | Keep the OVH scheduler stopped after restoring production data | Starting it while Hetzner remains active could duplicate reminders, retries, reconciliation, or other live actions; monitoring alerts are expected during this controlled window | Item 14 final synchronization and cutover |
| 2026-08-01 | 14 — Cutover consistency model | Use a hard Hetzner web/scheduler write freeze, a PostgreSQL 16 custom dump restored into a fresh OVH database, no authoritative Redis migration, and shared-R2 equivalence validation | The application has no maintenance-mode switch; PostgreSQL is authoritative, Redis is bounded runtime state, and both environments use the existing application R2 store | Before Gate B execution |
| 2026-08-01 | 14 — Approval boundary | Treat the Item 13 GO as authorization to plan only; require separate approval for TTL preparation, freeze/final sync, DNS, live validation/scheduler start, and rollback | Prevents planning approval from causing an externally visible or destructive production change | At every Item 14 gate |
