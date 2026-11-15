# Changelog

## 0.1.0

- Intel CPU core and cache voltage offsets through MSR 0x150, with normal and expert ranges.
- NVIDIA GPU core and memory clock offsets and a temperature target through NVML.
- Per-point V/F curve editing through NvAPI on Pascal.
- GPU fan control with Auto, Fixed and Curve modes through a minimal root helper.
- Five-second trial with Keep or automatic revert, saved defaults reapplied at boot, and abrupt-shutdown pausing.
- Thermal guard during trials and for 30 seconds after a confirm, plus preventive suspension on kernel Xid or machine-check evidence.
- Omarchy bar widget with Monitor, CPU and GPU tabs, and a CLI for the same daemon.
- System install with a dedicated service user, sandboxed units, udev ownership for the MSR nodes, a sleep hook and uninstall scripts.
- Replayed values are checked against the active range before they are written.
- The thermal guard is reported as unarmed, with a cause, when its sensor is unreadable.
- Firmware voltage locks are detected after the first refused apply, and the CPU controls are disabled with the reason.
- Kernel lockdown and virtual machines report why an MSR write is refused.
- The V/F curve is reported unavailable when more than one NVIDIA GPU is present.
- The fan write probe always hands the fan back to the driver and retries that hand-back once.
- A driver `NOT_SUPPORTED` answer is reported as the control's cause.
- The factory temperature target is clamped to the driver's window.
- The service account shell is resolved from the system's `nologin` locations instead of one hardcoded path.
- `bin/validate` counts skipped checks in its summary.
