"""Entrypoint inform-host pinning, without a UniFi OS rootfs."""

from __future__ import annotations

import os
import stat
import subprocess
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APPLY = ROOT / "scripts" / "uos-apply-inform.sh"
ENTRYPOINT = ROOT / "scripts" / "uos-entrypoint.sh"


def run_apply(args: list[str], env: dict[str, str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    merged = os.environ.copy()
    for key in (
        "UOS_SYSTEM_IP",
        "UNIFI_SYSTEM_IP",
        "INFORM_HOST",
        "UOS_INFORM_HOST_OVERRIDE",
        "MONGO_INTERNAL",
        "MONGO_HOST",
        "MONGO_PORT",
        "MONGO_USER",
        "MONGO_PASS",
        "UOS_MONGO_AUTH_REQUIRED",
    ):
        merged.pop(key, None)
    merged.update(env)
    return subprocess.run(
        ["bash", str(APPLY), *args],
        cwd=cwd or ROOT,
        env=merged,
        check=False,
        capture_output=True,
        text=True,
    )


class InformHostTests(unittest.TestCase):
    def test_requires_an_address(self) -> None:
        completed = run_apply(["--print-host"], {})
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("UOS_SYSTEM_IP", completed.stderr)

    def test_alias_and_conflict(self) -> None:
        ok = run_apply(["--print-host"], {"UNIFI_SYSTEM_IP": "192.0.2.10"})
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertEqual(ok.stdout.strip(), "192.0.2.10")
        bad = run_apply(
            ["--print-host"],
            {"UOS_SYSTEM_IP": "192.0.2.10", "INFORM_HOST": "192.0.2.11"},
        )
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn("disagree", bad.stderr)

    def test_rejects_shell_metacharacters(self) -> None:
        completed = run_apply(["--print-host"], {"UOS_SYSTEM_IP": "192.0.2.10;touch /tmp/pwn"})
        self.assertNotEqual(completed.returncode, 0)

    def test_properties_are_idempotent_and_follow_the_data_symlink(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            unifi = root / "var" / "lib" / "unifi"
            link_parent = root / "usr" / "lib" / "unifi"
            unifi.mkdir(parents=True)
            link_parent.mkdir(parents=True)
            (link_parent / "data").symlink_to(unifi, target_is_directory=True)
            (unifi / "system.properties").write_text(
                "# system_ip=a.b.c.d\ndb.mongo.local=false\n",
                encoding="utf-8",
            )
            env = {"UOS_SYSTEM_IP": "192.0.2.10"}
            first = run_apply(["--properties-only", "--root", str(root)], env)
            self.assertEqual(first.returncode, 0, first.stderr)
            second = run_apply(["--properties-only", "--root", str(root)], env)
            self.assertEqual(second.returncode, 0, second.stderr)
            text = (unifi / "system.properties").read_text(encoding="utf-8")
            self.assertEqual(text.count("\nsystem_ip=") + text.startswith("system_ip="), 1)
            self.assertIn("system_ip=192.0.2.10\n", text)
            self.assertIn("db.mongo.local=false", text)
            self.assertIn("# system_ip=a.b.c.d", text)
            self.assertNotIn("system_ip_override", text)
            host_ip = (root / "etc" / "host-ip").read_bytes()
            self.assertEqual(host_ip, b"192.0.2.10")
            self.assertEqual(stat.S_IMODE((root / "etc" / "host-ip").stat().st_mode) & 0o777, 0o644)

    def test_dns_name_skips_host_ip_file(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            completed = run_apply(
                ["--properties-only", "--root", str(root)],
                {"INFORM_HOST": "unifi.example.com"},
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            props = (root / "var" / "lib" / "unifi" / "system.properties").read_text(encoding="utf-8")
            self.assertIn("system_ip=unifi.example.com\n", props)
            self.assertFalse((root / "etc" / "host-ip").exists())
            self.assertIn("skipping /etc/host-ip", completed.stderr)

    def test_bridge_address_is_warned_and_still_written(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            completed = run_apply(
                ["--properties-only", "--root", str(root)],
                {"UOS_SYSTEM_IP": "172.18.0.1"},
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("172.18.0.1", completed.stderr)
            text = (root / "var" / "lib" / "unifi" / "system.properties").read_text(encoding="utf-8")
            self.assertIn("system_ip=172.18.0.1\n", text)

    def test_units_do_not_contain_a_mongo_password(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            completed = run_apply(
                ["--install-units", "--root", str(root)],
                {
                    "UOS_SYSTEM_IP": "192.0.2.10",
                    "MONGO_INTERNAL": "true",
                    "MONGO_USER": "unifi",
                    "MONGO_PASS": "secret-value",
                    "UOS_INFORM_HOST_OVERRIDE": "true",
                },
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            dropin = (
                root / "etc" / "systemd" / "system" / "unifi.service.d" / "10-inform-host.conf"
            ).read_text(encoding="utf-8")
            service = (root / "etc" / "systemd" / "system" / "uos-inform-override.service").read_text(
                encoding="utf-8"
            )
            combined = dropin + service
            self.assertIn("UOS_SYSTEM_IP=192.0.2.10", combined)
            self.assertIn("MONGO_PORT=27117", combined)
            self.assertIn("UOS_MONGO_AUTH_REQUIRED=true", combined)
            self.assertIn("ExecStartPre=+/usr/local/bin/uos-apply-inform.sh --properties-only", dropin)
            self.assertNotIn("secret-value", combined)
            self.assertNotIn("ExecStartPre=\n", dropin)
            link = root / "etc" / "systemd" / "system" / "multi-user.target.wants" / "uos-inform-override.service"
            self.assertTrue(link.is_symlink())

    def test_mongo_only_restarts_once_when_the_override_changes(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bindir = root / "bin"
            bindir.mkdir()
            log = root / "calls"
            (bindir / "python3").write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/bash
                    printf 'python3 %s\\n' "$*" >> {log}
                    printf 'changed\\n'
                    """
                ),
                encoding="utf-8",
            )
            (bindir / "systemctl").write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/bash
                    printf 'systemctl %s\\n' "$*" >> {log}
                    """
                ),
                encoding="utf-8",
            )
            os.chmod(bindir / "python3", 0o755)
            os.chmod(bindir / "systemctl", 0o755)
            env = {
                "PATH": f"{bindir}:{os.environ.get('PATH', '')}",
                "UOS_SYSTEM_IP": "192.0.2.10",
                "MONGO_INTERNAL": "true",
                "UOS_INFORM_HOST_OVERRIDE": "true",
                "UOS_INFORM_OVERRIDE_TIMEOUT": "5",
            }
            first = run_apply(["--mongo-only", "--root", str(root)], env)
            self.assertEqual(first.returncode, 0, first.stderr)
            calls = log.read_text(encoding="utf-8")
            self.assertIn("systemctl restart unifi", calls)
            second = run_apply(["--mongo-only", "--root", str(root)], env)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(log.read_text(encoding="utf-8").count("systemctl restart unifi"), 1)
            self.assertIn("already restarted", second.stderr)

    def test_override_disabled_does_not_call_python(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bindir = root / "bin"
            bindir.mkdir()
            (bindir / "python3").write_text("#!/bin/bash\nexit 99\n", encoding="utf-8")
            os.chmod(bindir / "python3", 0o755)
            completed = run_apply(
                ["--mongo-only", "--root", str(root)],
                {
                    "PATH": f"{bindir}:{os.environ.get('PATH', '')}",
                    "UOS_SYSTEM_IP": "192.0.2.10",
                    "UOS_INFORM_HOST_OVERRIDE": "false",
                },
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("disabled", completed.stderr)

    def test_entrypoint_delegates_to_the_apply_script(self) -> None:
        text = ENTRYPOINT.read_text(encoding="utf-8")
        self.assertIn("uos-apply-inform.sh", text)
        self.assertIn("--print-host", text)
        self.assertIn("--properties-only", text)
        self.assertIn("--install-units", text)
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("uos-apply-inform.sh", dockerfile)
        self.assertIn("unifi_mongo.py", dockerfile)


if __name__ == "__main__":
    unittest.main()
