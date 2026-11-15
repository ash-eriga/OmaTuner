"""GPU fan policy: validation, curve interpolation and the control loop.

The hardware knows two things: a duty in percent (0..100) and the temperature
that is driving it. Everything here is pure arithmetic so the policy can be
tested without NVML: the curve is a list of (temperature, percent) anchors,
and the loop adds hysteresis, a delayed spindown and a hard override at the
GPU thermal ceiling.

Compared with LACT's fan control, this keeps four things LACT leaves to the
caller or to chance:

- the curve is validated on the server (monotonic, inside the driver window,
  and ending hot enough in normal mode) instead of accepting any percentage;
- a fixed duty is refused below 25 % in normal mode, because a fixed low
  speed has no curve to ramp it back up;
- the target never waits for the hysteresis when the GPU reaches its thermal
  ceiling: it goes to 100 % immediately and stays there until the temperature
  falls back;
- the loop is a state machine with an explicit last applied duty: a move
  smaller than FAN_DUTY_STEP inside FAN_HYSTERESIS_C of the last change is
  held, a downward change waits FAN_SPINDOWN_MS, and an unchanged duty is
  rewritten now and then so a helper restart cannot leave it unattended.
"""

from __future__ import annotations

import math
from typing import Any

from . import config


def default_curve() -> list[dict[str, int]]:
    return [
        {"t": temp, "p": percent}
        for temp, percent in zip(config.FAN_CURVE_TEMPS_C, config.FAN_CURVE_DEFAULT)
    ]


def _as_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a number")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{label} must be a number")
    number = int(value)
    if number != value:
        raise ValueError(f"{label} must be a whole number")
    return number


def fixed_floor(info: dict[str, Any], expert: bool) -> int:
    driver_min = int(info.get("min", 0) or 0)
    if expert:
        return driver_min
    return max(driver_min, config.FAN_FIXED_MIN_PERCENT)


def validate(raw: Any, info: dict[str, Any], expert: bool) -> dict[str, Any]:
    """Return a canonical fan configuration or raise ValueError.

    `info` is the descriptor from the GPU backend: the driver's min/max duty.
    Nothing outside that window is accepted, in either mode.
    """
    if not isinstance(raw, dict):
        raise ValueError("fan settings must be an object")
    mode = raw.get("mode")
    if mode not in config.FAN_MODES:
        raise ValueError(f"unknown fan mode {mode!r}")
    if mode == "auto":
        return {"mode": "auto"}

    driver_min = int(info.get("min", 0) or 0)
    driver_max = int(info.get("max", 100) or 100)

    if mode == "fixed":
        speed = _as_int(raw.get("speed"), "fan speed")
        floor = fixed_floor(info, expert)
        if speed < floor or speed > driver_max:
            raise ValueError(
                f"fan speed {speed}% is outside {floor}..{driver_max}%"
                + ("" if expert else "; expert mode unlocks 0%")
            )
        return {"mode": "fixed", "speed": speed}

    points = raw.get("points")
    if not isinstance(points, list) or not (
        config.FAN_CURVE_MIN_POINTS <= len(points) <= config.FAN_CURVE_MAX_POINTS
    ):
        raise ValueError(
            f"fan curve needs {config.FAN_CURVE_MIN_POINTS} to "
            f"{config.FAN_CURVE_MAX_POINTS} points"
        )
    temp_lo = config.FAN_CURVE_TEMP_MIN_C
    temp_hi = config.FAN_CURVE_TEMP_MAX_C
    out: list[dict[str, int]] = []
    previous_speed = driver_min
    previous_temp: int | None = None
    for index, point in enumerate(points):
        if not isinstance(point, dict):
            raise ValueError(f"fan curve point {index} must be an object")
        temp = _as_int(point.get("t"), f"fan curve point {index} temperature")
        speed = _as_int(point.get("p"), f"fan curve point {index} speed")
        if temp < temp_lo or temp > temp_hi:
            raise ValueError(
                f"fan curve point {index} temperature {temp} °C is outside "
                f"{temp_lo}..{temp_hi} °C"
            )
        if previous_temp is not None and temp <= previous_temp:
            raise ValueError(
                "fan curve temperatures must rise: "
                f"{temp} °C does not follow {previous_temp} °C"
            )
        if speed < driver_min or speed > driver_max:
            raise ValueError(
                f"fan curve point {index} speed {speed}% is outside "
                f"{driver_min}..{driver_max}%"
            )
        if speed < previous_speed:
            raise ValueError("fan curve speeds must never decrease as temperature rises")
        previous_temp = temp
        previous_speed = speed
        out.append({"t": temp, "p": speed})

    if not expert and out[-1]["p"] < config.FAN_CURVE_LAST_MIN_PERCENT:
        raise ValueError(
            f"the hottest fan curve point must reach at least "
            f"{config.FAN_CURVE_LAST_MIN_PERCENT}%; expert mode removes this floor"
        )
    return {"mode": "curve", "points": out}


