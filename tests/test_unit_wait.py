"""`bin/install`'s helper check, exercised without systemd.

`systemctl restart` reports success before the unit has bound anything, and a
transient nvmlInit failure leaves the root helper in "activating
(auto-restart)" for RestartSec: `is-active --quiet` is false there even though
systemd is bringing it back. The install must retry past that window before it
warns, and must never abort. These tests source the shipped library with fake
`systemctl` and `sleep`, so the real loop runs without root or systemd.

Run from the project root: python3 -m unittest tests.test_unit_wait -v
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
LIB = os.path.join(HERE, "..", "bin", "lib-unit-wait.sh")


class UnitWaitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="omatuner-unit-wait-")
        self.bindir = os.path.join(self.tmp, "bin")
        os.makedirs(self.bindir)
        self.calls = os.path.join(self.tmp, "systemctl-calls")
        self.sleeps = os.path.join(self.tmp, "sleeps")
        self.write_tool("systemctl", """
name="$1 $2 $3"
printf '%s\\n' "$name" >> "$CALLS"
count=$(cat "$COUNT" 2>/dev/null || echo 0)
count=$((count + 1))
echo "$count" > "$COUNT"
[[ $count -ge ${ACTIVE_FROM:-1} ]]
""")
        self.write_tool("sleep", 'printf "%s\\n" "$*" >> "$SLEEPS"')

    def write_tool(self, name: str, body: str) -> None:
        path = os.path.join(self.bindir, name)
        with open(path, "w") as fh:
            fh.write("#!/usr/bin/env bash\n" + body + "\n")
        os.chmod(path, 0o755)

    def run_wait(self, delays: str, active_from: int) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env["PATH"] = self.bindir + ":" + env["PATH"]
        env["CALLS"] = self.calls
        env["SLEEPS"] = self.sleeps
        env["COUNT"] = os.path.join(self.tmp, "count")
        env["ACTIVE_FROM"] = str(active_from)
        script = (
            f'set -u; . "{os.path.abspath(LIB)}"; '
            f"wait_for_active_unit omatuner-fan.service {delays}"
        )
        return subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True, env=env, timeout=30
        )

    def slept(self) -> list[str]:
        if not os.path.exists(self.sleeps):
            return []
        with open(self.sleeps) as fh:
            return fh.read().split()

    def test_an_immediately_active_unit_returns_after_one_check(self):
        result = self.run_wait("1 3", active_from=1)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.slept(), ["1"])

    def test_a_unit_restarted_by_systemd_is_not_reported_as_down(self):
        # Inactive at 1 s (activating/auto-restart), active after the retry
        # past RestartSec: the install must not warn.
        result = self.run_wait("1 3", active_from=2)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.slept(), ["1", "3"], "the retry waits past RestartSec=2 s")
        with open(self.calls) as fh:
            self.assertEqual(fh.read().split(), ["is-active", "--quiet", "omatuner-fan.service"] * 2)

    def test_a_unit_that_stays_down_is_still_reported(self):
        result = self.run_wait("1 3", active_from=99)
        self.assertEqual(result.returncode, 1, "a dead unit still fails the check")
        self.assertEqual(self.slept(), ["1", "3"])

    def test_the_default_is_one_short_check(self):
        result = self.run_wait("", active_from=1)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.slept(), ["1"])


if __name__ == "__main__":
    unittest.main()
