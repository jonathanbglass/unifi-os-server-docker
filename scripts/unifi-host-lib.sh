#!/bin/bash
# Helpers for host-side backup and device recovery.
# Source this file. It does not contact a container until a function is called.

unifi_host_log() {
  printf '[unifi-host] %s\n' "$*" >&2
}

unifi_host_fail() {
  unifi_host_log "$*"
  return 1
}

normalize_mac() {
  local raw="$1"
  local hex
  hex="$(printf '%s' "$raw" | tr '[:upper:]' '[:lower:]' | tr -cd '0-9a-f')"
  if [[ ${#hex} -ne 12 ]]; then
    return 1
  fi
  printf '%s:%s:%s:%s:%s:%s\n' \
    "${hex:0:2}" "${hex:2:2}" "${hex:4:2}" "${hex:6:2}" "${hex:8:2}" "${hex:10:2}"
}

unifi_runtime_cmd() {
  local runtime="$1"
  local podman_user="${2:-}"
  shift 2
  if [[ "$runtime" == "podman" && -n "$podman_user" && "$(id -un)" != "$podman_user" ]]; then
    local uid runtime_dir
    uid="$(id -u "$podman_user")"
    runtime_dir="${UOS_PODMAN_RUNTIME_DIR:-/run/user/${uid}}"
    sudo -u "$podman_user" env "XDG_RUNTIME_DIR=${runtime_dir}" podman "$@"
    return
  fi
  "$runtime" "$@"
}

unifi_container_pid() {
  local runtime="$1"
  local container="$2"
  local podman_user="${3:-}"
  local pid
  pid="$(unifi_runtime_cmd "$runtime" "$podman_user" inspect "$container" --format '{{.State.Pid}}')" || return 1
  pid="${pid//[[:space:]]/}"
  if [[ -z "$pid" || "$pid" == "0" || ! "$pid" =~ ^[0-9]+$ ]]; then
    unifi_host_fail "container ${container} is not running"
    return 1
  fi
  printf '%s\n' "$pid"
}

# Run a command in the container network namespace. The mount namespace
# stays the host's, so mongodump and python3 are the host binaries and
# --out is a host path. UNIFI_NSENTER replaces nsenter in tests.
unifi_run_in_netns() {
  local pid="$1"
  shift
  if [[ -n "${UNIFI_NSENTER:-}" ]]; then
    "$UNIFI_NSENTER" -t "$pid" -n -- "$@"
    return
  fi
  if [[ "$(id -u)" -eq 0 ]]; then
    nsenter -t "$pid" -n -- "$@"
  else
    sudo nsenter -t "$pid" -n -- "$@"
  fi
}

unifi_restart_network() {
  local runtime="$1"
  local container="$2"
  local podman_user="${3:-}"
  unifi_host_log "restarting the Network application in ${container}"
  unifi_runtime_cmd "$runtime" "$podman_user" exec "$container" systemctl restart unifi
}

resolve_dump_dir() {
  local from="$1"
  if [[ -d "${from}/ace" ]]; then
    printf '%s\n' "${from}/ace"
    return 0
  fi
  if compgen -G "${from}/*.bson" >/dev/null; then
    printf '%s\n' "$from"
    return 0
  fi
  return 1
}
