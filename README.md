# UniFi OS Server container image

**This project is not affiliated with, endorsed by, or supported by Ubiquiti.**
UniFi, UniFi OS, and UniFi OS Server are Ubiquiti names. The software inside
the image is proprietary. This repository holds build scripts and docs only.

Pinned release: 5.1.42

The image is published to a private GitHub Container Registry package:

```text
ghcr.io/jonathanbglass/unifi-os-server:5.1.42
ghcr.io/jonathanbglass/unifi-os-server:5.1.42-<short sha>
```

There is no `latest` tag. The `5.1.42` tag moves when that pinned release is
rebuilt. The tag with the short commit SHA identifies one build.

## What this is

Ubiquiti ships UniFi OS Server for Linux as an installer, not as a container
image you can pull from Docker Hub. The installer is an executable with a zip
archive appended. That zip contains `image.tar`, an OCI image layout.

This repository downloads that official installer in GitHub Actions, checks
the SHA256 Ubiquiti publishes, unpacks the OCI layers, and builds a
multi-arch image. The installer is not executed. No Ubiquiti binary, rootfs,
or image layer is stored in git.

The container starts systemd, which is how UniFi OS Server runs its own
services. A separate MongoDB 4.4 container holds the Network application
database. You can switch to the MongoDB build that ships inside the image
with `MONGO_INTERNAL=true`.

