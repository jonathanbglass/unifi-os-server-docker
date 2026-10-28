#!/bin/bash
# Drop a device that is stuck adopted while it checks in with the default key.
#
# The Network log line is "used default key in ADOPTING state, reject it!"
# and Remove stays greyed out. After a backup, this deletes that MAC from
# ace.device and ace.power_supervisor, then restarts only the Network
# application. The device comes back as pending adoption.
#
# The match is the mac field, compared as aa:bb:cc:dd:ee:ff, the same MAC
# in uppercase, and the 12 hex digits with no separators. Other fields are
# left alone. The printed summary omits keys and credentials.
#
# Confirmation is the MAC typed again, or --yes when stdin is not a terminal.
# --yes still takes a backup before it deletes anything. --dry-run only
# counts matching documents.

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${script_dir}/unifi-host-lib.sh"

usage() {
  cat <<'EOF' >&2
Usage:
  unifi-forget-device.sh [--yes] [--dry-run] MAC

MAC may be aa:bb:cc:dd:ee:ff, aa-bb-cc-dd-ee-ff, or aabbccddeeff.

Options match unifi-backup.sh:
  --runtime, --container, --podman-user, --mongo-container, --port, --db
  --yes       delete after the backup without retyping the MAC
  --dry-run   show matching records and exit
EOF
}

main() {
  set -euo pipefail
  local runtime="${UOS_RUNTIME:-}"
  local container="${UOS_CONTAINER:-uosserver}"
  local podman_user="${UOS_PODMAN_USER:-}"
  local mongo_container="${UNIFI_MONGO_CONTAINER:-}"
  local port="${UNIFI_MONGO_PORT:-27117}"
  local db="${UNIFI_MONGO_DB:-ace}"
  local assume_yes="false"
  local dry_run="false"
  local mac_arg=""

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --runtime)
        runtime="${2:-}"
        shift
        ;;
      --container)
        container="${2:-}"
        shift
        ;;
      --podman-user)
        podman_user="${2:-}"
        shift
        ;;
      --mongo-container)
        mongo_container="${2:-}"
        shift
        ;;
      --port)
        port="${2:-}"
        shift
        ;;
      --db)
        db="${2:-}"
        shift
        ;;
      --yes)
        assume_yes="true"
        ;;
      --dry-run)
        dry_run="true"
        ;;
      -h|--help)
        usage
        return 0
        ;;
      --)
        shift
        break
        ;;
      -*)
        unifi_host_log "unknown argument: $1"
        usage
        return 2
        ;;
      *)
        if [[ -n "$mac_arg" ]]; then
          unifi_host_fail "only one MAC address"
          return 1
        fi
        mac_arg="$1"
        ;;
    esac
    shift
  done
  if [[ $# -gt 0 ]]; then
    unifi_host_fail "unexpected arguments: $*"
    return 1
  fi
  if [[ -z "$mac_arg" ]]; then
    usage
    return 2
  fi

  local mac
  mac="$(normalize_mac "$mac_arg")" || {
    unifi_host_fail "not a MAC address: ${mac_arg}"
    return 1
  }
  if [[ -z "$runtime" ]]; then
    runtime="$(detect_runtime "$container" "$podman_user")" || return 1
  fi
  if [[ -z "$mongo_container" ]]; then
    mongo_container="$container"
  fi

  local script_dir mongo_py python_bin pid counts
  script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  mongo_py="${script_dir}/unifi_mongo.py"
  python_bin="$(command -v python3)"
  pid="$(unifi_container_pid "$runtime" "$mongo_container" "$podman_user")" || return 1

  counts="$(unifi_run_in_netns "$pid" "$python_bin" "$mongo_py" \
    --host 127.0.0.1 --port "$port" --db "$db" count-device --mac "$mac")"
  printf '%s\n' "$counts" >&2
  local device_count power_count
  device_count="$(printf '%s\n' "$counts" | python3 -c 'import json,sys; print(json.load(sys.stdin)["device"]["count"])')"
  power_count="$(printf '%s\n' "$counts" | python3 -c 'import json,sys; print(json.load(sys.stdin)["power_supervisor"]["count"])')"
  unifi_host_log "ace.device matches: ${device_count}; ace.power_supervisor matches: ${power_count}"
  if [[ "$device_count" == "0" && "$power_count" == "0" ]]; then
    unifi_host_log "no records for ${mac}. Nothing was deleted and the Network application was not restarted."
    return 0
  fi
  if [[ "$dry_run" == "true" ]]; then
    unifi_host_log "dry run; no backup, no delete, no restart"
    return 0
  fi
  confirm_mac "$mac" "$assume_yes" || return 1

  local stamp backup_dir
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  backup_dir="${UNIFI_BACKUP_DIR:-./unifi-backups}/forget-${mac//:/}-${stamp}"
  unifi_host_log "backing up before delete: ${backup_dir}"
  UOS_RUNTIME="$runtime" \
    UOS_CONTAINER="$container" \
    UOS_PODMAN_USER="$podman_user" \
    UNIFI_MONGO_CONTAINER="$mongo_container" \
    UNIFI_MONGO_PORT="$port" \
    UNIFI_MONGO_DB="$db" \
    bash "${script_dir}/unifi-backup.sh" backup --out "$backup_dir"

  local deleted
  deleted="$(unifi_run_in_netns "$pid" "$python_bin" "$mongo_py" \
    --host 127.0.0.1 --port "$port" --db "$db" delete-device --mac "$mac")"
  printf '%s\n' "$deleted"
  local device_deleted power_deleted
  device_deleted="$(printf '%s\n' "$deleted" | python3 -c 'import json,sys; print(json.load(sys.stdin)["device"])')"
  power_deleted="$(printf '%s\n' "$deleted" | python3 -c 'import json,sys; print(json.load(sys.stdin)["power_supervisor"])')"
  if [[ "$device_deleted" == "0" && "$power_deleted" == "0" ]]; then
    unifi_host_fail "backup is at ${backup_dir} but no documents were deleted"
    return 1
  fi
  unifi_restart_network "$runtime" "$container" "$podman_user"
  unifi_host_log "deleted device=${device_deleted} power_supervisor=${power_deleted} for ${mac}. It should appear pending adoption. Backup: ${backup_dir}"
}

detect_runtime() {
  local container="$1"
  local podman_user="$2"
  if command -v podman >/dev/null 2>&1; then
    if [[ -n "$podman_user" && "$(id -un)" != "$podman_user" ]]; then
      if unifi_runtime_cmd podman "$podman_user" inspect "$container" >/dev/null 2>&1; then
        printf 'podman\n'
        return 0
      fi
    elif podman inspect "$container" >/dev/null 2>&1; then
      printf 'podman\n'
      return 0
    fi
  fi
  if command -v docker >/dev/null 2>&1 && docker inspect "$container" >/dev/null 2>&1; then
    printf 'docker\n'
    return 0
  fi
  unifi_host_fail "could not find container ${container}. Set UOS_RUNTIME and UOS_CONTAINER."
  return 1
}

confirm_mac() {
  local mac="$1"
  local assume_yes="$2"
  local reply
  if [[ "$assume_yes" == "true" ]]; then
    return 0
  fi
  if [[ ! -t 0 ]]; then
    unifi_host_fail "re-run with --yes to delete records for ${mac} after a backup"
    return 1
  fi
  printf 'Type %s to back up ace, delete its device and Smart Power records, and restart the Network application: ' "$mac" >&2
  IFS= read -r reply || true
  if [[ "$reply" != "$mac" ]]; then
    unifi_host_fail "confirmation did not match; nothing was deleted"
    return 1
  fi
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  main "$@"
fi
