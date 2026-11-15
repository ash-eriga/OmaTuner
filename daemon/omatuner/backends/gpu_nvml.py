"""NVIDIA GPU backend over NVML, loaded with ctypes.

Query functions work unprivileged. Clock offsets need CAP_SYS_ADMIN, which
the system service carries; the temperature target is stricter, the driver
only accepts it from uid 0 (measured on driver 580: a nonzero uid with
CAP_SYS_ADMIN + CAP_SYS_RAWIO gets NVML error 4), so the daemon hands that
write to the root helper, exactly as it already does for the fan. The
per-point V/F curve has no NVML API on Linux; `nvapi.py` reads and writes it
through the driver's undocumented NvAPI interface (the same one LACT uses).
Anything the driver or GPU genuinely does not expose is reported unavailable
with its cause instead of guessed.
"""

from __future__ import annotations

import ctypes
import os
import time
from typing import Any

from .. import fanhelper, nvml_defs
from ..nvml_defs import (
    NVML_CLOCK_MEM,
    NVML_CLOCK_SM,
    NVML_ERROR_NOT_SUPPORTED,
    NVML_ERROR_NO_PERMISSION,
    NVML_SUCCESS,
    NVML_TEMP_ACOUSTIC_CURR,
    NVML_TEMP_ACOUSTIC_MAX,
    NVML_TEMP_ACOUSTIC_MIN,
    NVML_TEMP_SHUTDOWN,
    NVML_TEMP_SLOWDOWN,
)
from .base import Backend, BackendError, Control, Curve, CurvePoint
from .nvapi import NvApi, NvApiError, detect_curve_scale

# Seconds between helper re-probes while a write verdict is still negative.
# The helper starts independently of the daemon (no unit ordering), so the
# verdict from probe() must not last for the session.
HELPER_RECHECK_SECONDS = 5.0


def driver_visible() -> bool:
    """Cheapest signal that the NVIDIA driver is up in this boot.

    Either the first device node or the driver's /proc entry is enough; used
    by refresh() to notice a driver that loaded after the unit started.
    """
    return os.path.exists("/dev/nvidia0") or os.path.exists("/proc/driver/nvidia/version")


class Utilization(ctypes.Structure):
    _fields_ = [
        ("gpu", ctypes.c_uint),
        ("mem", ctypes.c_uint),
    ]


class Nvml:
    def __init__(self) -> None:
        self.lib = None
        self.device = None
        self.name = ""
        self.lib, err = nvml_defs.load_library(nvml_defs.NVML_LIB_CANDIDATES)
        if self.lib is None:
            raise BackendError(f"libnvidia-ml not found: {err}", cause="driver")

        # Every prototype is declared explicitly: default ctypes marshalling
        # passes pointers through int-sized slots. Symbols older drivers do
        # not export (per-point limits, power, temperature) are guarded so a
        # missing function becomes an unavailable control, never an
        # AttributeError at import.
        nvml_defs.declare(self.lib)
        u32 = ctypes.c_uint
        c_int_p = ctypes.POINTER(ctypes.c_int)
        u32_p = ctypes.POINTER(u32)

        def proto(name: str, argtypes: list, restype=u32) -> None:
            nvml_defs.proto(self.lib, name, argtypes, restype)

        proto("nvmlDeviceGetName", [ctypes.c_void_p, ctypes.c_char_p, u32])
        proto("nvmlDeviceGetGpcClkVfOffset", [ctypes.c_void_p, c_int_p])
        proto("nvmlDeviceGetMemClkVfOffset", [ctypes.c_void_p, c_int_p])
        proto("nvmlDeviceSetGpcClkVfOffset", [ctypes.c_void_p, ctypes.c_int])
        proto("nvmlDeviceSetMemClkVfOffset", [ctypes.c_void_p, ctypes.c_int])
        proto("nvmlDeviceGetGpcClkMinMaxVfOffset", [ctypes.c_void_p, c_int_p, c_int_p])
        proto("nvmlDeviceGetMemClkMinMaxVfOffset", [ctypes.c_void_p, c_int_p, c_int_p])
        proto("nvmlDeviceGetMaxClockInfo", [ctypes.c_void_p, u32, u32_p])
        proto("nvmlDeviceGetUtilizationRates", [ctypes.c_void_p, ctypes.c_void_p])
        proto("nvmlDeviceGetTemperature", [ctypes.c_void_p, u32, u32_p])
        proto("nvmlDeviceGetClockInfo", [ctypes.c_void_p, u32, u32_p])
        proto("nvmlDeviceGetPowerUsage", [ctypes.c_void_p, u32_p])
        proto("nvmlDeviceGetEnforcedPowerLimit", [ctypes.c_void_p, u32_p])
        # Fan control. The _v2 getter takes a fan index; the original one
        # only exists for single-fan cards. Every card seen here exposes the
        # v2 pair, but the fallback keeps a missing symbol an unavailable
        # control instead of an AttributeError.
        proto("nvmlDeviceGetFanSpeed", [ctypes.c_void_p, u32_p])
        proto("nvmlDeviceGetFanSpeedRPM", [ctypes.c_void_p, u32, u32_p])
        proto("nvmlDeviceGetFanControlPolicy_v2", [ctypes.c_void_p, u32, c_int_p])

        rc = self.lib.nvmlInit_v2()
        if rc != NVML_SUCCESS:
            raise BackendError(f"nvmlInit_v2 failed: {rc}", cause="driver")
        handle = ctypes.c_void_p()
        rc = self.lib.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(handle))
        if rc != NVML_SUCCESS:
            raise BackendError(f"no NVIDIA device: {rc}", cause="device")
        self.device = handle
        buf = ctypes.create_string_buffer(80)
        if self.lib.nvmlDeviceGetName(self.device, buf, 80) == NVML_SUCCESS:
            self.name = buf.value.decode("utf-8", "replace")

    def has(self, fn: str) -> bool:
        return hasattr(self.lib, fn)

    def close(self) -> None:
        if self.device is not None:
            if hasattr(self.lib, "nvmlShutdown"):
                self.lib.nvmlShutdown()
            self.device = None


