#!/bin/bash
# Pin the address UniFi devices use to reach this controller.
#
# The Network application reads system_ip from its data system.properties
# and, when that key is set, does not auto-detect an interface address.
# Auto-detection keeps bridges whose names do not start with "docker"
# (a br-* bridge at 172.18.0.1 is one of those) and can hand that address
# out as the inform URL. For the UOSSERVER model, a detected address of
# exactly 10.88.0.3 is replaced by the contents of /etc/host-ip. Other
# addresses are not. This script writes both on every start.
#
# The inform URL stored for adoption is separate: ace.setting key mgmt,
# fields override_inform_host and override_inform_host_location. --mongo-only
# sets those once MongoDB is up and restarts the Network application if the
# document changed.
#
# UOS_SYSTEM_IP is the name compose already uses. UNIFI_SYSTEM_IP and
# INFORM_HOST are accepted aliases. If more than one is set, they must match.

log() {
  printf '[uos-inform] %s\n' "$*" >&2
}

trim() {
  local value="$1"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  printf '%s' "$value"
}

validate_inform_host() {
  local host="$1"
  if [[ -z "$host" || ${#host} -gt 253 ]]; then
    return 1
  fi
  # Hostnames, IPv4, and IPv6. The character set is what gets written into
  # system.properties, a systemd unit, and a mongo update.
  [[ "$host" =~ ^[A-Za-z0-9._:-]+$ ]]
}

resolve_inform_host() {
  local primary alt_unifi alt_inform chosen name value
  primary="$(trim "${UOS_SYSTEM_IP:-}")"
  alt_unifi="$(trim "${UNIFI_SYSTEM_IP:-}")"
  alt_inform="$(trim "${INFORM_HOST:-}")"
  chosen=""
  for name in UOS_SYSTEM_IP UNIFI_SYSTEM_IP INFORM_HOST; do
    case "$name" in
      UOS_SYSTEM_IP) value="$primary" ;;
      UNIFI_SYSTEM_IP) value="$alt_unifi" ;;
      INFORM_HOST) value="$alt_inform" ;;
    esac
    [[ -n "$value" ]] || continue
    if ! validate_inform_host "$value"; then
      log "${name} must be a hostname or IP, without spaces or punctuation other than . _ : -"
      return 1
    fi
    if [[ -z "$chosen" ]]; then
      chosen="$value"
    elif [[ "$chosen" != "$value" ]]; then
      log "UOS_SYSTEM_IP, UNIFI_SYSTEM_IP, and INFORM_HOST disagree (${chosen} vs ${value})"
      return 1
    fi
  done
  if [[ -z "$chosen" ]]; then
    log "UOS_SYSTEM_IP is required. UNIFI_SYSTEM_IP and INFORM_HOST are accepted aliases. Set one to an address your UniFi devices can reach."
    return 1
  fi
  printf '%s' "$chosen"
}

