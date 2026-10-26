#!/usr/bin/env bash
# Fail unless ghcr.io/<owner>/<package>:<tag> is private.
# An anonymous pull that succeeds means the package is public. GitHub does not
# let a public package become private again, so this script tries a visibility
# update and then deletes the package with GITHUB_TOKEN. Deletion is best
# effort: the workflow token may lack permission, and the job still fails.

set -euo pipefail

owner="${1:?owner}"
package="${2:?package}"
tag="${3:?tag}"
token="${GITHUB_TOKEN:?GITHUB_TOKEN is required}"

api="https://api.github.com"
accept="application/vnd.github+json"
api_version="2022-11-28"
settings_url="https://github.com/users/${owner}/packages/container/${package}/settings"

log() {
  printf '[ghcr-private] %s\n' "$*"
}

anon_manifest_code() {
  local scope token_json anon code
  scope="repository:${owner}/${package}:pull"
  token_json="$(curl -sS --retry 3 --retry-delay 2 \
    -H "Accept: application/json" \
    "https://ghcr.io/token?service=ghcr.io&scope=${scope}" || true)"
  anon="$(printf '%s' "$token_json" | jq -r '.token // empty' 2>/dev/null || true)"
  if [[ -n "$anon" ]]; then
    code="$(curl -sS -o /dev/null -w '%{http_code}' \
      -H "Authorization: Bearer ${anon}" \
      -H "Accept: application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json" \
      "https://ghcr.io/v2/${owner}/${package}/manifests/${tag}" || true)"
  else
    code="$(curl -sS -o /dev/null -w '%{http_code}' \
      "https://ghcr.io/v2/${owner}/${package}/manifests/${tag}" || true)"
  fi
  printf '%s' "$code"
}

read_package() {
  local endpoint code
  for endpoint in \
    "/user/packages/container/${package}" \
    "/users/${owner}/packages/container/${package}"
  do
    code="$(curl -sS -o /tmp/ghcr-pkg.json -w '%{http_code}' \
      -H "Authorization: Bearer ${token}" \
      -H "Accept: ${accept}" \
      -H "X-GitHub-Api-Version: ${api_version}" \
      "${api}${endpoint}" || true)"
    if [[ "$code" == "200" ]]; then
      return 0
    fi
  done
  return 1
}

try_put_private() {
  local endpoint code
  for endpoint in \
    "/user/packages/container/${package}" \
    "/users/${owner}/packages/container/${package}"
  do
    code="$(curl -sS -o /tmp/ghcr-put.json -w '%{http_code}' -X PUT \
      -H "Authorization: Bearer ${token}" \
      -H "Accept: ${accept}" \
      -H "Content-Type: application/json" \
      -H "X-GitHub-Api-Version: ${api_version}" \
      "${api}${endpoint}" \
      -d '{"visibility":"private"}' || true)"
    log "PUT ${endpoint} returned HTTP ${code}"
  done
}

delete_package() {
  local endpoint code
  for endpoint in \
    "/user/packages/container/${package}" \
    "/users/${owner}/packages/container/${package}"
  do
    code="$(curl -sS -o /tmp/ghcr-del.json -w '%{http_code}' -X DELETE \
      -H "Authorization: Bearer ${token}" \
      -H "Accept: ${accept}" \
      -H "X-GitHub-Api-Version: ${api_version}" \
      "${api}${endpoint}" || true)"
    log "DELETE ${endpoint} returned HTTP ${code}"
    if [[ "$code" == "204" || "$code" == "200" ]]; then
      return 0
    fi
  done
  return 1
}

public=false
confirmed_private=false
attempt=1
while [[ "$attempt" -le 12 ]]; do
  code="$(anon_manifest_code)"
  log "anonymous manifest for ${tag}, attempt ${attempt}: HTTP ${code}"
  if [[ "$code" == "200" ]]; then
    public=true
    break
  fi
  if read_package; then
    visibility="$(jq -r '.visibility // empty' /tmp/ghcr-pkg.json)"
    log "package API visibility: ${visibility:-unknown}"
    if [[ "$visibility" == "public" ]]; then
      public=true
      break
    fi
    if [[ "$visibility" == "private" && "$code" =~ ^(401|403|404)$ ]]; then
      confirmed_private=true
      break
    fi
  else
    log "package metadata is not readable yet"
  fi
  attempt=$((attempt + 1))
  sleep 10
done

if [[ "$public" == "true" ]]; then
  visibility=""
  if read_package; then
    visibility="$(jq -r '.visibility // empty' /tmp/ghcr-pkg.json)"
  fi
  log "Anonymous pull succeeded. Package API visibility: ${visibility:-unknown}."
  if [[ "$visibility" == "private" ]]; then
    log "The visibility is already private, but the tag is still anonymously pullable."
    log "A public repository can grant that access when the package inherits repository permissions."
    log "Open ${settings_url}"
    log "Turn off Inherit access from repository, remove any public access, and invite people explicitly."
    log "The package was left in place. Delete it yourself if it should not stay online."
    exit 1
  fi
  log "Trying to set visibility to private."
  try_put_private
  sleep 5
  visibility=""
  if read_package; then
    visibility="$(jq -r '.visibility // empty' /tmp/ghcr-pkg.json)"
  fi
  code="$(anon_manifest_code)"
  if [[ "$visibility" == "private" && "$code" != "200" ]]; then
    log "The package is private now and the tag is not anonymously pullable."
    log "If this repository is public, also turn off Inherit access from repository at ${settings_url}"
    exit 0
  fi
  log "Deleting the package so a publicly visible copy is not left online."
  if ! delete_package; then
    log "GITHUB_TOKEN could not delete the package."
  fi
  log "Open ${settings_url}"
  log "If the package is still public, delete it. GitHub does not allow a public package to become private."
  exit 1
fi

if [[ "$confirmed_private" != "true" ]]; then
  log "Could not confirm that ${owner}/${package}:${tag} is private."
  log "Open ${settings_url} and set the visibility to Private before using the image."
  exit 1
fi

log "Confirmed ${owner}/${package}:${tag} is private and not anonymously pullable."
log "Also turn off 'Inherit access from repository' if this repository is public."
log "Settings: ${settings_url}"