If you only need the Network application, the maintained
[linuxserver/unifi-network-application](https://docs.linuxserver.io/images/docker-unifi-network-application/)
image is a smaller system to operate. LinuxServer.io has said it will not
publish a UniFi OS Server image. See their note from 22 February 2026:
<https://info.linuxserver.io/issues/2026-02-22-unifi-network-application/>.

## How the pipeline works

`versions.json` is the only pin. It records the UniFi OS Server version, the
MongoDB image tag, and the `linux-x64` and `linux-arm64` installer URL, size,
and SHA256.

Ubiquiti publishes those checksums on the firmware API:

```text
https://fw-update.ui.com/api/firmware-latest
```

The current pin matches the `release` channel documents for product
`unifi-os-server`, platforms `linux-x64` and `linux-arm64`. The download
center API lists installers and does not publish checksums, so the weekly
check uses it only as a cross-check.

On a build, `scripts/extract_rootfs.py`:

1. Downloads the installer for the target architecture from `fw-download.ubnt.com`.
2. Rejects any other host, any non-HTTPS URL, a size mismatch, or a SHA256 mismatch.
3. Opens the zip member `image.tar` and checks each OCI blob against its digest.
4. Applies the image layers, including OCI whiteout deletions.
5. Refuses a rootfs that has no systemd.

The Dockerfile runs that extractor on the builder machine, then copies the
rootfs into a `scratch` image with `scripts/uos-entrypoint.sh`. The entrypoint
writes the platform and version files UniFi OS expects, stores a console UUID
on `/data`, sets `system_ip` in the Network application data directory, writes
`/etc/host-ip` when that address is an IP, points the Network application at
MongoDB, installs a oneshot that sets the inform host override once MongoDB
is up, and executes `/sbin/init`.

| Workflow | When it runs | What it does |
| --- | --- | --- |
| Lint and scan | Pull requests, pushes to `main`, version tags, manual runs | `actionlint`, `hadolint`, `shellcheck`, and the Python unit tests |
| Build and publish | Same events | Builds `linux/amd64`, checks the rootfs, runs Trivy, runs the smoke test, and pushes only when the event is allowed to publish and a private GHCR package already exists |
| Check upstream | Weekly, Monday 06:17 UTC, and manual runs | Compares the pin with Ubiquiti and opens a pull request when both architectures have a newer release with checksums |

Pull requests build and test. They do not push. Pushes to `main`, version tags
that match `versions.json`, and manual runs push. A manual run can set
`push` to false.

Published tags:

- `5.1.42-<short sha>` is the multi-arch index for that commit
- `5.1.42-<short sha>-amd64` and `5.1.42-<short sha>-arm64` are the single-arch images
- `5.1.42` points at the same index
- a git tag such as `v5.1.42` is added only when the tag name, without a
  leading `v`, equals the pin

Trivy writes a SARIF report for HIGH and CRITICAL findings and uploads it on
same-repository runs. The default exit code is `0`, so vendor CVEs in the
UniFi OS Server rootfs are reported and the job stays green. Set the
repository variable `TRIVY_EXIT_CODE` to `1` to make those findings fail the
job. A manual build can also set `fail_on_vulnerabilities` to true for one run.

The smoke test starts `docker-compose.yml` with the image just built and
`mongo:4.4.29`. It waits until MongoDB is healthy and PID 1 in the UniFi OS
Server container is systemd. The entrypoint records that handoff in the
container log and in a marker file on the data volume, then execs systemd.
The test then waits until either `https://127.0.0.1:11443` or
`http://127.0.0.1:8080` returns any HTTP status. The UI timeout is 10
minutes in CI. nginx can return that status while UniFi Core is up and the
Network application is still starting Java. The test does not log in, adopt
a device, or prove that every UniFi service finished starting. If PID 1
never becomes systemd, or the listener never opens, the job prints the
container logs, process list, and recent journal entries, keeps the smoke
data directory, and uploads that directory as an artifact.

## Run it

Install Docker Engine and the Docker Compose plugin. Copy the environment
file and set the address your UniFi devices can reach. That address is the
host LAN address or a DNS name, not the container address.

```sh
cp .env.example .env
# Edit UOS_SYSTEM_IP, then:
docker compose up -d
```

`UOS_SYSTEM_IP` is the address devices can reach. `UNIFI_SYSTEM_IP` and
`INFORM_HOST` are aliases; if more than one is set, they must match. On
every container start, and again immediately before the Network application
process starts, the image writes `system_ip` to
`/usr/lib/unifi/data/system.properties` and
`/var/lib/unifi/system.properties`. In this rootfs those paths are the same
file: `data` is a symlink to `/var/lib/unifi`, which is the directory behind
the `var_lib_unifi` volume. When the value is an IP, the image also writes
it to `/etc/host-ip` with no trailing newline.

`UOS_INFORM_HOST_OVERRIDE` defaults to `true`. After MongoDB has an
`ace.setting` document with `key` `mgmt`, a oneshot sets
`override_inform_host` and `override_inform_host_location` and, if it
changed them, restarts only the Network application
(`systemctl restart unifi`). Set the variable to `false` to keep the
properties file and skip MongoDB. The oneshot does not log in to MongoDB.
Leave `MONGO_USER` empty, or set the override in the UI. With
`MONGO_INTERNAL=true` it uses `127.0.0.1:27117`. The compose file uses the
external database on `mongodb:27017`.

The web UI is `https://<host>:11443`. The first boot takes several minutes.
`docker compose logs -f unifi-os-server` shows the entrypoint line
`starting systemd` and, by default, the journal.

Create the local admin in the UI. Devices adopt against
`http://<UOS_SYSTEM_IP>:8080/inform`. The troubleshooting section below
covers a controller that picked another bridge on the host instead.

### Ports

The host bindings live in `docker-compose.yml`.

| Host | Container | Use |
| --- | --- | --- |
| 11443/tcp | 443 | Web UI, HTTPS |
| 8443/tcp | 8443 | Network application HTTPS |
| 8080/tcp | 8080 | Device inform |
| 8444/tcp | 8444 | Secure hotspot portal |
| 8880-8882/tcp | 8880-8882 | Hotspot portal redirects |
| 6789/tcp | 6789 | Mobile speed test |
| 9543/tcp | 9543 | UniFi Identity hub |
| 5671/tcp | 5671 | AMQP over TLS |
| 28082/tcp | 28082 | Device support file download |
| 3478/udp | 3478 | STUN, used by remote devices |
| 5514/udp | 5514 | Remote syslog |
| 10003/udp | 10003 | Local discovery |
| 127.0.0.1:11084/tcp | 11084 | UCORE, bound on the host loopback |

MongoDB is on the compose network only. Its port is not published. PostgreSQL
inside UniFi OS Server is also unpublished. Add a loopback binding yourself
if you have a specific need, and treat that port as sensitive.

### Permissions

The compose file does not set `privileged: true`. It sets the cgroup
namespace to `host`, bind-mounts `/sys/fs/cgroup` writable, and adds
`NET_ADMIN` and `NET_RAW`. Those capabilities let the entrypoint create a
macvlan `eth0` when you set `UOS_PARENT_IF`, and they let UniFi services open
raw sockets. `tmpfs` covers `/run`, `/run/lock`, `/tmp`, the journal, and
`/var/opt/unifi/tmp`.

On some hosts systemd still exits because it cannot set up cgroups. In that
case start with the overlay:

```sh
docker compose -f docker-compose.yml -f compose.privileged.yml up -d
```

`privileged: true` gives the container root-equivalent control of the Docker
host. Use it only when the capability list is not enough, and only on a
host you trust with that tradeoff. The smoke test can be repeated with
`SMOKE_PRIVILEGED=true` to exercise the overlay.

### Internal MongoDB

`MONGO_INTERNAL=false` is the default. The entrypoint writes
`db.mongo.uri` and `statdb.mongo.uri` in `/var/lib/unifi/system.properties`
and masks the bundled `mongodb.service`.

For the bundled database, set `MONGO_INTERNAL=true` and remove the `mongodb`
service and the `depends_on` block from the compose file. The bundled data
stays under `./data/var-lib-mongodb` and the Network application data
directory. Do not point an old 4.4 data directory at a different major
version.

Leave `MONGO_USER` and `MONGO_PASS` empty unless you have already created a
user in MongoDB. The sample compose file does not enable MongoDB
authentication.

## Create the private package once

A workflow that creates a package with `GITHUB_TOKEN` inherits the visibility
of the repository. This repository is public, so that first push is a public
package. Anonymous `docker pull` then succeeds. GitHub does not let a public
package become private, and the package REST API has no visibility update.
`PUT /user/packages/container/unifi-os-server` is not a documented method and
returns 404. `GITHUB_TOKEN` can push and, when it has admin on the package,
delete. It cannot change visibility.

The publish job therefore refuses to push until package metadata already says
`private`. After the push it still requires an anonymous manifest pull to
fail. If the API says the package is public, the job deletes it and fails.
If the API says private but an anonymous pull still works, the job fails and
leaves the package in place. Deleting that copy would make the next
`GITHUB_TOKEN` push create a new public package.

Do this once, from your own machine, before the next publish on `main`. The
package created by the first `main` publish was deleted by that check.

1. Create a classic personal access token with `write:packages` and
   `delete:packages`. GitHub Packages does not accept a fine-grained token for
   this push. Do not store the token in the repository.
2. Push a placeholder that does not set `org.opencontainers.image.source`.
   That label links the image to this public repository and is how a package
   inherits repository access. The image built by Actions sets the label. The
   placeholder must not.

   ```sh
   printf '%s' "$GHCR_PAT" | docker login ghcr.io -u jonathanbglass --password-stdin
   docker pull alpine:3.20
   docker tag alpine:3.20 ghcr.io/jonathanbglass/unifi-os-server:private-bootstrap
   docker push ghcr.io/jonathanbglass/unifi-os-server:private-bootstrap
   ```

   A personal-account push that is not made with `GITHUB_TOKEN` defaults to
   private.
3. Open the package settings:
   `https://github.com/users/jonathanbglass/packages/container/unifi-os-server/settings`
4. Under Danger Zone, the visibility must be Private. If it says Public,
   delete the package and repeat step 2.
5. Under Manage Actions access, add `jonathanbglass/unifi-os-server-docker`
   and set the role to Admin. Admin is what lets this workflow push and
   delete. Adding the repository here is separate from connecting the
   package to the repository.
6. Leave **Inherit access from repository** turned off. If you connect the
   repository from the package settings page, do not select inheritance.
   On a public repository, inheritance lets the world pull the image even
   when the visibility field says Private.
7. Re-run **Build and publish** on `main` from the Actions tab, with `push`
   left on. After `5.1.42` and the commit tag exist, delete the
   `private-bootstrap` version from the package's Manage versions page.

## Pull the private image

The package is private. `docker pull` needs a GitHub account that can read
it and a personal access token with the `read:packages` scope. A
fine-grained token needs repository access plus permission to read packages.

```sh
printf '%s' "$GITHUB_PAT" | docker login ghcr.io -u YOUR_GITHUB_USERNAME --password-stdin
docker pull ghcr.io/jonathanbglass/unifi-os-server:5.1.42
```

Put the same image name in `.env` as `UOS_IMAGE` if you are not using the
default. Log out with `docker logout ghcr.io` when you are done on a shared
machine.

Confirm the package is private before you rely on it:

1. Open the package page. From your GitHub profile, choose Packages, then
   `unifi-os-server`, then Package settings.
2. Under Danger Zone, the visibility must be Private. If it says Public,
   delete the package. GitHub does not allow a public package to become
   private again.
3. If the repository is public, turn off **Inherit access from repository**.
   Inheritance copies the repository's read access onto the package. On a
   public repository that lets the world pull the image even when the
   visibility field says Private.
4. From a machine that is not logged in to GHCR, `docker pull` of the tag
   must fail.

`scripts/ensure-ghcr-private.sh --preflight` runs before `docker push`.
`scripts/ensure-ghcr-private.sh` runs again after the push and checks the new
tag. Both fail the job when the package is missing, public, or anonymously
pullable. The post-push check is what deletes a public package.

## Share it with a collaborator

Give the person these two grants:

1. Read access to this repository, if they should see the workflows and pins.
2. Read access on the package. In Package settings, invite their GitHub
   username and choose the Read role.

Repository access alone does not pull the image once inheritance is off.
Package access alone is enough to pull. They still log in with their own
personal access token that has `read:packages`.

Write access to the repository lets them change workflows. Admin access on
the package lets them change visibility. Keep both limited.

## Bump the version

The weekly workflow `Check upstream UniFi OS Server` reads the firmware API
for `linux-x64` and `linux-arm64` on the `release` channel. It opens a pull
request only when all of the following are true:

- both platforms report the same version
- that version is newer than `versions.json`
- both documents include a 64-character SHA256, a file size, and an
  `https://fw-download.ubnt.com/data/unifi-os-server/` URL
- the download center is missing, agrees, or is older

The pull request updates `versions.json`, the `ARG UOS_VERSION` line, the
compose and `.env.example` tags, and the release strings in this README.
Merging it to `main` is what starts a build. The checker never publishes an
image by itself.

It will not open a pull request when:

- the two architectures disagree
- the download center lists a newer build that the firmware API cannot
  describe with a checksum
- the pin is already equal to or newer than the firmware API
- the firmware API is down, in which case the job fails and the pin stays

Those limits are the reason a release can exist on Ubiquiti's download site
before this repository moves. Run the workflow with `workflow_dispatch` to
check again. For a manual pin, edit `versions.json` using values from the
firmware API, run `python3 scripts/check_upstream.py --versions versions.json`
to see the comparison, and update the `5.1.42` strings in the Dockerfile,
compose file, `.env.example`, and this README. Keep installer URLs and
checksums in `versions.json` only. The rewriter refuses to touch a line that
contains both the version and an installer URL or the word `sha256`.

## Backup and restore

Stop the stack, then copy the whole `./data` directory. These mounts matter:

| Host path | Container path | Contents |
| --- | --- | --- |
| `./data/unifi` | `/data` | Console UUID and UniFi OS state |
| `./data/var-lib-unifi` | `/var/lib/unifi` | Network application files and `system.properties` |
| `./data/mongodb` | `/data/db` in the MongoDB container | External MongoDB 4.4 data |
| `./data/var-lib-mongodb` | `/var/lib/mongodb` | Bundled MongoDB directory |
| `./data/etc-rabbitmq-ssl` | `/etc/rabbitmq/ssl` | RabbitMQ certificates |

`/data/uos_uuid` is the console identity. Restoring a copy that has a
different UUID makes the controller look like a new console to devices that
were already adopted.

A logical backup of the external database, taken while the stack is running:

```sh
docker compose exec mongodb mongo --quiet --eval 'db.getSiblingDB("ace").getCollectionNames()'
docker compose exec mongodb sh -c 'mongodump --archive' > ace.archive
```

Restore onto a stopped stack by replacing `./data` with the copy, or by
loading `ace.archive` into an empty MongoDB 4.4 data directory with
`mongorestore --archive`. Bring the stack back with `docker compose up -d`.
Take a fresh backup before every image upgrade and before any MongoDB tag
change.

The image has `mongod` and the `mongo` shell. It does not have `mongodump`.
On a host that uses the bundled database (port 27117 inside the UniFi OS
container), run `mongodump` on the host in the container's network
namespace. `scripts/unifi-backup.sh` does that. Install MongoDB database
tools on the host first. The defaults match a rootless Podman container
named `uosserver`:

```sh
# As the user that owns the container, or as root with UOS_PODMAN_USER set.
export UOS_PODMAN_USER=uosserver
export UNIFI_BACKUP_DIR=/var/backups/unifi
bash scripts/unifi-backup.sh backup
bash scripts/unifi-backup.sh restore --from /var/backups/unifi/ace-YYYYmmddTHHMMSSZ
```

The script runs:

```sh
pid="$(podman inspect uosserver --format '{{.State.Pid}}')"
sudo nsenter -t "$pid" -n mongodump --port 27117 --db ace --out <dir>
```

`nsenter -n` changes the network namespace and leaves the host filesystem
in place, so `--out` is a host directory and `mongodump` is the host binary.
Restore asks you to type `restore` (or pass `--yes`) and runs
`mongorestore --drop` for that database. For the compose stack's external
database, point the script at the MongoDB container and port 27017:

```sh
UOS_RUNTIME=docker \
UOS_CONTAINER="$(docker compose ps -q unifi-os-server)" \
UNIFI_MONGO_CONTAINER="$(docker compose ps -q mongodb)" \
UNIFI_MONGO_PORT=27017 \
  bash scripts/unifi-backup.sh backup
```

The dump can contain device credentials. The script creates the output
directory mode `0700` and does not print the dump. Keep it with the rest of
`./data`, off the git tree.

## Troubleshooting

### Inform URL uses another bridge on the host

A host that also runs other containers has bridges of its own. Docker's
user-defined bridges are often `172.18.0.0/16`, so the bridge address is
`172.18.0.1`. If UniFi selects that address, adoption tells the device to
use `http://172.18.0.1:8080/inform`. The device cannot open that address,
and adoption loops.

Two selectors are involved.

The Network application reads `system_ip` from
`/usr/lib/unifi/data/system.properties`. When the key is set, it uses that
value and skips interface detection. The application keeps the key across
its own restarts. Detection otherwise walks the interfaces, skips `lo0`,
and skips names that start with `docker` only in some cases. A bridge named
`br-*` is still a candidate, and the first remaining IPv4 can be
`172.18.0.1`. For the `UOSSERVER` model, a detected address of exactly
`10.88.0.3` (a Podman bridge address the application special-cases) is
replaced by the contents of `/etc/host-ip`. Any other address, including
`172.18.0.1`, is kept. `/etc/host-ip` is a single IP with no trailing
newline; `0.0.0.0` is rejected. This image writes `system_ip` on every
start and writes `/etc/host-ip` when `UOS_SYSTEM_IP` is an IP. The
properties write is what stops a foreign bridge from becoming the Server IP.

Adoption also stores an inform host on the `mgmt` setting. That is the
value the UI calls Inform Host Override (Settings, System, Advanced, and on
current Network builds also Devices, Device Updates and Settings, Device
SSH Settings). It lives in MongoDB database `ace`, collection `setting`,
document `key` `mgmt`, fields `override_inform_host` and
`override_inform_host_location`. The oneshot sets those fields when
`UOS_INFORM_HOST_OVERRIDE` is `true`.

UniFi OS Core keeps a separate LAN address. For model `UOSSERVER`,
`/usr/share/unifi-core/app/service.js` takes the first address of the first
interface returned by the discovery client at
`http://127.0.0.1:11002/interfaces`. There is no environment variable or
config key for a preferred address. `NODE_CONFIG` can change
`discovery.discoveryClientUrl`, and this image does not. Replacing that
list would hide interfaces Core uses for more than the LAN address, and a
public report against this pin found that preferring one address inside
that selector did not correct the address Site Manager shows. Host
networking makes the discovery client list every host bridge, Docker
bridges included, and those bridges are often first. Leave the container
on its own network. The compose file publishes port 8080 and the UI ports.
`UOS_PARENT_IF` creates a macvlan `eth0` only when the container does not
already have `eth0`. Docker and Podman create `eth0` themselves, so that
option does not hide a bridge.

Check the pin from the host:

```sh
podman exec uosserver grep '^system_ip=' /usr/lib/unifi/data/system.properties
podman exec uosserver cat /etc/host-ip
```

With the external database:

```sh
docker compose exec mongodb mongo --quiet --eval \
  'var d=db.getSiblingDB("ace").setting.findOne({key:"mgmt"}); if (d) print(d.override_inform_host + " " + d.override_inform_host_location)'
```

With the bundled database, the image includes the `mongo` shell:

```sh
podman exec uosserver mongo --quiet --port 27117 ace --eval \
  'var d=db.setting.findOne({key:"mgmt"}); if (d) print(d.override_inform_host + " " + d.override_inform_host_location)'
```

Devices that already adopted keep the old inform URL until they are
provisioned again or you `set-inform` twice on the device.

### Backup and restore when the container has no mongodump

Use `scripts/unifi-backup.sh`, described in Backup and restore. The
container PID comes from `podman inspect uosserver --format '{{.State.Pid}}'`
(or `docker inspect`). `sudo nsenter -t <pid> -n` is what reaches port
27117. Run `podman` as the rootless user (`UOS_PODMAN_USER=uosserver` when
the script itself is root) and `nsenter` as root.

### Device stuck adopted, Remove greyed out

A device can stay Adopted while it checks in with the default key. The
Network log says `used default key in ADOPTING state, reject it!` and
Remove is greyed out. Forget the controller's record, then adopt again.
`scripts/unifi-forget-device.sh` takes the MAC, shows the `mac`, `name`,
and `model` of matching documents without printing keys, requires you to
type the MAC again (or pass `--yes`), backs up `ace` first, deletes that
MAC from `ace.device` and `ace.power_supervisor`, and restarts only the
Network application. The device then shows as pending adoption.

```sh
export UOS_PODMAN_USER=uosserver
bash scripts/unifi-forget-device.sh --dry-run aa:bb:cc:dd:ee:ff
bash scripts/unifi-forget-device.sh aa:bb:cc:dd:ee:ff
```

The match is the `mac` field: colon form, the same MAC in uppercase, and
the 12 hex digits with no separators. If the count is zero, the script
does not delete and does not restart. A failed backup stops the script
before the delete.

### Smart Power can power-cycle a PoE-fed switch

Smart Power state is in `ace.power_supervisor`. It can power-cycle a switch
that is itself powered by PoE from another UniFi switch. During an outage
or a flap that cycle repeats: the upstream port drops, Smart Power bounces
the downstream switch, and the switch never stays up. Turn Smart Power off
for a switch that feeds other UniFi gear or that is powered by PoE when a
site reboots in a loop. Forgetting a stuck device deletes that MAC from
`power_supervisor` as well as from `device`.

### The Network application slows when the host CPU is busy

The Network application is a Java process in the same container as the rest
of UniFi OS. It shares the host CPUs. When the host is busy, the UI, inform
handling, and adoption slow down with it. Check the host load before
treating a slow controller as a UniFi fault. The compose file does not set
a CPU limit.

## Warnings and caveats

- This is unofficial. Ubiquiti does not support running UniFi OS Server from
  this image. A new UniFi OS Server release can change the installer layout
  or the files systemd expects, and the extract or the boot will fail. Keep
  the pin, and test a new pin before you upgrade a controller that manages
  real sites.
- The container needs a host cgroup namespace, writable `/sys/fs/cgroup`,
  `NET_ADMIN`, and `NET_RAW`. Some hosts also need `privileged: true`. That
  is a security tradeoff: a bug in a process inside the container is much
  closer to host root than a normal container. Run it on a host you are
  willing to give that power, and do not expose the UI to the open internet
  without your own access control.
- Ubiquiti's software is proprietary. This repository's MIT license covers
  the scripts, workflows, and docs here. It does not cover UniFi OS Server.
  The git repository contains no Ubiquiti code. The built image contains
  Ubiquiti's rootfs and must stay in a private registry. Redistributing that
  image, or leaving it anonymously pullable, can violate Ubiquiti's terms.
  Read Ubiquiti's UniFi OS Server terms before you share the package:
  <https://www.ui.com/legal/unifi-os-server/>.
- The compose file pins the external database to `mongo:4.4.29`, the last
  4.4 release. The rootfs also contains MongoDB 3.6.23, which the Network
  application starts on port 27117 when `MONGO_INTERNAL=true`. Ubiquiti's
  release notes say Network 7.5 and newer requires MongoDB 3.6, versions
  7.5 through 8.0 support up to 4.4, 8.1 and newer support up to 7.0, and
  9.0 and newer support up to 8.0. This 5.1.42 rootfs includes Network
  10.5.67, so a newer server can be inside that range. Changing the major
  version of an existing data directory is still a migration. Dump the
  database first. WiredTiger files from 4.4 do not open on 3.6, and they do
  not drop into 5, 6, 7, or 8.
- Device adoption depends on `UOS_SYSTEM_IP` and the inform host override.
  If devices cannot open `http://<UOS_SYSTEM_IP>:8080/inform`, they loop in
  adoption. STUN on UDP 3478 matters for devices that are not on the same LAN.
  See Troubleshooting when the controller has picked another bridge on the host.
- Back up `./data` before you upgrade the image or the MongoDB tag. Keep the
  previous image tag until the new one has booted and you have signed in.
- Ubiquiti is moving self-hosted management from the standalone Network
  application toward UniFi OS Server. When you only need network management,
  `linuxserver/unifi-network-application` is the simpler image to run and it
  is maintained for that job. Use this project when you specifically need
  UniFi OS Server.

## Local checks

The Python tests do not download Ubiquiti software:

```sh
python3 -m unittest discover -s tests -v
```

After a local image build:

```sh
bash scripts/structural-check.sh unifi-os-server:local
UOS_IMAGE=unifi-os-server:local UOS_SYSTEM_IP=127.0.0.1 bash scripts/smoke-test.sh
```

`scripts/structural-check.sh` checks systemd, the entrypoint, Debian,
`mongod`, and the packaging version file. It does not boot UniFi OS Server.

## Prior art

These projects showed that the Linux installer contains an OCI image and that
systemd plus an external MongoDB 4.4 can run it under Docker. This
repository uses its own extractor, entrypoint, and workflows.

- [unihosted/unifi-os-server-docker](https://github.com/unihosted/unifi-os-server-docker)
  extracts the embedded image and publishes a compose file. That repository
  has no license file. None of its files are copied here.
- [lemker/unifi-os-server](https://github.com/lemker/unifi-os-server) is
  licensed under the AGPL-3.0 and covers Docker and Kubernetes. None of its
  files are copied here. Behavior that both projects have to match, such as
  the `/usr/lib/version` string, comes from UniFi OS itself.

## License

The files in this repository are under the MIT license in `LICENSE`.
Ubiquiti software is excluded. Report problems in these scripts through the
process in `SECURITY.md`.
