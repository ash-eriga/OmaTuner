"""V/F curve frequency scale: detection, write conversion, read-back, migration.

Driver 580.196.07 on the reference GTX 1060 reports the per-point V/F
frequencies at twice the real SM clock (top point 3796 MHz against a 1911 MHz
NVML max SM clock). These tests use the real 80-point table captured in
tests/fixtures/gtx1060-curve.json and fake NvAPI structs; nothing here opens
libnvidia-api or writes to the GPU.

Run from the project root: python3 -m unittest tests.test_curve_scale -v
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "daemon"))

from omatuner import config
from omatuner.backends.base import BackendError, Curve, CurvePoint
from omatuner.backends.fake import FakeBackend
from omatuner.backends.gpu_nvml import GpuNvmlBackend
from omatuner.backends.nvapi import (
    _POINT_TYPE_PROG,
    CurveReading,
    NvApi,
    NvApiError,
    _PointsControlV1,
    _PointsInfoV1,
    _PointsStatusV3,
    detect_curve_scale,
)
from omatuner.core import TunerCore, migrate_curve_settings
from omatuner.statestore import Journal, write_json

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(HERE, "fixtures", "gtx1060-curve.json")

RAW_SCALE = 2.0
MAX_CLOCK_MHZ = 1911


def load_fixture() -> dict:
    with open(FIXTURE, encoding="utf-8") as fh:
        return json.load(fh)


class FakeNvml:
    """Just enough NVML for GpuNvmlBackend.probe()."""

    def __init__(self, max_clock: int = MAX_CLOCK_MHZ, lib=None):
        class Lib:
            pass

        self.lib = lib or Lib()
        self.device = ctypes.c_void_p(1)
        self.name = "GTX 1060 6GB"
        self.max_clock = max_clock
        if lib is None:
            self.lib.nvmlDeviceGetMaxClockInfo = self._max_clock
            self.lib.nvmlDeviceGetGpcClkVfOffset = lambda device, out: 0
            self.lib.nvmlDeviceSetGpcClkVfOffset = lambda device, value: 0
            self.lib.nvmlDeviceGetMemClkVfOffset = lambda device, out: 0
            self.lib.nvmlDeviceSetMemClkVfOffset = lambda device, value: 0
            self.lib.nvmlDeviceGetTemperatureThreshold = lambda device, kind, out: 3
            self.lib.nvmlDeviceGetGpcClkMinMaxVfOffset = lambda device, low, high: 0
            self.lib.nvmlDeviceGetMemClkMinMaxVfOffset = lambda device, low, high: 0

    def _max_clock(self, device, kind, out):
        out._obj.value = self.max_clock
        return 0

    def has(self, fn: str) -> bool:
        return hasattr(self.lib, fn)


class FakeCurve:
    """The NvAPI point calls over real ctypes structs, without the driver.

    ``base_khz`` are raw driver frequencies, ``voltages_uv`` the fixed point
    voltages. A write records the posted per-point offsets, which a second
    read reports back exactly as the driver would.
    """

    def __init__(self, base_khz: dict[int, int], voltages_uv: dict[int, int],
                 scale: float, ignore_write: bool = False, round_raw_khz: int = 0):
        self.base = dict(base_khz)
        self.voltages = dict(voltages_uv)
        self.offsets = {i: 0 for i in self.base}
        self.ignore_write = ignore_write
        self.round_raw_khz = round_raw_khz
        self.posted = {}
        self.info = _PointsInfoV1()
        for i in self.base:
            self.info.vf_points[i].type_ = _POINT_TYPE_PROG
            self.info.vf_points[i].b_voltage_based = 1
        self.api = NvApi()
        self.api.lib = object()
        self.api.handle = ctypes.c_void_p(1)
        self.api.scale = scale
        self.api._read_raw = self.read_raw
        self.api._query = self.query

    def read_raw(self):
        status = _PointsStatusV3()
        status.b_vf_tuple_base_supported = 1
        control = _PointsControlV1()
        for i, base in self.base.items():
            offset = self.offsets.get(i, 0)
            status.vf_points[i].type_ = _POINT_TYPE_PROG
            status.vf_points[i].freq_khz = base + offset
            status.vf_points[i].voltage_uv = self.voltages[i]
            status.vf_points[i].vf_tuple_base.freq_khz = base
            status.vf_points[i].vf_tuple_base.voltage_uv = self.voltages[i]
            control.vf_points[i].type_ = _POINT_TYPE_PROG
            control.vf_points[i].data.prog.freq_offset_khz = offset
        return self.info, status, control

    def query(self, ident):
        return self.set_control

    def set_control(self, handle, control_ptr):
        if self.ignore_write:
            return 0
        posted = control_ptr.contents
        for i in self.offsets:
            value = posted.vf_points[i].data.prog.freq_offset_khz
            if self.round_raw_khz:
                value += self.round_raw_khz
            self.offsets[i] = value
            self.posted[i] = value
        return 0


def fixture_api(scale: float = RAW_SCALE, ignore_write: bool = False,
                round_raw_khz: int = 0) -> FakeCurve:
    """Three points of the real table: 139, ~954 and 1898 real MHz.

    The structs always hold the driver's raw (doubled) frequencies; ``scale``
    is what the NvApi instance divides them by.
    """
    real = [(450000, 139), (900000, 954), (1243750, 1898)]
    base = {i: int(round(f * RAW_SCALE * 1000)) for i, (_, f) in enumerate(real)}
    volts = {i: v for i, (v, _) in enumerate(real)}
    return FakeCurve(base, volts, scale, ignore_write=ignore_write,
                     round_raw_khz=round_raw_khz)


class DetectionTests(unittest.TestCase):
    def test_fixture_is_the_real_doubled_table(self) -> None:
        curve = load_fixture()
        self.assertEqual(len(curve["points"]), 80)
        self.assertEqual(len(curve["factoryPoints"]), 80)
        self.assertEqual(max(p["f"] for p in curve["factoryPoints"]), 3796.0)
        self.assertEqual(min(p["f"] for p in curve["factoryPoints"]), 278.0)

    def test_double_scale_matches_nvml_max_clock(self) -> None:
        curve = load_fixture()
        stock = max(p["f"] for p in curve["factoryPoints"])
        self.assertEqual(detect_curve_scale(stock, MAX_CLOCK_MHZ), (2.0, None))
        self.assertAlmostEqual(stock / MAX_CLOCK_MHZ, 1.986, places=3)

    def test_unity_scale(self) -> None:
        self.assertEqual(detect_curve_scale(1911.0, 1911), (1.0, None))
        self.assertEqual(detect_curve_scale(1900.0, 1911), (1.0, None))

    def test_unknown_ratio_is_refused_with_a_cause(self) -> None:
        scale, cause = detect_curve_scale(2500.0, 1911)
        self.assertIsNone(scale)
        self.assertIn("unknown", cause)
        self.assertIn("refused", cause)

    def test_missing_clock_cannot_scale_anything(self) -> None:
        self.assertEqual(detect_curve_scale(3796.0, 0), (1.0, None))
        self.assertEqual(detect_curve_scale(0, 1911), (1.0, None))


class ReadTests(unittest.TestCase):
    def test_scale_two_reports_real_frequencies(self) -> None:
        fake = fixture_api(scale=2.0)
        reading = fake.api.read_curve()
        self.assertEqual([round(p.frequency_mhz) for p in reading.points], [139, 954, 1898])
        self.assertEqual([round(p.base_frequency_mhz) for p in reading.points], [139, 954, 1898])
        self.assertEqual([p.offset_mhz for p in reading.points], [0.0, 0.0, 0.0])

    def test_scale_one_is_untouched(self) -> None:
        fake = fixture_api(scale=1.0)
        reading = fake.api.read_curve()
        self.assertEqual([round(p.frequency_mhz) for p in reading.points], [278, 1908, 3796])

    def test_read_uses_base_plus_point_offset_not_global_status(self) -> None:
        fake = fixture_api(scale=2.0)
        # A global NVML offset already shifts the status frequency; the
        # per-point control is what this editor owns, so it must not be
        # folded into the curve and applied twice.
        fake.offsets[1] = 200000  # +100 real MHz per point
        reading = fake.api.read_curve()
        self.assertEqual(round(reading.points[1].frequency_mhz), 1054)
        self.assertAlmostEqual(reading.points[1].offset_mhz, 100.0)


class WriteTests(unittest.TestCase):
    def test_scale_two_multiplies_the_request(self) -> None:
        fake = fixture_api(scale=2.0)
        actual = fake.api.apply_curve(
            [{"v": 0.45, "f": 200}, {"v": 0.9, "f": 1100}, {"v": 1.24375, "f": 2100}],
            -300, 300,
        )
        self.assertEqual(fake.offsets, {0: 122000, 1: 292000, 2: 404000})
        self.assertEqual([round(p.frequency_mhz) for p in actual], [200, 1100, 2100])
        self.assertEqual([round(p.offset_mhz) for p in actual], [61, 146, 202])
        self.assertEqual([round(p.base_frequency_mhz) for p in actual], [139, 954, 1898])

    def test_scale_one_does_not_multiply(self) -> None:
        fake = fixture_api(scale=1.0)
        fake.api.apply_curve(
            [{"v": 0.45, "f": 278}, {"v": 0.9, "f": 2008}, {"v": 1.24375, "f": 3796}],
            -300, 300,
        )
        self.assertEqual(fake.offsets, {0: 0, 1: 100000, 2: 0})

    def test_offset_limits_are_real_mhz(self) -> None:
        fake = fixture_api(scale=2.0)
        # +290 real MHz is 580000 raw kHz; validated in raw units it would
        # look like a 580 MHz offset and be rejected.
        fake.api.apply_curve(
            [{"v": 0.45, "f": 139}, {"v": 0.9, "f": 954}, {"v": 1.24375, "f": 2188}],
            -300, 300,
        )
        self.assertEqual(fake.offsets[2], 580000)
        with self.assertRaises(NvApiError) as ctx:
            fake.api.apply_curve(
                [{"v": 0.45, "f": 139}, {"v": 0.9, "f": 954}, {"v": 1.24375, "f": 2199}],
                -300, 300,
            )
        self.assertIn("outside", str(ctx.exception))

    def test_read_back_verifies_the_applied_offset(self) -> None:
        fake = fixture_api(scale=2.0, ignore_write=True)
        with self.assertRaises(NvApiError) as ctx:
            fake.api.apply_curve(
                [{"v": 0.45, "f": 139}, {"v": 0.9, "f": 1054}, {"v": 1.24375, "f": 1898}],
                -300, 300,
            )
        self.assertIn("did not apply", str(ctx.exception))

    def test_read_back_tolerance_is_two_real_mhz(self) -> None:
        # The driver rounds the offset by 2000 raw kHz = 1 real MHz.
        fake = fixture_api(scale=2.0, round_raw_khz=2000)
        actual = fake.api.apply_curve(
            [{"v": 0.45, "f": 139}, {"v": 0.9, "f": 1054}, {"v": 1.24375, "f": 1898}],
            -300, 300,
        )
        self.assertEqual(round(actual[1].frequency_mhz), 1055)


class BackendDetectionTests(unittest.TestCase):
    def backend(self, max_clock: int) -> tuple[GpuNvmlBackend, FakeCurve]:
        fake = fixture_api(scale=1.0)  # raw: 278 / 1908 / 3796 MHz
        fake.api.scale = 1.0
        backend = GpuNvmlBackend(nvml=FakeNvml(max_clock), nvapi=fake.api)
        backend.probe()
        return backend, fake

    def test_probe_detects_two_and_exposes_real_curve(self) -> None:
        backend, _ = self.backend(MAX_CLOCK_MHZ)
        curve = backend.curve()
        self.assertEqual(curve.scale, 2.0)
        self.assertTrue(curve.supported, curve.cause)
        self.assertTrue(curve.writable, curve.cause)
        self.assertEqual([round(p.frequency) for p in curve.factory_points], [139, 954, 1898])
        self.assertEqual(curve.min_frequency, max(1, 139 - 300))
        self.assertEqual(curve.max_frequency, 1898 + 300)
        self.assertEqual(curve.descriptor()["scale"], 2.0)

    def test_probe_refuses_an_unknown_ratio(self) -> None:
        backend, _ = self.backend(1000)
        curve = backend.curve()
        self.assertEqual(curve.scale, 1.0)
        self.assertTrue(curve.supported)
        self.assertFalse(curve.writable)
        self.assertIn("unknown", curve.cause)
        with self.assertRaises(Exception) as ctx:
            backend.apply({"points": [{"v": 0.45, "f": 139}]})
        self.assertIn("unknown", str(ctx.exception))

    def test_write_through_the_backend_scales_the_request(self) -> None:
        backend, fake = self.backend(MAX_CLOCK_MHZ)
        backend.apply({"points": [
            {"v": 0.45, "f": 139}, {"v": 0.9, "f": 1054}, {"v": 1.24375, "f": 1898},
        ]})
        self.assertEqual(fake.offsets[1], 200000)


class ScaledGpu(FakeBackend):
    """A GPU whose driver table is reported at 2x, like the real machine."""

    def __init__(self) -> None:
        super().__init__("gpu")
        raw = load_fixture()["points"]
        self.scaled_points = [CurvePoint(p["v"], p["f"] / RAW_SCALE) for p in raw]
        self.apply_calls: list[dict] = []

    def curve(self) -> Curve:
        return Curve(
            supported=True,
            writable=True,
            cause=None,
            points=[CurvePoint(p.voltage, p.frequency) for p in self.scaled_points],
            factory_points=[CurvePoint(p.voltage, p.frequency) for p in self.scaled_points],
            min_frequency=1,
            max_frequency=1898 + 300,
            scale=2.0,
        )

    def apply(self, values):
        self.apply_calls.append(dict(values))
        super().apply(values)


def doubled_points() -> list[dict]:
    return [{"v": p["v"], "f": p["f"]} for p in load_fixture()["points"]]


class MigrationTests(unittest.TestCase):
    def curve(self) -> Curve:
        return ScaledGpu().curve()

    def test_raw_record_is_halved_and_marked(self) -> None:
        migrated, changed = migrate_curve_settings({"core": 0, "points": doubled_points()}, self.curve())
        self.assertTrue(changed)
        self.assertEqual(migrated["curveScale"], 2.0)
        self.assertEqual(round(migrated["points"][0]["f"]), 139)
        self.assertEqual(round(migrated["points"][-1]["f"]), 1898)
        self.assertTrue(all(p["f"] <= 2198 + 1 for p in migrated["points"]))

    def test_raw_record_with_marker_one_is_halved(self) -> None:
        migrated, changed = migrate_curve_settings(
            {"points": doubled_points(), "curveScale": 1}, self.curve()
        )
        self.assertTrue(changed)
        self.assertEqual(round(migrated["points"][0]["f"]), 139)

    def test_current_scale_record_is_untouched(self) -> None:
        real = [{"v": p["v"], "f": p["f"] / RAW_SCALE} for p in load_fixture()["points"]]
        migrated, changed = migrate_curve_settings(
            {"points": real, "curveScale": 2}, self.curve()
        )
        self.assertFalse(changed)
        self.assertEqual(migrated["points"][0]["f"], 139)

    def test_unconvertible_record_drops_points_but_keeps_controls(self) -> None:
        huge = [{"v": 0.45, "f": 9000}, {"v": 1.2, "f": 12000}]
        migrated, changed = migrate_curve_settings(
            {"core": 42, "points": huge}, self.curve()
        )
        self.assertTrue(changed)
        self.assertEqual(migrated["core"], 42)
        self.assertNotIn("points", migrated)

    def test_out_of_range_points_are_dropped_even_with_a_current_marker(self) -> None:
        corrupt = [{"v": 0.45, "f": 139}, {"v": 1.2, "f": 9000}]
        migrated, changed = migrate_curve_settings(
            {"core": 42, "points": corrupt, "curveScale": 2}, self.curve()
        )
        self.assertTrue(changed)
        self.assertEqual(migrated["core"], 42)
        self.assertNotIn("points", migrated)

    def test_no_curve_drops_points_safely(self) -> None:
        migrated, changed = migrate_curve_settings(
            {"core": 0, "points": doubled_points()}, None
        )
        self.assertTrue(changed)
        self.assertNotIn("points", migrated)

    def test_boot_fallback_from_a_failed_read_migrates_the_points(self) -> None:
        # The read fallback runs before _migrate_curve_state, so it must
        # migrate its own copy: otherwise `applied` reports the raw driver
        # frequencies and the next reapply writes them doubled.
        class ReadFailingGpu(ScaledGpu):
            def read(self):
                raise BackendError("the driver did not answer", cause="driver")

        with tempfile.TemporaryDirectory(prefix="omatuner-scale-") as tmp:
            with open(os.path.join(tmp, "clean-shutdown"), "w", encoding="ascii") as fh:
                fh.write("1\n")
            write_json(os.path.join(tmp, "last-applied.json"), {
                "cpu": None,
                "gpu": {"settings": {"core": 0, "points": doubled_points()},
                        "at": 1, "mode": "session"},
            })
            dirs = config.Dirs(system=False, state_dir=tmp,
                               socket_path=os.path.join(tmp, "test.sock"))
            core = TunerCore(dirs, {"cpu": FakeBackend("cpu"), "gpu": ReadFailingGpu()},
                             Journal(dirs.events_file))
            core.schedule_timer = lambda delay, callback: (lambda: None)
            core.boot()

            applied = core.applied["gpu"]
            self.assertEqual(applied["curveScale"], 2.0)
            self.assertEqual(round(applied["points"][0]["f"]), 139)
            self.assertLessEqual(max(p["f"] for p in applied["points"]), 2198)

    def test_boot_migrates_defaults_last_applied_and_applied_state(self) -> None:
        with tempfile.TemporaryDirectory(prefix="omatuner-scale-") as tmp:
            with open(os.path.join(tmp, "clean-shutdown"), "w", encoding="ascii") as fh:
                fh.write("1\n")
            write_json(os.path.join(tmp, "defaults.json"), {
                "cpu": None,
                "gpu": {"core": 0, "points": doubled_points()},
            })
            write_json(os.path.join(tmp, "last-applied.json"), {
                "cpu": None,
                "gpu": {"settings": {"core": 0, "points": doubled_points()},
                        "at": 1, "mode": "session"},
            })
            dirs = config.Dirs(system=False, state_dir=tmp,
                               socket_path=os.path.join(tmp, "test.sock"))
            gpu = ScaledGpu()
            core = TunerCore(dirs, {"cpu": FakeBackend("cpu"), "gpu": gpu},
                             Journal(dirs.events_file))
            core.schedule_timer = lambda delay, callback: (lambda: None)
            core.boot()

            defaults = core.defaults["gpu"]
            self.assertEqual(defaults["curveScale"], 2.0)
            self.assertEqual(round(defaults["points"][0]["f"]), 139)
            self.assertEqual(round(defaults["points"][-1]["f"]), 1898)
            recorded = core.last_applied["gpu"]["settings"]
            self.assertEqual(recorded["curveScale"], 2.0)
            self.assertEqual(round(recorded["points"][0]["f"]), 139)
            applied = core.applied["gpu"]
            self.assertEqual(applied["curveScale"], 2.0)
            self.assertLessEqual(max(p["f"] for p in applied["points"]), 2198)
            # The reapply that follows boot must receive real MHz.
            for call in gpu.apply_calls:
                if "points" in call:
                    self.assertLessEqual(max(p["f"] for p in call["points"]), 2198)


if __name__ == "__main__":
    unittest.main()
