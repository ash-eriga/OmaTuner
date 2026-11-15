"""Portability fixes in the NVML/NvAPI backends.

Every test here uses a fake NVML library or a fake NvApi object; none opens
libnvidia-ml, libnvidia-api or writes to a GPU.

Run from the project root: python3 -m unittest tests.test_portability_gpu -v
"""

from __future__ import annotations

import ctypes
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "daemon"))

from omatuner import nvml_defs
from omatuner.backends import gpu_nvml
from omatuner.backends.gpu_nvml import GpuNvmlBackend, Nvml
from omatuner.backends.nvapi import CurvePointReading, CurveReading

MULTI_GPU = (
    "more than one NVIDIA GPU is present; the V/F curve cannot be "
    "matched to the tuned GPU (GPU 0)"
)
UNKNOWN_CLOCK = (
    "the driver did not report the GPU clock range, so the curve scale is "
    "unknown; the curve is read-only"
)


class FakeNvml:
    """Just enough NVML for GpuNvmlBackend.probe()."""

    def __init__(self, lib, device_count: int | None = 1):
        self.lib = lib
        self.device = ctypes.c_void_p(1)
        self.name = "GTX 1060 6GB"
        self.device_count = device_count

    def has(self, fn: str) -> bool:
        return hasattr(self.lib, fn)


class FakeNvApi:
    """Read/write-once stand-in for the point-curve calls."""

    def __init__(self, physical_gpu_count: int | None = 1, max_frequency: float = 1911.0):
        self.physical_gpu_count = physical_gpu_count
        self.scale = 1.0
        self.wrote = False
        self.points = [
            CurvePointReading(
                voltage_mv=800.0,
                frequency_mhz=max_frequency,
                base_frequency_mhz=max_frequency,
                base_voltage_mv=800.0,
                offset_mhz=0.0,
            )
        ]

    def read_curve(self) -> CurveReading:
        return CurveReading(points=[CurvePointReading(**vars(p)) for p in self.points])

    def probe_write(self) -> None:
        self.wrote = True

    def apply_curve(self, points, min_offset_mhz, max_offset_mhz):
        raise AssertionError("the test must not write a curve")

    def voltage_volts(self):
        return None

    def close(self) -> None:
        pass


def make_lib(*, set_rc: int = 0, max_clock: int = 1911) -> object:
    """A plain object with the NVML symbols probe() needs."""

    class Lib:
        pass

    lib = Lib()

    def max_clock_info(device, kind, out):
        out._obj.value = max_clock if kind == nvml_defs.NVML_CLOCK_SM else 4004
        return 0

    def get_offset(device, out):
        out._obj.value = 0
        return 0

    def set_offset(device, value):
        return set_rc

    lib.nvmlDeviceGetMaxClockInfo = max_clock_info
    lib.nvmlDeviceGetGpcClkVfOffset = get_offset
    lib.nvmlDeviceSetGpcClkVfOffset = set_offset
    lib.nvmlDeviceGetMemClkVfOffset = get_offset
    lib.nvmlDeviceSetMemClkVfOffset = set_offset
    return lib


class ClockCauseTests(unittest.TestCase):
    """F5: a missing clock feature is not a permission problem."""

    def probe(self, set_rc: int) -> GpuNvmlBackend:
        backend = GpuNvmlBackend(nvml=FakeNvml(make_lib(set_rc=set_rc)))
        backend.probe()
        return backend

    def test_not_supported_names_the_gpu_not_the_service(self):
        backend = self.probe(nvml_defs.NVML_ERROR_NOT_SUPPORTED)
        self.assertEqual(
            backend.write_cause, "this GPU or driver does not support clock offsets"
        )
        self.assertNotIn("system service", backend.write_cause)

    def test_no_permission_keeps_the_service_cause(self):
        backend = self.probe(nvml_defs.NVML_ERROR_NO_PERMISSION)
        self.assertIn("OmaTuner system service", backend.write_cause)


class FactoryTemperatureTests(unittest.TestCase):
    """F10: the reset value follows a driver window that excludes 83 °C."""

    def test_factory_value_is_clamped_into_the_driver_window(self):
        thresholds = {
            nvml_defs.NVML_TEMP_ACOUSTIC_MIN: 60,
            nvml_defs.NVML_TEMP_ACOUSTIC_CURR: 83,
            nvml_defs.NVML_TEMP_ACOUSTIC_MAX: 80,
        }

        def get_temp(device, kind, out):
            if kind not in thresholds:
                return nvml_defs.NVML_ERROR_NOT_SUPPORTED
            out._obj.value = thresholds[kind]
            return 0

        def set_temp(device, kind, value):
            thresholds[kind] = ctypes.cast(
                value, ctypes.POINTER(ctypes.c_int)
            ).contents.value
            return 0

        lib = make_lib()
        lib.nvmlDeviceGetTemperatureThreshold = get_temp
        lib.nvmlDeviceSetTemperatureThreshold = set_temp

        class Helper:
            def set_temperature(self, celsius):
                thresholds[nvml_defs.NVML_TEMP_ACOUSTIC_CURR] = int(celsius)
                return int(celsius)

        backend = GpuNvmlBackend(nvml=FakeNvml(lib))
        backend.helper = Helper()
        with mock.patch.object(gpu_nvml.os, "geteuid", return_value=1000):
            backend.probe()
        controls = {control.key: control for control in backend.controls()}
        self.assertEqual((controls["temp"].minimum, controls["temp"].maximum), (60, 80))
        self.assertEqual(controls["temp"].factory, 80)


