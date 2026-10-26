#!/bin/bash
# Runtime setup for UniFi OS Server under Docker, then hand off to systemd.
# The image is Ubiquiti's own rootfs. This script only prepares writable
# state, the inform address, and an external MongoDB before PID 1 starts.

set -euo pipefail

log() {
  printf '[uos-entrypoint] %s\n' "$*"
}

urlencode() {
  local value="$1"
  local index=0
  local character encoded
  encoded=""
  while [[ "$index" -lt "${#value}" ]]; do
    character="${value:index:1}"
    case "$character" in
      [a-zA-Z0-9.~_-])
        encoded+="$character"
        ;;
      *)
        printf -v character '%%%02X' "'$character"
        encoded+="$character"
        ;;
    esac
    index=$((index + 1))
  done
  printf '%s' "$encoded"
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

remove_property() {
  local file="$1"
  local key="$2"
  local tmp
  [[ -f "$file" ]] || return 0
  tmp="$(mktemp)"
  awk -v k="$key" 'BEGIN { FS = "=" } $1 == k { next } { print }' "$file" > "$tmp"
  cat "$tmp" > "$file"
  rm -f "$tmp"
}

ensure_dir() {
  local dir="$1"
  local owner="${2:-}"
  mkdir -p "$dir"
  chmod 755 "$dir" || true
  if [[ -n "$owner" ]] && id -u "$owner" >/dev/null 2>&1; then
    chown "${owner}:${owner}" "$dir" || true
  fi
}

if [[ -z "${UOS_SYSTEM_IP:-}" ]]; then
  log "UOS_SYSTEM_IP is required. Set it to an address your UniFi devices can reach."
  exit 1
fi

APP_MODEL="${APP_MODEL:-UOSSERVER}"
PRODUCT_NAME="${PRODUCT_NAME:-UniFi OS Server}"
APP_VERSION="${APP_VERSION:-${UOS_VERSION:-unknown}}"

case "$(dpkg --print-architecture)" in
  amd64) default_platform="linux-x64" ;;
  arm64) default_platform="arm64" ;;
  *)
    log "unsupported architecture: $(dpkg --print-architecture)"
    exit 1
    ;;
esac
FIRMWARE_PLATFORM="${FIRMWARE_PLATFORM:-$default_platform}"

log "firmware platform ${FIRMWARE_PLATFORM}"
log "product ${PRODUCT_NAME} model ${APP_MODEL} version ${APP_VERSION}"
log "system IP ${UOS_SYSTEM_IP}"

printf '%s\n' "$FIRMWARE_PLATFORM" > /usr/lib/platform
printf '%s\n' "$PRODUCT_NAME" > /usr/lib/product_name
printf '%s\n' "$APP_MODEL" > /usr/lib/app_model
if [[ "$APP_MODEL" != "PROTECT_SERVER" ]]; then
  # UniFi OS reads this exact dotted shape from /usr/lib/version.
  printf '%s\n' "${APP_MODEL}.0000000.${APP_VERSION}.0000000.000000.0000" > /usr/lib/version
fi

ensure_dir /data
# UniFi Core copies ssl-dynamic.conf here and does not create the directory.
mkdir -p /data/unifi-core/config/http
if [[ ! -s /data/uos_uuid ]]; then
  if [[ -n "${UOS_UUID:-}" ]]; then
    printf '%s\n' "$UOS_UUID" > /data/uos_uuid
    log "stored configured console UUID"
  else
    raw="$(cat /proc/sys/kernel/random/uuid)"
    # The version nibble is the first character of the third group.
    printf '%s\n' "${raw:0:14}5${raw:15}" > /data/uos_uuid
    log "generated a console UUID on the data volume"
  fi
  chmod 644 /data/uos_uuid
fi

# Docker already provides eth0. Create one only for an explicit parent or tap0.
if [[ ! -d /sys/devices/virtual/net/eth0 ]]; then
  parent=""
  if [[ -n "${UOS_PARENT_IF:-}" ]]; then
    parent="$UOS_PARENT_IF"
  elif [[ -d /sys/devices/virtual/net/tap0 ]]; then
    parent="tap0"
  fi
  if [[ -n "$parent" ]]; then
    log "creating eth0 macvlan on ${parent}"
    ip link add name eth0 link "$parent" type macvlan
    ip link set eth0 up
  fi
