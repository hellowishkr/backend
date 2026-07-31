#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "ERROR: run as root" >&2
  exit 1
fi

source_dir=${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}

for required_file in \
  wishkr-backup \
  wishkr-backup-status \
  test-r2-restore.sh \
  wishkr-backup.service \
  wishkr-backup.timer \
  wishkr-backup-monitor.service \
  wishkr-backup-monitor.timer; do
  if [[ ! -f ${source_dir}/${required_file} ]]; then
    echo "ERROR: missing ${source_dir}/${required_file}" >&2
    exit 1
  fi
done

for command_name in docker restic; do
  if ! command -v "${command_name}" >/dev/null 2>&1; then
    echo "ERROR: required command not installed: ${command_name}" >&2
    exit 1
  fi
done

install -d -o root -g root -m 0700 \
  /var/backups/wishkr/staging \
  /var/cache/wishkr-restic \
  /var/lib/wishkr/backup

install -o root -g root -m 0750 "${source_dir}/wishkr-backup" /usr/local/sbin/wishkr-backup
install -o root -g root -m 0750 "${source_dir}/wishkr-backup-status" /usr/local/sbin/wishkr-backup-status
install -o root -g root -m 0750 "${source_dir}/test-r2-restore.sh" /usr/local/sbin/wishkr-test-backup-restore
install -o root -g root -m 0644 "${source_dir}/wishkr-backup.service" /etc/systemd/system/wishkr-backup.service
install -o root -g root -m 0644 "${source_dir}/wishkr-backup.timer" /etc/systemd/system/wishkr-backup.timer
install -o root -g root -m 0644 "${source_dir}/wishkr-backup-monitor.service" /etc/systemd/system/wishkr-backup-monitor.service
install -o root -g root -m 0644 "${source_dir}/wishkr-backup-monitor.timer" /etc/systemd/system/wishkr-backup-monitor.timer

systemctl daemon-reload
systemctl enable --now wishkr-backup.timer wishkr-backup-monitor.timer

echo "PASS: Wishkr backup service installed"
systemctl list-timers wishkr-backup.timer wishkr-backup-monitor.timer --no-pager
