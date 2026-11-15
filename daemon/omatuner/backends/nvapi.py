"""Undocumented NVIDIA NvAPI calls for the per-point V/F curve.

Linux exposes no NVML call for individual V/F points, but the driver ships
``libnvidia-api.so`` (the same interface the Windows tools use). LACT reads
and writes the curve through it; the call IDs and struct layouts below come
from LACT's ``lact-daemon/src/server/gpu_controller/nvidia/nvapi.rs`` (GPL-3.0)
and were confirmed read-only on a GTX 1060 / driver 580.196.07.

Only the frequency offset of a point is writable. The voltage of each point is
fixed by the driver; an edit moves the point horizontally. That is why the UI
labels the control as a frequency curve, not a voltage curve.

Fractional scales
-----------------
Some driver builds report the V/F frequencies at a fixed multiple of the SM
clock. Driver 580.196.07 on a GTX 1060 reports them at twice the real clock:
the top point reads 3796 MHz while NVML reports a 1911 MHz max SM clock. The
scale is detected from those two numbers at runtime and applied to every
frequency this module reports or accepts; the offset validation and the
read-back check compare in the same (real) units. A frequency of 0 in a
point's ``freq_offset_khz`` is unitless-safe, so nothing here depends on the
guess when the scale cannot be established.
"""

from __future__ import annotations

import ctypes
from dataclasses import dataclass, field

from ..nvml_defs import load_library

_NVAPI_INITIALIZE = 0x0150E828
_NVAPI_UNLOAD = 0xD22BDD7E
_NVAPI_ENUM_PHYSICAL_GPUS = 0xE5AC921F
_NVAPI_GET_ERROR_MESSAGE = 0x6C2D048C
_NVAPI_CLK_VF_POINTS_GET_INFO = 0x507B4B59
_NVAPI_CLK_VF_POINTS_GET_STATUS = 0x21537AD4
_NVAPI_CLK_VF_POINTS_GET_CONTROL = 0x23F1B133
_NVAPI_CLK_VF_POINTS_SET_CONTROL = 0x0733E009
_NVAPI_CLIENT_VOLT_RAILS_GET_STATUS = 0x465F9BCF

_MAX_GPUS = 64
_POINT_TYPE_PROG = 0
_LIB_CANDIDATES = ("libnvidia-api.so.1", "libnvidia-api.so")

# Windows in which a curve-to-NVML ratio counts as a known scale. Anything
# else means the two numbers disagree in a way this code cannot explain, and
# writing a curve whose frequencies may be wrong by an unknown factor is not
# acceptable.
_SCALE_WINDOWS = ((1.8, 2.2, 2.0), (0.9, 1.1, 1.0))


def detect_curve_scale(curve_max_mhz: float, max_clock_mhz: float) -> tuple[float | None, str | None]:
    """Raw-to-real divisor for the driver's V/F frequencies.

    ``curve_max_mhz`` is the stock maximum the driver reports for the curve,
    ``max_clock_mhz`` NVML's max SM clock for the same GPU. Returns
    ``(scale, None)`` when the ratio matches a known scale, ``(1.0, None)``
    when there is nothing to compare against (no NVML clock, which some
    drivers do not expose), and ``(None, cause)`` when the ratio matches
    neither window. The ``(1.0, None)`` fallback is not a proven scale:
    the caller must keep the curve read-only in that case.
    """
    try:
        curve_max = float(curve_max_mhz)
        clock_max = float(max_clock_mhz)
    except (TypeError, ValueError):
        return 1.0, None
    if curve_max <= 0 or clock_max <= 0:
        return 1.0, None
    ratio = curve_max / clock_max
    for low, high, scale in _SCALE_WINDOWS:
        if low <= ratio <= high:
            return scale, None
    return None, (
        f"the driver reports its V/F curve at {curve_max:.0f} MHz while NVML "
        f"reports a {clock_max:.0f} MHz SM clock (ratio {ratio:.2f}); the "
        "frequency scale is unknown, so per-point writes are refused"
    )


UNKNOWN_CLOCK_CAUSE = (
    "the driver did not report the GPU clock range, so the curve scale is "
    "unknown; the curve is read-only"
)


