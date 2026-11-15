"""Limits, modes, MSR encoding and NVML clamping.

The daemon enforces the range table server-side; the UI only mirrors it.
These tests pin the two mode tables, the MSR 0x150 encoding at its sign
boundary, the NVML driver-reported limit intersection, the getter-without-
setter temperature path, and the applied-state merge that used to drop
controls from a partial apply.

Run from the project root: python3 -m unittest tests.test_limits -v
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "daemon"))

from omatuner import config, fanhelper
from omatuner.backends import gpu_nvml
from omatuner.backends.base import BackendError
from omatuner.backends.cpu_msr import (
    PLANE_CACHE,
    PLANE_CORE,
    CpuMsrBackend,
    DATA_MASK,
    decode_offset,
    encode_offset,
)
from omatuner.backends.fake import FakeBackend
from omatuner.backends.gpu_nvml import NVML_CLOCK_MEM, NVML_CLOCK_SM, GpuNvmlBackend
from omatuner.backends.nvapi import CurvePointReading, CurveReading, NvApiError
from omatuner.core import CoreError, TunerCore
from omatuner.statestore import Journal, now_ms, write_json

try:
    from tests.support import make_core
except ImportError:  # discover -s tests puts the tests directory on sys.path
    from support import make_core


class RangeTableTests(unittest.TestCase):
    def test_normal_ranges_are_wider_than_the_old_table(self):
        self.assertEqual(config.range_for("cpu", "core", "normal"), (-250, 100))
        self.assertEqual(config.range_for("gpu", "core", "normal"), (-300, 300))
        self.assertEqual(config.range_for("gpu", "memory", "normal"), (-1000, 1500))
        self.assertEqual(config.range_for("gpu", "temp", "normal"), (50, 90))

    def test_expert_ranges_are_the_widest(self):
        self.assertEqual(config.range_for("cpu", "core", "expert"), (-500, 200))
        self.assertEqual(config.range_for("gpu", "core", "expert"), (-600, 600))
        self.assertEqual(config.range_for("gpu", "memory", "expert"), (-2000, 3000))
        self.assertEqual(config.range_for("gpu", "temp", "expert"), (45, 95))

    def test_intersect_never_widens(self):
        self.assertEqual(config.intersect((-300, 300), (-100, 400)), (-100, 300))
        self.assertEqual(config.intersect((-300, 300), None), (-300, 300))
        self.assertEqual(config.intersect((-300, 300), (500, 600)), (-300, 300))


class MsrEncodingTests(unittest.TestCase):
    def test_roundtrip_including_the_sign_boundary(self):
        for mv in (0, -50, -60, 60, -250, 100, -500, 200, 999, -1000):
            self.assertAlmostEqual(decode_offset(encode_offset(mv)), mv, delta=1.0)

    def test_minus_one_thousand_is_not_reported_as_plus(self):
        # raw 1024 in an 11-bit two's-complement field is -1024 LSB, i.e.
        # -1000 mV. The old decoder treated 1024 as positive.
        self.assertEqual(decode_offset(encode_offset(-1000)), -1000.0)
        raw = (encode_offset(-1000) & DATA_MASK) >> 21
        self.assertEqual(raw, 1024)

    def test_planes_are_core_0_and_cache_2(self):
        self.assertEqual((PLANE_CORE, PLANE_CACHE), (0, 2))
        backend = CpuMsrBackend({"intel": True})
        writes = []
        backend._write_plane = lambda plane, mv: writes.append((plane, mv))
        backend.apply({"core": -50, "cache": -40})
        self.assertEqual(writes, [(0, -50.0), (2, -40.0)])

    def test_an_offset_that_would_wrap_is_refused(self):
        # +1000 mV rounds to raw 1024, which the 11-bit field reads as -1024
        # LSB (-1000 mV), so the encoder must refuse it instead of wrapping.
        with self.assertRaises(ValueError):
            encode_offset(1000)
        self.assertAlmostEqual(decode_offset(encode_offset(-999)), -999, delta=1.0)
        self.assertAlmostEqual(decode_offset(encode_offset(999)), 999, delta=1.0)

    def test_an_out_of_range_offset_is_a_backend_error(self):
        backend = CpuMsrBackend({"intel": True})
        with self.assertRaises(BackendError):
            backend._write_plane(PLANE_CORE, 1000)

    def test_control_ranges_follow_the_mode(self):
        backend = CpuMsrBackend({"intel": True})
        backend.tuning_mode = "normal"
        normal = {c.key: c for c in backend.controls()}
        self.assertEqual((normal["core"].minimum, normal["core"].maximum), (-250, 100))
        backend.tuning_mode = "expert"
        expert = {c.key: c for c in backend.controls()}
        self.assertEqual((expert["core"].minimum, expert["core"].maximum), (-500, 200))


class FakeNvml:
    """Just enough of the NVML surface for GpuNvmlBackend.probe()."""

    def __init__(self, lib):
        self.lib = lib
        self.device = ctypes.c_void_p(1)
        self.name = "GTX 1060 6GB"

    def has(self, fn: str) -> bool:
        return hasattr(self.lib, fn)


class HelperTempLink:
    """Stands in for the root helper, which owns the real temperature write."""

    def __init__(self, thresholds):
        self.thresholds = thresholds
        self.calls: list[int] = []
        self.refuse = False

    def set_temperature(self, celsius):
        self.calls.append(int(celsius))
        if self.refuse:
            raise fanhelper.FanDeviceError(
                "the NVIDIA driver refused the temperature write: it requires uid 0"
            )
        self.thresholds[5] = int(celsius)
        return int(celsius)


class NvmlLimitTests(unittest.TestCase):
    def setUp(self):
        class Lib:
            pass

        lib = Lib()

        def max_clock(device, kind, out):
            out._obj.value = 1911 if kind == NVML_CLOCK_SM else 4004
            return 0

        def get_offset(device, out):
            out._obj.value = 0
            return 0

        def set_offset(device, value):
            return 0

        thresholds = {0: 102, 1: 99, 4: 60, 5: 83, 6: 92}

        def get_temp(device, sensor, out):
            if sensor not in thresholds:
                return 3
            out._obj.value = thresholds[sensor]
            return 0

        def set_temp(device, sensor, temp):
            # The NVML ABI takes `int *temp`; reading it as a scalar here
            # would hide a by-value call that segfaults the real library.
            thresholds[sensor] = ctypes.cast(temp, ctypes.POINTER(ctypes.c_int)).contents.value
            return 0

        def core_minmax(device, low, high):
            low._obj.value = -100
            high._obj.value = 400
            return 0

        def mem_minmax(device, low, high):
            low._obj.value = -200
            high._obj.value = 500
            return 0

        lib.nvmlDeviceGetMaxClockInfo = max_clock
        lib.nvmlDeviceGetGpcClkVfOffset = get_offset
        lib.nvmlDeviceSetGpcClkVfOffset = set_offset
        lib.nvmlDeviceGetMemClkVfOffset = get_offset
        lib.nvmlDeviceSetMemClkVfOffset = set_offset
        # Driver 580 exports the threshold API, not the older limit symbols.
        lib.nvmlDeviceGetTemperatureThreshold = get_temp
        lib.nvmlDeviceSetTemperatureThreshold = set_temp
        lib.nvmlDeviceGetGpcClkMinMaxVfOffset = core_minmax
        lib.nvmlDeviceGetMemClkMinMaxVfOffset = mem_minmax
        self.thresholds = thresholds
        self.helper = HelperTempLink(thresholds)
        self.backend = GpuNvmlBackend(nvml=FakeNvml(lib))
        self.backend.helper = self.helper
        # The daemon's uid is not root, so the target write is delegated.
        with mock.patch.object(gpu_nvml.os, "geteuid", return_value=1000):
            self.backend.probe()

    def test_driver_reported_limits_narrow_the_normal_range(self):
        controls = {c.key: c for c in self.backend.controls()}
        self.assertEqual((controls["core"].minimum, controls["core"].maximum), (-100, 300))
        self.assertEqual((controls["memory"].minimum, controls["memory"].maximum), (-200, 500))

    def test_expert_mode_reaches_the_full_driver_limit(self):
        self.backend.tuning_mode = "expert"
        controls = {c.key: c for c in self.backend.controls()}
        self.assertEqual((controls["core"].minimum, controls["core"].maximum), (-100, 400))

    def test_expert_mode_uses_the_full_driver_window_when_it_is_wider(self):
        self.backend.core_offset_limits = (-1000, 2000)
        self.backend.mem_offset_limits = (-3000, 4000)
        self.backend.tuning_mode = "expert"
        controls = {c.key: c for c in self.backend.controls()}
        self.assertEqual((controls["core"].minimum, controls["core"].maximum), (-1000, 2000))
        self.assertEqual((controls["memory"].minimum, controls["memory"].maximum), (-3000, 4000))

    def test_threshold_api_exposes_the_real_target_range(self):
        controls = {c.key: c for c in self.backend.controls()}
        self.assertTrue(self.backend.temp_supported)
        self.assertEqual(self.backend.temp_api, "threshold")
        self.assertEqual(self.backend.temp_limit_c, 83)
        self.assertEqual(controls["temp"].minimum, 60)
        self.assertEqual(controls["temp"].maximum, 90)
        self.assertTrue(controls["temp"].available)

    def test_apply_rejects_values_beyond_the_driver_limit(self):
        with self.assertRaises(BackendError) as ctx:
            self.backend.apply({"core": 500})
        self.assertIn("driver-reported range", str(ctx.exception))
        self.backend.apply({"core": 300})  # inside the limit

    def test_apply_sets_the_threshold_and_verifies_it(self):
        self.backend.apply({"temp": 80})
        self.assertEqual(self.backend.read()["temp"], 80)

    def test_apply_rejects_a_target_outside_the_driver_range(self):
        with self.assertRaises(BackendError) as ctx:
            self.backend.apply({"temp": 95})
        self.assertIn("driver range", str(ctx.exception))

    def test_refused_threshold_write_is_not_a_writable_control(self):
        class Lib:
            pass

        lib = Lib()
        thresholds = {4: 60, 5: 83, 6: 92}
        lib.nvmlDeviceGetTemperatureThreshold = lambda device, kind, out: (
            setattr(out._obj, "value", thresholds[kind]) or 0 if kind in thresholds else 3
        )
        lib.nvmlDeviceSetTemperatureThreshold = lambda device, kind, value: 4
        lib.nvmlDeviceGetGpcClkVfOffset = lambda device, out: 0
        lib.nvmlDeviceSetGpcClkVfOffset = lambda device, value: 0
        lib.nvmlDeviceGetMemClkVfOffset = lambda device, out: 0
        lib.nvmlDeviceSetMemClkVfOffset = lambda device, value: 0
        backend = GpuNvmlBackend(nvml=FakeNvml(lib))
        helper = HelperTempLink(thresholds)
        helper.refuse = True
        backend.helper = helper
        with mock.patch.object(gpu_nvml.os, "geteuid", return_value=1000):
            backend.probe()
        controls = {c.key: c for c in backend.controls()}
        self.assertTrue(backend.temp_supported)
        self.assertIn("uid 0", backend.temp_write_cause or "")
        self.assertFalse(controls["temp"].available)

    def test_root_writes_the_target_through_the_pointer_argument(self):
        # The driver accepts the write from uid 0 and dereferences the third
        # argument: passing the int by value crashed libnvidia-ml here.
        self.helper.calls.clear()
        with mock.patch.object(gpu_nvml.os, "geteuid", return_value=0):
            self.backend.probe()
        self.assertTrue(self.backend.temp_direct)
        self.assertEqual(self.helper.calls, [], "root probes the write itself")
        self.backend.apply({"temp": 78})
        self.assertEqual(self.thresholds[5], 78)
        self.assertEqual(self.backend.read()["temp"], 78)

    def test_missing_threshold_setter_is_not_a_writable_control(self):
        class Lib:
            pass

        lib = Lib()
        lib.nvmlDeviceGetTemperatureThreshold = lambda device, kind, out: 0
        lib.nvmlDeviceGetGpcClkVfOffset = lambda device, out: 0
        lib.nvmlDeviceSetGpcClkVfOffset = lambda device, value: 0
        lib.nvmlDeviceGetMemClkVfOffset = lambda device, out: 0
        lib.nvmlDeviceSetMemClkVfOffset = lambda device, value: 0
        backend = GpuNvmlBackend(nvml=FakeNvml(lib))
        backend.probe()
        controls = {c.key: c for c in backend.controls()}
        self.assertFalse(backend.temp_supported)
        self.assertFalse(controls["temp"].available)
        self.assertIn("not writable", controls["temp"].cause or "")


class NoDriverTests(unittest.TestCase):
    def test_a_probe_without_a_driver_disables_every_control_with_a_cause(self):
        backend = GpuNvmlBackend()
        with mock.patch.object(
                gpu_nvml, "Nvml",
                side_effect=BackendError("libnvidia-ml not found: absent", cause="driver")):
            backend.probe()
        self.assertFalse(backend.present)
        controls = backend.controls()
        self.assertTrue(controls)
        for control in controls:
            self.assertFalse(control.available, control.key)
            self.assertTrue(control.cause, control.key)


class FakeNvApi:
    """The four point-curve calls, without touching the real driver."""

    def __init__(self):
        self.voltage = 0.9
        self.fail_write = False
        self.points = [
            CurvePointReading(
                voltage_mv=700 + i * 50, frequency_mhz=1000 + i * 100,
                base_frequency_mhz=1000 + i * 100, base_voltage_mv=700 + i * 50,
                offset_mhz=0,
            )
            for i in range(3)
        ]

    def read_curve(self) -> CurveReading:
        return CurveReading(points=[CurvePointReading(**vars(p)) for p in self.points])

    def probe_write(self) -> None:
        if self.fail_write:
            raise NvApiError("simulated SetControl refusal")

    def apply_curve(self, points, min_offset_mhz, max_offset_mhz):
        if self.fail_write:
            raise NvApiError("simulated SetControl refusal")
        if len(points) != len(self.points):
            raise NvApiError("point count mismatch")
        out = []
        for point, current in zip(points, self.points):
            requested = int(point["f"])
            current.frequency_mhz = requested
            out.append(current)
        return out

    def voltage_volts(self) -> float:
        return self.voltage

    def close(self) -> None:
        pass


class GpuCurveTests(unittest.TestCase):
    def make_backend(self, nvapi=None):
        class Lib:
            pass

        lib = Lib()
        lib.nvmlDeviceGetMaxClockInfo = lambda device, kind, out: 0
        lib.nvmlDeviceGetGpcClkVfOffset = lambda device, out: 0
        lib.nvmlDeviceSetGpcClkVfOffset = lambda device, value: 0
        lib.nvmlDeviceGetMemClkVfOffset = lambda device, out: 0
        lib.nvmlDeviceSetMemClkVfOffset = lambda device, value: 0
        lib.nvmlDeviceGetTemperatureThreshold = lambda device, kind, out: 3
        lib.nvmlDeviceGetGpcClkMinMaxVfOffset = lambda device, low, high: 0
        lib.nvmlDeviceGetMemClkMinMaxVfOffset = lambda device, low, high: 0
        return GpuNvmlBackend(nvml=FakeNvml(lib), nvapi=nvapi or FakeNvApi())

    def test_curve_is_read_and_written_through_nvapi(self):
        api = FakeNvApi()
        backend = self.make_backend(api)
        backend.probe()
        curve = backend.curve()
        self.assertTrue(curve.supported)
        self.assertTrue(curve.writable)
        self.assertEqual(len(curve.points), 3)
        backend.apply({"points": [
            {"v": 0.7, "f": 1100}, {"v": 0.75, "f": 1200}, {"v": 0.8, "f": 1300},
        ]})
        self.assertEqual([p.frequency_mhz for p in api.points], [1100, 1200, 1300])

    def test_curve_write_refusal_is_reported_not_swallowed(self):
        api = FakeNvApi()
        api.fail_write = True
        backend = self.make_backend(api)
        backend.probe()
        curve = backend.curve()
        self.assertTrue(curve.supported)
        self.assertFalse(curve.writable)
        self.assertTrue(curve.cause)
        with self.assertRaises(BackendError) as ctx:
            backend.apply({"points": [{"v": 0.7, "f": 1100}]})
        self.assertIn("SetControl", str(ctx.exception))

    def test_curve_is_absent_when_nvapi_cannot_open(self):
        backend = GpuNvmlBackend(nvml=FakeNvml(type("L", (), {})()))
        backend.probe()
        curve = backend.curve()
        self.assertFalse(curve.supported)
        self.assertFalse(curve.writable)
        self.assertTrue(curve.cause)


class ReadOnlyCurveBackend(FakeBackend):
    def curve(self):
        found = super().curve()
        if found is not None:
            found.writable = False
            found.cause = "curve is readable but writing requires the service"
        return found


class CurveAvailabilityTests(unittest.TestCase):
    def test_core_rejects_points_on_a_read_only_curve(self):
        tmp = tempfile.mkdtemp(prefix="omatuner-readonly-curve-")
        with open(os.path.join(tmp, "clean-shutdown"), "w") as fh:
            fh.write("clean\n")
        gpu = ReadOnlyCurveBackend("gpu")
        core, _, _ = make_core(tmp, "normal", gpu=gpu)
        curve = gpu.curve()
        self.assertTrue(curve.supported)
        self.assertFalse(curve.writable)
        points = [{"v": p.voltage, "f": p.frequency} for p in curve.points]
        with self.assertRaises(CoreError) as ctx:
            core.apply("gpu", {"core": 0, "points": points})
        self.assertEqual(ctx.exception.code, "unavailable")
        self.assertIn("requires the service", ctx.exception.message)
        self.assertEqual(gpu.apply_log, [])


class LateTempBackend(FakeBackend):
    """The root helper starts after the daemon, so temp is briefly unavailable."""

    def __init__(self):
        super().__init__("gpu")
        self.temp_ready = False
        self.checks = 0

    def controls(self):
        controls = super().controls()
        for control in controls:
            if control.key == "temp":
                control.available = self.temp_ready
                control.cause = None if self.temp_ready else "the root helper is not running"
        return controls

    def check_temp_write(self):
        self.checks += 1
        self.temp_ready = True


class LateTempWriteTests(unittest.TestCase):
    def test_a_late_helper_is_asked_before_the_change_is_refused(self):
        # The helper may start after the daemon probed; a real change must
        # re-probe instead of refusing until the next daemon restart.
        tmp = tempfile.mkdtemp(prefix="omatuner-late-temp-")
        with open(os.path.join(tmp, "clean-shutdown"), "w") as fh:
            fh.write("clean\n")
        gpu = LateTempBackend()
        core, _, _ = make_core(tmp, "normal", gpu=gpu)
        core.apply("gpu", {"temp": 80})
        self.assertEqual(gpu.checks, 1)
        self.assertEqual(gpu.temp_target_c, 80)


class CoreModeEnforcementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="omatuner-limits-")
        with open(os.path.join(self.tmp, "clean-shutdown"), "w") as fh:
            fh.write("clean\n")

    def test_normal_mode_rejects_expert_values_with_a_clear_error(self):
        core, cpu, _ = make_core(self.tmp, "normal")
        self.assertEqual(core.snapshot()["tuningMode"], "normal")
        with self.assertRaises(CoreError) as ctx:
            core.apply("cpu", {"core": -300})
        self.assertEqual(ctx.exception.code, "bad-value")
        self.assertIn("normal mode", ctx.exception.message)
        self.assertIn("expert", ctx.exception.message)
        self.assertEqual(cpu.apply_log, [])

    def test_expert_mode_accepts_the_wider_range(self):
        core, cpu, _ = make_core(self.tmp, "expert")
        core.apply("cpu", {"core": -300})
        self.assertEqual(cpu.values["core"], -300)
        state = core.snapshot()
        self.assertEqual(state["tuningMode"], "expert")
        self.assertFalse(state["expertWarned"])

    def test_expert_warning_is_persisted_once(self):
        core, _, _ = make_core(self.tmp, "expert")
        core.ack_expert()
        self.assertTrue(core.snapshot()["expertWarned"])
        # A fresh daemon over the same state reads the acknowledgement back.
        core2, _, _ = make_core(self.tmp, "expert")
        self.assertTrue(core2.snapshot()["expertWarned"])

    def test_mode_is_part_of_status_and_doctor(self):
        core, _, _ = make_core(self.tmp, "expert")
        state = core.snapshot()
        self.assertEqual(state["tuningMode"], "expert")
        self.assertIn("expertWarned", state)


class PartialWriteBackend(FakeBackend):
    """GPU with an unwritable thermal limit, like driver 580."""

    def controls(self):
        found = super().controls()
        for control in found:
            if control.key == "temp":
                control.available = False
                control.cause = "temperature limit is not writable through this driver/GPU"
        return found

    def apply(self, values):
        if "temp" in values:
            raise BackendError("the backend was asked to write the unwritable temp control")
        super().apply(values)


class PartialWriteTests(unittest.TestCase):
    def make_partial_core(self):
        from omatuner.core import TunerCore
        from omatuner.statestore import Journal

        tmp = tempfile.mkdtemp(prefix="omatuner-partial-")
        with open(os.path.join(tmp, "clean-shutdown"), "w") as fh:
            fh.write("clean\n")
        dirs = config.Dirs(system=False, state_dir=tmp, socket_path=os.path.join(tmp, "s.sock"))
        gpu = PartialWriteBackend("gpu")
        core = TunerCore(dirs, {"cpu": FakeBackend("cpu"), "gpu": gpu}, Journal(dirs.events_file))
        core.schedule_timer = lambda delay, callback: (lambda: None)
        core.boot()
        return core, gpu

    def test_revert_skips_controls_the_backend_cannot_write(self):
        core, gpu = self.make_partial_core()
        # `applied` carries the temp factory value, so a naive restore would
        # send it to a backend that refuses it and report a failed revert.
        core.apply("gpu", {"core": 50})
        result = core.revert("deadline")
        self.assertTrue(result["restored"])
        self.assertEqual(gpu.values["core"], 0)
        self.assertNotIn("temp", gpu.apply_log[-1]["values"])

    def test_unchanged_unavailable_control_is_tolerated(self):
        core, gpu = self.make_partial_core()
        # The pre-update UI sent the whole draft, including the unwritable
        # temperature limit at its current value.
        core.apply("gpu", {"core": 50, "temp": 83})
        self.assertEqual(gpu.values["core"], 50)
        self.assertNotIn("temp", gpu.apply_log[-1]["values"])
        core.confirm()
        with self.assertRaises(CoreError) as ctx:
            core.apply("gpu", {"core": 40, "temp": 70})
        self.assertEqual(ctx.exception.code, "unavailable")


class StorageFailureTests(unittest.TestCase):
    """A full disk must fail safe and must not break a needed restore."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="omatuner-storage-")
        with open(os.path.join(self.tmp, "clean-shutdown"), "w") as fh:
            fh.write("clean\n")
        self.core, self.cpu, _ = make_core(self.tmp)

    def test_apply_refuses_the_hardware_write_without_a_recovery_record(self):
        from omatuner import core as core_module
        from omatuner.statestore import write_json as real_write

        def fail(path, data):
            if path.endswith("pending.json"):
                raise OSError("disk full")
            return real_write(path, data)

        with mock.patch.object(core_module, "write_json", side_effect=fail):
            with self.assertRaises(CoreError) as ctx:
                self.core.apply("cpu", {"core": -30})
        self.assertEqual(ctx.exception.code, "state-write-failed")
        self.assertEqual(self.cpu.apply_log, [])
        self.assertIsNone(self.core.trial)

    def test_revert_survives_a_failed_reverted_marker_write(self):
        from omatuner import core as core_module
        from omatuner.statestore import write_json as real_write

        self.core.apply("cpu", {"core": -40})

        def fail(path, data):
            if path.endswith("last-reverted.json"):
                raise OSError("disk full")
            return real_write(path, data)

        with mock.patch.object(core_module, "write_json", side_effect=fail):
            result = self.core.revert("deadline")
        self.assertTrue(result["restored"])
        self.assertEqual(self.cpu.values["core"], 0)


class AppliedStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="omatuner-applied-")
        with open(os.path.join(self.tmp, "clean-shutdown"), "w") as fh:
            fh.write("clean\n")
        self.core, self.cpu, self.gpu = make_core(self.tmp)

    def test_partial_apply_keeps_the_other_applied_values(self):
        # The UI always sends the whole draft, but the CLI can send one
        # key=value pair. Replacing `applied` with the partial candidate used
        # to drop every other control to factory in the next snapshot.
        self.core.apply("cpu", {"core": -30, "cache": -20})
        self.core.confirm()
        self.core.apply("cpu", {"core": -40})
        self.core.confirm()
        applied = self.core.snapshot()["applied"]["cpu"]
        self.assertEqual(applied["core"], -40)
        self.assertEqual(applied["cache"], -20)

    def test_revert_reports_the_rolled_back_candidate(self):
        self.core.apply("cpu", {"core": -30, "cache": -20})
        self.core.confirm()
        self.core.apply("cpu", {"core": -120, "cache": -90})
        self.core.revert("deadline")
        state = self.core.snapshot()
        self.assertEqual(state["lastReverted"]["component"], "cpu")
        self.assertEqual(state["lastReverted"]["candidate"], {"core": -120, "cache": -90})
        self.assertEqual(state["lastReverted"]["reason"], "deadline")
        self.assertEqual(state["applied"]["cpu"]["core"], -30)
        # Retrying clears the marker once the new application starts.
        self.core.apply("cpu", {"core": -120, "cache": -90})
        self.assertIsNone(self.core.snapshot()["lastReverted"])

    def test_revert_failure_does_not_claim_the_old_values_are_live(self):
        self.core.apply("cpu", {"core": -40})
        self.cpu.fail_next_apply = True
        result = self.core.revert("deadline")
        self.assertFalse(result["restored"])
        state = self.core.snapshot()
        self.assertEqual(state["applied"]["cpu"]["core"], -40)
        self.assertTrue(state["notice"]["uncertain"])

    def test_curve_points_are_validated_server_side(self):
        curve = self.gpu.curve()
        points = [{"v": p.voltage, "f": p.frequency} for p in curve.points]
        bad_frequency = [dict(p) for p in points]
        bad_frequency[0]["f"] = 9000
        with self.assertRaises(CoreError) as ctx:
            self.core.apply("gpu", {"core": 0, "points": bad_frequency})
        self.assertEqual(ctx.exception.code, "bad-value")
        bad_voltage = [dict(p) for p in points]
        bad_voltage[0]["v"] = 0.1
        with self.assertRaises(CoreError) as ctx:
            self.core.apply("gpu", {"core": 0, "points": bad_voltage})
        self.assertEqual(ctx.exception.code, "bad-value")

    def test_curve_payload_rejected_when_the_driver_does_not_support_it(self):
        tmp = tempfile.mkdtemp(prefix="omatuner-nocurve-")
        with open(os.path.join(tmp, "clean-shutdown"), "w") as fh:
            fh.write("clean\n")
        gpu_backend = FakeBackend("gpu", unavailable=True, cause="Pascal exposes offsets only")
        core, _, gpu = make_core(tmp, gpu=gpu_backend)
        with self.assertRaises(CoreError) as ctx:
            core.apply("gpu", {"points": [{"v": 0.7, "f": 1140}]})
        self.assertEqual(ctx.exception.code, "unavailable")
        self.assertEqual(gpu.apply_log, [])

    def test_boot_uses_last_applied_when_the_hardware_read_fails(self):
        # A session apply leaves no saved default, so the only evidence at
        # the next boot is the last-applied record.
        self.core.apply("cpu", {"core": -30}, "session")
        self.core.confirm()

        class BrokenRead(FakeBackend):
            def read(self):
                raise BackendError("hardware went away", cause="device")

        with open(os.path.join(self.tmp, "clean-shutdown"), "w") as fh:
            fh.write("clean\n")
        core2, _, _ = make_core(self.tmp, cpu=BrokenRead("cpu"))
        self.assertEqual(core2.snapshot()["applied"]["cpu"]["core"], -30)


