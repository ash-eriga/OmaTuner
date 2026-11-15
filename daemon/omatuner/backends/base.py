"""Backend interface: one CPU backend and one GPU backend per daemon.

A backend knows nothing about policy. It reports which controls exist and why
any of them are unavailable, reads current values, applies values, and
contributes telemetry. `probe()` must leave the hardware as it found it (no
net effect), so the daemon can call it at startup and for `doctor`: the GPU
backend proves its write permissions with value-preserving writes and restores
what it touched.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..config import RISK_PERCENT, TUNING_MODE_NORMAL


@dataclass
class Control:
    key: str
    label: str
    unit: str
    minimum: int
    maximum: int
    step: int = 1
    factory: int = 0
    available: bool = True
    cause: str | None = None
    # Aggressive-zone reference: percent of `ref` beyond the factory value,
    # in `direction`, counts as risky. "both" treats either direction as risk.
    risk_ref: float = 0.0
    risk_ref_label: str = ""
    risk_direction: str = "up"

    def descriptor(self, value: int) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "unit": self.unit,
            "min": self.minimum,
            "max": self.maximum,
            "step": self.step,
            "factory": self.factory,
            "available": self.available,
            "cause": self.cause,
            "risk": {
                "percent": RISK_PERCENT,
                "edge": None if not self.risk_ref else self.risk_percent_boundary(),
                "ref": self.risk_ref,
                "refLabel": self.risk_ref_label,
                "direction": self.risk_direction,
            },
            "value": value,
        }

    def risk_percent_boundary(self) -> int:
        """Offset from factory at which the aggressive zone begins."""
        import math

        return int(math.floor(self.risk_ref * RISK_PERCENT / 100.0)) if self.risk_ref else 0


@dataclass
class CurvePoint:
    voltage: float  # volts
    # MHz. The driver's V/F table has quarter-MHz steps on Pascal, so this is
    # a float; rounding it would make "reset to factory" land next to stock.
    frequency: float


@dataclass
class Curve:
    supported: bool = False
    # A curve can be readable but not writable (the daemon is running without
    # the privileges the driver requires, or the driver refuses SetControl).
    # The UI may display those points but must not offer an edit.
    writable: bool = True
    cause: str | None = None
    points: list[CurvePoint] = field(default_factory=list)
    factory_points: list[CurvePoint] = field(default_factory=list)
    min_frequency: int = 1000
    max_frequency: int = 2250
    # Raw-to-real divisor the backend applied to the driver's frequencies
    # (2.0 when the driver reports the V/F table at twice the SM clock). It
    # travels in the descriptor so persisted points can be migrated instead
    # of being replayed in the wrong units.
    scale: float = 1.0

    def descriptor(self) -> dict[str, Any]:
        return {
            "supported": self.supported,
            "writable": self.writable,
            "cause": self.cause,
            "points": [{"v": p.voltage, "f": p.frequency} for p in self.points],
            "factoryPoints": [{"v": p.voltage, "f": p.frequency} for p in self.factory_points],
            "minF": self.min_frequency,
            "maxF": self.max_frequency,
            "scale": self.scale,
        }


class BackendError(Exception):
    def __init__(self, message: str, cause: str = "error"):
        super().__init__(message)
        self.cause = cause


class Backend:
    component = "cpu"
    device_name = ""
    # Tuning mode the daemon runs in ("normal" or "expert"). The core sets it
    # before probe/apply, and controls() exposes the matching limits so the
    # daemon can reject out-of-range values server-side.
    tuning_mode = TUNING_MODE_NORMAL
    # Set by the core when probe() itself raises; controls() fold it into
    # their cause so a broken probe degrades the backend instead of aborting
    # boot.
    probe_failed: str | None = None
    # Whether the device node or driver was reachable at the last probe.
    # refresh() compares against this, not against `available`, so a device
    # that exists but refuses access is not re-opened every second.
    device_present: bool = False
    # (owner, group, mode, inode, rdev) of the device node at the last probe.
    # The install applies a udev rule that hands the node to the service user,
    # and a module reload recreates the inode; both must be noticed without
    # restarting the daemon.
    device_signature: tuple[int, ...] | None = None

    def probe(self) -> None: ...

    def refresh(self) -> bool:
        """Re-check a verdict that the environment can change later.

        `probe()` runs once at startup. Loading the `msr` module, or the
        NVIDIA driver coming up, happens after a daemon is already running
        (an install does exactly that), so availability must follow the
        hardware instead of freezing at boot. Return True when the reported
        answer changed; the default backend has nothing environmental.
        """
        return False

    def controls(self) -> list[Control]: ...

    def read(self) -> dict[str, int]:
        """Current applied value per control key."""

    def apply(self, values: dict[str, int]) -> None: ...

    def curve(self) -> Curve | None:
        return None

    def fan_info(self) -> dict[str, Any]:
        """Fan capability and live reading. GPU only; the default is absent."""
        return {
            "supported": False,
            "writable": False,
            "cause": "this component has no controllable fan",
            "count": 0,
            "min": 0,
            "max": 100,
        }

    def set_fan_duty(self, percent: int) -> int:
        raise BackendError("fan control is unavailable", cause="device")

    def set_fan_auto(self) -> None:
        raise BackendError("fan control is unavailable", cause="device")

    def thermal_info(self) -> dict[str, Any] | None:
        """Current temperature and the driver's own thermal limits, if any."""
        return None

    def telemetry(self) -> dict[str, Any]:
        return {}
