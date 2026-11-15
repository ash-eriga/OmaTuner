"""Intel CPU voltage-offset backend over the MSR 0x150 OC mailbox.

Protocol verified against kitsunyan/intel-undervolt and georgewhewell/undervolt:
a write of the read command 0x8000001000000000 | plane<<40 followed by a read
returns the plane's applied offset; a write adds bit 32 and the 11-bit offset
field in bits 31:21 (1 LSB = 1/1.024 mV, two's complement). There is no lock
bit: a firmware that refuses the write shows up as a read-back mismatch.

Voltage planes: 0 = CPU core, 2 = CPU cache (1 is the Intel GPU rail).
Offsets reset on reboot and S3, which is why the daemon reapplies state at
boot and resume.
"""

from __future__ import annotations

import os
import pwd
import struct
from typing import Any

from .base import Backend, BackendError, Control

MSR_OC_MAILBOX = 0x150
PLANE_CORE = 0
PLANE_CACHE = 2

READ_COMMAND = 0x8000001000000000
WRITE_FLAG = 0x100000000
DATA_MASK = 0xFFE00000


def encode_offset(mv: float) -> int:
    # 11-bit two's complement: -1024..1023 raw LSBs, i.e. about ±999 mV.
    raw = int(round(mv * 1.024))
    if not -1024 <= raw <= 1023:
        raise ValueError(
            f"voltage offset {mv} mV is outside the encodable range -1000..999 mV"
        )
    return ((raw & 0x7FF) << 21) & DATA_MASK


def decode_offset(data_dword: int) -> float:
    raw = (data_dword >> 21) & 0x7FF
    # raw == 1024 is -1024, not +1024: the field is 11-bit two's complement.
    signed = raw if raw < 1024 else raw - 2048
    return signed / 1.024


class CpuMsrBackend(Backend):
    component = "cpu"

    def __init__(self, cpu_info: dict[str, Any] | None = None):
        self.cpu_info = cpu_info or {}
        self.present = bool(self.cpu_info.get("intel", True))
        self.available = False
        self.cause: str | None = None
        self.fd: int | None = None
        self.device_name = self.cpu_info.get("name", "")

    def _msr_path(self) -> str:
        return "/dev/cpu/0/msr"

    def _signature(self) -> tuple[int, int, int, int, int] | None:
        """Identity of the device node, or None when absent.

        The inode and rdev are part of it: a module reload recreates the node
        with the same owner and mode, and a daemon that only compared those
        would keep a file descriptor to the dead inode.
        """
        try:
            info = os.stat(self._msr_path())
        except OSError:
            return None
        return (info.st_uid, info.st_gid, info.st_mode, info.st_ino, info.st_rdev)

    def probe(self) -> None:
        self._close()
        self.available = False
        if not self.present:
            self.cause = "no Intel CPU detected"
            return
        path = self._msr_path()
        self.device_present = os.path.exists(path)
        self.device_signature = self._signature()
        if not self.device_present:
            self.cause = (
                "msr kernel module not loaded; install the OmaTuner system service "
                "(bin/install) or run `modprobe msr`"
            )
            return
        try:
            self.fd = os.open(path, os.O_RDWR | os.O_SYNC)
        except PermissionError:
            # The msr driver wants CAP_SYS_RAWIO, but the VFS checks the node's
            # mode first: the service cannot open a 0600 root-owned device, so
            # the install hands the node to the service user with a udev rule.
            self.cause = (
                f"no permission for {path}: the node belongs to "
                f"{self._owner_name()} and the service user cannot open it. "
                "Re-run `sudo bin/install` to apply the udev ownership rule"
            )
            return
        except OSError as exc:
            self.cause = f"cannot open {path}: {exc.strerror or exc}"
            return
        try:
            self._transact(READ_COMMAND | (PLANE_CORE << 40))
        except OSError as exc:
            self.cause = f"OC mailbox did not respond: {exc.strerror or exc}"
            os.close(self.fd)
            self.fd = None
            return
        self.available = True
        if not self.device_name:
            self.device_name = self._cpu_model()

    def _owner_name(self) -> str:
        try:
            info = os.stat(self._msr_path())
            return pwd.getpwuid(info.st_uid).pw_name
        except (OSError, KeyError):
            return "root"

    def _close(self) -> None:
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None

    def refresh(self) -> bool:
        """Follow the device node: loading msr must not need a restart.

        The installer loads the module in the running system, and a kernel
        update or a manual `modprobe` can do the same at any time. A stat(2)
        per call is the whole cost; the probe (the expensive part) only runs
        when the answer the daemon holds is stale — which includes the node
        appearing, disappearing, or being handed to the service user by the
        udev rule the install applies after the load.
        """
        signature = self._signature()
        if signature == self.device_signature:
            return False
        before = (self.available, self.cause)
        self._close()
        self.available = False
        self.cause = None
        # A probe that raised once (a driver hiccup at boot) must not keep
        # poisoning the fresh verdict.
        self.probe_failed = None
        self.probe()
        return (self.available, self.cause) != before

    def _cpu_model(self) -> str:
        try:
            with open("/proc/cpuinfo", "r", encoding="ascii") as fh:
                for line in fh:
                    if line.startswith("model name"):
                        return line.split(":", 1)[1].strip()
        except OSError:
            pass
        return "Intel CPU"

    def _transact(self, value: int) -> int:
        """Write a command to the mailbox and read the response."""
        if self.fd is None:
            raise BackendError("MSR device not open", cause="permission")
        os.pwrite(self.fd, struct.pack("<Q", value), MSR_OC_MAILBOX)
        data = os.pread(self.fd, 8, MSR_OC_MAILBOX)
        if len(data) != 8:
            raise OSError("short read from MSR 0x150")
        return struct.unpack("<Q", data)[0]

    def _read_plane(self, plane: int) -> float:
        response = self._transact(READ_COMMAND | (plane << 40))
        return decode_offset(response & 0xFFFFFFFF)

    def _write_plane(self, plane: int, mv: float) -> None:
        try:
            data = encode_offset(mv)
        except ValueError as exc:
            raise BackendError(str(exc), cause="range") from exc
        command = READ_COMMAND | (plane << 40) | WRITE_FLAG | data
        self._transact(command)
        readback = self._transact(READ_COMMAND | (plane << 40))
        if (readback & DATA_MASK) != data:
            raise BackendError(
                "firmware did not accept the voltage offset (write locked)",
                cause="firmware",
            )

    def controls(self) -> list[Control]:
        from ..config import CPU_VREF_MV, range_for

        available = self.available and self.probe_failed is None
        cause = (None if self.available else self.cause) or self.probe_failed
        core_range = range_for("cpu", "core", self.tuning_mode)
        return [
            Control(
                key=key,
                label=label,
                unit="mV",
                minimum=core_range[0],
                maximum=core_range[1],
                factory=0,
                available=available,
                cause=cause,
                risk_ref=CPU_VREF_MV,
                risk_ref_label="stock voltage",
                risk_direction="both",
            )
            for key, label in (("core", "CPU core"), ("cache", "CPU cache"))
        ]

    def read(self) -> dict[str, int]:
        if not self.available:
            return {}
        core = self._read_plane(PLANE_CORE)
        cache = self._read_plane(PLANE_CACHE)
        return {"core": int(round(core)), "cache": int(round(cache))}

    def apply(self, values: dict[str, int]) -> None:
        if "core" in values:
            self._write_plane(PLANE_CORE, float(values["core"]))
        if "cache" in values:
            self._write_plane(PLANE_CACHE, float(values["cache"]))
