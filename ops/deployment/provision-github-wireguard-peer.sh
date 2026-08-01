#!/usr/bin/env bash
set -Eeuo pipefail

readonly WG_INTERFACE=wg-admin
readonly WG_CONFIG=/etc/wireguard/wg-admin.conf

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

if [[ ${EUID} -ne 0 ]]; then
  fail "run as root"
fi
if [[ $# -ne 2 ]]; then
  fail "usage: provision-github-wireguard-peer.sh PUBLIC_KEY ALLOWED_IP"
fi

peer_public_key=$1
allowed_ip=$2

[[ ${peer_public_key} =~ ^[A-Za-z0-9+/]{43}=$ ]] \
  || fail "WireGuard public key is malformed"
[[ ${allowed_ip} =~ ^10\.77\.0\.([0-9]{1,3})/32$ ]] \
  || fail "CI peer must use one address in the administrative VPN subnet"
peer_octet=${BASH_REMATCH[1]}
(( peer_octet >= 3 && peer_octet <= 254 )) \
  || fail "CI peer address is reserved or invalid"

[[ -f ${WG_CONFIG} && ! -L ${WG_CONFIG} ]] \
  || fail "WireGuard configuration is invalid"
[[ $(stat -c '%U:%G:%a' "${WG_CONFIG}") == root:root:600 ]] \
  || fail "WireGuard configuration ownership or mode is invalid"
wg show "${WG_INTERFACE}" >/dev/null 2>&1 \
  || fail "administrative WireGuard interface is unavailable"

if wg show "${WG_INTERFACE}" peers | grep -Fxq "${peer_public_key}"; then
  fail "WireGuard peer already exists"
fi
if wg show "${WG_INTERFACE}" allowed-ips \
  | awk '{print $2}' | tr ',' '\n' | grep -Fxq "${allowed_ip}"; then
  fail "WireGuard peer address is already assigned"
fi

backup="${WG_CONFIG}.pre-github-actions"
[[ ! -e ${backup} ]] || fail "WireGuard backup path already exists"
install -o root -g root -m 0600 "${WG_CONFIG}" "${backup}"

candidate_dir=$(mktemp -d /etc/wireguard/wishkr-ci.XXXXXX)
candidate="${candidate_dir}/wg-admin.conf"
cleanup() {
  rm -f "${candidate}"
  rmdir "${candidate_dir}" 2>/dev/null || true
}
trap cleanup EXIT HUP INT TERM

install -o root -g root -m 0600 "${WG_CONFIG}" "${candidate}"
{
  printf '\n# GitHub Actions deployment peer\n'
  printf '[Peer]\n'
  printf 'PublicKey = %s\n' "${peer_public_key}"
  printf 'AllowedIPs = %s\n' "${allowed_ip}"
} >>"${candidate}"

wg-quick strip "${candidate}" >/dev/null
install -o root -g root -m 0600 "${candidate}" "${WG_CONFIG}"

if ! wg set "${WG_INTERFACE}" peer "${peer_public_key}" allowed-ips "${allowed_ip}"; then
  install -o root -g root -m 0600 "${backup}" "${WG_CONFIG}"
  fail "failed to activate the dedicated WireGuard peer"
fi

wg show "${WG_INTERFACE}" peers | grep -Fxq "${peer_public_key}" \
  || fail "dedicated WireGuard peer is not active"

echo "PASS: dedicated GitHub Actions WireGuard peer installed"