def curve_scale_for_writes(
    curve_max_mhz: float, max_clock_mhz: float
) -> tuple[float | None, str | None]:
    """Scale for a write, refusing the unknown-clock fallback.

    `detect_curve_scale` answers (1.0, None) when there is no clock to
    compare against, which is harmless for a reader showing raw frequencies.
    A writer cannot use it: the raw-to-real factor may be 2, so an unknown
    clock becomes (None, cause) and the curve stays read-only.
    """
    try:
        curve_max = float(curve_max_mhz)
        clock_max = float(max_clock_mhz)
    except (TypeError, ValueError):
        return None, UNKNOWN_CLOCK_CAUSE
    if curve_max <= 0 or clock_max <= 0:
        return None, UNKNOWN_CLOCK_CAUSE
    return detect_curve_scale(curve_max, clock_max)


class NvApiError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class _TupleV1(ctypes.Structure):
    _fields_ = [
        ("freq_khz", ctypes.c_uint32),
        ("voltage_uv", ctypes.c_uint32),
        ("rsvd", ctypes.c_uint8 * 32),
    ]


class _PointInfoV1(ctypes.Structure):
    _fields_ = [
        ("type_", ctypes.c_uint32),
        ("b_voltage_based", ctypes.c_uint8),
        ("rsvd", ctypes.c_uint8 * 16),
    ]


class _PointStatusV3(ctypes.Structure):
    _fields_ = [
        ("type_", ctypes.c_uint32),
        ("freq_khz", ctypes.c_uint32),
        ("voltage_uv", ctypes.c_uint32),
        ("vf_tuple_base", _TupleV1),
        ("vf_tuple_offset", _TupleV1),
        ("rsvd", ctypes.c_uint8 * 256),
    ]


class _PointsInfoV1(ctypes.Structure):
    _fields_ = [
        ("version", ctypes.c_uint32),
        ("vf_points_mask", ctypes.c_uint32 * 8),
        ("rsvd", ctypes.c_uint8 * 32),
        ("vf_points", _PointInfoV1 * 255),
    ]


class _PointsStatusV3(ctypes.Structure):
    _fields_ = [
        ("version", ctypes.c_uint32),
        ("vf_points_mask", ctypes.c_uint32 * 8),
        ("b_vf_tuple_base_supported", ctypes.c_uint8),
        ("rsvd", ctypes.c_uint8 * 64),
        ("vf_points", _PointStatusV3 * 255),
    ]


class _PointControlProgV1(ctypes.Structure):
    _fields_ = [("freq_offset_khz", ctypes.c_int32)]


class _PointControlDataV1(ctypes.Union):
    _fields_ = [
        ("prog", _PointControlProgV1),
        ("rsvd", ctypes.c_uint8 * 16),
    ]


class _PointControlV1(ctypes.Structure):
    _fields_ = [
        ("type_", ctypes.c_uint32),
        ("rsvd", ctypes.c_uint8 * 16),
        ("data", _PointControlDataV1),
    ]


class _PointsControlV1(ctypes.Structure):
    _fields_ = [
        ("version", ctypes.c_uint32),
        ("vf_points_mask", ctypes.c_uint32 * 8),
        ("rsvd", ctypes.c_uint8 * 32),
        ("vf_points", _PointControlV1 * 255),
    ]


class _VoltRailV1(ctypes.Structure):
    _fields_ = [
        ("rail_id", ctypes.c_uint32),
        ("current_voltage_uv", ctypes.c_uint32),
        ("rsvd", ctypes.c_uint8 * 32),
    ]


class _VoltRailsStatusV1(ctypes.Structure):
    _fields_ = [
        ("version", ctypes.c_uint32),
        ("rsvd", ctypes.c_uint8 * 32),
        ("rails", _VoltRailV1 * 1),
    ]


def _version(cls: type, revision: int) -> int:
    # NvAPI's version field is `sizeof(struct) | revision << 16`; the low 16
    # bits truncate for the 88 KB status struct, exactly as the driver and
    # LACT expect.
    return (ctypes.sizeof(cls) & 0xFFFF) | (revision << 16)


@dataclass
class CurvePointReading:
    voltage_mv: float
    frequency_mhz: float
    base_frequency_mhz: float
    base_voltage_mv: float
    offset_mhz: float


@dataclass
class CurveReading:
    points: list[CurvePointReading] = field(default_factory=list)


