#!/usr/bin/env bash
set -Eeuo pipefail

readonly DEPLOY_USER=wishkr-deploy
readonly HELPER_PATH=/usr/local/sbin/wishkr-deploy-release
readonly SUDOERS_PATH=/etc/sudoers.d/90-wishkr-deploy
readonly SSH_CONFIG_PATH=/etc/ssh/sshd_config.d/10-wishkr-hardening.conf

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

if [[ ${EUID} -ne 0 ]]; then
  fail "run as root"
fi
if [[ $# -ne 1 ]]; then
  fail "usage: provision-github-deploy-user.sh SSH_PUBLIC_KEY_FILE"
fi

public_key_file=$1
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(cd -- "${script_dir}/../.." && pwd)

[[ -f ${public_key_file} && ! -L ${public_key_file} ]] \
  || fail "SSH public key file is invalid"
[[ -f ${script_dir}/deploy-release.sh ]] || fail "deployment helper is missing"
[[ -f ${script_dir}/cleanup-docker.sh ]] || fail "Docker cleanup helper is missing"
[[ -f ${script_dir}/run-migrations.sh ]] || fail "migration helper is missing"
[[ -f ${script_dir}/90-wishkr-deploy.sudoers ]] || fail "sudoers policy is missing"
[[ -f ${repo_root}/ops/host-security/ssh/10-wishkr-hardening.conf ]] \
  || fail "SSH policy is missing"

read -r key_type key_material _ <"${public_key_file}"
[[ ${key_type} == ssh-ed25519 ]] || fail "deployment key must be Ed25519"
[[ ${key_material} =~ ^[A-Za-z0-9+/]+={0,2}$ ]] \
  || fail "deployment public key is malformed"
[[ $(wc -l <"${public_key_file}") -eq 1 ]] \
  || fail "deployment public key file must contain exactly one key"

visudo -cf "${script_dir}/90-wishkr-deploy.sudoers" >/dev/null

if ! id "${DEPLOY_USER}" >/dev/null 2>&1; then
  useradd --create-home --shell /bin/bash --user-group "${DEPLOY_USER}"
fi

[[ $(id -u "${DEPLOY_USER}") -ne 0 ]] || fail "deployment user cannot be UID 0"
usermod --lock "${DEPLOY_USER}"

deploy_home=$(getent passwd "${DEPLOY_USER}" | cut -d: -f6)
[[ -n ${deploy_home} && ${deploy_home} != / ]] || fail "invalid deployment home"

install -d -o "${DEPLOY_USER}" -g "${DEPLOY_USER}" -m 0700 \
  "${deploy_home}/.ssh"
authorized_keys=$(mktemp)
printf 'restrict %s %s wishkr-github-actions\n' \
  "${key_type}" "${key_material}" >"${authorized_keys}"
install -o "${DEPLOY_USER}" -g "${DEPLOY_USER}" -m 0600 \
  "${authorized_keys}" "${deploy_home}/.ssh/authorized_keys"
rm -f "${authorized_keys}"

install -o root -g root -m 0755 \
  "${script_dir}/deploy-release.sh" "${HELPER_PATH}"
install -o root -g root -m 0755 \
  "${script_dir}/cleanup-docker.sh" /usr/local/sbin/wishkr-cleanup-docker
install -o root -g root -m 0755 \
  "${script_dir}/run-migrations.sh" /usr/local/sbin/wishkr-run-migrations
install -o root -g root -m 0440 \
  "${script_dir}/90-wishkr-deploy.sudoers" "${SUDOERS_PATH}"
install -o root -g root -m 0644 \
  "${repo_root}/ops/host-security/ssh/10-wishkr-hardening.conf" \
  "${SSH_CONFIG_PATH}"

visudo -cf "${SUDOERS_PATH}" >/dev/null
sshd -t
systemctl reload ssh

sudo -u "${DEPLOY_USER}" test -r "${deploy_home}/.ssh/authorized_keys"
echo "PASS: dedicated GitHub deployment user, SSH policy, and sudo boundary installed"
