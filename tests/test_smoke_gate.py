"""Regression tests for the systemd smoke gate and publish decision.

The post-merge failure on main was `compose logs | grep -q` under
`pipefail`. grep -q closes the pipe on the first match, compose logs gets
SIGPIPE, and pipefail reports failure after the journal has started
flooding. PID 1's command name is the signal the smoke test uses instead.
"""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run_bash(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", script],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


class SmokeGateTests(unittest.TestCase):
    def test_comm_is_systemd_accepts_pid1_names(self) -> None:
        completed = run_bash(
            """
            set -euo pipefail
            source scripts/smoke-test.sh
            comm_is_systemd systemd
            comm_is_systemd init
            comm_is_systemd $'systemd\\n'
            if comm_is_systemd bash; then exit 1; fi
            if comm_is_systemd ''; then exit 1; fi
            if comm_is_systemd journalctl; then exit 1; fi
            """
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_pipefail_grep_q_hides_a_matched_line(self) -> None:
        completed = run_bash(
            r"""
            set -o pipefail
            if { printf 'starting systemd\n'; yes 'journal line'; } | grep -q 'starting systemd'; then
              printf 'matched\n'
              exit 0
            fi
            printf 'hidden\n'
            exit 1
            """
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("hidden", completed.stdout)

    def test_smoke_script_does_not_grep_the_handoff_line(self) -> None:
        lines = (ROOT / "scripts" / "smoke-test.sh").read_text(encoding="utf-8").splitlines()
        code = "\n".join(line for line in lines if not line.lstrip().startswith("#"))
        self.assertNotIn("grep -q", code)
        self.assertIn("comm_is_systemd", code)

    def test_should_publish_skips_dispatch_when_push_is_false(self) -> None:
        cases = {
            ("pull_request", ""): "false",
            ("pull_request", "true"): "false",
            ("workflow_dispatch", "false"): "false",
            ("workflow_dispatch", "true"): "true",
            ("workflow_dispatch", ""): "true",
            ("push", ""): "true",
        }
        for (event_name, input_push), expected in cases.items():
            with self.subTest(event_name=event_name, input_push=input_push):
                completed = run_bash(
                    f"""
                    EVENT_NAME={event_name!r} INPUT_PUSH={input_push!r} \
                      bash scripts/should-publish.sh
                    """
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertEqual(completed.stdout.strip(), expected)


if __name__ == "__main__":
    unittest.main()
