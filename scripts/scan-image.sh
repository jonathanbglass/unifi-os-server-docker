#!/usr/bin/env bash
# Scan a local Docker image with Trivy and write a SARIF report.
# TRIVY_EXIT_CODE defaults to 0 so upstream UniFi OS Server CVEs are reported
# without failing the build. Set it to 1 to make HIGH and CRITICAL findings fail.

set -euo pipefail

image="${1:?image reference}"
exit_code="${TRIVY_EXIT_CODE:-0}"
output="${TRIVY_SARIF:-trivy-results.sarif}"
trivy_image="${TRIVY_IMAGE:-ghcr.io/aquasecurity/trivy:0.76.1}"

case "$exit_code" in
  0|1) ;;
  *)
    printf 'TRIVY_EXIT_CODE must be 0 or 1\n' >&2
    exit 1
    ;;
esac

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$repo_root"

docker run --rm \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$repo_root:/work" \
  "$trivy_image" image \
  --severity HIGH,CRITICAL \
  --format sarif \
  --output "/work/${output}" \
  --exit-code "$exit_code" \
  --no-progress \
  "$image"
