#!/usr/bin/env python3
"""Unit tests for the version pin comparison. No network access."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import check_upstream  # noqa: E402


def firmware(version: str, platform: str, arch_token: str) -> dict:
    return {
        "version": version,
        "platform": platform,
        "url": f"https://fw-download.ubnt.com/data/unifi-os-server/example-{arch_token}-{version}",
        "sha256": "a" * 64,
        "size": 10,
    }


class CheckUpstreamTests(unittest.TestCase):
    def test_version_order(self) -> None:
        self.assertTrue(check_upstream.is_newer("5.1.42", "5.1.9"))
        self.assertFalse(check_upstream.is_newer("5.1.42", "5.1.42"))
        self.assertFalse(check_upstream.is_newer("5.1.40", "5.1.42"))
        self.assertEqual(check_upstream.version_key("v5.1.42"), (5, 1, 42))
        with self.assertRaises(ValueError):
            check_upstream.version_key("5.1.42-rc1")

    def test_parse_firmware_payload(self) -> None:
        payload = {
            "_embedded": {
                "firmware": [
                    {
                        "channel": "release",
                        "product": "unifi-os-server",
                        "platform": "linux-x64",
                        "version": "v5.1.42",
                        "file_size": 100,
                        "sha256_checksum": "ab" * 32,
                        "_links": {
                            "data": {
                                "href": "https://fw-download.ubnt.com/data/unifi-os-server/file-5.1.42"
                            }
                        },
                    }
                ]
            }
        }
        parsed = check_upstream.parse_firmware_payload(payload, "linux-x64")
        self.assertEqual(parsed["version"], "5.1.42")
        self.assertEqual(parsed["sha256"], "ab" * 32)

    def test_update_available_and_ahead_download_center(self) -> None:
        current = "5.1.40"
        fw = {
            "amd64": firmware("5.1.42", "linux-x64", "x64"),
            "arm64": firmware("5.1.42", "linux-arm64", "arm64"),
        }
        result = check_upstream.evaluate_update(
            current,
            fw,
            {"linux-x64": "5.1.42", "linux-arm64": "5.1.42"},
        )
        self.assertEqual(result["status"], "update_available")
        ahead = check_upstream.evaluate_update(
            current,
            fw,
            {"linux-x64": "5.1.50", "linux-arm64": "5.1.42"},
        )
        self.assertEqual(ahead["status"], "download_center_ahead")
        self.assertFalse(ahead["update_available"])

    def test_no_downgrade(self) -> None:
        fw = {
            "amd64": firmware("5.1.40", "linux-x64", "x64"),
            "arm64": firmware("5.1.40", "linux-arm64", "arm64"),
        }
        result = check_upstream.evaluate_update("5.1.42", fw, None)
        self.assertEqual(result["status"], "up_to_date")
        self.assertFalse(result["update_available"])

    def test_pinned_checksums_match_the_recorded_api_values(self) -> None:
        document = json.loads((ROOT / "versions.json").read_text(encoding="utf-8"))
        self.assertEqual(document["version"], "5.1.42")
        amd64 = document["installers"]["amd64"]
        arm64 = document["installers"]["arm64"]
        self.assertEqual(
            amd64["sha256"],
            "f6111e9396a42c74016f5dde9b01fbef486a6fe69efe137935e6e38b7c22f94d",
        )
        self.assertEqual(amd64["size"], 785796978)
        self.assertEqual(
            arm64["sha256"],
            "8e29bd50cde8f781ace4b85599256ef62c02723bdc751044d4db90a087fb5207",
        )
        self.assertEqual(arm64["size"], 770177202)
        self.assertTrue(amd64["url"].startswith("https://fw-download.ubnt.com/data/unifi-os-server/"))
        self.assertIn("5.1.42", amd64["url"])
        self.assertIn("5.1.42", arm64["url"])
        self.assertEqual(document["mongodb_image"], "mongo:4.4.29")

    def test_rewrite_pin_references(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "Dockerfile").write_text("ARG UOS_VERSION=5.1.42\n", encoding="utf-8")
            (root / "docker-compose.yml").write_text(
                "image: ghcr.io/example/unifi-os-server:5.1.42\n", encoding="utf-8"
            )
            (root / ".env.example").write_text(
                "UOS_IMAGE=ghcr.io/example/unifi-os-server:5.1.42\n", encoding="utf-8"
            )
            (root / "README.md").write_text(
                "Pinned release: 5.1.42\n", encoding="utf-8"
            )
            changed = check_upstream.rewrite_pin_references(root, "5.1.42", "5.1.43")
            self.assertEqual(changed, list(check_upstream.PIN_FILES))
            self.assertIn("5.1.43", (root / "Dockerfile").read_text(encoding="utf-8"))
            self.assertNotIn("5.1.42", (root / "README.md").read_text(encoding="utf-8"))

    def test_rewrite_refuses_installer_urls(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in check_upstream.PIN_FILES:
                (root / name).write_text("version 5.1.42\n", encoding="utf-8")
            (root / "README.md").write_text(
                "https://fw-download.ubnt.com/data/unifi-os-server/file-5.1.42\n",
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                check_upstream.rewrite_pin_references(root, "5.1.42", "5.1.43")


if __name__ == "__main__":
    unittest.main()
