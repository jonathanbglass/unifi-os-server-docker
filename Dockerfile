# syntax=docker/dockerfile:1

# The extractor stage always runs on the builder machine. It downloads the
# official installer for TARGETARCH, checks the pinned SHA256, and unpacks
# the embedded OCI image. The installer itself is not executed. The final
# stage is the unpacked rootfs plus the entrypoint in this repository.

# hadolint ignore=DL3029
FROM --platform=$BUILDPLATFORM debian:bookworm-slim AS extractor

# hadolint ignore=DL3008
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        python3 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src

COPY versions.json /src/versions.json
COPY scripts/extract_rootfs.py /src/extract_rootfs.py

ARG TARGETARCH

RUN python3 /src/extract_rootfs.py \
        --versions /src/versions.json \
        --arch "${TARGETARCH}" \
        --download-only /src/installer.bin

COPY scripts/uos-entrypoint.sh /src/uos-entrypoint.sh
COPY scripts/uos-apply-inform.sh /src/uos-apply-inform.sh
COPY scripts/unifi_mongo.py /src/unifi_mongo.py

RUN python3 /src/extract_rootfs.py \
        --versions /src/versions.json \
        --arch "${TARGETARCH}" \
        --installer /src/installer.bin \
        --output /rootfs \
    && mkdir -p /rootfs/usr/local/bin \
    && install -m 0755 /src/uos-entrypoint.sh /rootfs/usr/local/bin/uos-entrypoint.sh \
    && install -m 0755 /src/uos-apply-inform.sh /rootfs/usr/local/bin/uos-apply-inform.sh \
    && install -m 0755 /src/unifi_mongo.py /rootfs/usr/local/bin/unifi_mongo.py

FROM scratch

COPY --from=extractor /rootfs/ /

ARG UOS_VERSION=5.1.42
ARG SOURCE_REPO=https://github.com/jonathanbglass/unifi-os-server-docker

LABEL org.opencontainers.image.title="unifi-os-server" \
      org.opencontainers.image.description="Unofficial packaging of Ubiquiti UniFi OS Server for Docker. Not affiliated with Ubiquiti. The software inside is proprietary. Keep the image in a private registry." \
      org.opencontainers.image.version="${UOS_VERSION}" \
      org.opencontainers.image.source="${SOURCE_REPO}" \
      org.opencontainers.image.licenses="LicenseRef-Proprietary" \
      org.opencontainers.image.vendor="Unofficial packaging. Not Ubiquiti."

ENV UOS_VERSION="${UOS_VERSION}" \
    APP_VERSION="${UOS_VERSION}" \
    APP_MODEL="UOSSERVER" \
    PRODUCT_NAME="UniFi OS Server" \
    container="docker"

STOPSIGNAL SIGRTMIN+3

ENTRYPOINT ["/usr/local/bin/uos-entrypoint.sh"]
