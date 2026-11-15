"""System telemetry from /proc and hwmon, sampled on demand.

The CPU usage rate is a delta between consecutive samples, so the first
sample after daemon start reports nothing rather than an uptime average.
"""

from __future__ import annotations

import os
import time
from typing import Any

# Sentinel for "the scan ran and found no package sensor". Caching it keeps a
# machine without one from rescanning /sys/class/hwmon on every sample.
_NO_PACKAGE_TEMP = object()


class SystemTelemetry:
    def __init__(self) -> None:
        self._cpu_total = 0.0
        self._cpu_idle = 0.0
        self._have_sample = False
        self._package_temp_file: str | None | object = None
        self._energy_uj: int | None = None
        self._energy_ts: float | None = None

    @staticmethod
    def _read_proc_stat() -> tuple[float, float]:
        with open("/proc/stat", "r", encoding="ascii") as fh:
            fields = fh.readline().split()[1:]
        values = [float(v) for v in fields]
        idle = values[3] + (values[4] if len(values) > 4 else 0.0)
        return sum(values), idle

    def cpu_usage(self) -> int | None:
        try:
            total, idle = self._read_proc_stat()
        except (OSError, ValueError, IndexError):
            return None
        if not self._have_sample:
            self._cpu_total, self._cpu_idle, self._have_sample = total, idle, True
            return None
        dt = total - self._cpu_total
        di = idle - self._cpu_idle
        self._cpu_total, self._cpu_idle = total, idle
        if dt <= 0:
            return 0
        return round(100.0 * (1.0 - di / dt))

    def _find_package_temp(self) -> str | None:
        if self._package_temp_file is _NO_PACKAGE_TEMP:
            return None
        if self._package_temp_file is not None:
            return self._package_temp_file
        base = "/sys/class/hwmon"
        try:
            entries = sorted(os.listdir(base))
        except OSError:
            self._package_temp_file = _NO_PACKAGE_TEMP
            return None
        for hwmon in entries:
            try:
                with open(os.path.join(base, hwmon, "name"), "r", encoding="ascii") as fh:
                    if fh.read().strip() not in ("coretemp", "k10temp", "zenpower"):
                        continue
                candidates = []
                for entry in sorted(os.listdir(os.path.join(base, hwmon))):
                    if not entry.startswith("temp") or not entry.endswith("_input"):
                        continue
                    path = os.path.join(base, hwmon, entry)
                    label_path = os.path.join(base, hwmon, entry.replace("_input", "_label"))
                    try:
                        with open(label_path, "r", encoding="ascii") as fh:
                            label = fh.read().strip()
                    except OSError:
                        label = ""
                    if label in ("Package id 0", "Tctl", "Tdie"):
                        self._package_temp_file = path
                        return path
                    candidates.append(path)
                # No package label (some k10temp revisions): the first sensor
                # is the CPU die, and `_input` files sort before `_max`.
                if candidates and hwmon == "k10temp":
                    self._package_temp_file = candidates[0]
                    return self._package_temp_file
            except OSError:
                continue
        self._package_temp_file = _NO_PACKAGE_TEMP
        return None

    def cpu_package_temp(self) -> int | None:
        path = self._find_package_temp()
        if not path:
            return None
        try:
            with open(path, "r", encoding="ascii") as fh:
                return int(round(int(fh.read().strip()) / 1000.0))
        except (OSError, ValueError):
            return None

    def cpu_package_limit(self) -> int | None:
        """Hardware critical limit of the CPU package sensor, in °C."""
        path = self._find_package_temp()
        if not path:
            return None
        base = path[:-len("_input")]
        # `crit` is the emergency limit; `max` is a normal operating bound and
        # using it (84 °C here) as the guard ceiling would revert healthy
        # load. This returns None when the sensor has no `_crit`; the caller
        # then applies the configured ceiling.
        try:
            with open(base + "_crit", "r", encoding="ascii") as fh:
                return int(round(int(fh.read().strip()) / 1000.0))
        except (OSError, ValueError):
            return None

    def cpu_frequency_mhz(self) -> int | None:
        try:
            with open("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq", "r", encoding="ascii") as fh:
                return int(round(int(fh.read().strip()) / 1000.0))
        except (OSError, ValueError):
            return None

    def _read_energy_uj(self) -> int | None:
        try:
            with open("/sys/class/powercap/intel-rapl:0/energy_uj", "r", encoding="ascii") as fh:
                return int(fh.read().strip())
        except (OSError, ValueError):
            return None

    def cpu_power_w(self) -> float | None:
        """Package power from the RAPL energy counter (delta between samples)."""
        energy = self._read_energy_uj()
        now = time.monotonic()
        if energy is None:
            return None
        previous, previous_ts = self._energy_uj, self._energy_ts
        self._energy_uj, self._energy_ts = energy, now
        if previous is None or previous_ts is None or now <= previous_ts:
            return None
        delta = energy - previous
        if delta < 0:  # counter wrapped
            return None
        return round(delta / (now - previous_ts) / 1_000_000.0, 1)

    def ram_usage(self) -> int | None:
        info: dict[str, int] = {}
        try:
            with open("/proc/meminfo", "r", encoding="ascii") as fh:
                for line in fh:
                    parts = line.split()
                    if len(parts) >= 2:
                        info[parts[0].rstrip(":")] = int(parts[1])
        except (OSError, ValueError):
            return None
        total = info.get("MemTotal")
        available = info.get("MemAvailable")
        if not total or available is None:
            return None
        return round(100.0 * (total - available) / total)

    def sample(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "cpuUsage": self.cpu_usage(),
            "cpuTemp": self.cpu_package_temp(),
            "cpuFrequency": self.cpu_frequency_mhz(),
            "cpuPowerW": self.cpu_power_w(),
            "ramUsage": self.ram_usage(),
        }
        # Omit what this machine cannot read instead of sending nulls that
        # the UI would render as an empty row.
        return {key: value for key, value in data.items() if value is not None}