class FanHandbackTests(unittest.TestCase):
    """F1: the probe always hands the fan back, once more if it fails."""

    class Lib:
        def __init__(self, handback):
            self.calls: list[tuple] = []
            self.handback = handback
            self.attempts = 0
            self.nvmlDeviceGetFanSpeed_v2 = self.get_speed
            self.nvmlDeviceSetFanSpeed_v2 = self.set_speed
            self.nvmlDeviceSetDefaultFanSpeed_v2 = self.set_default

        def get_speed(self, device, fan, out):
            out._obj.value = 40
            return 0

        def set_speed(self, device, fan, speed):
            self.calls.append(("set", speed))
            return 0

        def set_default(self, device, fan):
            self.calls.append(("default",))
            attempt = self.attempts
            self.attempts += 1
            return self.handback(attempt)

    def probe(self, lib) -> GpuNvmlBackend:
        backend = GpuNvmlBackend(nvml=FakeNvml(lib))
        backend.present = True
        backend.fan_supported = True
        backend.fan_count = 1
        with mock.patch.object(gpu_nvml.os, "geteuid", return_value=0):
            backend._probe_fan_write()
        return backend

    def test_a_handback_that_keeps_failing_is_retried_once(self):
        lib = self.Lib(lambda attempt: 5)
        backend = self.probe(lib)
        self.assertFalse(backend.fan_writable)
        self.assertEqual(lib.calls, [("set", 40), ("default",), ("default",)])
        self.assertIn("fixed duty", backend.fan_write_cause)
        self.assertIn("NVML error 5", backend.fan_write_cause)

    def test_a_handback_that_raises_once_recovers(self):
        def handback(attempt):
            if attempt == 0:
                raise RuntimeError("driver busy")
            return 0

        lib = self.Lib(handback)
        backend = self.probe(lib)
        self.assertTrue(backend.fan_writable)
        self.assertTrue(backend.fan_direct)
        self.assertIsNone(backend.fan_write_cause)
        self.assertEqual(lib.calls, [("set", 40), ("default",), ("default",)])


class SingleGpuCurveTests(unittest.TestCase):
    """F2: the curve is only exposed when both enumerations report one GPU."""

    def test_two_nvml_devices_make_the_curve_unsupported(self):
        backend = GpuNvmlBackend(
            nvml=FakeNvml(make_lib(), device_count=2), nvapi=FakeNvApi()
        )
        backend.probe()
        curve = backend.curve()
        self.assertFalse(curve.supported)
        self.assertFalse(curve.writable)
        self.assertEqual(curve.cause, MULTI_GPU)
        controls = {control.key: control for control in backend.controls()}
        self.assertTrue(controls["core"].available, controls["core"].cause)

    def test_one_nvml_and_one_nvapi_gpu_keep_the_curve(self):
        backend = GpuNvmlBackend(
            nvml=FakeNvml(make_lib(), device_count=1),
            nvapi=FakeNvApi(physical_gpu_count=1),
        )
        backend.probe()
        curve = backend.curve()
        self.assertTrue(curve.supported, curve.cause)
        self.assertTrue(curve.writable, curve.cause)

    def test_two_nvapi_gpus_make_the_curve_unsupported(self):
        backend = GpuNvmlBackend(
            nvml=FakeNvml(make_lib(), device_count=1),
            nvapi=FakeNvApi(physical_gpu_count=2),
        )
        backend.probe()
        self.assertFalse(backend.curve().supported)
        self.assertEqual(backend.curve_cause, MULTI_GPU)

    def test_a_missing_count_symbol_makes_the_curve_unsupported(self):
        class Fn:
            """A ctypes-like callable that accepts an assigned signature."""

            def __init__(self, impl):
                self.impl = impl
                self.argtypes = None
                self.restype = None

            def __call__(self, *args):
                return self.impl(*args)

        def get_handle(index, out):
            out._obj.value = 1
            return 0

        class Lib:
            nvmlInit_v2 = Fn(lambda: 0)
            nvmlShutdown = Fn(lambda: 0)
            nvmlDeviceGetHandleByIndex_v2 = Fn(get_handle)
            nvmlDeviceGetName = Fn(lambda device, buf, length: 0)

        with mock.patch.object(
            nvml_defs, "load_library", return_value=(Lib(), None)
        ):
            nvml = Nvml()
        self.assertIsNone(nvml.device_count)
        backend = GpuNvmlBackend(nvml=nvml, nvapi=FakeNvApi())
        backend.probe()
        curve = backend.curve()
        self.assertFalse(curve.supported)
        self.assertIn("did not report how many GPUs", curve.cause)


class UnknownClockTests(unittest.TestCase):
    """F18: an unknown SM clock makes the curve readable but read-only."""

    def test_unknown_clock_max_refuses_writes_with_a_cause(self):
        backend = GpuNvmlBackend(
            nvml=FakeNvml(make_lib(max_clock=0)), nvapi=FakeNvApi()
        )
        backend.probe()
        curve = backend.curve()
        self.assertTrue(curve.supported, curve.cause)
        self.assertFalse(curve.writable)
        self.assertEqual(curve.cause, UNKNOWN_CLOCK)


if __name__ == "__main__":
    unittest.main()
