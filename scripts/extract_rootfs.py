#!/usr/bin/env python3
"""Flatten the OCI image embedded in a UniFi OS Server installer.

The official Linux installer is an ELF executable with a zip archive
appended. The zip member image.tar is an OCI image layout. This program
downloads that installer, or reads a local copy, checks the published
SHA256, and applies the image layers to a directory. It does not execute
the installer.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.parse
import zipfile
from pathlib import Path

ALLOWED_INSTALLER_HOSTS = {"fw-download.ubnt.com"}
CHUNK = 1024 * 1024


def log(message: str) -> None:
    print(f"[extract] {message}", flush=True)


def die(message: str) -> None:
    print(f"[extract] ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_installer_url(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https":
        raise ValueError(f"installer URL must use https: {url}")
    if parsed.username or parsed.password:
        raise ValueError(f"installer URL must not include userinfo: {url}")
    host = parsed.hostname or ""
    if host not in ALLOWED_INSTALLER_HOSTS:
        raise ValueError(f"installer URL host is not allowed: {host}")
    if not parsed.path.startswith("/data/unifi-os-server/"):
        raise ValueError(f"installer URL path is not a UniFi OS Server download: {url}")


def require_sha256(path: Path, expected: str, expected_size: int | None = None) -> None:
    if expected_size is not None and path.stat().st_size != expected_size:
        raise ValueError(
            f"installer size mismatch: expected {expected_size} bytes, got {path.stat().st_size}"
        )
    actual = file_sha256(path)
    if actual.lower() != expected.lower():
        raise ValueError(f"installer sha256 mismatch: expected {expected}, got {actual}")


def download_installer(url: str, dest: Path) -> None:
    validate_installer_url(url)
    dest.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "curl",
        "--fail",
        "--location",
        "--retry",
        "5",
        "--retry-delay",
        "2",
        "--retry-all-errors",
        "--connect-timeout",
        "20",
        "--output",
        str(dest),
        url,
    ]
    log(f"downloading {url}")
    subprocess.run(command, check=True)


def parts_of(name: str) -> list[str]:
    """Split a tar member name into safe components.

    `.` and `./` are the archive root and return an empty list. Callers skip
    that entry. Absolute paths and `..` are rejected.
    """
    normalized = name.replace("\\", "/")
    if normalized.startswith("/"):
        raise ValueError(f"absolute path in image layer: {name}")
    parts: list[str] = []
    for part in normalized.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise ValueError(f"path escapes the rootfs: {name}")
        parts.append(part)
    return parts


def _contained(root: Path, candidate: Path) -> bool:
    try:
        resolved = candidate.resolve()
    except FileNotFoundError:
        resolved = candidate
    return resolved == root or root in resolved.parents


def destination_for(rootfs: Path, parts: list[str], *, create_parents: bool) -> Path:
    root = rootfs.resolve()
    current = root
    for part in parts[:-1]:
        nxt = current / part
        if nxt.is_symlink():
            if not _contained(root, nxt):
                raise ValueError(f"symlink escapes the rootfs: {nxt}")
            current = nxt.resolve()
        else:
            if create_parents and not nxt.exists():
                nxt.mkdir()
            current = nxt
        if not _contained(root, current):
            raise ValueError(f"path escapes the rootfs: {current}")
    return current / parts[-1]


def remove_path(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    if path.is_symlink() or path.is_file():
        path.unlink()
        return
    shutil.rmtree(path)


def apply_whiteout(rootfs: Path, parts: list[str]) -> None:
    basename = parts[-1]
    parent_parts = parts[:-1]
    if parent_parts:
        parent = destination_for(
            rootfs, [*parent_parts, "placeholder"], create_parents=True
        ).parent
    else:
        parent = rootfs
    if basename == ".wh..wh..opq":
        if parent.exists() and not parent.is_symlink():
            for child in list(parent.iterdir()):
                remove_path(child)
        return
    if basename.startswith(".wh."):
        target_name = basename[len(".wh.") :]
        if not target_name or target_name in (".", ".."):
            raise ValueError(f"invalid whiteout name: {basename}")
        remove_path(parent / target_name)


def apply_owner_mode(path: Path, member: tarfile.TarInfo, *, follow_symlinks: bool) -> None:
    if os.geteuid() == 0:
        if follow_symlinks:
            os.chown(path, member.uid, member.gid)
        else:
            os.lchown(path, member.uid, member.gid)
    if follow_symlinks:
        os.chmod(path, member.mode & 0o7777)


def apply_member(tar: tarfile.TarFile, member: tarfile.TarInfo, rootfs: Path) -> str:
    if member.ischr() or member.isblk():
        return "device"
    if member.issym() or member.islnk() or member.isdir() or member.isfile() or member.isfifo():
        parts = parts_of(member.name)
        if not parts:
            return "dir"
    else:
        # GNU long-name headers are consumed by tarfile and not yielded.
        name = member.name
        if not name or name.endswith("/"):
            return "other"
        try:
            parts = parts_of(name)
        except ValueError:
            raise
        if stat.S_ISCHR(member.mode) or stat.S_ISBLK(member.mode):
            return "device"
        return "other"

    basename = parts[-1]
    if basename == ".wh..wh..opq" or basename.startswith(".wh."):
        apply_whiteout(rootfs, parts)
        return "whiteout"

    dest = destination_for(rootfs, parts, create_parents=True)

    if member.isdir():
        if dest.is_symlink() or dest.is_file():
            dest.unlink()
        dest.mkdir(exist_ok=True)
        apply_owner_mode(dest, member, follow_symlinks=True)
        return "dir"

    if member.issym():
        if dest.is_symlink() or dest.is_file():
            dest.unlink()
        elif dest.is_dir():
            shutil.rmtree(dest)
        dest.symlink_to(member.linkname)
        apply_owner_mode(dest, member, follow_symlinks=False)
        return "symlink"

    if member.islnk():
        target_parts = parts_of(member.linkname)
        if not target_parts:
            raise ValueError(f"hardlink target is the archive root: {member.name}")
        target = destination_for(rootfs, target_parts, create_parents=False)
        if dest.is_symlink() or dest.is_file():
            dest.unlink()
        elif dest.exists():
            shutil.rmtree(dest)
        os.link(target, dest)
        return "hardlink"

    if member.isfifo():
        if dest.exists() or dest.is_symlink():
            remove_path(dest)
        os.mkfifo(dest, member.mode & 0o7777)
        apply_owner_mode(dest, member, follow_symlinks=True)
        return "fifo"

    if member.isfile():
        if dest.is_symlink() or dest.is_file():
            dest.unlink()
        elif dest.is_dir():
            shutil.rmtree(dest)
        source = tar.extractfile(member)
        if source is None:
            raise ValueError(f"missing file payload for {member.name}")
        with dest.open("wb") as output, source:
            for chunk in iter(lambda: source.read(CHUNK), b""):
                output.write(chunk)
        apply_owner_mode(dest, member, follow_symlinks=True)
        return "file"

    if member.ischr() or member.isblk():
        return "device"
    return "other"


def apply_gzip_layer(blob: Path, rootfs: Path) -> None:
    counts: dict[str, int] = {}
    with gzip.open(blob, "rb") as compressed, tarfile.open(fileobj=compressed, mode="r|") as layer:
        for member in layer:
            kind = apply_member(layer, member, rootfs)
            counts[kind] = counts.get(kind, 0) + 1
            total = sum(counts.values())
            if total % 10000 == 0:
                log(f"  {blob.name[:12]} applied {total} entries")
    summary = ", ".join(f"{key}={value}" for key, value in sorted(counts.items()))
    log(f"  {blob.name[:12]} done ({summary or 'empty'})")


def apply_tar_layer(blob: Path, rootfs: Path) -> None:
    """Apply an uncompressed tar layer. Gzip layers are detected by magic."""
    with blob.open("rb") as handle:
        magic = handle.read(2)
    if magic == b"\x1f\x8b":
        apply_gzip_layer(blob, rootfs)
        return
    counts: dict[str, int] = {}
    with tarfile.open(blob, mode="r:") as layer:
        for member in layer:
            kind = apply_member(layer, member, rootfs)
            counts[kind] = counts.get(kind, 0) + 1
    summary = ", ".join(f"{key}={value}" for key, value in sorted(counts.items()))
    log(f"  {blob.name[:12]} done ({summary or 'empty'})")


def unpack_image_tar(installer: Path, oci_dir: Path) -> None:
    oci_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(installer) as archive:
        try:
            info = archive.getinfo("image.tar")
        except KeyError as exc:
            names = ", ".join(archive.namelist()[:12])
            raise ValueError(f"installer has no image.tar member (saw: {names})") from exc
        log(
            f"image.tar compressed {info.compress_size} bytes, "
            f"uncompressed {info.file_size} bytes"
        )
        with archive.open(info) as raw, tarfile.open(fileobj=raw, mode="r|") as image_tar:
            for member in image_tar:
                if not member.isfile() and not member.isdir():
                    continue
                parts = parts_of(member.name)
                if not parts:
                    continue
                dest = destination_for(oci_dir, parts, create_parents=True)
                if member.isdir():
                    dest.mkdir(exist_ok=True)
                    continue
                source = image_tar.extractfile(member)
                if source is None:
                    raise ValueError(f"missing OCI payload for {member.name}")
                digest = hashlib.sha256()
                with dest.open("wb") as output, source:
                    for chunk in iter(lambda: source.read(CHUNK), b""):
                        digest.update(chunk)
                        output.write(chunk)
                if parts[0] == "blobs" and len(parts) == 3 and parts[1] == "sha256":
                    actual = digest.hexdigest()
                    if actual != parts[2]:
                        raise ValueError(
                            f"OCI blob sha256 mismatch for {parts[2]}: got {actual}"
                        )


def _blob_path(oci_dir: Path, digest: str) -> Path:
    if not digest.startswith("sha256:"):
        raise ValueError(f"unsupported digest algorithm: {digest}")
    path = oci_dir / "blobs" / "sha256" / digest.split(":", 1)[1]
    if not path.is_file():
        raise ValueError(f"missing OCI blob {digest}")
    return path


def layer_blob_paths(oci_dir: Path, arch: str) -> list[Path]:
    index_path = oci_dir / "index.json"
    if not index_path.is_file():
        raise ValueError("OCI layout is missing index.json")
    index = json.loads(index_path.read_text(encoding="utf-8"))
    manifests = index.get("manifests") or []
    chosen = None
    for manifest in manifests:
        platform = (manifest.get("platform") or {}).get("architecture")
        if platform == arch:
            chosen = manifest
            break
    if chosen is None:
        architectures = [
            (item.get("platform") or {}).get("architecture") for item in manifests
        ]
        raise ValueError(
            f"no OCI manifest for architecture {arch} (found {architectures})"
        )
    manifest_path = _blob_path(oci_dir, chosen["digest"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    layers = manifest.get("layers") or []
    if not layers:
        raise ValueError("OCI manifest has no layers")
    paths: list[Path] = []
    for layer in layers:
        media = layer.get("mediaType", "")
        if "foreign" in media:
            raise ValueError(f"foreign image layer is not supported: {media}")
        paths.append(_blob_path(oci_dir, layer["digest"]))
    return paths


def flatten_oci(oci_dir: Path, arch: str, output: Path) -> None:
    if output.exists() and any(output.iterdir()):
        raise ValueError(f"output directory is not empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    for blob in layer_blob_paths(oci_dir, arch):
        log(f"applying layer {blob.name}")
        apply_tar_layer(blob, output)


def assert_usable_rootfs(output: Path) -> None:
    systemd = output / "lib" / "systemd" / "systemd"
    init_link = output / "sbin" / "init"
    if not systemd.is_file():
        raise ValueError("extracted rootfs is missing /lib/systemd/systemd")
    if not init_link.exists() and not init_link.is_symlink():
        raise ValueError("extracted rootfs is missing /sbin/init")
    os_release = output / "etc" / "os-release"
    if os_release.is_file():
        text = os_release.read_text(encoding="utf-8", errors="replace")
        pretty = next(
            (line.split("=", 1)[1].strip().strip('"') for line in text.splitlines() if line.startswith("PRETTY_NAME=")),
            "unknown",
        )
        log(f"rootfs operating system: {pretty}")


def write_packaging_version(output: Path, version: str) -> None:
    dest = output / "usr" / "local" / "share" / "uos-packaging"
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "version").write_text(version + "\n", encoding="utf-8")


def load_versions(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def installer_record(versions: dict, arch: str) -> dict:
    try:
        record = versions["installers"][arch]
    except KeyError as exc:
        raise ValueError(f"versions file has no installer for {arch}") from exc
    for key in ("url", "sha256"):
        if not record.get(key):
            raise ValueError(f"installer record for {arch} is missing {key}")
    return record


def extract_installer(
    installer: Path,
    arch: str,
    output: Path,
    *,
    version: str | None = None,
    allow_incomplete_rootfs: bool = False,
) -> None:
    with tempfile.TemporaryDirectory(prefix="uos-oci-") as temporary:
        oci_dir = Path(temporary) / "oci"
        unpack_image_tar(installer, oci_dir)
        flatten_oci(oci_dir, arch, output)
    if version:
        write_packaging_version(output, version)
    if not allow_incomplete_rootfs:
        assert_usable_rootfs(output)
    log(f"rootfs ready at {output}")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arch", required=True, choices=("amd64", "arm64"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--versions", type=Path, help="Pinned versions.json")
    parser.add_argument("--installer", type=Path, help="Use a local installer instead of downloading")
    parser.add_argument(
        "--download-only",
        type=Path,
        help="Verify the installer, save it at this path, and do not unpack it",
    )
    parser.add_argument("--sha256", help="Expected installer SHA256 when using --installer")
    parser.add_argument(
        "--allow-incomplete-rootfs",
        action="store_true",
        help="Skip the systemd sanity check. Used by unit tests.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    version = None
    record = None
    if args.versions:
        versions = load_versions(args.versions)
        version = str(versions.get("version") or "")
        record = installer_record(versions, args.arch)
    if args.installer is None and record is None:
        die("pass --versions or --installer")
    if args.download_only is None and args.output is None:
        die("pass --output, or pass --download-only to stop after the checksum check")

    work = tempfile.TemporaryDirectory(prefix="uos-installer-")
    try:
        if args.installer is not None:
            installer = args.installer
            expected = args.sha256 or (record["sha256"] if record else None)
            expected_size = record.get("size") if record and not args.sha256 else None
            if expected:
                log(f"checking SHA256 of {installer}")
                require_sha256(installer, expected, expected_size if isinstance(expected_size, int) else None)
        else:
            assert record is not None
            installer = Path(work.name) / "installer.bin"
            download_installer(record["url"], installer)
            size = record.get("size")
            require_sha256(installer, record["sha256"], size if isinstance(size, int) else None)
            log(f"SHA256 verified: {record['sha256']}")
        if args.download_only is not None:
            args.download_only.parent.mkdir(parents=True, exist_ok=True)
            if installer.resolve() != args.download_only.resolve():
                shutil.copyfile(installer, args.download_only)
            log(f"installer saved at {args.download_only}")
            return
        extract_installer(
            installer,
            args.arch,
            args.output,
            version=version,
            allow_incomplete_rootfs=args.allow_incomplete_rootfs,
        )
    except (ValueError, subprocess.CalledProcessError, zipfile.BadZipFile, tarfile.TarError, OSError) as exc:
        die(str(exc))
    finally:
        work.cleanup()


if __name__ == "__main__":
    main()
