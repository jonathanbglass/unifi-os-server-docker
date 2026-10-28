"""Host backup and stuck-device helpers, with the container namespace stubbed out."""

from __future__ import annotations

import os
import stat
import subprocess
import textwrap
import unittest
from pathlib import Path

from tests.test_unifi_mongo import FakeMongo

ROOT = Path(__file__).resolve().parents[1]


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


class HostMaintenanceTests(unittest.TestCase):
    def test_normalize_and_restore_confirmation(self) -> None:
        script = r"""
        set -euo pipefail
        source scripts/unifi-host-lib.sh
        source scripts/unifi-backup.sh
        got="$(normalize_mac 'AA-BB-CC-DD-EE-FF')"
        test "$got" = "aa:bb:cc:dd:ee:ff"
        if normalize_mac nope; then exit 1; fi
        dir="$(mktemp -d)"
        mkdir -p "$dir/ace"
        printf 'x' > "$dir/ace/device.bson"
        test "$(resolve_dump_dir "$dir")" = "$dir/ace"
        if printf 'no\n' | confirm_restore false "$dir"; then exit 1; fi
        confirm_restore true "$dir"
        """
        completed = subprocess.run(
            ["bash", "-c", script],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_forget_backs_up_before_delete_and_restarts_network(self) -> None:
        server = FakeMongo()
        self.addCleanup(server.close)
        server.docs["device"].append({"mac": "aa:bb:cc:dd:ee:ff", "name": "porch", "model": "USW-Lite"})
        server.docs["power_supervisor"].append({"mac": "aa:bb:cc:dd:ee:ff", "name": "porch"})
        self._run_forget(server, ["--yes", "aa:bb:cc:dd:ee:ff"], expect=0)
        self.assertEqual(server.docs["device"], [])
        self.assertEqual(server.docs["power_supervisor"], [])

    def test_forget_without_confirmation_does_not_dump_or_delete(self) -> None:
        server = FakeMongo()
        self.addCleanup(server.close)
        server.docs["device"].append({"mac": "aa:bb:cc:dd:ee:ff", "name": "porch"})
        log = self._run_forget(server, ["aa:bb:cc:dd:ee:ff"], expect=1)
        self.assertNotIn("mongodump", log.read_text(encoding="utf-8"))
        self.assertEqual(len(server.docs["device"]), 1)

    def test_dry_run_does_not_dump(self) -> None:
        server = FakeMongo()
        self.addCleanup(server.close)
        server.docs["device"].append({"mac": "aa:bb:cc:dd:ee:ff", "name": "porch"})
        log = self._run_forget(server, ["--dry-run", "AA:BB:CC:DD:EE:FF"], expect=0)
        self.assertNotIn("mongodump", log.read_text(encoding="utf-8"))
        self.assertEqual(len(server.docs["device"]), 1)

    def test_backup_then_restore_round_trip_invokes_host_tools(self) -> None:
        server = FakeMongo()
        self.addCleanup(server.close)
        work = Path(self._work())
        out = work / "dump"
        env = self._stub_env(work, server.port)
        backup = subprocess.run(
            ["bash", str(ROOT / "scripts" / "unifi-backup.sh"), "backup", "--out", str(out)],
            cwd=ROOT,
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(backup.returncode, 0, backup.stderr)
        self.assertTrue((out / "ace" / "device.bson").is_file())
        mode = stat.S_IMODE(out.stat().st_mode)
        self.assertEqual(mode & 0o077, 0)
        calls = (work / "calls").read_text(encoding="utf-8")
        self.assertIn("mongodump", calls)
        restore = subprocess.run(
            ["bash", str(ROOT / "scripts" / "unifi-backup.sh"), "restore", "--from", str(out), "--yes"],
            cwd=ROOT,
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(restore.returncode, 0, restore.stderr)
        self.assertIn("mongorestore", (work / "calls").read_text(encoding="utf-8"))
        self.assertIn("--drop", (work / "calls").read_text(encoding="utf-8"))

    def _run_forget(self, server: FakeMongo, args: list[str], expect: int) -> Path:
        work = Path(self._work())
        env = self._stub_env(work, server.port)
        completed = subprocess.run(
            ["bash", str(ROOT / "scripts" / "unifi-forget-device.sh"), *args],
            cwd=work,
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, expect, completed.stderr + completed.stdout)
        log = work / "calls"
        if expect == 0 and "--yes" in args:
            text = log.read_text(encoding="utf-8")
            dump_at = text.find("mongodump")
            delete_at = text.find("delete-device")
            restart_at = text.find("exec")
            self.assertGreaterEqual(dump_at, 0)
            self.assertGreater(delete_at, dump_at)
            self.assertGreater(restart_at, delete_at)
            self.assertNotIn("x_authkey", completed.stdout + completed.stderr)
        return log

    def _work(self) -> str:
        import tempfile

        tmp = tempfile.mkdtemp(prefix="unifi-host-")
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", tmp], check=False))
        return tmp

    def _stub_env(self, work: Path, port: int) -> dict[str, str]:
        bindir = work / "bin"
        bindir.mkdir()
        log = work / "calls"
        _write_executable(
            bindir / "podman",
            textwrap.dedent(
                f"""\
                #!/bin/bash
                printf 'podman %s\\n' "$*" >> {log}
                if [[ "$1" == inspect ]]; then
                  printf '4242\\n'
                  exit 0
                fi
                if [[ "$1" == exec ]]; then
                  exit 0
                fi
                exit 1
                """
            ),
        )
        _write_executable(
            bindir / "mongodump",
            textwrap.dedent(
                f"""\
                #!/bin/bash
                printf 'mongodump %s\\n' "$*" >> {log}
                out=""
                while [[ $# -gt 0 ]]; do
                  if [[ "$1" == --out ]]; then
                    out="$2"
                    shift 2
                  else
                    shift
                  fi
                done
                mkdir -p "$out/ace"
                printf '{{}}' > "$out/ace/device.bson"
                """
            ),
        )
        _write_executable(
            bindir / "mongorestore",
            textwrap.dedent(
                f"""\
                #!/bin/bash
                printf 'mongorestore %s\\n' "$*" >> {log}
                """
            ),
        )
        _write_executable(
            bindir / "nsenter-stub",
            textwrap.dedent(
                f"""\
                #!/bin/bash
                while [[ "$1" != -- ]]; do
                  shift
                done
                shift
                printf 'nsenter %s\\n' "$*" >> {log}
                exec "$@"
                """
            ),
        )
        env = os.environ.copy()
        env["PATH"] = f"{bindir}:{env.get('PATH', '')}"
        env["UNIFI_NSENTER"] = str(bindir / "nsenter-stub")
        env["UOS_RUNTIME"] = "podman"
        env["UOS_CONTAINER"] = "uosserver"
        env["UNIFI_MONGO_PORT"] = str(port)
        env["UNIFI_BACKUP_DIR"] = str(work / "backups")
        env.pop("UOS_PODMAN_USER", None)
        return env


if __name__ == "__main__":
    unittest.main()
