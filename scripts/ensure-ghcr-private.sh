#!/usr/bin/env bash
# Keep ghcr.io/<owner>/<package> private.
#
# A workflow that creates a package with GITHUB_TOKEN inherits the visibility
# of the repository. This repository is public, so that first push is public,
# anonymous pull succeeds, and GitHub will not make the package private again.
# The package REST API has GET and DELETE for a user package. It has no method
# that changes visibility, so PUT /user/packages/... returns 404. GITHUB_TOKEN
# can publish and, with admin on the package, delete. It cannot set visibility.
#
# --preflight refuses to continue unless the package already exists and the
# API says private. The publish job runs that before docker push.
# Without --preflight, the script checks one tag. An anonymous manifest pull
# must fail. A package the API says is public is deleted, and the script exits
# 1. A private package that is still anonymously pullable is left in place so
# a later GITHUB_TOKEN push does not create a new public package.

set -euo pipefail

log() {
  printf '[ghcr-private] %s\n' "$*" >&2
}

# mode, visibility, anonymous HTTP code -> ok | delete | inherit | setup | unknown
decide_privacy() {
  local mode="$1"
  local visibility="$2"
  local anon_code="${3:-}"
  if [[ "$visibility" == "public" ]]; then
    printf 'delete\n'
    return 0
  fi
  if [[ "$mode" == "preflight" ]]; then
    if [[ "$visibility" == "private" ]]; then
      printf 'ok\n'
    else
      printf 'setup\n'
    fi
    return 0
  fi
  if [[ "$anon_code" == "200" && "$visibility" == "private" ]]; then
    printf 'inherit\n'
    return 0
  fi
  if [[ "$anon_code" == "200" ]]; then
    printf 'delete\n'
    return 0
  fi
  if [[ "$visibility" == "private" && "$anon_code" =~ ^(401|403|404)$ ]]; then
    printf 'ok\n'
    return 0
  fi
  printf 'unknown\n'
}

print_manual_setup() {
  local owner="$1"
  local package="$2"
  cat <<EOF >&2
[ghcr-private] GITHUB_TOKEN cannot create this package as private from a public repository.
[ghcr-private] Create it once from your own machine, then let Actions push into that existing package.
[ghcr-private] 1. Create a classic personal access token with write:packages and delete:packages.
[ghcr-private]    Fine-grained tokens cannot push to GHCR. Do not put this token in the repository.
[ghcr-private] 2. Push a placeholder that does not set org.opencontainers.image.source:
[ghcr-private]      printf '%s' "\$GHCR_PAT" | docker login ghcr.io -u ${owner} --password-stdin
[ghcr-private]      docker pull alpine:3.20
[ghcr-private]      docker tag alpine:3.20 ghcr.io/${owner}/${package}:private-bootstrap
[ghcr-private]      docker push ghcr.io/${owner}/${package}:private-bootstrap
[ghcr-private]    A personal-account push that is not made with GITHUB_TOKEN defaults to private.
[ghcr-private] 3. Open https://github.com/users/${owner}/packages/container/${package}/settings
[ghcr-private] 4. Under Danger Zone, visibility must be Private. If it says Public, delete the
[ghcr-private]    package and repeat step 2. GitHub does not allow a public package to become private.
[ghcr-private] 5. Under Manage Actions access, add ${owner}/unifi-os-server-docker with the Admin role.
[ghcr-private]    Admin lets this workflow push and delete. Write is not enough for the delete call.
[ghcr-private] 6. Leave Inherit access from repository turned off. On a public repository that
[ghcr-private]    inheritance lets anonymous clients pull even when visibility is Private.
[ghcr-private]    Connecting the repository from this settings page does not turn inheritance on
[ghcr-private]    unless you select that option.
[ghcr-private] 7. Re-run Build and publish on main. After the real tags exist you can delete the
[ghcr-private]    private-bootstrap version from the package's Manage versions page.
EOF
}