class MemoryClockTests(unittest.TestCase):
    """The GPU memory clock is a first-class control on the daemon side.

    The reported UI bug was a stale readout, but a daemon that dropped
    `memory` would look identical from the panel. These tests pin the backend
    write, the recovery record, confirmation, the typed round trip and the
    factory reset that the restore line and the Factory button trigger.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="omatuner-memory-")
        with open(os.path.join(self.tmp, "clean-shutdown"), "w") as fh:
            fh.write("clean\n")
        self.core, self.cpu, self.gpu = make_core(self.tmp)

    def read(self, path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    def test_session_apply_writes_memory_and_the_recovery_record(self):
        self.core.apply("gpu", {"memory": 500})
        self.assertEqual(self.gpu.values["memory"], 500)
        self.assertEqual(self.gpu.apply_log[-1]["values"], {"memory": 500})
        pending = self.read(self.core.dirs.pending_file)
        self.assertEqual(pending["component"], "gpu")
        self.assertEqual(pending["candidate"]["memory"], 500)
        # The candidate carries the untouched fan policy too: confirming or
        # saving this partial apply must not drop it from the snapshot.
        self.assertEqual(pending["candidate"]["fan"], {"mode": "auto"})
        self.assertEqual(self.core.snapshot()["applied"]["gpu"]["memory"], 500)
        self.assertFalse(os.path.exists(self.core.dirs.defaults_file))

    def test_confirm_records_the_typed_value_without_a_saved_default(self):
        self.core.apply("gpu", {"memory": 500})
        self.core.confirm()
        self.assertFalse(os.path.exists(self.core.dirs.pending_file))
        self.assertFalse(os.path.exists(self.core.dirs.defaults_file))
        last = self.read(self.core.dirs.last_applied_file)
        self.assertEqual(last["gpu"]["settings"]["memory"], 500)
        self.assertEqual(self.core.snapshot()["applied"]["gpu"]["memory"], 500)

    def test_default_mode_writes_the_defaults_file(self):
        self.core.apply("gpu", {"memory": 400}, "default")
        self.core.confirm()
        defaults = self.read(self.core.dirs.defaults_file)
        self.assertEqual(defaults["gpu"]["memory"], 400)
        self.assertEqual(self.core.snapshot()["defaults"]["gpu"], True)

    def test_typed_round_trip_back_to_zero(self):
        self.core.apply("gpu", {"memory": 500})
        self.core.confirm()
        self.core.apply("gpu", {"memory": 0})
        self.core.confirm()
        self.assertEqual(self.gpu.values["memory"], 0)
        self.assertEqual(self.core.snapshot()["applied"]["gpu"]["memory"], 0)
        self.assertFalse(os.path.exists(self.core.dirs.pending_file))

    def test_factory_reset_restores_memory_to_zero(self):
        self.core.apply("gpu", {"memory": 500})
        self.core.confirm()
        result = self.core.apply("gpu", {}, "reset")
        self.assertEqual(result["applied"]["memory"], 0)
        self.assertEqual(self.gpu.values["memory"], 0)
        self.assertEqual(self.core.snapshot()["applied"]["gpu"]["memory"], 0)
        self.assertFalse(os.path.exists(self.core.dirs.pending_file))

    def test_out_of_range_value_never_reaches_the_backend(self):
        with self.assertRaises(CoreError) as ctx:
            self.core.apply("gpu", {"memory": 5000})
        self.assertEqual(ctx.exception.code, "bad-value")
        self.assertEqual(self.gpu.apply_log, [])


if __name__ == "__main__":
    unittest.main()
