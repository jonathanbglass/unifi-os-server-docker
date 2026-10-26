#!/usr/bin/env python3
"""Compare the pinned UniFi OS Server release with Ubiquiti's firmware API.

The firmware API is the checksum source. The download-center API lists the
same installers but does not publish checksums, so it is only a cross-check.
This program never selects a release that the firmware API cannot describe
with a SHA256, and it does not download the installer.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ARCH_PLATFORMS = {
    "amd64": "linux-x64",
    "arm64": "linux-arm64",
}
FIRMWARE_API = "https://fw-update.ui.com/api/firmware-latest"
DOWNLOAD_API = "https://download.svc.ui.com/v1/downloads/products/slugs/unifi-os-server"
PRODUCT = "unifi-os-server"
ALLOWED_HOST = "fw-download.ubnt.com"


def version_key(value: str) -> tuple[int, ...]:
    text = value.strip()
    if text[:1] in ("v", "V"):
        text = text[1:]
    if not text:
        raise ValueError("empty version")
    parts: list[int] = []
    for piece in text.split("."):
        if not piece.isdigit():
            raise ValueError(f"version is not numeric: {value}")
        parts.append(int(piece))
    return tuple(parts)


def is_newer(candidate: str, current: str) -> bool:
    return version_key(candidate) > version_key(current)


def firmware_url(platform: str) -> str:
    return (
        f"{FIRMWARE_API}?filter=eq~~product~~{PRODUCT}"
        f"&filter=eq~~platform~~{platform}"
        f"&filter=eq~~channel~~release"
    )


def fetch_json(url: str, timeout: int = 45) -> dict:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "unifi-os-server-docker-update-check",
        },
    )
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError(f"expected a JSON object from {url}")
            return payload
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
            last_error = exc
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"request failed for {url}: {last_error}")


def parse_firmware_payload(payload: dict, platform: str) -> dict:
    items = (payload.get("_embedded") or {}).get("firmware") or []
    if not items:
        raise ValueError(f"firmware API returned no release for {platform}")
    item = items[0]
    if item.get("product") != PRODUCT:
        raise ValueError(f"firmware product is {item.get('product')}, expected {PRODUCT}")
    if item.get("platform") != platform:
        raise ValueError(f"firmware platform is {item.get('platform')}, expected {platform}")
    if item.get("channel") not in (None, "release"):
        raise ValueError(f"firmware channel is {item.get('channel')}, expected release")
    version = str(item.get("version") or "")
    version_key(version)
    if version[:1] in ("v", "V"):
        version = version[1:]
    sha256 = str(item.get("sha256_checksum") or "")
    if len(sha256) != 64 or any(character not in "0123456789abcdef" for character in sha256.lower()):
        raise ValueError(f"firmware API sha256 is missing or not 64 hex characters: {sha256!r}")
    url = str(((item.get("_links") or {}).get("data") or {}).get("href") or "")
    parsed_host = urllib.parse.urlparse(url).hostname if url else ""
    if urllib.parse.urlparse(url).scheme != "https" or parsed_host != ALLOWED_HOST:
        raise ValueError(f"firmware download URL is not on {ALLOWED_HOST}: {url}")
    if version not in url:
        raise ValueError(f"firmware download URL does not contain version {version}: {url}")
    size = item.get("file_size")
    if not isinstance(size, int) or size <= 0:
        raise ValueError(f"firmware file_size is not a positive integer: {size!r}")
    return {
        "version": version,
        "platform": platform,
        "url": url,
        "sha256": sha256.lower(),
        "size": size,
    }


def parse_download_center(payload: dict) -> dict[str, str]:
    downloads = payload.get("downloads")
    if not isinstance(downloads, list):
        raise ValueError("download center response has no downloads array")
    best: dict[str, str] = {}
    for item in downloads:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        version = str(item.get("version") or "")
        if "Linux" not in name:
            continue
        try:
            version_key(version)
        except ValueError:
            continue
        if "arm64" in name:
            platform = "linux-arm64"
        elif "x64" in name:
            platform = "linux-x64"
        else:
            continue
        current = best.get(platform)
        if current is None or is_newer(version, current):
            best[platform] = version
    if "linux-x64" not in best or "linux-arm64" not in best:
        raise ValueError(f"download center is missing a Linux installer: {best}")
    return best


def evaluate_update(current: str, firmware: dict[str, dict], download: dict[str, str] | None) -> dict:
    version_key(current)
    amd64 = firmware["amd64"]
    arm64 = firmware["arm64"]
    warnings: list[str] = []
    if amd64["version"] != arm64["version"]:
        return {
            "status": "arch_mismatch",
            "current_version": current,
            "firmware_version": "",
            "warnings": [
                f"linux-x64 is {amd64['version']} and linux-arm64 is {arm64['version']}"
            ],
            "update_available": False,
        }
    firmware_version = amd64["version"]
    if download is not None:
        for platform in ("linux-x64", "linux-arm64"):
            listed = download.get(platform)
            if listed and is_newer(listed, firmware_version):
                return {
                    "status": "download_center_ahead",
                    "current_version": current,
                    "firmware_version": firmware_version,
                    "download_center": download,
                    "warnings": [
                        "The download center lists a newer Linux installer than the "
                        "firmware API. The pin was left unchanged because that newer "
                        "build has no published SHA256 yet."
                    ],
                    "update_available": False,
                }
            if listed and is_newer(firmware_version, listed):
                warnings.append(
                    f"The firmware API {platform} release {firmware_version} is newer "
                    f"than the download center release {listed}."
                )
    else:
        warnings.append(
            "The download center API was unavailable, so this result uses only "
            "the firmware API."
        )
    if not is_newer(firmware_version, current):
        status = "up_to_date"
        update = False
        if is_newer(current, firmware_version):
            warnings.append(
                f"The pin {current} is newer than the firmware API release {firmware_version}. "
                "No downgrade pull request was opened."
            )
    else:
        status = "update_available"
        update = True
    return {
        "status": status,
        "current_version": current,
        "firmware_version": firmware_version,
        "download_center": download,
        "warnings": warnings,
        "update_available": update,
        "installers": {
            "amd64": amd64,
            "arm64": arm64,
        },
    }


PIN_FILES = ("Dockerfile", "docker-compose.yml", ".env.example", "README.md")


def rewrite_pin_references(root: Path, old: str, new: str) -> list[str]:
    """Replace the pinned version in packaging files.

    versions.json is updated separately. Installer URLs and checksums stay
    out of these files so a version string replace cannot corrupt them.
    """
    if old == new:
        return []
    version_key(old)
    version_key(new)
    changed: list[str] = []
    for name in PIN_FILES:
        path = root / name
        text = path.read_text(encoding="utf-8")
        if old not in text:
            raise ValueError(f"{name} does not mention pinned version {old}")
        for line in text.splitlines():
            if old not in line:
                continue
            lowered = line.lower()
            if "fw-download.ubnt.com" in lowered or "sha256" in lowered:
                raise ValueError(
                    f"{name} mentions {old} next to an installer URL or checksum. "
                    "Keep those only in versions.json."
                )
        path.write_text(text.replace(old, new), encoding="utf-8")
        changed.append(name)
    return changed


def apply_update(path: Path, document: dict, result: dict) -> None:
    if not result.get("update_available"):
        return
    installers = result["installers"]
    document["version"] = result["firmware_version"]
    document.setdefault("installers", {})
    for arch in ("amd64", "arm64"):
        item = installers[arch]
        document["installers"][arch] = {
            "platform": item["platform"],
            "url": item["url"],
            "sha256": item["sha256"],
            "size": item["size"],
        }
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


def load_versions(path: Path) -> dict:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or not document.get("version"):
        raise ValueError(f"{path} has no version")
    return document


def check(path: Path, *, apply: bool) -> dict:
    document = load_versions(path)
    current = str(document["version"])
    firmware: dict[str, dict] = {}
    for arch, platform in ARCH_PLATFORMS.items():
        payload = fetch_json(firmware_url(platform))
        firmware[arch] = parse_firmware_payload(payload, platform)
    download = None
    try:
        download = parse_download_center(fetch_json(DOWNLOAD_API))
    except (RuntimeError, ValueError) as exc:
        print(f"[check] download center unavailable: {exc}", file=sys.stderr)
    result = evaluate_update(current, firmware, download)
    if apply and result.get("update_available"):
        apply_update(path, document, result)
        rewrite_pin_references(path.parent, current, str(result["firmware_version"]))
    return result


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--versions", type=Path, default=Path("versions.json"))
    parser.add_argument("--apply", action="store_true", help="Rewrite versions.json when an update is available")
    parser.add_argument("--json-out", type=Path, help="Write the machine-readable result")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    try:
        result = check(args.versions, apply=args.apply)
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(f"[check] ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    text = json.dumps(result, indent=2)
    if args.json_out:
        args.json_out.write_text(text + "\n", encoding="utf-8")
    print(text)
    print(f"[check] status={result['status']} current={result['current_version']} firmware={result.get('firmware_version', '')}", file=sys.stderr)


if __name__ == "__main__":
    main()