anon_manifest_code() {
  local owner="$1"
  local package="$2"
  local tag="$3"
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

read_package_visibility() {
  local owner="$1"
  local package="$2"
  local token="$3"
  local endpoint code
  local api="https://api.github.com"
  local accept="application/vnd.github+json"
  local api_version="2022-11-28"
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
      jq -r '.visibility // empty' /tmp/ghcr-pkg.json
      return 0
    fi
    log "GET ${endpoint} returned HTTP ${code}"
  done
  return 1
}

delete_package() {
  local owner="$1"
  local package="$2"
  local token="$3"
  local endpoint code
  local api="https://api.github.com"
  local accept="application/vnd.github+json"
  local api_version="2022-11-28"
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

explain_delete() {
  log "The package is public, or an anonymous client can pull it and the API does not say it is private."
  log "GitHub's package REST API has no visibility update. PUT and PATCH are not documented and return 404."
  log "GITHUB_TOKEN cannot make a package private. A public package cannot be made private again."
  log "Deleting the package so a publicly visible copy is not left online."
}

main() {
  local mode="check"
  if [[ "${1:-}" == "--preflight" ]]; then
    mode="preflight"
    shift
  fi

  local owner="${1:?owner}"
  local package="${2:?package}"
  local tag="${3:-}"
  local token="${GITHUB_TOKEN:?GITHUB_TOKEN is required}"
  local settings_url="https://github.com/users/${owner}/packages/container/${package}/settings"
  local visibility="" anon="" decision="unknown"
  local attempt=1 max_attempts=12

  if [[ "$mode" == "check" && -z "$tag" ]]; then
    log "A tag is required for the anonymous pull check."
    exit 1
  fi
  if [[ "$mode" == "preflight" ]]; then
    max_attempts=3
  fi

  while [[ "$attempt" -le "$max_attempts" ]]; do
    visibility=""
    anon=""
    if [[ "$mode" == "check" ]]; then
      anon="$(anon_manifest_code "$owner" "$package" "$tag")"
      log "anonymous manifest for ${tag}, attempt ${attempt}: HTTP ${anon}"
    fi
    if visibility="$(read_package_visibility "$owner" "$package" "$token")"; then
      log "package API visibility: ${visibility:-unknown}"
    else
      visibility=""
      log "package metadata is not readable yet"
    fi
    decision="$(decide_privacy "$mode" "$visibility" "$anon")"
    if [[ "$decision" != "unknown" ]]; then
      break
    fi
    attempt=$((attempt + 1))
    sleep 10
  done

  case "$decision" in
    ok)
      if [[ "$mode" == "preflight" ]]; then
        log "Package ${owner}/${package} already exists and is private. Publishing into it."
      else
        log "Confirmed ${owner}/${package}:${tag} is private and not anonymously pullable."
      fi
      log "Inherit access from repository must stay off: ${settings_url}"
      exit 0
      ;;
    inherit)
      log "Anonymous pull succeeded. Package API visibility: private."
      log "A public repository grants that pull when the package inherits repository permissions."
      log "Open ${settings_url}"
      log "Turn off Inherit access from repository, remove any public access, and invite people explicitly."
      log "The package was left in place. Deleting it would make the next GITHUB_TOKEN push create a public package."
      exit 1
      ;;
    delete)
      explain_delete
      if ! delete_package "$owner" "$package" "$token"; then
        log "GITHUB_TOKEN could not delete the package. Delete it at ${settings_url}"
      fi
      print_manual_setup "$owner" "$package"
      exit 1
      ;;
    setup)
      log "No private package named ${owner}/${package} is visible to GITHUB_TOKEN."
      print_manual_setup "$owner" "$package"
      exit 1
      ;;
    *)
      log "Could not confirm that ${owner}/${package} is private."
      log "Open ${settings_url} and check the visibility before using the image."
      print_manual_setup "$owner" "$package"
      exit 1
      ;;
  esac
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  main "$@"
fi
