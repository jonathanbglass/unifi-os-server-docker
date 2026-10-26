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
on `/data`, sets `system_ip`, points the Network application at MongoDB, and
executes `/sbin/init`.

| Workflow | When it runs | What it does |
| --- | --- | --- |
| Lint and scan | Pull requests, pushes to `main`, version tags, manual runs | `actionlint`, `hadolint`, `shellcheck`, and the Python unit tests |
| Build and publish | Same events | Builds `linux/amd64`, checks the rootfs, runs Trivy, runs the smoke test, and pushes only when the event is allowed to publish |
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
`mongo:4.4.29`. It waits until MongoDB is healthy, the entrypoint logs that
it is starting systemd, and either `https://127.0.0.1:11443` or
`http://127.0.0.1:8080` returns any HTTP status. The timeout is 10 minutes
in CI. nginx can return that status while UniFi Core is up and the Network
application is still starting Java. The test does not log in, adopt a
device, or prove that every UniFi service finished starting. If systemd
cannot boot on a runner, the job fails and the logs are kept as the smoke
data directory on that runner.

## Run it

Install Docker Engine and the Docker Compose plugin. Copy the environment
file and set the address your UniFi devices can reach. That address is the
host LAN address or a DNS name, not the container address.

```sh
cp .env.example .env
# Edit UOS_SYSTEM_IP, then:
docker compose up -d
```

The web UI is `https://<host>:11443`. The first boot takes several minutes.
`docker compose logs -f unifi-os-server` shows the entrypoint line
`starting systemd` and, by default, the journal.

Create the local admin in the UI. In the UniFi site settings, set the inform
host to the same value as `UOS_SYSTEM_IP`. Devices adopt against
`http://<that address>:8080/inform`.

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

The publish job runs `scripts/ensure-ghcr-private.sh` after a push. The job
fails when an anonymous client can read the new tag. If the package metadata
says the visibility is public, the job tries to set it to private and then
deletes the package with `GITHUB_TOKEN` so a public copy is not left online.
Deletion can fail when the token is not allowed to delete packages. The job
is still red, and the package has to be deleted by hand. When the visibility
is already private and an anonymous pull still works, the job fails without
deleting the package and tells you to turn inheritance off.

`GITHUB_TOKEN` links the package to this repository. That link is what turns
inheritance on. The workflow cannot clear inheritance by itself.

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
- Device adoption depends on `UOS_SYSTEM_IP` and the inform host. If devices
  cannot open `http://<UOS_SYSTEM_IP>:8080/inform`, they will not appear.
  STUN on UDP 3478 matters for devices that are not on the same LAN.
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