fi

ensure_dir /var/log/nginx nginx
ensure_dir /var/log/mongodb mongodb
ensure_dir /var/log/rabbitmq rabbitmq
ensure_dir /var/lib/unifi unifi
ensure_dir /var/lib/mongodb mongodb
ensure_dir /etc/rabbitmq/ssl rabbitmq

properties="/var/lib/unifi/system.properties"
set_property "$properties" "system_ip" "$UOS_SYSTEM_IP"

mongo_internal="${MONGO_INTERNAL:-false}"
case "$mongo_internal" in
  true|false) ;;
  *)
    log "MONGO_INTERNAL must be true or false"
    exit 1
    ;;
esac

if [[ "$mongo_internal" == "false" ]]; then
  mongo_host="${MONGO_HOST:-mongodb}"
  mongo_port="${MONGO_PORT:-27017}"
  mongo_user="${MONGO_USER:-}"
  mongo_pass="${MONGO_PASS:-}"
  mongo_tls="${MONGO_TLS:-false}"
  auth_source="${MONGO_AUTH_SOURCE:-admin}"
  auth=""
  if [[ -n "$mongo_user" || -n "$mongo_pass" ]]; then
    auth="$(urlencode "$mongo_user"):$(urlencode "$mongo_pass")@"
  fi
  query="tls=${mongo_tls}"
  if [[ -n "$mongo_user" ]]; then
    query="${query}&authSource=${auth_source}"
  fi
  ace_uri="mongodb://${auth}${mongo_host}:${mongo_port}/ace?${query}"
  stat_uri="mongodb://${auth}${mongo_host}:${mongo_port}/ace_stat?${query}"
  log "external MongoDB ${mongo_host}:${mongo_port}"
  set_property "$properties" "db.mongo.local" "false"
  set_property "$properties" "db.mongo.uri" "$ace_uri"
  set_property "$properties" "statdb.mongo.uri" "$stat_uri"
  if [[ -e /lib/systemd/system/mongodb.service ]]; then
    ln -sfn /dev/null /etc/systemd/system/mongodb.service
    log "masked the bundled mongodb.service so the external server is used"
  fi
else
  log "using the MongoDB server bundled in the image"
  set_property "$properties" "db.mongo.local" "true"
  remove_property "$properties" "db.mongo.uri"
  remove_property "$properties" "statdb.mongo.uri"
  if [[ -L /etc/systemd/system/mongodb.service ]] && [[ "$(readlink /etc/systemd/system/mongodb.service)" == "/dev/null" ]]; then
    rm -f /etc/systemd/system/mongodb.service
  fi
fi

if id -u unifi >/dev/null 2>&1; then
  chown unifi:unifi "$properties" /var/lib/unifi || true
fi

if [[ "$APP_MODEL" == "UOSSERVER" && -e /lib/systemd/system/mongodb.service ]]; then
  if [[ ! -e /etc/systemd/system/unifi-mongodb.service && ! -L /etc/systemd/system/unifi-mongodb.service ]]; then
    ln -s /lib/systemd/system/mongodb.service /etc/systemd/system/unifi-mongodb.service
  fi
fi

if [[ ! -s /etc/machine-id ]]; then
  if command -v systemd-machine-id-setup >/dev/null 2>&1; then
    systemd-machine-id-setup || true
  fi
  if [[ ! -s /etc/machine-id ]]; then
    tr -d '-' < /proc/sys/kernel/random/uuid > /etc/machine-id
  fi
fi

if [[ "${UOS_SHOW_JOURNAL:-true}" == "true" ]]; then
  (
    for _ in $(seq 1 90); do
      if journalctl -n 1 --no-pager >/dev/null 2>&1; then
        break
      fi
      sleep 2
    done
    exec 3>&1
    journalctl -f --no-hostname -o short >&3 2>&3 || true
  ) &
fi

if [[ ! -x /sbin/init ]]; then
  log "/sbin/init is missing or not executable"
  exit 1
fi

log "starting systemd"
exec /sbin/init
