"""Paths and tuning constants, resolved per run mode.

System mode runs from the systemd unit as the dedicated `omatuner` user with
state under /var/lib and the socket under /run. User mode needs no root and
keeps everything inside the desktop user's own directories; privileged
controls simply report unavailable with their cause.
"""

from __future__ import annotations

import os
import pwd

VERSION = "0.1.0"
SYSTEM_SOCKET = "/run/omatuner/omatuner.sock"

CONFIRM_WINDOW_MS = 5000
BOOT_NOTICE_MS = 5000
TELEMETRY_INTERVAL_S = 1.0

# Aggressive zone: percent of the factory reference beyond which a value is
# marked in the UI. It no longer gates a slider: the zone is a light, inline
# note, not a modal confirmation. Expert mode is the only thing that unlocks
# ranges beyond the normal table below.
RISK_PERCENT = 10.0

# Factory reference for CPU voltage offsets. Skylake desktop VID sits around
# 1.2 V, which matches the design document's reference; there is no reliable
# runtime read of the stock VID, so the constant is the documented baseline.
CPU_VREF_MV = 1200.0

# Tuning modes. Normal is what the hardware tolerates daily; expert is an
# explicit opt-in (`sudo bin/install --expert`, persisted in mode_file) that
# unlocks the widest range the backend can represent. The daemon enforces
# both tables server-side; the UI only mirrors them.
TUNING_MODE_NORMAL = "normal"
TUNING_MODE_EXPERT = "expert"

# CPU offset in mV. MSR 0x150 stores the offset in 1/1.024 mV units in an
# 11-bit two's-complement field, so ±999 mV is the encoding limit. Normal
# undervolt reaches the practical Skylake floor; the overvolt side stays
# conservative. Expert widens both directions.
CPU_OFFSET_RANGE_MV = (-250, 100)
CPU_OFFSET_RANGE_EXPERT_MV = (-500, 200)

# GPU offsets in MHz for a GTX 1060 class card. NVML reports the driver's own
# accept window through nvmlDeviceGet[Gpc|Mem]ClkMinMaxVfOffset; when it does,
# the backend uses it instead of these fallbacks.
GPU_CORE_RANGE_MHZ = (-300, 300)
GPU_CORE_RANGE_EXPERT_MHZ = (-600, 600)
GPU_MEM_RANGE_MHZ = (-1000, 1500)
GPU_MEM_RANGE_EXPERT_MHZ = (-2000, 3000)
GPU_TEMP_RANGE_C = (50, 90)
GPU_TEMP_RANGE_EXPERT_C = (45, 95)
GPU_TEMP_FACTORY_C = 83

# Thermal verification. The daemon samples CPU package and GPU temperature
# during a trial and for a short window after confirm. A tuned component that
# reaches its ceiling, or either component reaching the lower of its ceiling
# and the driver's hard limit, is restored to its previous values.
THERMAL_SAMPLE_MS = 1000
THERMAL_GUARD_MS = 30_000
CPU_THERMAL_CEILING_C = 95
GPU_THERMAL_CEILING_C = 88
THERMAL_HARD_LIMIT_MARGIN_C = 2

# Fan control. NVML reports duty in whole percent and the driver's own
# min/max window. The loop samples the GPU temperature on FAN_SAMPLE_MS and
# writes when the target moved: a move smaller than FAN_DUTY_STEP inside
# FAN_HYSTERESIS_C of the last change is held, a downward change waits
# FAN_SPINDOWN_MS so the fan does not hunt, and an unchanged duty is
# re-asserted every FAN_REASSERT_MS.
FAN_MODES = ("auto", "fixed", "curve")
FAN_SAMPLE_MS = 2000
FAN_HYSTERESIS_C = 2
FAN_DUTY_STEP = 3
FAN_SPINDOWN_MS = 8000
# The same duty is rewritten this often even when the target did not move.
# The helper hands the fan back to the driver when it stops or restarts, and
# a loop that only writes on change would believe its old duty was still
# applied until the temperature (or the target) happened to move.
FAN_REASSERT_MS = 10_000
FAN_FAILURE_LIMIT = 3
# Consecutive refused fan writes before the policy gives up and hands the fan
# to the driver. Long enough to ride out a helper restart (a few seconds at
# one sample every 2 s), short enough to surface a driver that really refuses.
FAN_WRITE_FAILURE_LIMIT = 15
# A fixed speed has no curve to ramp it back up, so normal mode refuses a
# duty below this. Expert mode unlocks the driver's own minimum (usually 0).
FAN_FIXED_MIN_PERCENT = 25
# The curve is a list of points the user places freely: any count inside this
# range, temperatures strictly rising inside the window below. The hot end
# must still reach a useful duty in normal mode, otherwise the curve is a
# silent way to stop the fan.
FAN_CURVE_TEMPS_C = (40, 50, 60, 70, 80)  # the factory points
FAN_CURVE_DEFAULT = (30, 35, 50, 75, 100)
FAN_CURVE_LAST_MIN_PERCENT = 90
FAN_CURVE_MIN_POINTS = 2
FAN_CURVE_MAX_POINTS = 12
# The temperature window a point may sit in. Below 30 °C every curve asks for
# its coldest duty anyway, and above 90 °C the loop's own ceiling takes over.
FAN_CURVE_TEMP_MIN_C = 30
FAN_CURVE_TEMP_MAX_C = 90