warn_if_container_bridge() {
  local host="$1"
  local first second
  [[ "$host" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || return 0
  IFS=. read -r first second _ <<<"$host"
  if [[ "$first" == "172" && ( "$second" == "17" || "$second" == "18" ) ]]; then
    log "warning: ${host} is in 172.17.0.0/16 or 172.18.0.0/16. Those ranges are typical Docker bridges, and devices on the LAN often cannot open the inform port there."
  fi
}

is_ipv4() {
  local host="$1"
  local octet
  [[ "$host" =~ ^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$ ]] || return 1
  local o1 o2 o3 o4
  IFS=. read -r o1 o2 o3 o4 <<<"$host"
  for octet in "$o1" "$o2" "$o3" "$o4"; do
    if ((10#$octet > 255)); then
      return 1
    fi
  done
}

is_ip_literal() {
  local host="$1"
  if is_ipv4 "$host"; then
    return 0
  fi
  [[ "$host" == *:* && "$host" =~ ^[0-9A-Fa-f:]+$ ]]
}

set_property() {
  local file="$1"
  local key="$2"
  local value="$3"
  local tmp
  mkdir -p "$(dirname "$file")"
  touch "$file"
  tmp="$(mktemp)"
  awk -v k="$key" -v v="$value" '
    BEGIN { FS = "=" }
    $1 == k { next }
    { print }
    END { printf "%s=%s\n", k, v }
  ' "$file" > "$tmp"
  cat "$tmp" > "$file"
  rm -f "$tmp"
}

chown_unifi() {
  local path
  if ! id -u unifi >/dev/null 2>&1; then
    return 0
  fi
  for path in "$@"; do
    if [[ -e "$path" ]]; then
      chown unifi:unifi "$path" || true
    fi
  done
}

apply_properties() {
  local root="$1"
  local host="$2"
  local prefix="$root"
  local path real
  local -a candidates=()
  local -A seen=()
  prefix="${prefix%/}"
  candidates=(
    "${prefix}/usr/lib/unifi/data/system.properties"
    "${prefix}/var/lib/unifi/system.properties"
  )
  for path in "${candidates[@]}"; do
    mkdir -p "$(dirname "$path")"
    if real="$(readlink -f "$path" 2>/dev/null)"; then
      :
    else
      real="$path"
    fi
    if [[ -n "${seen[$real]:-}" ]]; then
      continue
    fi
    seen["$real"]=1
    set_property "$path" "system_ip" "$host"
    chown_unifi "$(dirname "$path")" "$path"
    log "system_ip=${host} in ${path}"
  done

  # /etc/host-ip is consulted only when UOSSERVER detection returns the
  # Podman address 10.88.0.3. The validator rejects a trailing newline and
  # rejects 0.0.0.0, and it expects an IP rather than a DNS name.
  if is_ip_literal "$host" && [[ "$host" != "0.0.0.0" ]]; then
    local host_ip_file="${prefix}/etc/host-ip"
    local current=""
    mkdir -p "${prefix}/etc"
    if [[ -f "$host_ip_file" ]]; then
      current="$(cat "$host_ip_file" 2>/dev/null || true)"
    fi
    if [[ "$current" != "$host" ]]; then
      printf '%s' "$host" > "$host_ip_file"
    fi
    chmod 644 "$host_ip_file" || true
    log "wrote ${host_ip_file}"
  else
    log "skipping /etc/host-ip because ${host} is not an IP literal"
  fi
}

write_units() {
  local root="$1"
  local host="$2"
  local prefix="${root%/}"
  local override="${UOS_INFORM_HOST_OVERRIDE:-true}"
  local mongo_internal="${MONGO_INTERNAL:-false}"
  local mongo_host mongo_port auth_required dropin service
  case "$override" in
    true|false) ;;
    *)
      log "UOS_INFORM_HOST_OVERRIDE must be true or false"
      return 1
      ;;
  esac
  case "$mongo_internal" in
    true|false) ;;
    *)
      log "MONGO_INTERNAL must be true or false"
      return 1
      ;;
  esac
  if [[ "$mongo_internal" == "true" ]]; then
    mongo_host="127.0.0.1"
    mongo_port="27117"
  else
    mongo_host="${MONGO_HOST:-mongodb}"
    mongo_port="${MONGO_PORT:-27017}"
  fi
  if ! [[ "$mongo_host" =~ ^[A-Za-z0-9._:-]+$ && "$mongo_port" =~ ^[0-9]+$ ]]; then
    log "MONGO_HOST or MONGO_PORT is not safe to write into a systemd unit"
    return 1
  fi
  if [[ -n "$(trim "${MONGO_USER:-}")" || -n "$(trim "${MONGO_PASS:-}")" ]]; then
    auth_required="true"
  else
    auth_required="false"
  fi
  dropin="${prefix}/etc/systemd/system/unifi.service.d/10-inform-host.conf"
  service="${prefix}/etc/systemd/system/uos-inform-override.service"
  mkdir -p "$(dirname "$dropin")" "${prefix}/etc/systemd/system/multi-user.target.wants"
  cat > "$dropin" <<EOF
[Service]
Environment=UOS_SYSTEM_IP=${host}
Environment=UOS_INFORM_HOST_OVERRIDE=${override}
Environment=MONGO_INTERNAL=${mongo_internal}
Environment=MONGO_HOST=${mongo_host}
Environment=MONGO_PORT=${mongo_port}
Environment=UOS_MONGO_AUTH_REQUIRED=${auth_required}
ExecStartPre=+/usr/local/bin/uos-apply-inform.sh --properties-only
EOF
  cat > "$service" <<EOF
[Unit]
Description=Pin the UniFi inform host override
After=unifi.service
ConditionPathExists=/usr/local/bin/uos-apply-inform.sh

[Service]
Type=oneshot
TimeoutStartSec=1800
Environment=UOS_SYSTEM_IP=${host}
Environment=UOS_INFORM_HOST_OVERRIDE=${override}
Environment=MONGO_INTERNAL=${mongo_internal}
Environment=MONGO_HOST=${mongo_host}
Environment=MONGO_PORT=${mongo_port}
Environment=UOS_MONGO_AUTH_REQUIRED=${auth_required}
ExecStart=/usr/local/bin/uos-apply-inform.sh --mongo-only
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF
  ln -sfn ../uos-inform-override.service \
    "${prefix}/etc/systemd/system/multi-user.target.wants/uos-inform-override.service"
  log "installed inform host units for ${host}"
}

