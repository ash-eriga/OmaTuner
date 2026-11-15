"""Service account lifecycle, exercised without root.

The install used to abort with

    useradd: group omatuner exists - if you want to add this user to that
    group, use -g.

after a previous uninstall: `userdel -r` removes a private group only when
USERGROUPS_ENAB=yes *and* the group has no members, and the desktop user is
a member (that is how it reaches the socket). These tests run the shipped
script with fake account tools, so both the orphan-group install and the
uninstall that prevents it are covered without touching the real database.

Run from the project root: python3 -m unittest tests.test_service_user -v
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
LIB = os.path.abspath(os.path.join(HERE, "..", "bin", "lib-service-user.sh"))


def nologin_shell() -> str:
    """The shell the library resolves, in the same order."""
    for candidate in ("/usr/sbin/nologin", "/usr/bin/nologin", "/sbin/nologin"):
        if os.access(candidate, os.X_OK):
            return candidate
    return shutil.which("nologin") or "/bin/false"


GETENT = """#!/usr/bin/env bash
case "$1" in
  passwd) [[ " ${FAKE_USERS:-} " == *" $2 "* ]] ;;
  group)  [[ " ${FAKE_GROUPS:-} " == *" $2 "* ]] ;;
  *) exit 2 ;;
esac
"""

RECORDER = """#!/usr/bin/env bash
name=$(basename "$0")
printf '%s\\n' "$name $*" >> "$CALLS"
case "$name" in
  useradd)  exit "${FAKE_EXIT_USERADD_STATUS:-0}" ;;
  userdel)  exit "${FAKE_EXIT_USERDEL_STATUS:-0}" ;;
  groupdel) exit "${FAKE_EXIT_GROUPDEL_STATUS:-0}" ;;
  gpasswd)  exit "${FAKE_EXIT_GPASSWD_STATUS:-0}" ;;
esac
exit 0
"""


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="omatuner-account-")
        self.bindir = os.path.join(self.tmp, "bin")
        self.calls = os.path.join(self.tmp, "calls")
        os.makedirs(self.bindir)
        open(self.calls, "w").close()
        for tool in ("getent", "useradd", "userdel", "groupdel", "gpasswd"):
            path = os.path.join(self.bindir, tool)
            with open(path, "w") as fh:
                fh.write(GETENT if tool == "getent" else RECORDER)
            os.chmod(path, 0o755)

    def run_lib(self, snippet: str, users: str = "", groups: str = "", **extra_env) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env.update({
            "PATH": self.bindir + ":" + env["PATH"],
            "CALLS": self.calls,
            "FAKE_USERS": users,
            "FAKE_GROUPS": groups,
        })
        env.update(extra_env)
        script = f'set -euo pipefail; . "{LIB}"; {snippet}'
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env, timeout=30)

    def calls_made(self) -> list[str]:
        with open(self.calls) as fh:
            return [line.strip() for line in fh if line.strip()]

    # ---------------------------------------------------------------- install

    def test_fresh_install_creates_a_private_group(self):
        result = self.run_lib('ensure_service_user omatuner /var/lib/omatuner')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls_made(), [
            "useradd --system --user-group --home-dir /var/lib/omatuner "
            f"--create-home --shell {nologin_shell()} omatuner",
        ])

    def test_orphan_group_is_reused_instead_of_failing(self):
        result = self.run_lib('ensure_service_user omatuner /var/lib/omatuner', groups="omatuner")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("reusing it", result.stdout)
        self.assertEqual(self.calls_made(), [
            "useradd --system --gid omatuner --home-dir /var/lib/omatuner "
            f"--create-home --shell {nologin_shell()} omatuner",
        ])

    def test_existing_user_is_untouched(self):
        result = self.run_lib('ensure_service_user omatuner /var/lib/omatuner',
                              users="omatuner", groups="omatuner")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("already exists", result.stdout)
        self.assertEqual(self.calls_made(), [])

    # -------------------------------------------------------------- uninstall

    def test_uninstall_removes_member_user_and_group_in_that_order(self):
        result = self.run_lib('remove_service_user omatuner ash',
                              users="omatuner", groups="omatuner")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls_made(), [
            "gpasswd -d ash omatuner",
            "userdel -r omatuner",
            "groupdel omatuner",
        ])

    def test_group_is_removed_even_without_a_desktop_member(self):
        result = self.run_lib('remove_service_user omatuner',
                              users="omatuner", groups="omatuner")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls_made(), ["userdel -r omatuner", "groupdel omatuner"])

    def test_missing_user_and_group_are_no_ops(self):
        result = self.run_lib('remove_service_user omatuner ash')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("already absent", result.stdout)
        self.assertEqual(self.calls_made(), [])

    def test_foreign_primary_group_is_reported_not_fatal(self):
        result = self.run_lib('remove_service_user omatuner ash',
                              users="omatuner", groups="omatuner",
                              FAKE_EXIT_GROUPDEL_STATUS="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("primary group", result.stderr)

    def test_userdel_failure_does_not_abort_the_uninstall(self):
        result = self.run_lib('remove_service_user omatuner ash',
                              users="omatuner", groups="omatuner",
                              FAKE_EXIT_USERDEL_STATUS="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("could not remove user", result.stderr)
        self.assertNotIn("groupdel omatuner", self.calls_made())

    # ------------------------------------------------------------------ dry run

    def test_dry_run_wrapper_records_instead_of_executing(self):
        # bin/install defines `run` for --dry-run; the library must use it.
        result = self.run_lib(
            'run() { echo "DRY $*"; }; ensure_service_user omatuner /var/lib/omatuner',
            groups="omatuner",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("DRY useradd --system --gid omatuner", result.stdout)
        self.assertEqual(self.calls_made(), [])


if __name__ == "__main__":
    unittest.main()
