#!/bin/bash
# Backup or restore the Network application database from the host.
#
# The container does not ship mongodump. The working invocation enters the
# container network namespace and runs the host mongodump against the
# bundled server:
#
#   pid=$(podman inspect uosserver --format '{{.State.Pid}}')
#   sudo nsenter -t "$pid" -n mongodump --port 27117 --db ace --out <dir>
#
# Defaults match that deployment. Override them for compose:
#   UOS_RUNTIME=docker
#   UOS_CONTAINER=<unifi-os-server container>
#   UNIFI_MONGO_CONTAINER=<mongodb container>
#   UNIFI_MONGO_PORT=27017
#
# Restore uses mongorestore --drop for the dumped database and asks you to
# confirm. It does not restart the Network application.

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${script_dir}/unifi-host-lib.sh"

usage() {
  cat <<'EOF' >&2
Usage:
  unifi-backup.sh backup [--out DIR]
  unifi-backup.sh restore --from DIR [--yes]

Options:
  --runtime podman|docker     default: UOS_RUNTIME, or podman if it can see the container
  --container NAME            default: UOS_CONTAINER or uosserver
  --podman-user USER          default: UOS_PODMAN_USER (empty runs podman as the current user)
  --mongo-container NAME      namespace to enter for mongodump; default is --container
  --port PORT                 default: UNIFI_MONGO_PORT or 27117
  --db NAME                   default: ace
  --out DIR                   backup directory to create
  --from DIR                  backup directory to restore
  --yes                       restore without the interactive confirmation
EOF
}

main() {
  set -euo pipefail
  local action=""
  local runtime="${UOS_RUNTIME:-}"
  local container="${UOS_CONTAINER:-uosserver}"
  local podman_user="${UOS_PODMAN_USER:-}"
  local mongo_container="${UNIFI_MONGO_CONTAINER:-}"
  local port="${UNIFI_MONGO_PORT:-27117}"
  local db="${UNIFI_MONGO_DB:-ace}"
  local out=""
  local from=""
  local assume_yes="false"

  while [[ $# -gt 0 ]]; do
    case "$1" in
      backup|restore)
        action="$1"
        ;;
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
      --out)
        out="${2:-}"
        shift
        ;;
      --from)
        from="${2:-}"
        shift
        ;;
      --yes)
        assume_yes="true"
        ;;
      -h|--help)
        usage
        return 0
        ;;
      *)
        unifi_host_log "unknown argument: $1"
        usage
        return 2
        ;;
    esac
    shift
  done

  if [[ "$action" != "backup" && "$action" != "restore" ]]; then
    usage
    return 2
  fi
  if [[ ! "$port" =~ ^[0-9]+$ ]]; then
    unifi_host_fail "port must be numeric"
    return 1
  fi
  if [[ ! "$db" =~ ^[A-Za-z0-9_-]+$ ]]; then
    unifi_host_fail "database name must be letters, numbers, underscore, or hyphen"
    return 1
  fi
  if [[ -z "$runtime" ]]; then
    runtime="$(detect_runtime "$container" "$podman_user")" || return 1
  fi
  if [[ "$runtime" != "podman" && "$runtime" != "docker" ]]; then
    unifi_host_fail "runtime must be podman or docker"
    return 1
  fi
  if [[ -z "$mongo_container" ]]; then
    mongo_container="$container"
  fi

  local tool pid
  if [[ "$action" == "backup" ]]; then
    tool="mongodump"
  else
    tool="mongorestore"
  fi
  if ! command -v "$tool" >/dev/null 2>&1; then
    unifi_host_fail "${tool} is not on this host. Install MongoDB database tools. The container does not include ${tool}."
    return 1
  fi

  pid="$(unifi_container_pid "$runtime" "$mongo_container" "$podman_user")" || return 1
  unifi_host_log "using ${runtime} container ${mongo_container} pid ${pid} port ${port} db ${db}"

  if [[ "$action" == "backup" ]]; then
    run_backup "$pid" "$port" "$db" "$out"
  else
    run_restore "$pid" "$port" "$db" "$from" "$assume_yes"
  fi
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

run_backup() {
  local pid="$1"
  local port="$2"
  local db="$3"
  local out="$4"
  local stamp
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  if [[ -z "$out" ]]; then
    out="${UNIFI_BACKUP_DIR:-./unifi-backups}/ace-${stamp}"
  fi
  if [[ -e "$out" ]]; then
    unifi_host_fail "backup path already exists: ${out}"
    return 1
  fi
  mkdir -p "$out"
  chmod 700 "$out" || true
  # Absolute path: nsenter -n keeps the host mount namespace.
  out="$(cd "$out" && pwd)"
  unifi_host_log "writing backup to ${out}"
  if ! unifi_run_in_netns "$pid" mongodump --port "$port" --db "$db" --out "$out"; then
    unifi_host_fail "mongodump failed. Partial output may be in ${out}"
    return 1
  fi
  cat > "${out}/backup-info.txt" <<EOF
created_utc=${stamp}
port=${port}
db=${db}
EOF
  chmod 600 "${out}/backup-info.txt" || true
  unifi_host_log "backup complete: ${out}"
}

confirm_restore() {
  local assume_yes="$1"
  local from="$2"
  local reply
  if [[ "$assume_yes" == "true" ]]; then
    return 0
  fi
  if [[ ! -t 0 ]]; then
    unifi_host_fail "re-run with --yes to restore ${from}. Restore drops existing collections in the dumped database."
    return 1
  fi
  printf 'Type restore to drop and replace the dumped database from %s: ' "$from" >&2
  IFS= read -r reply || true
  if [[ "$reply" != "restore" ]]; then
    unifi_host_fail "confirmation did not match; nothing was restored"
    return 1
  fi
}

run_restore() {
  local pid="$1"
  local port="$2"
  local db="$3"
  local from="$4"
  local assume_yes="$5"
  local dump
  if [[ -z "$from" || ! -d "$from" ]]; then
    unifi_host_fail "restore needs an existing --from directory"
    return 1
  fi
  from="$(cd "$from" && pwd)"
  dump="$(resolve_dump_dir "$from")" || {
    unifi_host_fail "no ace dump or bson files in ${from}"
    return 1
  }
  confirm_restore "$assume_yes" "$from" || return 1
  unifi_host_log "restoring ${dump} into ${db} with --drop"
  unifi_run_in_netns "$pid" mongorestore --port "$port" --db "$db" --drop "$dump"
  unifi_host_log "restore complete. Restart the Network application before relying on the data: systemctl restart unifi"
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  main "$@"
fi
