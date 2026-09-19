#!/usr/bin/env bash
# install-sops.sh - install a pinned, checksum-verified sops binary.
#
# The ONE home of the sops pin. Three callers, so the version and the release
# checksums are not repeated anywhere else:
#   - compose/openclaw-gateway/Dockerfile  (the gateway resolver runs sops in
#     the container; this file lives here because that Dockerfile's build
#     context is this directory)
#   - scripts/bootstrap-vps.sh             (the host: rendering the master
#     file needs sops on a rebuilt box)
#   - .github/workflows/ci.yml             (the tests job proves the gateway
#     file decrypts on the box)
#
# Usage: install-sops.sh <dest-dir>      -> <dest-dir>/sops, mode 0755
#
# sops decrypts age recipients natively, so no age binary is needed anywhere
# a machine decrypts. age-keygen is only used by a human minting a key
# (docs/secrets-runbook.md). Checksums come from the release's
# sops-v<ver>.checksums.txt; bump the three literals together.

set -euo pipefail

SOPS_VERSION="3.13.3"
SOPS_SHA256_ARM64="53b0abacd38ef1b12a66d6c100956691b9cefce018d91f81e73ddf7438b94d77"
SOPS_SHA256_AMD64="e5bec3346a873ae91d871550f3e698c1aad962aff462a080e40f25fde17fef6b"

dest="${1:?usage: install-sops.sh <dest-dir>}"

case "$(uname -m)" in
  aarch64|arm64) arch=arm64; sha="${SOPS_SHA256_ARM64}" ;;
  x86_64|amd64)  arch=amd64; sha="${SOPS_SHA256_AMD64}" ;;
  *) echo "install-sops: unsupported architecture $(uname -m)" >&2; exit 1 ;;
esac

if [[ -x "${dest}/sops" ]] && "${dest}/sops" --version 2>/dev/null | grep -q "^sops ${SOPS_VERSION}\b"; then
  echo "install-sops: ${dest}/sops is already ${SOPS_VERSION}"
  exit 0
fi

tmp="$(mktemp)"
trap 'rm -f "${tmp}"' EXIT
curl -fsSL --retry 3 -o "${tmp}" \
  "https://github.com/getsops/sops/releases/download/v${SOPS_VERSION}/sops-v${SOPS_VERSION}.linux.${arch}"
echo "${sha}  ${tmp}" | sha256sum -c - >/dev/null
install -d -m 0755 "${dest}"
install -m 0755 "${tmp}" "${dest}/sops"
echo "install-sops: installed sops ${SOPS_VERSION} (${arch}) at ${dest}/sops"
