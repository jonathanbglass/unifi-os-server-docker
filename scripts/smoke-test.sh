#!/usr/bin/env bash
# Start UniFi OS Server and MongoDB with docker compose and wait for a listener.
#
# Always required:
#   - MongoDB becomes healthy
#   - the UniFi OS Server container stays running
#   - the entrypoint logs that it is starting systemd
# UI check (SMOKE_REQUIRE_UI, default true):
#   - HTTPS on 127.0.0.1:11443, or HTTP on 127.0.0.1:8080, returns any status
#
# This does not log in, adopt a device, or prove that every UniFi service is ready.
# A listener only shows that something in the container accepted a TCP connection.

set -euo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$repo_root"

root="${SMOKE_DATA_ROOT:-$repo_root/data/smoke}"
logdir="$root/logs"
mkdir -p "$logdir"

export COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-uos-smoke}"
export UOS_IMAGE="${UOS_IMAGE:-unifi-os-server:local}"
export UOS_SYSTEM_IP="${UOS_SYSTEM_IP:-127.0.0.1}"
export DATA_DIR="$root/unifi-data"
export UNIFI_DIR="$root/var-lib-unifi"
export MONGODB_LIB_DIR="$root/var-lib-mongodb"
export RABBITMQ_SSL_DIR="$root/etc-rabbitmq-ssl"
export MONGO_DATA_DIR="$root/mongodb"
export MONGO_USER="${MONGO_USER:-}"
export MONGO_PASS="${MONGO_PASS:-}"

require_ui="${SMOKE_REQUIRE_UI:-true}"
mongo_timeout="${SMOKE_MONGO_TIMEOUT:-180}"
ui_timeout="${SMOKE_UI_TIMEOUT:-480}"
ui_port="${UI_PORT:-11443}"

compose() {
  if [[ "${SMOKE_PRIVILEGED:-false}" == "true" ]]; then
    docker compose -f docker-compose.yml -f compose.privileged.yml "$@"
  else
    docker compose "$@"
  fi
}

# Invoked by the EXIT trap. Shellcheck cannot see that reference.
# shellcheck disable=SC2317
cleanup() {
  local status=$?
  mkdir -p "$logdir"
  compose logs --no-color --tail 300 > "$logdir/compose.log" 2>&1 || true
  compose ps > "$logdir/ps.txt" 2>&1 || true
  compose down --remove-orphans > "$logdir/down.log" 2>&1 || true
  if [[ "$status" -eq 0 && "${SMOKE_KEEP:-}" != "1" ]]; then
    # Container processes create root-owned files on the bind mounts.
    rm -rf "$root" || true
  else
    printf 'Smoke logs and data kept at %s\n' "$root"
  fi
}
trap cleanup EXIT

compose up -d

deadline=$((SECONDS + mongo_timeout))
mongo_ok=false
while (( SECONDS < deadline )); do
  cid="$(compose ps -q mongodb || true)"
  if [[ -n "$cid" ]]; then
    health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$cid" || echo none)"
    if [[ "$health" == "healthy" ]]; then
      mongo_ok=true
      break
    fi
  fi
  sleep 5
done
if [[ "$mongo_ok" != "true" ]]; then
  printf 'MongoDB did not become healthy within %s seconds\n' "$mongo_timeout" >&2
  exit 1
fi
printf 'MongoDB is healthy\n'

deadline=$((SECONDS + 180))
systemd_ready=false
while (( SECONDS < deadline )); do
  cid="$(compose ps -q unifi-os-server || true)"
  if [[ -z "$cid" ]]; then
    printf 'UniFi OS Server container is not running\n' >&2
    exit 1
  fi
  running="$(docker inspect -f '{{.State.Running}}' "$cid" || echo false)"
  if [[ "$running" != "true" ]]; then
    printf 'UniFi OS Server container exited before systemd started\n' >&2
    compose logs --no-color --tail 200 unifi-os-server >&2 || true
    exit 1
  fi
  if compose logs unifi-os-server 2>&1 | grep -q 'starting systemd'; then
    systemd_ready=true
    break
  fi
  sleep 3
done
if [[ "$systemd_ready" != "true" ]]; then
  printf 'Entrypoint did not reach systemd within 180 seconds\n' >&2
  exit 1
fi
printf 'Entrypoint started systemd\n'

deadline=$((SECONDS + ui_timeout))
ui_ok=false
while (( SECONDS < deadline )); do
  https_code="$(curl -k -s -o /dev/null -w '%{http_code}' --max-time 5 "https://127.0.0.1:${ui_port}/" || true)"
  http_code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:8080/" || true)"
  if [[ -n "$https_code" && "$https_code" != "000" ]]; then
    printf 'Web UI on port %s responded HTTP %s\n' "$ui_port" "$https_code"
    ui_ok=true
    break
  fi
  if [[ -n "$http_code" && "$http_code" != "000" ]]; then
    printf 'Inform port 8080 responded HTTP %s\n' "$http_code"
    ui_ok=true
    break
  fi
  cid="$(compose ps -q unifi-os-server || true)"
  if [[ -n "$cid" ]]; then
    running="$(docker inspect -f '{{.State.Running}}' "$cid" || echo false)"
    if [[ "$running" != "true" ]]; then
      printf 'UniFi OS Server container exited while waiting for a listener\n' >&2
      exit 1
    fi
  fi
  sleep 10
done

if [[ "$ui_ok" == "true" ]]; then
  exit 0
fi

printf 'No listener on https://127.0.0.1:%s or http://127.0.0.1:8080 within %s seconds\n' "$ui_port" "$ui_timeout" >&2
if [[ "$require_ui" == "true" ]]; then
  exit 1
fi
printf 'SMOKE_REQUIRE_UI=false, so the boot wait is recorded and the script is exiting 0\n'
exit 0
