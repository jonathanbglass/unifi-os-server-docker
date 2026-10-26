#!/usr/bin/env bash
# Confirm the image contains a Debian systemd rootfs and our entrypoint.
# This does not boot UniFi OS Server.

set -euo pipefail

image="${1:-unifi-os-server:local}"

docker run --rm --entrypoint /bin/bash "$image" -c '
set -euo pipefail
test -x /sbin/init
test -e /lib/systemd/systemd
test -x /usr/local/bin/uos-entrypoint.sh
grep -q "^ID=debian" /etc/os-release
test -x /usr/bin/mongod
test -s /usr/local/share/uos-packaging/version
echo "packaging version $(cat /usr/local/share/uos-packaging/version)"
echo "structural checks passed"
/usr/bin/mongod --version | head -n 3
'
