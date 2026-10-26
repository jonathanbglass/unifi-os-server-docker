#!/usr/bin/env python3
"""Unit tests for OCI layer flattening. These fixtures are not Ubiquiti images."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import sys
import tarfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import extract_rootfs  # noqa: E402


def _gzip_layer(members: list[tuple[str, bytes | None]]) -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as archive:
        for name, payload in members:
            info = tarfile.TarInfo(name)
            if payload is None:
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                archive.addfile(info)
                continue
            data = payload
            info.size = len(data)
            info.mode = 0o644
            info.type = tarfile.REGTYPE
            archive.addfile(info, io.BytesIO(data))
    compressed = io.BytesIO()
    with gzip.GzipFile(fileobj=compressed, mode="wb", mtime=0) as handle:
        handle.write(raw.getvalue())
    return compressed.getvalue()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ExtractTests(unittest.TestCase):
    def test_url_allowlist(self) -> None:
        good = (
            "https://fw-download.ubnt.com/data/unifi-os-server/"
            "5172-linux-x64-5.1.42-12e9e3cf-8f8b-4e54-928c-76b80a10c8a4.42-x64"
        )
        extract_rootfs.validate_installer_url(good)
        with self.assertRaises(ValueError):
            extract_rootfs.validate_installer_url("http://fw-download.ubnt.com/data/unifi-os-server/x")
        with self.assertRaises(ValueError):
            extract_rootfs.validate_installer_url("https://example.com/data/unifi-os-server/x")
        with self.assertRaises(ValueError):
            extract_rootfs.validate_installer_url(
                "https://user:pass@fw-download.ubnt.com/data/unifi-os-server/x"
            )

    def test_sha256_and_size(self) -> None:
        path = Path(self.id().replace(".", "_") + ".bin")
        # Keep the temp file inside the test's temporary directory via TemporaryDirectory.
        import tempfile

        with tempfile.TemporaryDirectory() as temporary:
            installer = Path(temporary) / "installer.bin"
            installer.write_bytes(b"abc")
            digest = hashlib.sha256(b"abc").hexdigest()
            extract_rootfs.require_sha256(installer, digest, 3)
            with self.assertRaises(ValueError):
                extract_rootfs.require_sha256(installer, "0" * 64, 3)
            with self.assertRaises(ValueError):
                extract_rootfs.require_sha256(installer, digest, 4)
        self.assertFalse(path.exists())

    def test_whiteouts_and_path_escape(self) -> None:
        import tempfile

        layer1 = _gzip_layer(
            [
                ("etc", None),
                ("etc/keep", b"keep"),
                ("dir", None),
                ("dir/old", b"old"),
                ("bin", None),
                ("bin/tool", b"tool"),
            ]
        )
        layer2 = _gzip_layer(
            [
                ("etc/.wh.keep", b""),
                ("dir/.wh..wh..opq", b""),
                ("dir/new", b"new"),
            ]
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            oci = root / "oci"
            output = root / "rootfs"
            blob_dir = oci / "blobs" / "sha256"
            blob_dir.mkdir(parents=True)
            digests = []
            for blob in (layer1, layer2):
                digest = _sha256(blob)
                (blob_dir / digest).write_bytes(blob)
                digests.append(digest)
            manifest = {
                "schemaVersion": 2,
                "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
                "layers": [
                    {
                        "mediaType": "application/vnd.docker.image.rootfs.diff.tar.gzip",
                        "digest": f"sha256:{digest}",
                        "size": len(blob),
                    }
                    for digest, blob in zip(digests, (layer1, layer2))
                ],
            }
            manifest_bytes = json.dumps(manifest).encode()
            manifest_digest = _sha256(manifest_bytes)
            (blob_dir / manifest_digest).write_bytes(manifest_bytes)
            index = {
                "schemaVersion": 2,
                "manifests": [
                    {
                        "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
                        "digest": f"sha256:{manifest_digest}",
                        "size": len(manifest_bytes),
                        "platform": {"architecture": "amd64", "os": "linux"},
                    }
                ],
            }
            (oci / "index.json").write_text(json.dumps(index), encoding="utf-8")
            (oci / "oci-layout").write_text('{"imageLayoutVersion":"1.0.0"}\n', encoding="utf-8")
            extract_rootfs.flatten_oci(oci, "amd64", output)
            self.assertFalse((output / "etc" / "keep").exists())
            self.assertFalse((output / "dir" / "old").exists())
            self.assertEqual((output / "dir" / "new").read_bytes(), b"new")
            self.assertEqual((output / "bin" / "tool").read_bytes(), b"tool")

            with self.assertRaises(ValueError):
                extract_rootfs.parts_of("../etc/passwd")
            with self.assertRaises(ValueError):
                extract_rootfs.flatten_oci(oci, "arm64", root / "other")

    def test_archive_root_entry_is_skipped(self) -> None:
        import tempfile

        layer = _gzip_layer([(".", None), ("./etc/hostname", b"uos")])
        with tempfile.TemporaryDirectory() as temporary:
            blob = Path(temporary) / "layer.gz"
            blob.write_bytes(layer)
            output = Path(temporary) / "rootfs"
            output.mkdir()
            extract_rootfs.apply_tar_layer(blob, output)
            self.assertEqual((output / "etc" / "hostname").read_bytes(), b"uos")
            self.assertEqual(extract_rootfs.parts_of("."), [])
            self.assertEqual(extract_rootfs.parts_of("./usr/bin"), ["usr", "bin"])

    def test_cli_on_synthetic_installer(self) -> None:
        import tempfile

        layer = _gzip_layer([("hello.txt", b"hi")])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            blob = layer
            digest = _sha256(blob)
            manifest = {
                "schemaVersion": 2,
                "layers": [
                    {
                        "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                        "digest": f"sha256:{digest}",
                        "size": len(blob),
                    }
                ],
            }
            manifest_bytes = json.dumps(manifest).encode()
            manifest_digest = _sha256(manifest_bytes)
            index = {
                "schemaVersion": 2,
                "manifests": [
                    {
                        "digest": f"sha256:{manifest_digest}",
                        "platform": {"architecture": "amd64", "os": "linux"},
                    }
                ],
            }
            image = io.BytesIO()
            with tarfile.open(fileobj=image, mode="w") as archive:
                def add(name: str, data: bytes) -> None:
                    info = tarfile.TarInfo(name)
                    info.size = len(data)
                    archive.addfile(info, io.BytesIO(data))

                add(f"blobs/sha256/{digest}", blob)
                add(f"blobs/sha256/{manifest_digest}", manifest_bytes)
                add("index.json", json.dumps(index).encode())
                add("oci-layout", b'{"imageLayoutVersion":"1.0.0"}\n')
            installer = root / "installer.bin"
            with zipfile.ZipFile(installer, "w") as archive:
                archive.writestr("image.tar", image.getvalue())
            output = root / "rootfs"
            extract_rootfs.main(
                [
                    "--installer",
                    str(installer),
                    "--arch",
                    "amd64",
                    "--output",
                    str(output),
                    "--allow-incomplete-rootfs",
                ]
            )
            self.assertEqual((output / "hello.txt").read_bytes(), b"hi")


if __name__ == "__main__":
    unittest.main()
