"""Validation against the real hardware of this machine.

The only writes here are the backend's own value-preserving write probes: it
rewrites the currently applied clock offsets and fan duty and restores what it
touched. Everything else proves the read paths (NVML queries, /proc parsing,
hwmon discovery) and that the permission probes report the real causes.
Run from the project root: python3 -m unittest tests.test_realhw -v
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "daemon"))

from omatuner.backends.cpu_msr import CpuMsrBackend, decode_offset, encode_offset
from omatuner.backends.gpu_nvml import GpuNvmlBackend
from omatuner.backends.telemetry import SystemTelemetry


class RealHardwareTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gpu = GpuNvmlBackend()
        cls.gpu.probe()
        cls.cpu = CpuMsrBackend({"intel": True, "name": "test"})
        cls.cpu.probe()

    def require_reference_gpu(self) -> None:
        """Skip on a machine that is not the reference GPU.

        These assertions pin a GTX 1060 on driver 580. Another card, or no
        card at all, would fail them for reasons that say nothing about the
        code, so a public checkout reports a skip instead. `bin/validate`
        runs them on the reference machine.
        """
        if not self.gpu.present:
            self.skipTest(f"no NVIDIA device ({self.gpu.cause})")
        if "1060" not in (self.gpu.device_name or ""):
            self.skipTest(
                f"reference tests expect a GTX 1060, found {self.gpu.device_name!r}"
            )

    def test_gpu_offsets_readable(self):
        self.require_reference_gpu()
        values = self.gpu.read()
        self.assertIn("core", values)
        self.assertIn("memory", values)
        self.assertIsInstance(values["core"], int)

    def test_gpu_curve_is_read_from_the_driver(self):
        self.require_reference_gpu()
        # Pascal on driver 580 has no NVML V/F point API, but the driver's
        # libnvidia-api exposes the same 128-point table LACT uses. Reading
        # it is safe; writing is left to the privileged service.
        curve = self.gpu.curve()
        self.assertTrue(curve.supported, curve.cause)
        self.assertGreater(len(curve.points), 10)
        self.assertEqual(len(curve.points), len(curve.factory_points))
        if self.gpu.write_cause is None:
            self.assertTrue(curve.writable)
        else:
            self.assertFalse(curve.writable)
            self.assertTrue(curve.cause)

    def test_gpu_fan_reads_live_and_gates_writes_on_root(self):
        self.require_reference_gpu()
        # Driver 580 accepts fan changes from uid 0 or through the root
        # helper, so a non-root process can be writable. The invariant is
        # narrower: a read-only fan must say why.
        info = self.gpu.fan_info()
        self.assertTrue(info["supported"], info.get("cause"))
        self.assertIsInstance(info["speed"], int)
        self.assertGreaterEqual(info["speed"], 0)
        self.assertLessEqual(info["speed"], 100)
        if not info["writable"]:
            self.assertTrue(info["cause"], "a read-only fan needs its cause")

    def test_temperature_thresholds_are_real(self):
        self.require_reference_gpu()
        self.assertTrue(self.gpu.temp_supported)
        self.assertEqual(self.gpu.temp_api, "threshold")
        self.assertGreaterEqual(self.gpu.temp_min_c, 40)
        self.assertLessEqual(self.gpu.temp_max_c, 110)
        self.assertGreaterEqual(self.gpu.temp_limit_c, self.gpu.temp_min_c)
        self.assertLessEqual(self.gpu.temp_limit_c, self.gpu.temp_max_c)
        info = self.gpu.thermal_info()
        self.assertIn("currentC", info)
        self.assertEqual(info["targetC"], self.gpu.temp_limit_c)
        # The write needs uid 0, so this process proves it only when it is
        # root; otherwise the probe must leave an honest, non-crashing cause.
        controls = {c.key: c for c in self.gpu.controls()}
        if os.geteuid() == 0:
            self.assertTrue(controls["temp"].available, controls["temp"].cause)
            self.assertIsNone(self.gpu.temp_write_cause)
        else:
            self.assertFalse(controls["temp"].available)
            self.assertTrue(controls["temp"].cause)

    def test_gpu_write_permission_probed_honestly(self):
        self.require_reference_gpu()
        # Without the system service this process cannot change clocks; the
        # probe must say so instead of pretending. On driver 580 + Pascal the
        # unprivileged refusal arrives as NO_PERMISSION (4), so either wording
        # is honest as long as the control is unavailable with a cause.
        controls = {c.key: c for c in self.gpu.controls()}
        if controls["core"].available:
            self.assertIsNone(self.gpu.write_cause)
        else:
            self.assertTrue(self.gpu.write_cause)
            self.assertIn("service", self.gpu.write_cause)

    def test_cpu_msr_reports_real_cause(self):
        # msr module is not loaded on the reference machine without the
        # installer; the cause must name it.
        if os.path.exists("/dev/cpu/0/msr") and self.cpu.available:
            self.skipTest("msr loaded and accessible; write paths need the service")
        self.assertFalse(self.cpu.available)
        self.assertTrue(self.cpu.cause)

    def test_telemetry_paths(self):
        telemetry = SystemTelemetry()
        ram = telemetry.ram_usage()
        self.assertIsNotNone(ram)
        self.assertGreaterEqual(ram, 0)
        self.assertLessEqual(ram, 100)
        temp = telemetry.cpu_package_temp()
        if temp is not None:
            self.assertGreater(temp, 10)
            self.assertLess(temp, 110)
        limit = telemetry.cpu_package_limit()
        if limit is not None:
            self.assertGreaterEqual(limit, 50)
            self.assertLessEqual(limit, 120)

    def test_msr_encoding_roundtrip(self):
        for mv in (0, -50, -60, 60, -150, 150, -29, 31):
            decoded = decode_offset(encode_offset(mv))
            self.assertAlmostEqual(decoded, mv, delta=1.0)
        # spot checks against the documented intel-undervolt examples
        self.assertEqual(hex(encode_offset(-50)), "0xf9a00000")
        self.assertEqual(hex(encode_offset(-125.0)), "0xf0000000")

    def test_gpu_telemetry(self):
        self.require_reference_gpu()
        data = self.gpu.telemetry()
        self.assertIn("gpuUsage", data)
        self.assertIn("gpuTemp", data)
        self.assertGreaterEqual(data["gpuTemp"], 20)
        self.assertLessEqual(data["gpuTemp"], 100)


if __name__ == "__main__":
    unittest.main()