_OFFSET_RANGES = {
    ("cpu", "core"): (CPU_OFFSET_RANGE_MV, CPU_OFFSET_RANGE_EXPERT_MV),
    ("cpu", "cache"): (CPU_OFFSET_RANGE_MV, CPU_OFFSET_RANGE_EXPERT_MV),
    ("gpu", "core"): (GPU_CORE_RANGE_MHZ, GPU_CORE_RANGE_EXPERT_MHZ),
    ("gpu", "memory"): (GPU_MEM_RANGE_MHZ, GPU_MEM_RANGE_EXPERT_MHZ),
    ("gpu", "temp"): (GPU_TEMP_RANGE_C, GPU_TEMP_RANGE_EXPERT_C),
}

SERVICE_USER = "omatuner"


def is_expert(mode: str | None) -> bool:
    return mode == TUNING_MODE_EXPERT


def range_for(component: str, key: str, mode: str = TUNING_MODE_NORMAL) -> tuple[int, int]:
    """(min, max) for one control in `mode`, before driver-reported limits."""
    normal, expert = _OFFSET_RANGES.get((component, key), ((0, 0), (0, 0)))
    return expert if is_expert(mode) else normal


def intersect(bounds: tuple[int, int], limit: tuple[int, int] | None) -> tuple[int, int]:
    """Narrow `bounds` by a driver-reported limit, never widening it."""
    if not limit:
        return bounds
    lo = max(bounds[0], limit[0])
    hi = min(bounds[1], limit[1])
    return (lo, hi) if lo <= hi else bounds


def _client_users() -> list[str]:
    raw = os.environ.get("OMATUNER_CLIENT_USERS", "")
    return [name.strip() for name in raw.split(",") if name.strip()]


def is_system_mode() -> bool:
    if os.environ.get("OMATUNER_MODE") == "user":
        return False
    if os.environ.get("OMATUNER_MODE") == "system":
        return True
    try:
        return pwd.getpwuid(os.getuid()).pw_name == SERVICE_USER
    except KeyError:
        return False


class Dirs:
    def __init__(self, system: bool, state_dir: str | None = None, socket_path: str | None = None):
        self.system = system
        # Desktop users granted socket access immediately (access.py). The
        # installer names its own user here because that session's
        # supplementary groups predate the new `omatuner` membership.
        self.client_users = _client_users() if system else []
        if system:
            self.state_dir = state_dir or "/var/lib/omatuner"
            self.socket_path = socket_path or SYSTEM_SOCKET
        else:
            runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
            xdg_state = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
            self.state_dir = state_dir or os.path.join(xdg_state, "omatuner")
            self.socket_path = socket_path or os.path.join(runtime, "omatuner.sock")

    @property
    def defaults_file(self) -> str:
        return os.path.join(self.state_dir, "defaults.json")

    @property
    def last_applied_file(self) -> str:
        return os.path.join(self.state_dir, "last-applied.json")

    @property
    def pending_file(self) -> str:
        return os.path.join(self.state_dir, "pending.json")

    @property
    def paused_file(self) -> str:
        return os.path.join(self.state_dir, "paused.json")

    @property
    def suspended_file(self) -> str:
        return os.path.join(self.state_dir, "suspended.json")

    @property
    def dismissed_file(self) -> str:
        return os.path.join(self.state_dir, "dismissed.json")

    @property
    def clean_shutdown_file(self) -> str:
        return os.path.join(self.state_dir, "clean-shutdown")

    @property
    def events_file(self) -> str:
        return os.path.join(self.state_dir, "events.jsonl")

    @property
    def mode_file(self) -> str:
        """Tuning mode opt-in. `/etc` in system mode so uninstall removes it."""
        if self.system:
            return "/etc/omatuner/mode"
        return os.path.join(self.state_dir, "mode")

    @property
    def last_reverted_file(self) -> str:
        return os.path.join(self.state_dir, "last-reverted.json")

    @property
    def expert_warned_file(self) -> str:
        return os.path.join(self.state_dir, "expert-warned")

    def ensure(self) -> None:
        os.makedirs(self.state_dir, mode=0o750, exist_ok=True)


def read_tuning_mode(dirs: Dirs) -> str:
    """Read the persisted opt-in; anything unreadable means normal mode."""
    try:
        with open(dirs.mode_file, "r", encoding="utf-8") as fh:
            value = fh.read().strip().lower()
    except (OSError, ValueError):
        return TUNING_MODE_NORMAL
    return TUNING_MODE_EXPERT if value in ("expert", "1", "true", "yes") else TUNING_MODE_NORMAL
