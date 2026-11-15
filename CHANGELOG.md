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
