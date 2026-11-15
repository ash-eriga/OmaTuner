"""Arguments that must be answered before a daemon or a removal is involved.

The CLI carries only the numeric controls, and `bin/uninstall` is the one
script whose mistake cannot be undone: a value the daemon could never accept,
or an option the script does not know, has to end as a usage error with a
sentence, not as a traceback or a real uninstall.
"""

from __future__ import annotations

import io
import json
import os
import pathlib
import subprocess
import sys
import unittest
from contextlib import redirect_stderr

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "daemon"))

from omatuner import config, daemon


class ApplyParsingTests(unittest.TestCase):
    def run_cli(self, *argv: str) -> tuple[int, str]:
        error = io.StringIO()
        with redirect_stderr(error):
            code = daemon.main(list(argv))
        return code, error.getvalue()

    def test_a_non_numeric_setting_is_a_usage_error(self):
        code, error = self.run_cli("apply", "--component", "gpu", "fan=curve")
        self.assertEqual(code, 2)
        self.assertIn("whole number", error)
        self.assertIn("plugin", error)

    def test_a_setting_without_equals_is_a_usage_error(self):
        code, error = self.run_cli("apply", "--component", "gpu", "memory")
        self.assertEqual(code, 2)
        self.assertIn("key=value", error)


class StatusFormatTests(unittest.TestCase):
    def test_a_temperature_limit_prints_without_a_plus_sign(self):
        self.assertEqual(daemon._format_control_value(85, "°C"), "85")

    def test_offsets_keep_their_sign(self):
        self.assertEqual(daemon._format_control_value(-50, "mV"), "-50")
        self.assertEqual(daemon._format_control_value(100, "MHz"), "+100")


class ManifestVersionTests(unittest.TestCase):
    def test_the_manifest_reports_the_daemon_version(self):
        manifest = (
            pathlib.Path(__file__).resolve().parents[1]
            / "ui"
            / "ash.omatuner"
            / "manifest.json"
        )
        with open(manifest, "r", encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["version"], config.VERSION)


class UninstallArgumentsTests(unittest.TestCase):
    """`sudo bin/uninstall` removes the service: a typo must never start it."""

    script = str(pathlib.Path(__file__).resolve().parents[1] / "bin" / "uninstall")

    def run_script(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", self.script, *args], capture_output=True, text=True, timeout=60
        )

    def test_help_exits_before_touching_anything(self):
        done = self.run_script("--help")
        self.assertEqual(done.returncode, 0)
        self.assertIn("usage: sudo bin/uninstall", done.stdout)
        self.assertNotIn("OmaTuner system uninstall", done.stdout)

    def test_an_unknown_option_is_rejected(self):
        done = self.run_script("--dryrun")
        self.assertEqual(done.returncode, 2)
        self.assertIn("unknown option", done.stderr)
        self.assertNotIn("OmaTuner system uninstall", done.stdout)

    def test_dry_run_plans_without_changing_anything(self):
        done = self.run_script("--dry-run")
        self.assertEqual(done.returncode, 0)
        self.assertIn("(dry run)", done.stdout)
        self.assertIn("done (dry run). Nothing was changed.", done.stdout)


class UninstallUiGuidanceTests(unittest.TestCase):
    """`bin/uninstall-ui` prints the by-hand system removal.

    It claims to be the same list bin/uninstall runs, and used to omit the
    steps that leave the machine in the state the next install fails on: the
    desktop member and its group, the udev trigger after removing the rule,
    the empty /etc/omatuner and reset-failed.
    """

    script = pathlib.Path(__file__).resolve().parents[1] / "bin" / "uninstall-ui"

    def by_hand_list(self) -> str:
        source = self.script.read_text()
        start = source.index("Or by hand with root")
        end = source.index("\nfi\n", start)
        return "\n".join(
            line for line in source[start:end].splitlines() if 'echo "  sudo ' in line
        )

    def test_the_list_names_the_steps_that_undo_a_system_install(self):
        lines = self.by_hand_list()
        for step in (
            "systemctl reset-failed",
            "rmdir /etc/omatuner",
            "udevadm trigger --subsystem-match=msr",
            "gpasswd -d",
            "groupdel omatuner",
        ):
            self.assertIn(step, lines)

    def test_the_steps_run_in_the_same_order_as_bin_uninstall(self):
        lines = self.by_hand_list()
        order = [
            "disable --now omatuner-fan.service",
            "reset-failed",
            "rm -f /usr/local/bin/omatuner",
            "rmdir /etc/omatuner",
            "rm -f /etc/modules-load.d/omatuner.conf",
            "udevadm control --reload-rules",
            "udevadm trigger --subsystem-match=msr",
            "gpasswd -d",
            "groupdel omatuner",
        ]
        positions = [lines.index(step) for step in order]
        self.assertEqual(positions, sorted(positions), order)

    def test_the_one_command_removal_is_recommended_first(self):
        source = self.script.read_text()
        self.assertLess(source.index("sudo bin/uninstall"), source.index("Or by hand with root"))


if __name__ == "__main__":
    unittest.main()
