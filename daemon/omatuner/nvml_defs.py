"""NVML constants, prototypes and the library loader shared by both processes.

`backends/gpu_nvml.py` (the unprivileged daemon) and `fanhelper.py` (the root
helper) talk to the same libnvidia-ml. Keeping the declarations here means
they cannot disagree about a constant or a prototype, and the root helper
never imports a backend: it only needs ctypes and this module.
"""

from __future__ import annotations

import ctypes
from typing import Any

NVML_SUCCESS = 0
# nvmlReturn_t: 3 = NOT_SUPPORTED, 4 = NO_PERMISSION.
NVML_ERROR_NOT_SUPPORTED = 3
NVML_ERROR_NO_PERMISSION = 4

NVML_CLOCK_SM = 1
NVML_CLOCK_MEM = 2

# nvmlFanControlPolicy_t: 0 hands the fan back to the driver's own
# temperature curve, 1 pins it to the duty the caller writes.
NVML_FAN_POLICY_AUTO = 0
NVML_FAN_POLICY_MANUAL = 1

# nvmlTemperatureThresholds_t. The acoustic values are the fan/temperature
# target the driver and nvidia-smi show; writing CURR needs uid 0.
NVML_TEMP_SHUTDOWN = 0
NVML_TEMP_SLOWDOWN = 1
NVML_TEMP_ACOUSTIC_MIN = 4
NVML_TEMP_ACOUSTIC_CURR = 5
NVML_TEMP_ACOUSTIC_MAX = 6

# Returned by set_fan_auto() when the driver exports neither way to hand the
# fan back, so a caller can tell that apart from an NVML error code.
NVML_NO_AUTO_CONTROL = -1

NVML_LIB_CANDIDATES = ("libnvidia-ml.so.1", "libnvidia-ml.so")

_U32 = ctypes.c_uint
_U32_P = ctypes.POINTER(_U32)
_INT_P = ctypes.POINTER(ctypes.c_int)
_VOID_P = ctypes.c_void_p
_VOID_P_P = ctypes.POINTER(_VOID_P)

# The declarations both callers need, byte for byte. Per-caller additions are
# applied through proto() after declare().
COMMON_PROTOTYPES: dict[str, tuple[list, Any]] = {
    "nvmlInit_v2": ([], _U32),
    "nvmlShutdown": ([], _U32),
    "nvmlDeviceGetHandleByIndex_v2": ([_U32, _VOID_P_P], _U32),
    "nvmlDeviceGetNumFans": ([_VOID_P, _U32_P], _U32),
    "nvmlDeviceGetMinMaxFanSpeed": ([_VOID_P, _U32_P, _U32_P], _U32),
    "nvmlDeviceGetFanSpeed_v2": ([_VOID_P, _U32, _U32_P], _U32),
    "nvmlDeviceSetFanSpeed_v2": ([_VOID_P, _U32, _U32], _U32),
    "nvmlDeviceSetDefaultFanSpeed_v2": ([_VOID_P, _U32], _U32),
    "nvmlDeviceSetFanControlPolicy": ([_VOID_P, _U32, ctypes.c_int], _U32),
    "nvmlDeviceGetTemperatureThreshold": ([_VOID_P, ctypes.c_int, _U32_P], _U32),
    # The C signature takes `int *temp`, not an int by value: the driver
    # dereferences the third argument.
    "nvmlDeviceSetTemperatureThreshold": ([_VOID_P, ctypes.c_int, _INT_P], _U32),
}


def load_library(candidates: tuple[str, ...]) -> tuple[Any | None, OSError | None]:
    """Load the first library that exists, or report why none did."""
    err = None
    for candidate in candidates:
        try:
            return ctypes.CDLL(candidate), None
        except OSError as exc:
            err = exc
    return None, err


def proto(lib: Any, name: str, argtypes: list, restype: Any = _U32) -> None:
    """Declare one function's signature, skipping a symbol the driver lacks."""
    fn = getattr(lib, name, None)
    if fn is None:
        return
    fn.argtypes = argtypes
    fn.restype = restype


def declare(lib: Any) -> None:
    for name, (argtypes, restype) in COMMON_PROTOTYPES.items():
        proto(lib, name, argtypes, restype)


def set_fan_auto(lib: Any, device: Any, fan: int) -> int:
    """Return one fan to driver control; the raw NVML code, or the sentinel."""
    if hasattr(lib, "nvmlDeviceSetDefaultFanSpeed_v2"):
        return lib.nvmlDeviceSetDefaultFanSpeed_v2(device, fan)
    if hasattr(lib, "nvmlDeviceSetFanControlPolicy"):
        return lib.nvmlDeviceSetFanControlPolicy(device, fan, NVML_FAN_POLICY_AUTO)
    return NVML_NO_AUTO_CONTROL
