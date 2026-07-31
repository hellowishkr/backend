#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
    echo "Run this installer as root" >&2
    exit 1
fi

# v0.72.0 is an immutable post-incident release and fixes the Trivy advisories
# published through 2026-06-30. The checksum-file digest is taken from the
# immutable GitHub release metadata.
TRIVY_VERSION="0.72.0"
CHECKSUM_FILE_SHA256="ebe9d19a774b950e240b1017a038e9b5a002ea068e02023369ff6d241c10c580"
RELEASE_BASE="https://github.com/aquasecurity/trivy/releases/download/v${TRIVY_VERSION}"
PACKAGE="trivy_${TRIVY_VERSION}_Linux-64bit.deb"
CHECKSUMS="trivy_${TRIVY_VERSION}_checksums.txt"
DOWNLOAD_DIR="/var/cache/wishkr-trivy"

install -d -o root -g root -m 0755 "${DOWNLOAD_DIR}"

curl -fsSL "${RELEASE_BASE}/${CHECKSUMS}" \
    -o "${DOWNLOAD_DIR}/${CHECKSUMS}"
printf '%s  %s\n' \
    "${CHECKSUM_FILE_SHA256}" "${DOWNLOAD_DIR}/${CHECKSUMS}" \
    | sha256sum --check --status

curl -fsSL "${RELEASE_BASE}/${PACKAGE}" \
    -o "${DOWNLOAD_DIR}/${PACKAGE}"

expected_package_sha256="$(
    awk -v package="${PACKAGE}" '$2 == package { print $1 }' \
        "${DOWNLOAD_DIR}/${CHECKSUMS}"
)"
if [ -z "${expected_package_sha256}" ]; then
    echo "Package checksum is missing from the verified release manifest" >&2
    exit 1
fi

printf '%s  %s\n' \
    "${expected_package_sha256}" "${DOWNLOAD_DIR}/${PACKAGE}" \
    | sha256sum --check --status

DEBIAN_FRONTEND=noninteractive apt-get install -y \
    "${DOWNLOAD_DIR}/${PACKAGE}"

installed_version="$(trivy --version | awk 'NR == 1 { print $2 }')"
if [ "${installed_version}" != "Version: ${TRIVY_VERSION}" ] \
    && [ "${installed_version}" != "${TRIVY_VERSION}" ]
then
    echo "Unexpected installed Trivy version: ${installed_version}" >&2
    exit 1
fi