class NvApi:
    """Thin wrapper around the four point-curve calls."""

    def __init__(self) -> None:
        self.lib = None
        self.handle = None
        self._fns: dict[int, object] = {}
        # How many physical GPUs NvAPI enumerated. The backend also reads the
        # NVML count; the curve is only addressed at one card when both say 1.
        # None means this instance has not enumerated (an injected test
        # double), not that the count is unknown.
        self.physical_gpu_count: int | None = None
        # Raw driver frequency is `scale * real MHz`. The backend sets this
        # after comparing the curve maximum with NVML's SM max clock.
        self.scale: float = 1.0

    # ------------------------------------------------------------- lifecycle

    def open(self) -> None:
        if self.handle is not None:
            return
        self.lib, err = load_library(_LIB_CANDIDATES)
        if self.lib is None:
            raise NvApiError(f"libnvidia-api not found: {err}")
        query = self.lib.nvapi_QueryInterface
        query.argtypes = [ctypes.c_uint32]
        query.restype = ctypes.c_void_p

        initialize = self._query(_NVAPI_INITIALIZE)
        init = ctypes.CFUNCTYPE(ctypes.c_int)(initialize)
        rc = init()
        if rc != 0:
            raise NvApiError(f"NvAPI_Initialize failed: {self._status(rc)}")

        enum = self._query(_NVAPI_ENUM_PHYSICAL_GPUS)
        gpu = ctypes.c_void_p
        enum_fn = ctypes.CFUNCTYPE(
            ctypes.c_int, ctypes.POINTER(gpu), ctypes.POINTER(ctypes.c_uint32)
        )(enum)
        handles = (gpu * _MAX_GPUS)()
        count = ctypes.c_uint32(0)
        rc = enum_fn(handles, ctypes.byref(count))
        if rc != 0:
            raise NvApiError(f"NvAPI_EnumPhysicalGPUs failed: {self._status(rc)}")
        if count.value < 1 or not handles[0]:
            raise NvApiError("NvAPI reported no physical GPU")
        self.handle = handles[0]
        self.physical_gpu_count = int(count.value)

    def close(self) -> None:
        if self.lib is not None:
            try:
                unload = ctypes.CFUNCTYPE(ctypes.c_int)(self._query(_NVAPI_UNLOAD))
                unload()
            except Exception:
                pass
        self.handle = None
        self.lib = None
        self.physical_gpu_count = None
        self._fns.clear()

    def _query(self, ident: int):
        if ident in self._fns:
            return self._fns[ident]
        addr = self.lib.nvapi_QueryInterface(ident)
        if not addr:
            raise NvApiError(f"NvAPI function {ident:#x} is not exported")
        self._fns[ident] = addr
        return addr

    def _status(self, status: int) -> str:
        text = f"{status:#x}"
        if self.lib is None:
            return text
        try:
            get = ctypes.CFUNCTYPE(
                ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_char * 64)
            )(self._query(_NVAPI_GET_ERROR_MESSAGE))
            buf = (ctypes.c_char * 64)()
            if get(status, ctypes.byref(buf)) == 0:
                raw = bytes(buf)
                text += ": " + raw.split(b"\0", 1)[0].decode("utf-8", "replace")
        except Exception:
            pass
        return text

    # ------------------------------------------------------------- reading

    def _read_raw(self):
        info = _PointsInfoV1()
        ctypes.memset(ctypes.byref(info), 0, ctypes.sizeof(info))
        info.version = _version(_PointsInfoV1, 1)
        fn = ctypes.CFUNCTYPE(
            ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(_PointsInfoV1)
        )(self._query(_NVAPI_CLK_VF_POINTS_GET_INFO))
        rc = fn(self.handle, ctypes.byref(info))
        if rc != 0:
            raise NvApiError(f"ClkVfPointsGetInfo failed: {self._status(rc)}")

        status = _PointsStatusV3()
        ctypes.memset(ctypes.byref(status), 0, ctypes.sizeof(status))
        status.version = _version(_PointsStatusV3, 3)
        for i in range(8):
            status.vf_points_mask[i] = info.vf_points_mask[i]
        fn = ctypes.CFUNCTYPE(
            ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(_PointsStatusV3)
        )(self._query(_NVAPI_CLK_VF_POINTS_GET_STATUS))
        rc = fn(self.handle, ctypes.byref(status))
        if rc != 0:
            raise NvApiError(f"ClkVfPointsGetStatus failed: {self._status(rc)}")

        control = _PointsControlV1()
        ctypes.memset(ctypes.byref(control), 0, ctypes.sizeof(control))
        control.version = _version(_PointsControlV1, 1)
        for i in range(8):
            control.vf_points_mask[i] = info.vf_points_mask[i]
        fn = ctypes.CFUNCTYPE(
            ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(_PointsControlV1)
        )(self._query(_NVAPI_CLK_VF_POINTS_GET_CONTROL))
        rc = fn(self.handle, ctypes.byref(control))
        if rc != 0:
            raise NvApiError(f"ClkVfPointsGetControl failed: {self._status(rc)}")
        return info, status, control

    @staticmethod
    def _editable_indices(info: _PointsInfoV1) -> list[int]:
        return [
            i
            for i in range(255)
            if info.vf_points[i].b_voltage_based == 1
            and info.vf_points[i].type_ == _POINT_TYPE_PROG
        ]

    @staticmethod
    def _base_khz(status: _PointsStatusV3, control: _PointsControlV1, i: int) -> int:
        if status.b_vf_tuple_base_supported:
            return status.vf_points[i].vf_tuple_base.freq_khz
        return status.vf_points[i].freq_khz - control.vf_points[i].data.prog.freq_offset_khz

    @staticmethod
    def _base_uv(status: _PointsStatusV3, i: int) -> int:
        if status.b_vf_tuple_base_supported:
            return status.vf_points[i].vf_tuple_base.voltage_uv
        return status.vf_points[i].voltage_uv

    def read_curve(self) -> CurveReading:
        if self.handle is None:
            self.open()
        info, status, control = self._read_raw()
        indices = self._editable_indices(info)
        scale = self.scale if self.scale else 1.0
        reading = CurveReading()
        for i in indices:
            point = status.vf_points[i]
            if point.voltage_uv <= 0 or point.freq_khz <= 0:
                continue
            base_khz = self._base_khz(status, control, i)
            if base_khz <= 0:
                continue
            # The global NVML offset is not part of the per-point control, so
            # the point's editable frequency is base + per-point offset. Using
            # the status frequency here would fold the global offset into the
            # curve and apply it a second time on the next write.
            offset_khz = control.vf_points[i].data.prog.freq_offset_khz
            reading.points.append(
                CurvePointReading(
                    voltage_mv=point.voltage_uv / 1000.0,
                    frequency_mhz=(base_khz + offset_khz) / (1000.0 * scale),
                    base_frequency_mhz=base_khz / (1000.0 * scale),
                    base_voltage_mv=self._base_uv(status, i) / 1000.0,
                    offset_mhz=offset_khz / (1000.0 * scale),
                )
            )
        if not reading.points:
            raise NvApiError("the driver reported no editable V/F points")
        return reading

    # ------------------------------------------------------------- writing

    def probe_write(self) -> None:
        """Write the current offsets back, unchanged, to prove SetControl works."""
        info, status, control = self._read_raw()
        editable = self._editable_indices(info)
        before = {i: status.vf_points[i].freq_khz for i in editable}
        scale = self.scale if self.scale else 1.0
        fn = ctypes.CFUNCTYPE(
            ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(_PointsControlV1)
        )(self._query(_NVAPI_CLK_VF_POINTS_SET_CONTROL))
        rc = fn(self.handle, ctypes.byref(control))
        if rc != 0:
            raise NvApiError(f"ClkVfPointsSetControl refused: {self._status(rc)}")
        _, after, _ = self._read_raw()
        # 2 MHz expressed in the driver's raw units: the tolerance must not
        # become tighter (or looser) when the curve is reported at 2x.
        tolerance_khz = 2000 * (self.scale if self.scale else 1.0)
        for i in editable:
            if abs(after.vf_points[i].freq_khz - before[i]) > tolerance_khz:
                raise NvApiError(
                    f"SetControl accepted the no-op write but point {i} changed "
                    f"from {before[i] / (1000 * scale):.0f} to "
                    f"{after.vf_points[i].freq_khz / (1000 * scale):.0f} MHz"
                )

    def apply_curve(
        self,
        points: list[dict],
        min_offset_mhz: int,
        max_offset_mhz: int,
    ) -> list[CurvePointReading]:
        if self.handle is None:
            self.open()
        info, status, control = self._read_raw()
        indices = self._editable_indices(info)
        if len(points) != len(indices):
            raise NvApiError(
                f"curve has {len(points)} points but the driver exposes {len(indices)}"
            )
        scale = self.scale if self.scale else 1.0
        requested: list[tuple[int, float, int]] = []
        for slot, (i, point) in enumerate(zip(indices, points)):
            try:
                # Descriptors carry volts; the driver reports microvolts.
                voltage_mv = float(point["v"]) * 1000.0
                frequency_mhz = float(point["f"])
            except (KeyError, TypeError, ValueError):
                raise NvApiError("curve points must be {v, f} pairs")
            live_mv = status.vf_points[i].voltage_uv / 1000.0
            if abs(voltage_mv - live_mv) > 5.0:
                raise NvApiError(
                    f"point {slot} voltage is {live_mv:.0f} mV, not {voltage_mv:.0f} mV; "
                    "reload the curve"
                )
            base_khz = self._base_khz(status, control, i)
            # The request is in real MHz; the driver's table is in raw kHz.
            target_khz = int(round(frequency_mhz * 1000 * scale))
            offset_khz = target_khz - base_khz
            offset_mhz = offset_khz / (1000.0 * scale)
            # Floor a point at 1 real MHz: a negative offset cannot take the
            # point's frequency below zero in the driver's units.
            point_min = max(min_offset_mhz, -(base_khz / (1000.0 * scale)) + 1)
            if not (point_min <= round(offset_mhz) <= max_offset_mhz):
                raise NvApiError(
                    f"point {slot} offset {offset_mhz:+.0f} MHz is outside "
                    f"{point_min}..{max_offset_mhz} MHz"
                )
            control.vf_points[i].data.prog.freq_offset_khz = int(offset_khz)
            requested.append((i, frequency_mhz, offset_khz))

        fn = ctypes.CFUNCTYPE(
            ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(_PointsControlV1)
        )(self._query(_NVAPI_CLK_VF_POINTS_SET_CONTROL))
        rc = fn(self.handle, ctypes.byref(control))
        if rc != 0:
            raise NvApiError(f"ClkVfPointsSetControl failed: {self._status(rc)}")

        # Read back: a control that accepted the request but kept the old
        # curve must not be reported as applied.
        _, after, after_control = self._read_raw()
        actual: list[CurvePointReading] = []
        # 2 MHz of real tolerance, expressed in the driver's units.
        tolerance_khz = 2000 * scale
        for i, wanted_mhz, wanted_offset_khz in requested:
            applied_offset_khz = after_control.vf_points[i].data.prog.freq_offset_khz
            if abs(applied_offset_khz - wanted_offset_khz) > tolerance_khz:
                base_khz = self._base_khz(after, after_control, i)
                got_mhz = (base_khz + applied_offset_khz) / (1000.0 * scale)
                raise NvApiError(
                    f"driver did not apply the curve: point {i} wanted {wanted_mhz:.0f} MHz, "
                    f"reports {got_mhz:.0f} MHz"
                )
            base_khz = self._base_khz(after, after_control, i)
            actual.append(
                CurvePointReading(
                    voltage_mv=after.vf_points[i].voltage_uv / 1000.0,
                    frequency_mhz=(base_khz + applied_offset_khz) / (1000.0 * scale),
                    base_frequency_mhz=base_khz / (1000.0 * scale),
                    base_voltage_mv=self._base_uv(after, i) / 1000.0,
                    offset_mhz=applied_offset_khz / (1000.0 * scale),
                )
            )
        return actual

    def voltage_volts(self) -> float | None:
        if self.handle is None:
            return None
        data = _VoltRailsStatusV1()
        ctypes.memset(ctypes.byref(data), 0, ctypes.sizeof(data))
        data.version = _version(_VoltRailsStatusV1, 1)
        fn = ctypes.CFUNCTYPE(
            ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(_VoltRailsStatusV1)
        )(self._query(_NVAPI_CLIENT_VOLT_RAILS_GET_STATUS))
        if fn(self.handle, ctypes.byref(data)) != 0:
            return None
        if data.rails[0].current_voltage_uv <= 0:
            return None
        return round(data.rails[0].current_voltage_uv / 1_000_000.0, 3)