mongo_python() {
  local script_dir
  script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  if [[ -f "${script_dir}/unifi_mongo.py" ]]; then
    printf '%s\n' "${script_dir}/unifi_mongo.py"
  elif [[ -f /usr/local/bin/unifi_mongo.py ]]; then
    printf '%s\n' /usr/local/bin/unifi_mongo.py
  else
    return 1
  fi
}

apply_mongo_override() {
  local host="$1"
  local mongo_host="$2"
  local mongo_port="$3"
  local python_bin="python3"
  local mongo_py status output
  if ! mongo_py="$(mongo_python)"; then
    log "unifi_mongo.py is not installed"
    return 1
  fi
  if ! command -v "$python_bin" >/dev/null 2>&1; then
    log "python3 is not available; the inform host override was not written"
    return 1
  fi
  set +e
  output="$("$python_bin" "$mongo_py" \
    --host "$mongo_host" \
    --port "$mongo_port" \
    --db ace \
    inform-override \
    --location "$host" 2>&1)"
  status=$?
  set -e
  printf '%s\n' "$output" >&2
  if [[ "$status" -eq 0 ]]; then
    printf '%s\n' "$output" | tail -n 1
    return 0
  fi
  if [[ "$status" -eq 2 ]]; then
    printf 'missing\n'
    return 0
  fi
  return 1
}

mongo_only() {
  local host="$1"
  local root="$2"
  local override="${UOS_INFORM_HOST_OVERRIDE:-true}"
  local mongo_internal="${MONGO_INTERNAL:-false}"
  local mongo_host mongo_port
  local timeout stamp result deadline
  if [[ "$override" != "true" ]]; then
    log "inform host override is disabled"
    return 0
  fi
  if [[ "${UOS_MONGO_AUTH_REQUIRED:-false}" == "true" || -n "$(trim "${MONGO_USER:-}")" || -n "$(trim "${MONGO_PASS:-}")" ]]; then
    log "MongoDB authentication is configured. The startup override does not authenticate. Set Inform Host Override in the Network application UI."
    return 0
  fi
  if [[ "$mongo_internal" == "true" ]]; then
    mongo_host="127.0.0.1"
    mongo_port="27117"
  else
    mongo_host="${MONGO_HOST:-mongodb}"
    mongo_port="${MONGO_PORT:-27017}"
  fi
  timeout="${UOS_INFORM_OVERRIDE_TIMEOUT:-600}"
  stamp="${root%/}/run/uos-inform-override-restarted"
  mkdir -p "$(dirname "$stamp")"
  deadline=$((SECONDS + timeout))
  while (( SECONDS < deadline )); do
    if result="$(apply_mongo_override "$host" "$mongo_host" "$mongo_port")"; then
      case "$result" in
        unchanged)
          log "inform host override already ${host}"
          return 0
          ;;
        changed)
          log "inform host override set to ${host}"
          if [[ -e "$stamp" ]]; then
            log "Network application was already restarted for this boot"
            return 0
          fi
          touch "$stamp"
          if command -v systemctl >/dev/null 2>&1; then
            log "restarting the Network application"
            systemctl restart unifi
          else
            log "systemctl is not available; restart the Network application so it reloads the override"
          fi
          return 0
          ;;
        missing)
          sleep 5
          ;;
        *)
          log "unexpected inform override result: ${result}"
          sleep 5
          ;;
      esac
    else
      sleep 5
    fi
  done
  log "ace.setting key mgmt was not ready within ${timeout}s. system_ip is still set. Set Inform Host Override in the UI if devices adopt against the wrong address."
  return 0
}

main() {
  set -euo pipefail
  local mode="" root="" host
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --print-host)
        mode="print"
        ;;
      --properties-only)
        mode="properties"
        ;;
      --install-units)
        mode="units"
        ;;
      --mongo-only)
        mode="mongo"
        ;;
      --root)
        root="${2:-}"
        shift
        ;;
      *)
        log "unknown argument: $1"
        return 2
        ;;
    esac
    shift
  done
  if [[ -z "$mode" ]]; then
    log "pass --print-host, --properties-only, --install-units, or --mongo-only"
    return 2
  fi
  host="$(resolve_inform_host)" || return 1
  warn_if_container_bridge "$host"
  case "$mode" in
    print)
      printf '%s\n' "$host"
      ;;
    properties)
      apply_properties "$root" "$host"
      ;;
    units)
      write_units "$root" "$host"
      ;;
    mongo)
      mongo_only "$host" "$root"
      ;;
  esac
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  main "$@"
fi
