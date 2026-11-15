"""The msr load path, exercised without root.

`bin/install` must make CPU voltage control work in this boot, not the next
one: `modprobe msr` is the whole mechanism (intel-undervolt does the same),
and its failures are diagnosed rather than answered with "reboot". These
tests source the shipped library with temporary paths and a fake modprobe,
so the real code runs as an unprivileged user.

Run from the project root: python3 -m unittest tests.test_load_msr -v
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
LIB = os.path.join(HERE, "..", "bin", "lib-msr.sh")


class LoadMsrTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="omatuner-msr-load-")
        self.sysfs = os.path.join(self.tmp, "sys", "module", "msr")
        self.device = os.path.join(self.tmp, "dev", "cpu", "0", "msr")
        os.makedirs(os.path.dirname(self.sysfs))
        os.makedirs(os.path.dirname(self.device))
        self.bindir = os.path.join(self.tmp, "bin")
        os.makedirs(self.bindir)

    def write_tool(self, name: str, body: str) -> None:
        path = os.path.join(self.bindir, name)
        with open(path, "w") as fh:
            fh.write("#!/usr/bin/env bash\n" + body + "\n")
        os.chmod(path, 0o755)

    def run_load(self, extra_env: dict | None = None) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env["PATH"] = self.bindir + ":" + env["PATH"]
        env.update(extra_env or {})
        script = f'set -u; . "{os.path.abspath(LIB)}"; load_msr "{self.sysfs}" "{self.device}"'
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env, timeout=30)

    def test_already_loaded_is_a_no_op(self):
        with open(self.sysfs, "w") as fh:
            fh.write("")
        with open(self.device, "w") as fh:
            fh.write("")
        self.write_tool("modprobe", "echo should-not-run >&2; exit 9")
        result = self.run_load()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("msr already loaded", result.stdout)

    def test_load_waits_for_the_device_node(self):
        # modprobe creates the node slightly later, like udev does.
        self.write_tool("modprobe", f'sleep 0.1; : > "{self.device}"')
        result = self.run_load()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("msr loaded", result.stdout)
        self.assertTrue(os.path.exists(self.device))

    def test_missing_module_names_the_real_cause(self):
        self.write_tool("modprobe", "echo 'modprobe: FATAL: Module msr not found' >&2; exit 1")
        self.write_tool("modinfo", "exit 1")
        result = self.run_load()
        self.assertEqual(result.returncode, 1)
        self.assertIn("could not load the msr module", result.stderr)
        self.assertIn("does not ship msr.ko", result.stderr)
        self.assertNotIn("reboot", result.stderr.lower().replace("needs a reboot", ""))

    def test_generic_failure_points_at_modprobe_now(self):
        self.write_tool("modprobe", "echo 'modprobe: ERROR: could not insert' >&2; exit 1")
        self.write_tool("modinfo", "echo /lib/modules/x/kernel/arch/x86/kernel/msr.ko.zst")
        result = self.run_load()
        self.assertEqual(result.returncode, 1)
        self.assertIn("Retry now with: sudo modprobe msr", result.stderr)
        self.assertIn("picks it up on its own", result.stderr)

    def test_loaded_module_without_device_is_reported(self):
        self.write_tool("modprobe", "exit 0")
        result = self.run_load()
        self.assertEqual(result.returncode, 1)
        self.assertIn("did not appear", result.stderr)

    def test_real_modprobe_message_is_reported_not_a_stale_file(self):
        # The reported install printed "Module msr not found" while modprobe
        # had never run: stderr went to a fixed /tmp path that
        # fs.protected_regular refuses to open, the redirection failed, and
        # the diagnosis quoted the file's stale contents. A stale file must
        # not be able to speak for this run.
        stale = "/tmp/omatuner-modprobe.err"
        with open(stale, "w") as fh:
            fh.write("modprobe: FATAL: Module msr not found\n")
        self.write_tool("modprobe", "echo 'modprobe: ERROR: could not insert: Operation not permitted' >&2; exit 1")
        self.write_tool("modinfo", "echo /lib/modules/x/kernel/arch/x86/kernel/msr.ko.zst")
        result = self.run_load()
        self.assertEqual(result.returncode, 1)
        self.assertIn("Operation not permitted", result.stderr)
        self.assertNotIn("Module msr not found", result.stderr)

    def test_privilege_failure_says_what_to_do(self):
        self.write_tool("modprobe", "echo \"modprobe: ERROR: could not insert 'msr': Operation not permitted\" >&2; exit 1")
        self.write_tool("modinfo", "echo /lib/modules/x/kernel/arch/x86/kernel/msr.ko.zst")
        result = self.run_load()
        self.assertEqual(result.returncode, 1)
        self.assertIn("lacks the privilege", result.stderr)
        self.assertIn("sudo modprobe msr", result.stderr)

    def test_missing_module_message_when_modprobe_says_so(self):
        self.write_tool("modprobe", "echo 'modprobe: FATAL: Module msr not found' >&2; exit 1")
        self.write_tool("modinfo", "echo /lib/modules/x/kernel/arch/x86/kernel/msr.ko.zst")
        result = self.run_load()
        self.assertEqual(result.returncode, 1)
        self.assertIn("cannot find msr.ko", result.stderr)

    def test_the_library_never_writes_a_fixed_temp_file(self):
        self.write_tool("modprobe", "echo 'boom' >&2; exit 1")
        self.write_tool("modinfo", "exit 1")
        for name in ("omatuner-modprobe.err",):
            try:
                os.unlink(os.path.join(tempfile.gettempdir(), name))
            except FileNotFoundError:
                pass
        self.run_load()
        self.assertFalse(os.path.exists(os.path.join(tempfile.gettempdir(), "omatuner-modprobe.err")))


if __name__ == "__main__":
    unittest.main()
