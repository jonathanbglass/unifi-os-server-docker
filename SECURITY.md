# Security policy

This repository packages Ubiquiti UniFi OS Server. It is not affiliated with
Ubiquiti. The image contains proprietary software that this project does not
own.

## Report a problem in this repository

Use a private GitHub security advisory on
[jonathanbglass/unifi-os-server-docker](https://github.com/jonathanbglass/unifi-os-server-docker/security/advisories/new)
for issues in the workflows, the extractor, the entrypoint, or the compose
file. Please include the pinned version from `versions.json` and whether the
package was reachable without authentication.

Do not open a public issue for a vulnerability that would let someone pull a
private image, weaken the checksum check, or run an unexpected installer URL.

## Out of scope

Vulnerabilities inside UniFi OS Server, its bundled MongoDB, or other
Ubiquiti components should go to Ubiquiti. The Trivy report in CI lists those
findings and does not fail the build unless `TRIVY_EXIT_CODE` is set to `1`.

## Registry

The GitHub Container Registry package is meant to stay private. Creating it
with `GITHUB_TOKEN` from this public repository makes it public, and a public
package cannot be made private again. The package REST API cannot change
visibility. If a published tag can be pulled without a token, the publish job
deletes the package when the API says it is public. Open a private advisory
as well.

## Installers

The build downloads installers only from `fw-download.ubnt.com` over HTTPS
and checks the SHA256 and size recorded in `versions.json`. Those checksums
are the values Ubiquiti publishes on `https://fw-update.ui.com/api/firmware-latest`.
The extractor unpacks the embedded OCI image. It does not execute the
installer.