class GpuNvmlBackend(Backend):
    component = "gpu"

    def __init__(self, nvml: Nvml | None = None, nvapi: NvApi | None = None):
        self.nvml = nvml
        self.nvapi = nvapi
        # An injected NVML (tests) must never fall through to the real
        # libnvidia-api on the host.
        self._nvapi_tried = nvml is not None
        self.present = False
        self.cause = None
        self.write_cause = None
        self.curve_cause: str | None = None
        self.curve_writable = False
        # Raw-to-real divisor for the V/F frequencies, detected once per probe
        # against NVML's SM max clock (driver 580 reports them at 2x).
        self.curve_scale: float = 1.0
        self.max_sm_clock = 0
        self.max_mem_clock = 0
        self.core_offset_limits: tuple[int, int] | None = None
        self.mem_offset_limits: tuple[int, int] | None = None
        self.temp_supported = False
        self.temp_api = ""
        self.temp_limit_c = 0
        self.temp_min_c = 0
        self.temp_max_c = 0
        self.temp_write_cause: str | None = None
        # The target write is proven, never assumed: directly when this
        # process is root, through the root helper otherwise.
        self.temp_writable = False
        self.temp_direct = False
        self.thermal_slowdown_c: int | None = None
        self.thermal_shutdown_c: int | None = None
        # Fan control. Reading never needs privilege; the write is gated on
        # the uid and is proven by _probe_fan_write, not assumed.
        self.fan_supported = False
        self.fan_count = 0
        self.fan_min = 0
        self.fan_max = 100
        self.fan_cause: str | None = None
        self.fan_writable = False
        self.fan_direct = False
        self.fan_write_cause: str | None = None
        # Driver 580 refuses both the fan and the temperature-target writes
        # from any non-root uid, so the daemon delegates them to the root
        # helper. Injected in tests.
        self.helper = fanhelper.FanHelperLink()
        # refresh() re-asks the helper while a write verdict is still
        # negative, at most once per this interval (monotonic seconds).
        self._helper_recheck_at = 0.0

    def _read_driver_limits(self) -> None:
        lib = self.nvml.lib
        self.core_offset_limits = None
        self.mem_offset_limits = None
        low = ctypes.c_int(0)
        high = ctypes.c_int(0)
        if self.nvml.has("nvmlDeviceGetGpcClkMinMaxVfOffset"):
            if lib.nvmlDeviceGetGpcClkMinMaxVfOffset(self.nvml.device, ctypes.byref(low), ctypes.byref(high)) == NVML_SUCCESS:
                if low.value < high.value:
                    self.core_offset_limits = (low.value, high.value)
        low = ctypes.c_int(0)
        high = ctypes.c_int(0)
        if self.nvml.has("nvmlDeviceGetMemClkMinMaxVfOffset"):
            if lib.nvmlDeviceGetMemClkMinMaxVfOffset(self.nvml.device, ctypes.byref(low), ctypes.byref(high)) == NVML_SUCCESS:
                if low.value < high.value:
                    self.mem_offset_limits = (low.value, high.value)

    def probe(self) -> None:
        try:
            self.nvml = self.nvml or Nvml()
        except BackendError as exc:
            self.present = False
            self.device_present = driver_visible()
            self.cause = str(exc)
            return
        self.present = True
        self.device_present = True
        self.cause = None
        self.device_name = self.nvml.name

        if self.nvml.has("nvmlDeviceGetMaxClockInfo"):
            clock = ctypes.c_uint(0)
            if self.nvml.lib.nvmlDeviceGetMaxClockInfo(self.nvml.device, NVML_CLOCK_SM, ctypes.byref(clock)) == NVML_SUCCESS:
                self.max_sm_clock = clock.value
            clock = ctypes.c_uint(0)
            if self.nvml.lib.nvmlDeviceGetMaxClockInfo(self.nvml.device, NVML_CLOCK_MEM, ctypes.byref(clock)) == NVML_SUCCESS:
                self.max_mem_clock = clock.value
        self._read_driver_limits()
        self._read_thermal_limits()
        self._read_fan_caps()

        # One no-op write per offset control answers whether this process may
        # change clocks at all: rewrite the value that is already applied.
        # Observed on the reference machine (GTX 1060, driver 580): an
        # unprivileged caller gets NO_PERMISSION (4); NOT_SUPPORTED (3) is
        # also reported as needing the elevated service.
        if not (self.nvml.has("nvmlDeviceGetGpcClkVfOffset")
                and self.nvml.has("nvmlDeviceSetGpcClkVfOffset")):
            self.write_cause = "this driver does not expose core clock offsets"
        else:
            try:
                current = self._get_offset("nvmlDeviceGetGpcClkVfOffset")
                rc = self.nvml.lib.nvmlDeviceSetGpcClkVfOffset(self.nvml.device, ctypes.c_int(current))
                if rc in (NVML_ERROR_NO_PERMISSION, NVML_ERROR_NOT_SUPPORTED):
                    self.write_cause = (
                        "the NVIDIA driver refuses clock changes from this process "
                        "(requires the OmaTuner system service; install with bin/install)"
                    )
                elif rc != NVML_SUCCESS:
                    self.write_cause = f"driver refused clock control (NVML error {rc})"
                elif self._get_offset("nvmlDeviceGetGpcClkVfOffset") != current:
                    self.write_cause = "driver did not confirm the no-op clock write"
            except BackendError as exc:
                self.write_cause = f"clock-offset probe failed: {exc}"

        self._probe_temp_write()
        self._probe_curve()
        self._probe_fan_write()

    def check_fan_write(self) -> None:
        """Re-run just the fan write probe (the helper may have just started)."""
        if self.fan_supported:
            self._probe_fan_write()

    def _probe_fan_write(self) -> None:
        """Learn whether this process can really write the fan.

        Driver 580 gates the fan setters on the caller being root, not on
        CAP_SYS_ADMIN: uid 0 with CAP_SYS_ADMIN + CAP_SYS_RAWIO succeeds, a
        nonzero uid with the same capabilities gets NVML error 4. So the
        answer comes from the process uid.

        When the process is root, prove the write with the duty the fan runs
        now (no visible change) and hand the fan straight back to the
        driver, so a stale `writable` can never outlive a driver change.
        When it is not root, ask the root helper: a hello only, no write.
        """
        self.fan_writable = False
        self.fan_direct = False
        if not self.fan_supported:
            self.fan_write_cause = None
            return
        if os.geteuid() == 0:
            has_default = self.nvml.has("nvmlDeviceSetDefaultFanSpeed_v2")
            has_policy = self.nvml.has("nvmlDeviceSetFanControlPolicy")
            if not (has_default or has_policy):
                self.fan_write_cause = (
                    "this driver cannot return the fan to automatic control"
                )
                return
            speed = self._fan_speed_once(0)
            if speed is None:
                self.fan_write_cause = "the driver did not report the current fan speed"
                return
            rc = self.nvml.lib.nvmlDeviceSetFanSpeed_v2(self.nvml.device, 0, speed)
            if rc != NVML_SUCCESS:
                self.fan_write_cause = f"the driver rejected a fan write (NVML error {rc})"
                return
            rc = nvml_defs.set_fan_auto(self.nvml.lib, self.nvml.device, 0)
            if rc != NVML_SUCCESS:
                self.fan_write_cause = (
                    "the driver did not return the fan to automatic control "
                    f"(NVML error {rc})"
                )
                return
            self.fan_writable = True
            self.fan_direct = True
            self.fan_write_cause = None
            return
        ok, cause = self.helper.available()
        self.fan_writable = ok
        self.fan_write_cause = None if ok else cause

    def _read_fan_caps(self) -> None:
        self.fan_supported = False
        self.fan_count = 0
        self.fan_min = 0
        self.fan_max = 100
        self.fan_cause = None
        if not self.present:
            return
        if not (
            self.nvml.has("nvmlDeviceGetNumFans")
            and self.nvml.has("nvmlDeviceSetFanSpeed_v2")
        ):
            self.fan_cause = "this driver does not expose fan control"
            return
        count = ctypes.c_uint(0)
        rc = self.nvml.lib.nvmlDeviceGetNumFans(self.nvml.device, ctypes.byref(count))
        if rc != NVML_SUCCESS:
            self.fan_cause = f"the driver did not report the fan count (NVML error {rc})"
            return
        if count.value < 1:
            self.fan_cause = "this GPU reports no controllable fan"
            return
        self.fan_count = int(count.value)
        low = ctypes.c_uint(0)
        high = ctypes.c_uint(0)
        if self.nvml.has("nvmlDeviceGetMinMaxFanSpeed"):
            rc = self.nvml.lib.nvmlDeviceGetMinMaxFanSpeed(
                self.nvml.device, ctypes.byref(low), ctypes.byref(high)
            )
            if rc == NVML_SUCCESS and low.value < high.value:
                self.fan_min = int(low.value)
                self.fan_max = int(high.value)
        self.fan_supported = True

    def _fan_speed_once(self, fan: int) -> int | None:
        value = ctypes.c_uint(0)
        if self.nvml.has("nvmlDeviceGetFanSpeed_v2"):
            rc = self.nvml.lib.nvmlDeviceGetFanSpeed_v2(
                self.nvml.device, fan, ctypes.byref(value)
            )
        elif fan == 0 and self.nvml.has("nvmlDeviceGetFanSpeed"):
            rc = self.nvml.lib.nvmlDeviceGetFanSpeed(self.nvml.device, ctypes.byref(value))
        else:
            return None
        return int(value.value) if rc == NVML_SUCCESS else None

    def _fan_speeds(self) -> list[int]:
        return [
            speed
            for fan in range(self.fan_count)
            if (speed := self._fan_speed_once(fan)) is not None
        ]

    def fan_info(self) -> dict[str, Any]:
        if not self.present or not self.fan_supported:
            return {
                "supported": False,
                "writable": False,
                "cause": self.fan_cause or self.cause or "fan control is unavailable",
                "count": 0,
                "min": 0,
                "max": 100,
            }
        speeds = self._fan_speeds()
        duty = int(round(sum(speeds) / len(speeds))) if speeds else None
        rpm = None
        if self.nvml.has("nvmlDeviceGetFanSpeedRPM"):
            value = ctypes.c_uint(0)
            if self.nvml.lib.nvmlDeviceGetFanSpeedRPM(
                self.nvml.device, 0, ctypes.byref(value)
            ) == NVML_SUCCESS and value.value > 0:
                rpm = int(value.value)
        temp = ctypes.c_uint(0)
        temperature = None
        if self.nvml.has("nvmlDeviceGetTemperature") and self.nvml.lib.nvmlDeviceGetTemperature(
            self.nvml.device, 0, ctypes.byref(temp)
        ) == NVML_SUCCESS:
            temperature = int(temp.value)
        return {
            "supported": True,
            # The fan has its own permission gate (the caller uid); a clock
            # probe failure says nothing about the fan.
            "writable": self.fan_writable,
            "cause": self.fan_write_cause,
            "count": self.fan_count,
            "min": self.fan_min,
            "max": self.fan_max,
            "speed": duty,
            "rpm": rpm,
            "temperatureC": temperature,
            "targetC": self._get_temp_target() if self.temp_supported else None,
            "slowdownC": self.thermal_slowdown_c,
        }

    def set_fan_duty(self, percent: int) -> int:
        if not self.fan_supported or not self.present:
            raise BackendError(self.fan_cause or "fan control is unavailable", cause="driver")
        speed = max(self.fan_min, min(self.fan_max, int(percent)))
        if not self.fan_direct:
            if not self.fan_writable:
                raise BackendError(self.fan_write_cause or "fan control is read-only", cause="driver")
            try:
                applied = self.helper.set_duty(speed)
            except fanhelper.FanDeviceError as exc:
                self.fan_write_cause = str(exc)
                raise BackendError(str(exc), cause="driver") from exc
            # A write that went through clears the cause of an older refusal:
            # the helper was restarted, or its link came back.
            self.fan_write_cause = None
            self.fan_writable = True
            return applied
        if self.fan_write_cause is not None:
            raise BackendError(self.fan_write_cause, cause="driver")
        for fan in range(self.fan_count):
            rc = self.nvml.lib.nvmlDeviceSetFanSpeed_v2(self.nvml.device, fan, speed)
            if rc in (NVML_ERROR_NO_PERMISSION, NVML_ERROR_NOT_SUPPORTED):
                self.fan_write_cause = (
                    "the NVIDIA driver refuses fan changes from this process "
                    "(requires the OmaTuner system service; install with bin/install)"
                )
                raise BackendError(self.fan_write_cause, cause="driver")
            if rc != NVML_SUCCESS:
                raise BackendError(
                    f"fan speed rejected by driver (NVML error {rc})", cause="driver"
                )
        return speed

    def set_fan_auto(self) -> None:
        if not self.fan_supported or not self.present:
            raise BackendError(self.fan_cause or "fan control is unavailable", cause="driver")
        if not self.fan_direct:
            if not self.fan_writable:
                raise BackendError(self.fan_write_cause or "fan control is read-only", cause="driver")
            try:
                self.helper.set_auto()
            except fanhelper.FanDeviceError as exc:
                self.fan_write_cause = str(exc)
                raise BackendError(str(exc), cause="driver") from exc
            self.fan_write_cause = None
            self.fan_writable = True
            return
        if self.fan_write_cause is not None:
            raise BackendError(self.fan_write_cause, cause="driver")
        for fan in range(self.fan_count):
            rc = nvml_defs.set_fan_auto(self.nvml.lib, self.nvml.device, fan)
            if rc == nvml_defs.NVML_NO_AUTO_CONTROL:
                raise BackendError(
                    "this driver cannot return the fan to automatic control", cause="driver"
                )
            if rc in (NVML_ERROR_NO_PERMISSION, NVML_ERROR_NOT_SUPPORTED):
                self.fan_write_cause = (
                    "the NVIDIA driver refuses fan changes from this process "
                    "(requires the OmaTuner system service; install with bin/install)"
                )
                raise BackendError(self.fan_write_cause, cause="driver")
            if rc != NVML_SUCCESS:
                raise BackendError(
                    f"the driver refused automatic fan control (NVML error {rc})",
                    cause="driver",
                )

    def check_temp_write(self) -> None:
        """Re-run just the temperature write probe (the helper may have just started)."""
        if self.temp_supported:
            self._probe_temp_write()

    def _probe_temp_write(self) -> None:
        """Rewrite the current target: the only way to know the setter works.

        The value is the one already applied, so a successful probe changes
        nothing. A root process proves the write locally; anyone else proves
        it through the root helper, because driver 580 accepts the thermal
        setter only from uid 0 even when CAP_SYS_ADMIN is held.
        """
        self.temp_writable = False
        self.temp_direct = False
        self.temp_write_cause = None
        if not self.temp_supported:
            return
        target = self._get_temp_target()
        if target is None:
            self.temp_write_cause = "the driver did not report the current temperature target"
            return
        if os.geteuid() == 0:
            cause = self._set_temp_local(target)
            if cause is None and self._get_temp_target() != target:
                cause = "driver did not confirm the temperature target"
            self.temp_write_cause = cause
            self.temp_writable = cause is None
            self.temp_direct = cause is None
            return
        if self.temp_api != "threshold":
            self.temp_write_cause = (
                "this driver's temperature-limit API is not carried by the root helper"
            )
            return
        try:
            self.helper.set_temperature(target)
        except fanhelper.FanDeviceError as exc:
            self.temp_write_cause = str(exc)
            return
        if self._get_temp_target() != target:
            self.temp_write_cause = "the root helper's temperature write was not confirmed"
            return
        self.temp_write_cause = None
        self.temp_writable = True

    def _set_temp_local(self, target: int) -> str | None:
        """Write the target from this process. Returns the refusal, or None."""
        value = ctypes.c_int(int(target))
        rc = self.nvml.lib.nvmlDeviceSetTemperatureThreshold(
            self.nvml.device, NVML_TEMP_ACOUSTIC_CURR, ctypes.byref(value)
        )
        if rc == NVML_ERROR_NO_PERMISSION:
            return (
                "the NVIDIA driver only accepts the temperature target from uid 0; "
                "the OmaTuner root helper provides that (sudo bin/install, "
                "sudo systemctl start omatuner-fan.service)"
            )
        if rc == NVML_ERROR_NOT_SUPPORTED:
            return "this driver does not support setting the temperature target"
        if rc != NVML_SUCCESS:
            return f"driver refused the temperature target (NVML error {rc})"
        return None

    def _get_temp_target(self) -> int | None:
        if not self.temp_supported:
            return None
        value = ctypes.c_uint(0)
        rc = self.nvml.lib.nvmlDeviceGetTemperatureThreshold(
            self.nvml.device, NVML_TEMP_ACOUSTIC_CURR, ctypes.byref(value)
        )
        return value.value if rc == NVML_SUCCESS else None

    def _get_temp_threshold(self, kind: int) -> int | None:
        if not self.nvml.has("nvmlDeviceGetTemperatureThreshold"):
            return None
        value = ctypes.c_uint(0)
        rc = self.nvml.lib.nvmlDeviceGetTemperatureThreshold(
            self.nvml.device, kind, ctypes.byref(value)
        )
        return value.value if rc == NVML_SUCCESS else None

    def _read_thermal_limits(self) -> None:
        self.thermal_slowdown_c = self._get_temp_threshold(NVML_TEMP_SLOWDOWN)
        self.thermal_shutdown_c = self._get_temp_threshold(NVML_TEMP_SHUTDOWN)
        self.temp_supported = False
        self.temp_api = ""
        self.temp_min_c = 0
        self.temp_max_c = 0
        self.temp_limit_c = 0

        # Driver 580 exports nvmlDeviceGet/SetTemperatureThreshold; the
        # acoustic bounds keep the target in a range the driver accepts.
        if (
            self.nvml.has("nvmlDeviceGetTemperatureThreshold")
            and self.nvml.has("nvmlDeviceSetTemperatureThreshold")
        ):
            target = self._get_temp_threshold(NVML_TEMP_ACOUSTIC_CURR)
            low = self._get_temp_threshold(NVML_TEMP_ACOUSTIC_MIN)
            high = self._get_temp_threshold(NVML_TEMP_ACOUSTIC_MAX)
            if target is not None and low is not None and high is not None and low < high:
                self.temp_api = "threshold"
                self.temp_supported = True
                self.temp_limit_c = target
                # The driver keeps the last written target across a daemon
                # restart and exposes no "default acoustic" query, so reading
                # it here would make "reset" restore the user's own value.
                # The factory target is the documented constant instead.
                self.temp_min_c = low
                self.temp_max_c = high

    def _ensure_nvapi(self) -> None:
        if self.nvapi is not None or self._nvapi_tried:
            return
        self._nvapi_tried = True
        api = None
        try:
            api = NvApi()
            api.open()
            self.nvapi = api
        except (NvApiError, OSError) as exc:
            if api is not None:
                try:
                    api.close()
                except Exception:
                    pass
            self.curve_cause = f"per-point V/F curve unavailable: {exc}"

    def _probe_curve(self) -> None:
        self.curve_writable = False
        self._ensure_nvapi()
        if self.nvapi is None:
            return
        # Read raw first: the stock maximum is what NVML's max SM clock can be
        # compared against even when the user has already moved points.
        self.nvapi.scale = 1.0
        try:
            reading = self.nvapi.read_curve()
        except NvApiError as exc:
            self.curve_cause = f"per-point V/F curve read failed: {exc}"
            return
        stock_max = max(
            (p.base_frequency_mhz or p.frequency_mhz for p in reading.points),
            default=0.0,
        )
        scale, scale_cause = detect_curve_scale(stock_max, float(self.max_sm_clock))
        if scale is None:
            self.curve_scale = 1.0
            self.curve_writable = False
            self.curve_cause = scale_cause
            return
        self.curve_scale = scale
        self.nvapi.scale = scale
        if self.write_cause is not None:
            self.curve_cause = (
                "the V/F curve is readable but writing requires the OmaTuner "
                "system service (install with bin/install)"
            )
            return
        try:
            # SetControl with the current offsets. Nothing changes, but this
            # is the only way to know the driver accepts per-point writes;
            # a read alone does not prove it.
            self.nvapi.probe_write()
            self.curve_writable = True
            self.curve_cause = None
        except NvApiError as exc:
            self.curve_writable = False
            self.curve_cause = f"the driver refused per-point V/F writes: {exc}"

    def refresh(self) -> bool:
        """Pick up the device when it was absent at startup.

        The unit starts at multi-user.target; if NVIDIA's modules or the
        device nodes come up after that, NVML failed once and the controls
        would stay unavailable until a manual restart. Re-probing only on a
        change of what the driver exposes keeps the no-op write probe from
        repeating.

        With the device present, the only verdict that can still change is
        the helper's: the daemon and omatuner-fan.service both start at
        multi-user.target with no ordering between them, so a daemon that
        probed first would hold fan_writable/temp_writable False for the
        whole session. `_recheck_helper` re-asks, rate-limited.
        """
        visible = driver_visible()
        if self.present and visible:
            return self._recheck_helper()
        if not self.present and visible == self.device_present:
            return False
        before = (self.present, self.cause, self.fan_supported)
        if self.nvml is not None and hasattr(self.nvml, "close"):
            try:
                self.nvml.close()
            except Exception:
                pass
        if self.nvapi is not None:
            try:
                self.nvapi.close()
            except Exception:
                pass
        self.nvapi = None
        self._nvapi_tried = False
        self.curve_cause = None
        self.curve_writable = False
        self.curve_scale = 1.0
        self.temp_write_cause = None
        self.temp_writable = False
        self.temp_direct = False
        self.fan_supported = False
        self.fan_count = 0
        self.fan_cause = None
        self.fan_writable = False
        self.fan_write_cause = None
        self.nvml = None
        self.cause = None
        self.write_cause = None
        self.probe_failed = None
        self.probe()
        return (self.present, self.cause, self.fan_supported) != before

    def _recheck_helper(self) -> bool:
        """Re-ask the root helper while a write verdict is still negative.

        Only a non-root process delegates; root proves the writes itself. A
        hello on the fan link and one value-preserving temperature write are
        the whole cost, so the interval keeps this off the once-a-second
        refresh path. Returns True when the daemon's view changed.
        """
        if os.geteuid() == 0:
            return False
        fan_missing = self.fan_supported and not self.fan_writable and not self.fan_direct
        temp_missing = (
            self.temp_supported and self.temp_api == "threshold"
            and not self.temp_writable and not self.temp_direct
        )
        if not (fan_missing or temp_missing):
            return False
        now = time.monotonic()
        if now - self._helper_recheck_at < HELPER_RECHECK_SECONDS:
            return False
        self._helper_recheck_at = now
        before = (self.fan_writable, self.temp_writable)
        if fan_missing:
            self.check_fan_write()
        if temp_missing:
            self.check_temp_write()
        return (self.fan_writable, self.temp_writable) != before

    def _get_offset(self, fn: str) -> int:
        value = ctypes.c_int(0)
        rc = getattr(self.nvml.lib, fn)(self.nvml.device, ctypes.byref(value))
        if rc != NVML_SUCCESS:
            raise BackendError(f"{fn} failed: {rc}", cause="driver")
        return value.value

    def controls(self) -> list[Control]:
        from ..config import GPU_TEMP_FACTORY_C, TUNING_MODE_EXPERT, intersect, range_for

        writable = (
            self.present and self.write_cause is None and self.probe_failed is None
        )
        cause = self.write_cause or (None if self.present else self.cause) or self.probe_failed
        expert = self.tuning_mode == TUNING_MODE_EXPERT
        # Expert mode unlocks exactly what the driver reports; normal mode
        # takes the conservative constant but never exceeds the driver window.
        core_range = (self.core_offset_limits if expert and self.core_offset_limits
                      else intersect(range_for("gpu", "core", self.tuning_mode), self.core_offset_limits))
        mem_range = (self.mem_offset_limits if expert and self.mem_offset_limits
                     else intersect(range_for("gpu", "memory", self.tuning_mode), self.mem_offset_limits))
        core = Control(
            key="core",
            label="GPU core",
            unit="MHz",
            minimum=core_range[0],
            maximum=core_range[1],
            factory=0,
            available=writable,
            cause=cause,
            risk_ref=float(self.max_sm_clock),
            risk_ref_label="max boost clock",
            risk_direction="up",
        )
        memory = Control(
            key="memory",
            label="Memory clock",
            unit="MHz",
            minimum=mem_range[0],
            maximum=mem_range[1],
            factory=0,
            available=writable,
            cause=cause,
            risk_ref=float(self.max_mem_clock),
            risk_ref_label="memory clock",
            risk_direction="up",
        )
        temp_cause = None if self.temp_supported else (
            "temperature target is not writable through this driver/GPU"
        )
        driver_temp = (
            (self.temp_min_c, self.temp_max_c)
            if self.temp_supported and self.temp_min_c < self.temp_max_c
            else None
        )
        temp_range = intersect(range_for("gpu", "temp", self.tuning_mode), driver_temp)
        # The temperature target has its own write probe; the clock verdict
        # says nothing about it (the driver gates them differently).
        temp_available = (
            self.temp_supported and self.temp_writable and self.probe_failed is None
        )
        temp = Control(
            key="temp",
            label="Temperature limit",
            unit="°C",
            minimum=temp_range[0],
            maximum=temp_range[1],
            factory=GPU_TEMP_FACTORY_C,
            available=temp_available,
            cause=None if temp_available else (
                self.temp_write_cause or temp_cause or self.probe_failed
                or "the driver did not confirm the temperature target"
            ),
            risk_ref=float(self.temp_limit_c or GPU_TEMP_FACTORY_C),
            risk_ref_label="stock thermal limit",
            risk_direction="up",
        )
        return [core, memory, temp]

    def read(self) -> dict[str, int]:
        if not self.present:
            return {}
        if not (self.nvml.has("nvmlDeviceGetGpcClkVfOffset")
                and self.nvml.has("nvmlDeviceGetMemClkVfOffset")):
            raise BackendError("this driver does not expose clock-offset reads", cause="driver")
        values = {
            "core": self._get_offset("nvmlDeviceGetGpcClkVfOffset"),
            "memory": self._get_offset("nvmlDeviceGetMemClkVfOffset"),
        }
        if self.temp_supported:
            target = self._get_temp_target()
            if target is not None:
                values["temp"] = int(target)
        return values

    @staticmethod
    def _check_limit(label: str, value: int, limits: tuple[int, int] | None) -> None:
        if limits and not (limits[0] <= value <= limits[1]):
            raise BackendError(
                f"{label} offset {value} MHz is outside the driver-reported range "
                f"{limits[0]}..{limits[1]} MHz",
                cause="driver",
            )

    def apply(self, values: dict[str, Any]) -> None:
        if "core" in values and not self.nvml.has("nvmlDeviceSetGpcClkVfOffset"):
            raise BackendError("this driver cannot set the core clock offset", cause="driver")
        if "memory" in values and not self.nvml.has("nvmlDeviceSetMemClkVfOffset"):
            raise BackendError("this driver cannot set the memory clock offset", cause="driver")
        if "core" in values:
            self._check_limit("core", int(values["core"]), self.core_offset_limits)
            rc = self.nvml.lib.nvmlDeviceSetGpcClkVfOffset(self.nvml.device, ctypes.c_int(values["core"]))
            if rc != NVML_SUCCESS:
                raise BackendError(f"core offset rejected by driver (NVML error {rc})", cause="driver")
        if "memory" in values:
            self._check_limit("memory", int(values["memory"]), self.mem_offset_limits)
            rc = self.nvml.lib.nvmlDeviceSetMemClkVfOffset(self.nvml.device, ctypes.c_int(values["memory"]))
            if rc != NVML_SUCCESS:
                raise BackendError(f"memory offset rejected by driver (NVML error {rc})", cause="driver")

        if "temp" in values:
            if not self.temp_supported or not self.temp_writable:
                raise BackendError(
                    self.temp_write_cause or "this driver cannot set the temperature target",
                    cause="driver",
                )
            target = int(values["temp"])
            if target < self.temp_min_c or target > self.temp_max_c:
                raise BackendError(
                    f"temperature target {target} °C is outside the driver range "
                    f"{self.temp_min_c}..{self.temp_max_c} °C",
                    cause="driver",
                )
            if self.temp_direct:
                cause = self._set_temp_local(target)
                if cause is not None:
                    self.temp_write_cause = cause
                    self.temp_writable = False
                    raise BackendError(cause, cause="driver")
            else:
                try:
                    self.helper.set_temperature(target)
                except fanhelper.FanDeviceError as exc:
                    self.temp_write_cause = str(exc)
                    self.temp_writable = False
                    raise BackendError(str(exc), cause="driver") from exc
            actual = self._get_temp_target()
            if actual is not None and actual != target:
                message = (
                    f"driver reports temperature target {actual} °C after requesting {target} °C"
                )
                self.temp_write_cause = message
                self.temp_writable = False
                raise BackendError(message, cause="driver")
            # The write went through, so any refusal recorded earlier (the
            # helper was stopped then) no longer describes the hardware.
            self.temp_write_cause = None
            self.temp_writable = True

        if "points" in values:
            self._apply_curve(values["points"])

    def _apply_curve(self, points: Any) -> None:
        if not isinstance(points, list):
            raise BackendError("curve points must be a list", cause="driver")
        if self.nvapi is None or not self.curve_writable:
            raise BackendError(
                self.curve_cause or "per-point V/F curve writing is not available",
                cause="driver",
            )
        from ..config import intersect, range_for

        offset_range = intersect(
            range_for("gpu", "core", self.tuning_mode), self.core_offset_limits
        )
        try:
            self.nvapi.apply_curve(points, offset_range[0], offset_range[1])
        except NvApiError as exc:
            raise BackendError(f"V/F curve write failed: {exc}", cause="driver") from exc

    def curve(self) -> Curve | None:
        if self.nvapi is None:
            return Curve(
                supported=False,
                writable=False,
                cause=self.curve_cause or "per-point V/F curve unavailable",
            )
        try:
            reading = self.nvapi.read_curve()
        except NvApiError as exc:
            return Curve(
                supported=False,
                writable=False,
                cause=f"per-point V/F curve read failed: {exc}",
            )
        from ..config import intersect, range_for

        offset_range = intersect(
            range_for("gpu", "core", self.tuning_mode), self.core_offset_limits
        )
        points = [
            CurvePoint(p.voltage_mv / 1000.0, p.frequency_mhz)
            for p in reading.points
        ]
        factory = [
            CurvePoint(p.base_voltage_mv / 1000.0, p.base_frequency_mhz)
            for p in reading.points
        ]
        # The descriptor is always in real MHz: the driver's raw frequencies
        # have been divided by the detected scale. The offset limits are real
        # MHz too, so they add to the real factory range without rescaling.
        min_f = max(1, int(round(min(p.frequency for p in factory))) + offset_range[0])
        max_f = int(round(max(p.frequency for p in factory))) + offset_range[1]
        return Curve(
            supported=True,
            writable=self.curve_writable,
            cause=self.curve_cause,
            points=points,
            factory_points=factory,
            min_frequency=min_f,
            max_frequency=max_f,
            scale=self.curve_scale,
        )

    def thermal_info(self) -> dict[str, Any]:
        if not self.present:
            return {}
        current = ctypes.c_uint(0)
        if not self.nvml.has("nvmlDeviceGetTemperature"):
            return {}
        if self.nvml.lib.nvmlDeviceGetTemperature(
            self.nvml.device, 0, ctypes.byref(current)
        ) != NVML_SUCCESS:
            return {}
        info: dict[str, Any] = {"currentC": int(current.value)}
        if self.temp_supported:
            target = self._get_temp_target()
            if target is not None:
                info["targetC"] = int(target)
        if self.thermal_slowdown_c is not None:
            info["slowdownC"] = int(self.thermal_slowdown_c)
        if self.thermal_shutdown_c is not None:
            info["shutdownC"] = int(self.thermal_shutdown_c)
        return info

    def telemetry(self) -> dict[str, Any]:
        if not self.present:
            return {}
        util = Utilization()
        temp = ctypes.c_uint(0)
        out: dict[str, Any] = {}
        if self.nvml.has("nvmlDeviceGetUtilizationRates"):
            if self.nvml.lib.nvmlDeviceGetUtilizationRates(self.nvml.device, ctypes.byref(util)) == NVML_SUCCESS:
                out["gpuUsage"] = int(util.gpu)
        if self.nvml.has("nvmlDeviceGetTemperature"):
            if self.nvml.lib.nvmlDeviceGetTemperature(self.nvml.device, 0, ctypes.byref(temp)) == NVML_SUCCESS:
                out["gpuTemp"] = int(temp.value)
        if self.fan_supported:
            speeds = self._fan_speeds()
            if speeds:
                out["gpuFanSpeed"] = int(round(sum(speeds) / len(speeds)))
        if self.nvml.has("nvmlDeviceGetClockInfo"):
            clock = ctypes.c_uint(0)
            if self.nvml.lib.nvmlDeviceGetClockInfo(self.nvml.device, NVML_CLOCK_SM, ctypes.byref(clock)) == NVML_SUCCESS:
                out["gpuFrequency"] = int(clock.value)
            clock = ctypes.c_uint(0)
            if self.nvml.lib.nvmlDeviceGetClockInfo(self.nvml.device, NVML_CLOCK_MEM, ctypes.byref(clock)) == NVML_SUCCESS:
                out["gpuMemFrequency"] = int(clock.value)
        if self.nvml.has("nvmlDeviceGetPowerUsage"):
            milliwatts = ctypes.c_uint(0)
            if self.nvml.lib.nvmlDeviceGetPowerUsage(self.nvml.device, ctypes.byref(milliwatts)) == NVML_SUCCESS:
                out["gpuPowerW"] = round(milliwatts.value / 1000.0, 1)
        if self.nvml.has("nvmlDeviceGetEnforcedPowerLimit"):
            milliwatts = ctypes.c_uint(0)
            if self.nvml.lib.nvmlDeviceGetEnforcedPowerLimit(self.nvml.device, ctypes.byref(milliwatts)) == NVML_SUCCESS:
                out["gpuPowerLimitW"] = round(milliwatts.value / 1000.0, 1)
        if self.temp_supported:
            target = self._get_temp_target()
            if target is not None:
                out["gpuTempLimit"] = int(target)
        if self.thermal_slowdown_c is not None:
            out["gpuTempSlowdown"] = int(self.thermal_slowdown_c)
        if self.nvapi is not None:
            voltage = self.nvapi.voltage_volts()
            if voltage is not None:
                out["gpuVoltage"] = voltage
        return out