def duty_at(points: list[dict[str, int]], temperature_c: int | float) -> int:
    """Interpolate the curve at `temperature_c`, clamped at both ends."""
    if not points:
        return 0
    ordered = sorted(points, key=lambda point: point["t"])
    if temperature_c <= ordered[0]["t"]:
        return int(ordered[0]["p"])
    for lower, higher in zip(ordered, ordered[1:]):
        if temperature_c <= higher["t"]:
            span = higher["t"] - lower["t"]
            if span <= 0:
                return int(higher["p"])
            ratio = (temperature_c - lower["t"]) / span
            return int(round(lower["p"] + (higher["p"] - lower["p"]) * ratio))
    return int(ordered[-1]["p"])


def thermal_ceiling(target_c: int | None, slowdown_c: int | None, *,
                    ceiling_c: int | None = None, margin_c: int | None = None) -> int:
    """The temperature at which the fan is forced to 100%.

    Same rule the tuning guard uses: never below the configured ceiling, never
    below the target the user chose, and always below the driver slowdown.
    The defaults are read from config at call time so a patched config is
    honored.
    """
    ceiling = config.GPU_THERMAL_CEILING_C if ceiling_c is None else int(ceiling_c)
    margin = config.THERMAL_HARD_LIMIT_MARGIN_C if margin_c is None else int(margin_c)
    if target_c is not None:
        ceiling = max(ceiling, int(target_c) + 2)
    if slowdown_c is not None:
        ceiling = min(ceiling, int(slowdown_c) - margin)
    return int(ceiling)


class FanLoop:
    """Hysteresis, delayed spindown and the ceiling override for one config."""

    def __init__(self, config_: dict[str, Any], info: dict[str, Any]):
        self.config = config_
        self.minimum = int(info.get("min", 0) or 0)
        self.maximum = int(info.get("max", 100) or 100)
        # Temperature at the last real change: the hysteresis baseline.
        self.last_temperature: int | None = None
        self.last_duty: int | None = None
        self.last_change_ms: int = 0
        self.last_write_ms: int = 0
        # True when the current write only re-asserts an unchanged duty (see
        # FAN_REASSERT_MS); callers keep it out of the journal and the UI.
        self.reassert = False
        self.failures = 0
        self.write_failures = 0
        self.was_forced = False

    def forget(self) -> None:
        """The hardware may no longer hold the duty this loop last wrote.

        Called after a refused write: the helper can have restarted and
        handed the fan back to the driver, so the next sample must write
        again instead of trusting `last_duty`.
        """
        self.last_duty = None
        self.last_write_ms = 0

    def target(self, temperature_c: int, ceiling_c: int, now_ms: int) -> tuple[int, bool, bool]:
        """Return (duty, forced, changed) for this sample."""
        if temperature_c >= ceiling_c:
            wanted = self.maximum
            forced = True
        elif self.config.get("mode") == "fixed":
            wanted = int(self.config.get("speed", self.minimum))
            forced = False
        else:
            wanted = duty_at(self.config.get("points") or [], temperature_c)
            forced = False
        wanted = max(self.minimum, min(self.maximum, wanted))

        previous = self.last_duty
        previous_temp = self.last_temperature
        changed = previous != wanted
        if not forced and previous is not None:
            small_move = (
                abs(wanted - previous) < config.FAN_DUTY_STEP
                and previous_temp is not None
                and abs(temperature_c - previous_temp) < config.FAN_HYSTERESIS_C
            )
            spindown = (
                wanted < previous
                and now_ms - self.last_change_ms < config.FAN_SPINDOWN_MS
            )
            if small_move or spindown:
                wanted = previous
                changed = False

        # Re-assert an unchanged duty now and then: the helper gives the fan
        # back to the driver when it restarts, so "nothing changed" does not
        # prove the duty is still in force.
        self.reassert = False
        if not changed and now_ms - self.last_write_ms >= config.FAN_REASSERT_MS:
            changed = True
            self.reassert = True

        if changed:
            if not self.reassert:
                self.last_change_ms = now_ms
                # Hysteresis is measured from the temperature of the last
                # real change, not from the previous sample: with a curve
                # where 1 °C moves a 3-point step, a per-sample comparison
                # lets the fan hunt between two duties at a point boundary.
                self.last_temperature = temperature_c
            self.last_duty = wanted
            self.last_write_ms = now_ms
        return wanted, forced, changed
