"""Simulated hardware backend for tests and UI validation.

Mimics the reference machine (i7-6700 + GTX 1060): full control ranges, a
V/F curve with editable points, drifting telemetry, and hooks to stage
failures (write refusals, firmware lock) so the diagnosis paths can be
exercised without touching real hardware.
"""

from __future__ import annotations

import math
import time
from typing import Any

from ..config import (
    CPU_VREF_MV,
    GPU_TEMP_FACTORY_C,
    range_for,
)
from .base import Backend, BackendError, Control, Curve, CurvePoint

BASE_POINTS = [
    (0.70, 1140), (0.75, 1330), (0.80, 1510), (0.85, 1650),
    (0.90, 1770), (0.95, 1850), (1.00, 1910), (1.05, 1970), (1.10, 2020),
]


def cpu_telemetry(t: float) -> dict[str, Any]:
    """The fake CPU's drifting readings, shared with the daemon's fake mode."""
    return {
        "cpuUsage": round(24 + 8 * math.sin(t * 0.4)),
        "cpuTemp": round(49 + 3 * math.sin(t * 0.2)),
        "cpuFrequency": 3600 + round(400 * math.sin(t * 0.2)),
        "cpuPowerW": round(34 + 9 * math.sin(t * 0.25), 1),
    }


class FakeBackend(Backend):
    def __init__(self, component: str, *, unavailable: bool = False, cause: str | None = None):
        self.component = component
        self.device_name = "i7-6700 · 4C/8T" if component == "cpu" else "GTX 1060 6GB"
        self.unavailable = unavailable
        self.fixed_cause = cause
        self.values: dict[str, int] = {}
        self.points = [CurvePoint(v, f) for v, f in BASE_POINTS] if component == "gpu" else []
        self.apply_log: list[dict[str, Any]] = []
        self.fail_next_apply = False
        self.started = time.time()
        self.temperature_c = 54.0
        self.temp_target_c = GPU_TEMP_FACTORY_C
        self.slowdown_c = 99
        self.shutdown_c = 102
        # Simulated single fan. `fan_auto` mirrors the driver owning the duty;
        # a manual write clears it, like nvmlDeviceSetFanSpeed_v2 does.
        self.fan_count = 1 if component == "gpu" else 0
        self.fan_min = 0
        self.fan_max = 100
        # Readable but unwritable is a real state (no service privileges):
        # the daemon must still accept Auto and refuse Fixed/Curve.
        self.fan_writable = True
        self.fan_speed = 34
        self.fan_auto = True
        self.fan_rpm = None
        self.fan_log: list[dict[str, Any]] = []

    def probe(self) -> None:
        pass

    def controls(self) -> list[Control]:
        cause = self.fixed_cause or self.probe_failed
        available = not self.unavailable and self.probe_failed is None
        if self.component == "cpu":
            return [
                Control(key, label, "mV", *range_for("cpu", key, self.tuning_mode),
                        factory=0, available=available, cause=cause,
                        risk_ref=CPU_VREF_MV, risk_ref_label="stock voltage", risk_direction="both")
                for key, label in (("core", "CPU core"), ("cache", "CPU cache"))
            ]
        return [
            Control("core", "GPU core", "MHz", *range_for("gpu", "core", self.tuning_mode),
                    factory=0, available=available, cause=cause,
                    risk_ref=1911, risk_ref_label="max boost clock", risk_direction="up"),
            Control("memory", "Memory clock", "MHz", *range_for("gpu", "memory", self.tuning_mode),
                    factory=0, available=available, cause=cause,
                    risk_ref=4004, risk_ref_label="memory clock", risk_direction="up"),
            Control("temp", "Temperature limit", "°C", *range_for("gpu", "temp", self.tuning_mode),
                    factory=GPU_TEMP_FACTORY_C, available=available,
                    cause=cause, risk_ref=GPU_TEMP_FACTORY_C,
                    risk_ref_label="stock thermal limit", risk_direction="up"),
        ]

    def read(self) -> dict[str, int]:
        values = dict(self.values)
        if self.component == "gpu":
            for key, factory in (("core", 0), ("memory", 0), ("temp", self.temp_target_c)):
                values.setdefault(key, factory)
        else:
            values.setdefault("core", 0)
            values.setdefault("cache", 0)
        return values

    def apply(self, values: dict[str, int | list]) -> None:
        if self.fail_next_apply:
            self.fail_next_apply = False
            raise BackendError("simulated hardware refused the write", cause="driver")
        self.apply_log.append({"ts": time.time(), "values": dict(values)})
        scalar = {k: v for k, v in values.items() if k != "points"}
        self.values.update(scalar)
        if "temp" in scalar:
            self.temp_target_c = int(scalar["temp"])
        if "points" in values and values["points"] is not None:
            self.points = [CurvePoint(float(p["v"]), float(p["f"])) for p in values["points"]]

    def curve(self) -> Curve | None:
        if self.component != "gpu":
            return None
        return Curve(
            supported=not self.unavailable,
            writable=not self.unavailable,
            cause=self.fixed_cause,
            points=[CurvePoint(p.voltage, p.frequency) for p in self.points],
            factory_points=[CurvePoint(v, f) for v, f in BASE_POINTS],
        )

    def fan_info(self) -> dict[str, Any]:
        if self.component != "gpu":
            return {"supported": False, "cause": "no GPU fan on this component"}
        writable = not self.unavailable and self.fan_writable
        cause = self.fixed_cause or self.probe_failed
        if not writable:
            cause = cause or "the driver refuses fan changes from this process"
        else:
            cause = None
        return {
            "supported": not self.unavailable,
            "writable": writable,
            "cause": cause,
            "count": self.fan_count,
            "min": self.fan_min,
            "max": self.fan_max,
            "speed": self.fan_speed if not self.fan_auto else self.stock_duty(),
            "rpm": self.fan_rpm,
            "temperatureC": int(round(self.temperature_c)),
            "targetC": self.temp_target_c,
            "slowdownC": self.slowdown_c,
        }

    def stock_duty(self) -> int:
        """A believable driver curve for the fake: quiet until it is warm."""
        temp = int(round(self.temperature_c))
        return max(self.fan_min, min(self.fan_max, 25 + (temp - 40) * 2))

    def set_fan_duty(self, percent: int) -> int:
        if self.component != "gpu":
            raise BackendError("no GPU fan on this component", cause="device")
        if self.unavailable or not self.fan_writable:
            raise BackendError(self.fixed_cause or "fan control is unavailable", cause="driver")
        speed = max(self.fan_min, min(self.fan_max, int(percent)))
        self.fan_auto = False
        self.fan_speed = speed
        self.fan_log.append({"ts": time.time(), "percent": speed})
        return speed

    def set_fan_auto(self) -> None:
        if self.component != "gpu":
            return
        if self.unavailable or not self.fan_writable:
            raise BackendError(self.fixed_cause or "fan control is unavailable", cause="driver")
        self.fan_auto = True
        self.fan_speed = self.stock_duty()
        self.fan_log.append({"ts": time.time(), "auto": True})

    def thermal_info(self) -> dict[str, Any] | None:
        if self.component != "gpu" or self.unavailable:
            return None
        return {
            "currentC": int(round(self.temperature_c)),
            "targetC": self.temp_target_c,
            "slowdownC": self.slowdown_c,
            "shutdownC": self.shutdown_c,
        }

    def telemetry(self) -> dict[str, Any]:
        t = time.time() - self.started
        if self.component == "cpu":
            return cpu_telemetry(t)
        return {
            "gpuUsage": round(38 + 13 * math.sin(t * 0.3)),
            "gpuTemp": int(round(self.temperature_c)),
            "gpuFanSpeed": self.fan_speed if not self.fan_auto else self.stock_duty(),
            "gpuTempLimit": self.temp_target_c,
            "gpuTempSlowdown": self.slowdown_c,
            "gpuVoltage": 0.9,
            "gpuFrequency": 1750 + round(150 * math.sin(t * 0.3)),
            "gpuMemFrequency": 4004,
            "gpuPowerW": round(78 + 21 * math.sin(t * 0.2), 1),
        }
