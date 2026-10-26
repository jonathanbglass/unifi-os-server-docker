#!/usr/bin/env bash
# Start UniFi OS Server and MongoDB with docker compose and wait for a listener.
#
# Always required:
#   - MongoDB becomes healthy
#   - the UniFi OS Server container stays running
#   - PID 1 is systemd (the entrypoint has exec'd /sbin/init)
# UI check (SMOKE_REQUIRE_UI, default true):
#   - HTTPS on 127.0.0.1:11443, or HTTP on 127.0.0.1:8080, returns any status
#
# This does not log in, adopt a device, or prove that every UniFi service is ready.
# A listener only shows that something in the container accepted a TCP connection.
#
# Do not detect the handoff with `compose logs | grep -q` while `pipefail` is
# on. Once the journal follower is writing, grep -q exits on the match and
# compose logs dies with SIGPIPE. pipefail then reports failure even though
# the line is present. That is what failed the post-merge run on main.

comm_is_systemd() {
  local comm="$1"
  comm="${comm//[[:space:]]/}"
  case "$comm" in
    systemd|init) return 0 ;;
    *) return 1 ;;
  esac
}

read_pid1_comm() {
  local cid="$1"
  local pid="" comm=""
  pid="$(docker inspect -f '{{.State.Pid}}' "$cid" 2>/dev/null || true)"
  if [[ -n "$pid" && "$pid" != "0" && -r "/proc/${pid}/comm" ]]; then
    comm="$(tr -d '[:space:]' < "/proc/${pid}/comm" || true)"
  fi
  if [[ -z "$comm" ]]; then
    comm="$(timeout 5 docker exec "$cid" cat /proc/1/comm 2>/dev/null | tr -d '[:space:]' || true)"
  fi
  printf '%s\n' "$comm"
}

compose() {
  if [[ "${SMOKE_PRIVILEGED:-false}" == "true" ]]; then
    docker compose -f docker-compose.yml -f compose.privileged.yml "$@"
  else
    docker compose "$@"
  fi
}

# Container processes create root-owned files. A plain rm prints
# "Permission denied" for each of them and leaves the tree behind.
remove_smoke_data() {
  local target="$1"
  local image="${2:-${UOS_IMAGE:-unifi-os-server:local}}"
  [[ -e "$target" ]] || return 0
  if rm -rf "$target" 2>/dev/null; then
    return 0
  fi
  if command -v docker >/dev/null 2>&1; then
    docker run --rm --entrypoint /bin/sh \
      -v "${target}:/cleanup" \
      "$image" \
      -c 'cd /cleanup && find . -mindepth 1 -maxdepth 1 -exec rm -rf {} +' \
      >/dev/null 2>&1 || true
    rm -rf "$target" 2>/dev/null || true
  fi
  if [[ -e "$target" ]] && command -v sudo >/dev/null 2>&1; then
    sudo -n rm -rf "$target" >/dev/null 2>&1 || true
  fi
  if [[ -e "$target" ]]; then
    printf 'Could not remove smoke data at %s\n' "$target" >&2
    return 1
  fi
}

# Invoked from the EXIT trap, which shellcheck treats as unreachable.
# shellcheck disable=SC2317
dump_diagnostics() {
  local cid pid
  mkdir -p "$logdir"
  {
    printf '===== smoke diagnostics =====\n'
    date -u || true
    compose ps || true
    cid="$(compose ps -q unifi-os-server || true)"
    if [[ -n "$cid" ]]; then
      docker inspect -f 'Status={{.State.Status}} Running={{.State.Running}} ExitCode={{.State.ExitCode}} OOMKilled={{.State.OOMKilled}} Error={{.State.Error}} Pid={{.State.Pid}} RestartCount={{.RestartCount}}' "$cid" || true
      pid="$(docker inspect -f '{{.State.Pid}}' "$cid" 2>/dev/null || true)"
      if [[ -n "$pid" && "$pid" != "0" ]]; then
        printf 'host pid %s comm=' "$pid"
        cat "/proc/${pid}/comm" 2>/dev/null || true
        printf 'cmdline='
        tr '\0' ' ' < "/proc/${pid}/cmdline" 2>/dev/null || true
        printf '\n'
      fi
      printf 'pid1 comm via read_pid1_comm: '
      read_pid1_comm "$cid" || true
      printf 'marker: '
      if [[ -f "${DATA_DIR}/.uos-entrypoint-reached-systemd" ]]; then
        cat "${DATA_DIR}/.uos-entrypoint-reached-systemd" || true
      else
        printf 'absent\n'
      fi
      timeout 10 docker top "$cid" -o pid,comm,args || true
      timeout 15 docker exec "$cid" /bin/bash -c 'printf "container pid1 comm="; cat /proc/1/comm; echo --- ps ---; ps -eo pid,comm,args | head -n 40; echo --- journal ---; journalctl -n 80 --no-pager || true' || true
    else
      printf 'UniFi OS Server container id is empty\n'
    fi
    echo --- compose logs ---
    compose logs --no-color --tail 200 || true
  } > "$logdir/diagnostics.txt" 2>&1 || true
  cat "$logdir/diagnostics.txt" >&2 || true
}

# Invoked by the EXIT trap. Shellcheck cannot see that reference.
# shellcheck disable=SC2317
cleanup() {
  local status=$?
  set +e
  mkdir -p "$logdir"
  if [[ "$status" -ne 0 ]]; then
    dump_diagnostics
  fi
  compose logs --no-color --tail 300 > "$logdir/compose.log" 2>&1
  compose ps > "$logdir/ps.txt" 2>&1
  compose down --remove-orphans > "$logdir/down.log" 2>&1
  if [[ "$status" -eq 0 && "${SMOKE_KEEP:-}" != "1" ]]; then
    remove_smoke_data "$root" || true
  else
    printf 'Smoke logs and data kept at %s\n' "$root"
  fi
}

main() {
  set -euo pipefail

  repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
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
  systemd_timeout="${SMOKE_SYSTEMD_TIMEOUT:-180}"
  ui_timeout="${SMOKE_UI_TIMEOUT:-480}"
  ui_port="${UI_PORT:-11443}"

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

  deadline=$((SECONDS + systemd_timeout))
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
      exit 1
    fi
    comm="$(read_pid1_comm "$cid")"
    if comm_is_systemd "$comm"; then
      systemd_ready=true
      break
    fi
    sleep 3
  done
  if [[ "$systemd_ready" != "true" ]]; then
    printf 'Entrypoint did not reach systemd within %s seconds\n' "$systemd_timeout" >&2
    exit 1
  fi
  printf 'Entrypoint started systemd (PID 1 is %s)\n' "$comm"

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
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  main "$@"
fi
