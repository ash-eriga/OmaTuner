"""Thermal verification: trial/guard sampling and automatic restore.

The sensors themselves are real hardware; these tests drive the policy with
a fake system telemetry source and the fake GPU backend, so they do not touch
the machine's temperatures.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "daemon"))

from omatuner import config
from omatuner.backends.fake import FakeBackend
from omatuner.backends.telemetry import SystemTelemetry
from omatuner.core import TunerCore
from omatuner.statestore import Journal, now_ms, read_json

try:
    from tests.support import make_core
except ImportError:  # discover -s tests puts the tests directory on sys.path
    from support import make_core


class FakeSystemTelemetry:
    def __init__(self):
        self.cpu_c = 50
        self.cpu_limit = 100

    def cpu_package_temp(self):
        return self.cpu_c

    def cpu_package_limit(self):
        return self.cpu_limit

    def sample(self):
        return {"cpuTemp": self.cpu_c, "ramUsage": 40}


class ThermalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="omatuner-thermal-")
        self.system = FakeSystemTelemetry()
        self.core, _, self.gpu = make_core(
            self.tmp, clean_marker=True, system_telemetry=self.system
        )

    def test_trial_is_reverted_when_the_gpu_reaches_its_ceiling(self):
        self.core.apply("gpu", {"core": 50})
        self.gpu.temperature_c = 90
        self.core._thermal_tick()
        self.assertIsNone(self.core.trial)
        self.assertEqual(self.gpu.values["core"], 0)
        self.assertIn("thermal", self.core.last_reverted["reason"])
        self.assertIn("90", self.core.last_reverted["reason"])
        with open(os.path.join(self.tmp, "events.jsonl"), encoding="utf-8") as fh:
            self.assertIn("thermal-revert", fh.read())

    def test_a_hot_cpu_does_not_revert_an_unconfirmed_gpu_trial(self):
        # The trial path used to revert the component under test for a breach
        # of the other one: a hot CPU discarded the user's GPU draft.
        self.core.apply("gpu", {"core": 50})
        self.system.cpu_c = 96  # CPU ceiling is min(95, 100 - 2) = 95
        self.core._thermal_tick()
        self.assertIsNotNone(self.core.trial, "the GPU trial survives a CPU breach")
        self.assertEqual(self.gpu.values["core"], 50)
        self.assertIsNone(self.core.last_reverted)

    def test_confirmed_apply_is_restored_inside_the_guard_window(self):
        self.core.apply("gpu", {"core": 50})
        self.core.confirm()
        self.gpu.temperature_c = 89
        self.core._thermal_tick()
        self.assertEqual(self.gpu.values["core"], 0)
        self.assertIn("thermal", self.core.last_reverted["reason"])
        self.assertFalse(self.core.snapshot()["thermalGuard"]["gpu"])

    def test_high_temperature_after_the_guard_window_does_not_revert(self):
        self.core.apply("gpu", {"core": 50})
        self.core.confirm()
        self.core.thermal_guard["gpu"]["until"] = now_ms() - 1
        self.gpu.temperature_c = 95
        self.core._thermal_tick()
        self.assertEqual(self.gpu.values["core"], 50)
        self.assertIsNone(self.core.last_reverted)

    def test_cpu_ceiling_accounts_for_the_hwmon_limit(self):
        sample = self.core._thermal_sample()
        cpu_ceiling, _ = self.core._thermal_ceilings(sample)
        self.assertEqual(cpu_ceiling, 95)  # min(95, crit 100 - margin 2)
        self.system.cpu_c = 96
        breaches = self.core._thermal_breaches(self.core._thermal_sample())
        self.assertEqual([b["component"] for b in breaches], ["cpu"])

    def test_target_above_the_default_ceiling_raises_the_gpu_guard(self):
        self.gpu.temp_target_c = 90
        sample = self.core._thermal_sample()
        _, gpu_ceiling = self.core._thermal_ceilings(sample)
        self.assertEqual(gpu_ceiling, 92)  # max(88, 90 + 2), still below 99 - 2
        self.gpu.temperature_c = 90
        self.assertEqual(self.core._thermal_breaches(self.core._thermal_sample()), [])
        self.gpu.temperature_c = 92
        self.assertEqual(
            [b["component"] for b in self.core._thermal_breaches(self.core._thermal_sample())],
            ["gpu"],
        )

    def test_revert_clears_the_guard_for_that_component(self):
        self.core.apply("gpu", {"core": 50})
        self.core.confirm()
        self.assertTrue(self.core.thermal_guard)
        self.core.apply("gpu", {"core": 20})
        self.core.revert("deadline")
        self.assertFalse(self.core.snapshot()["thermalGuard"]["gpu"])

    def test_reason_is_persisted_for_the_ui(self):
        self.core.apply("gpu", {"core": 50})
        self.gpu.temperature_c = 90
        self.core._thermal_tick()
        stored = read_json(os.path.join(self.tmp, "last-reverted.json"))
        self.assertIn("thermal", stored["reason"])
        self.assertEqual(stored["component"], "gpu")

    def test_a_guard_breach_drops_the_overheated_default_and_last_applied(self):
        # A breach inside the post-confirm guard restores the hardware, so
        # the persisted records must follow: the next boot used to reapply
        # the very values that overheated, with no guard left to catch them.
        self.core.apply("gpu", {"core": 50}, "default")
        self.core.confirm()
        self.assertEqual(read_json(os.path.join(self.tmp, "defaults.json"))["gpu"]["core"], 50)

        self.gpu.temperature_c = 89
        self.core._thermal_tick()
        self.assertEqual(self.gpu.values["core"], 0)

        self.assertIsNone(read_json(os.path.join(self.tmp, "defaults.json"))["gpu"])
        last = read_json(os.path.join(self.tmp, "last-applied.json"))["gpu"]
        self.assertEqual(last["settings"]["core"], 0)
        self.assertEqual(last["mode"], "thermal-revert")

        # The next boot must not put the overheated value back.
        with open(os.path.join(self.tmp, "clean-shutdown"), "w") as fh:
            fh.write("clean\n")
        core2, _, gpu2 = make_core(self.tmp, system_telemetry=FakeSystemTelemetry())
        self.assertEqual(gpu2.values.get("core", 0), 0)
        self.assertFalse(core2.snapshot()["defaults"]["gpu"])

    def test_a_guard_breach_restores_the_previous_default(self):
        self.core.apply("gpu", {"core": 30}, "default")
        self.core.confirm()
        self.core.thermal_guard.clear()
        self.core.apply("gpu", {"core": 50}, "default")
        self.core.confirm()

        self.gpu.temperature_c = 89
        self.core._thermal_tick()

        self.assertEqual(self.gpu.values["core"], 30)
        self.assertEqual(read_json(os.path.join(self.tmp, "defaults.json"))["gpu"]["core"], 30)
        last = read_json(os.path.join(self.tmp, "last-applied.json"))["gpu"]
        self.assertEqual(last["settings"]["core"], 30)


class TelemetrySampleTests(unittest.TestCase):
    def test_an_unreadable_proc_stat_reports_no_cpu_usage(self):
        telemetry = SystemTelemetry()
        with mock.patch.object(
                SystemTelemetry, "_read_proc_stat", side_effect=OSError("no /proc/stat")):
            sample = telemetry.sample()
        self.assertIsInstance(sample, dict)
        self.assertNotIn("cpuUsage", sample)


if __name__ == "__main__":
    unittest.main()
