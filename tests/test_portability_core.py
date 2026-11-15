"""Portability defects found on machines other than the reference one.

No hardware is touched: the core runs on the fake backends under a temporary
state directory, and the MSR backend gets an injected device path, lockdown
file and virtualization detector.

Run from the project root: PYTHONPATH=daemon python3 -m unittest tests.test_portability_core -v
"""

from __future__ import annotations

import errno
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "daemon"))

from omatuner.backends.base import BackendError
from omatuner.backends.cpu_msr import CpuMsrBackend
from omatuner.statestore import read_json, write_json

try:
    from tests.support import make_core
except ImportError:  # discover -s tests puts the tests directory on sys.path
    from support import make_core

LOCKED_CAUSE = (
    "the firmware locks the CPU voltage offset (common on Intel 10th gen and newer, "
    "or with CFG lock); undervolting is not possible on this machine"
)


def journal_records(core, kind: str) -> list[dict]:
    return [record for record in core.journal.tail(80) if record["kind"] == kind]


class SavedDefaultReplayTests(unittest.TestCase):
    """F3: a default saved in expert mode is not written in normal mode."""

    def prepare(self, defaults: dict) -> str:
        tmp = tempfile.mkdtemp(prefix="omatuner-replay-")
        with open(os.path.join(tmp, "events.jsonl"), "w", encoding="utf-8") as fh:
            fh.write('{"kind": "boot", "tsMs": 1}\n')
        with open(os.path.join(tmp, "clean-shutdown"), "w", encoding="utf-8") as fh:
            fh.write("clean\n")
        write_json(os.path.join(tmp, "defaults.json"), defaults)
        return tmp

    def test_an_expert_default_is_not_written_under_normal_mode(self):
        # Saved by a build installed with --expert; this daemon runs normal.
        tmp = self.prepare({"cpu": {"core": -400, "cache": -350}, "gpu": None})
        core, cpu, _ = make_core(tmp)

        self.assertEqual(cpu.apply_log, [], "the out-of-range default is never written")
        rejected = journal_records(core, "replay-out-of-range")
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]["component"], "cpu")
        self.assertEqual(rejected[0]["values"], {"core": -400, "cache": -350})

        notice = core.snapshot()["notice"]
        self.assertEqual(notice["type"], "apply-failed")
        self.assertIn("exceed the normal mode range", notice["message"])
        self.assertIn("must be applied again", notice["message"])

        # The record is kept, on disk and in memory.
        self.assertEqual(core.defaults["cpu"], {"core": -400, "cache": -350})
        self.assertEqual(
            read_json(os.path.join(tmp, "defaults.json"))["cpu"]["core"], -400
        )

    def test_an_in_range_default_still_replays(self):
        tmp = self.prepare({"cpu": {"core": -100, "cache": -80}, "gpu": None})
        core, cpu, _ = make_core(tmp)

        self.assertEqual(cpu.values, {"core": -100, "cache": -80})
        self.assertEqual(journal_records(core, "replay-out-of-range"), [])


class NoCpuSensor:
    """A machine whose hwmon tree has no package sensor."""

    def cpu_package_temp(self):
        return None

    def cpu_package_limit(self):
        return None

    def sample(self):
        return {}


class ThermalGuardReportingTests(unittest.TestCase):
    """F6: a guard with no reading is not reported as armed."""

    def test_a_guard_without_a_sensor_is_not_reported_armed(self):
        tmp = tempfile.mkdtemp(prefix="omatuner-guard-")
        core, _, _ = make_core(tmp, system_telemetry=NoCpuSensor())
        core.apply("cpu", {"core": -50})
        core.confirm()

        state = core.snapshot()
        self.assertIs(state["thermalGuard"]["cpu"], False)
        self.assertIsNone(state["thermalGuardCause"]["gpu"])
        self.assertIn("CPU package temperature", state["thermalGuardCause"]["cpu"])

        # Once per guard window, not once per sample.
        core._thermal_tick()
        core._thermal_tick()
        missing = journal_records(core, "thermal-sensor-missing")
        self.assertEqual(len(missing), 1)
        self.assertEqual(missing[0]["component"], "cpu")


class MsrFirmwareLockTests(unittest.TestCase):
    """F7: a refused offset takes both CPU controls off the panel."""

    def test_a_lock_failure_marks_both_controls_unavailable(self):
        backend = CpuMsrBackend({"intel": True, "name": "Intel"})
        backend.available = True
        # Write, then a read-back that does not hold the requested offset.
        with mock.patch.object(backend, "_transact", side_effect=[0, 0]):
            with self.assertRaises(BackendError) as ctx:
                backend.apply({"core": -100})

        self.assertEqual(ctx.exception.cause, "firmware")
        self.assertIn("write locked", str(ctx.exception))
        controls = {control.key: control for control in backend.controls()}
        for key in ("core", "cache"):
            self.assertFalse(controls[key].available)
            self.assertEqual(controls[key].cause, LOCKED_CAUSE)


class MsrPermissionCauseTests(unittest.TestCase):
    """F8: EPERM under kernel lockdown names the lockdown, not the mailbox."""

    def test_eperm_under_lockdown_reports_the_lockdown(self):
        tmp = tempfile.mkdtemp(prefix="omatuner-msr-")
        msr = os.path.join(tmp, "msr")
        with open(msr, "w", encoding="ascii") as fh:
            fh.write("")
        lockdown = os.path.join(tmp, "lockdown")
        with open(lockdown, "w", encoding="ascii") as fh:
            fh.write("none [integrity] confidentiality\n")
        backend = CpuMsrBackend(
            {"intel": True, "name": "Intel"},
            msr_path=msr,
            lockdown_path=lockdown,
        )

        with mock.patch.object(
            backend,
            "_transact",
            side_effect=OSError(errno.EPERM, "Operation not permitted"),
        ):
            backend.probe()

        self.assertFalse(backend.available)
        self.assertEqual(
            backend.cause,
            "kernel lockdown (integrity) blocks MSR writes; "
            "it is usually enabled by Secure Boot",
        )


class MsrVirtualMachineTests(unittest.TestCase):
    """F9: a missing device in a VM points at the host, not at modprobe."""

    def test_a_missing_device_in_a_vm_names_host_access(self):
        tmp = tempfile.mkdtemp(prefix="omatuner-msr-")
        backend = CpuMsrBackend(
            {"intel": True, "name": "Intel"},
            msr_path=os.path.join(tmp, "msr"),
            detect_virt=lambda: "kvm",
        )

        backend.probe()

        self.assertFalse(backend.available)
        self.assertEqual(
            backend.cause,
            "running in a virtual machine or container (kvm); "
            "the host must expose MSR access",
        )


if __name__ == "__main__":
    unittest.main()
