# OmaTuner

OmaTuner tunes Intel CPUs and NVIDIA GPUs for Omarchy users, from a bar widget or a CLI.

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

## What it controls

- Core and cache voltage offsets through MSR 0x150.
- GPU core and memory clock offsets.
- A temperature target and read-only power-limit telemetry through NVML.
- A per-point V/F curve through NvAPI on Pascal.
- GPU fan control.

Hardware or a driver that does not expose a control reports it as unavailable with the cause.

> [!WARNING]
> Undervolting and overclocking can crash the machine. Every change except a factory reset runs for 5 seconds and is reverted unless you keep it.
> The daemon owns that deadline, so a dead interface cannot leave an unconfirmed change applied.

## Contents

- [What it controls](#what-it-controls)
- [Requirements](#requirements)
- [Install](#install)
- [Using it](#using-it)
- [Uninstall](#uninstall)
- [Safety model](#safety-model)
- [Control matrix](#control-matrix)
- [Privileges](#privileges)
- [Troubleshooting](#troubleshooting)
- [Known limitations](#known-limitations)
- [Development](#development)
- [Security](#security)
- [License](#license)

## Requirements

- Omarchy on Arch Linux with Hyprland for the bar widget.
  The daemon and CLI run on Linux; the installer uses systemd.
- Python 3.11 or newer.
- An Intel CPU for voltage offsets, plus the `msr` kernel module and `/dev/cpu/0/msr`.
- The NVIDIA proprietary driver for GPU controls.
  NVML comes from `libnvidia-ml.so.1`; the V/F curve also needs `libnvidia-api.so`.
- Root for installation: it creates a service user, systemd units and a udev rule, and loads `msr`.

## Install

```bash
git clone https://github.com/ash-eriga/OmaTuner.git
cd OmaTuner
sudo bin/install
```

`sudo bin/install --dry-run` prints every action without changing the system.

`sudo bin/install --expert` writes `/etc/omatuner/mode` with `expert`.
A later plain install removes it and returns to normal mode.

The system install creates:

- the `omatuner` system user with home `/var/lib/omatuner` and shell `/usr/sbin/nologin`;
- `/usr/local/lib/omatuner` with the daemon and `/usr/local/bin/omatuner` as the CLI;
- `omatuner.service`, running as `omatuner` with only `CAP_SYS_ADMIN` and `CAP_SYS_RAWIO`;
- `omatuner-fan.service`, the root helper for fan and temperature-target writes;
- `/etc/modules-load.d/omatuner.conf` and the udev rule `/etc/udev/rules.d/90-omatuner-msr.rules`, which gives `/dev/cpu/*/msr` to the `omatuner` user in mode 0600;
- the sleep hook `/usr/lib/systemd/system-sleep/omatuner.sh`;
- the plugin `ash.omatuner` in the bar, with a plugin rescan of the running shell, and a socket ACL for the installing session.

It adds your user to the `omatuner` group and loads `msr` now.
If the module refuses, the installer prints the cause.

The daemon grants the current session a socket ACL, so a new login is not required.

If the installer cannot find a desktop user, it says so; run `bin/install-ui` as that user.

Without root, run `bin/install-ui` as your desktop user.
It installs the plugin and a user-mode daemon under `systemctl --user`; privileged controls stay unavailable with their causes.

## Using it

1. Click the OmaTuner chip in the bar to open the panel. It has Monitor, CPU and GPU tabs.
2. Edit a value, then press **Apply**. The change runs for 5 seconds with a countdown.
3. Press **Keep** (or Enter) to keep it, or **Revert** (or Esc) to restore the previous values.

With **Keep after restart** checked, a kept change is also saved as the default and reapplied at the next clean boot.
A revert does not save the new values.

**Factory** stages the factory values.
Applying them is a reset, which takes effect without the 5-second trial.

The GPU tab also holds the fan control:

- **Auto** leaves the fan to the driver.
- **Fixed** holds one duty.
- **Curve** interpolates between points from 30 to 90 °C.

The curve editor accepts 2 to 12 rising points.

Expert mode is an install-time opt-in.
The panel shows its warning once.
Normal ranges are the daily limits; expert ranges are the wider tables below, and the daemon enforces both tables on every request.

### CLI

The CLI talks to the same daemon.

| Command | What it does |
| --- | --- |
| `omatuner status [--json]` | Prints the state and whether a daemon is listening. `--json` prints it as JSON. |
| `omatuner doctor` | Reports whether `msr` is loaded, plus the device names and the backend cause. |
| `omatuner apply --component cpu core=-50 cache=-30` | Applies integer `key=value` settings to one component. |
| `omatuner confirm` | Keeps the pending apply; with `--mode default` the values are also saved for boot. |
| `omatuner revert` | Restores the previous values and does not save the new ones. |
| `omatuner apply --component gpu --mode reset` | Resets the component to factory values. |
| `omatuner keep-paused` | Keeps the components paused after the boot notice. |
| `omatuner dismiss-notice` | Dismisses the notice. |

Apply options:

- `apply` takes `--component cpu|gpu` (required) and `--mode session|default|reset` (default `session`).
- Settings are integer `key=value` pairs.
- `--mode reset` needs `--component` too, for example `omatuner apply --component cpu --mode reset`.
- A non-reset apply waits for `omatuner confirm` or `omatuner revert` within its 5 seconds.
- `--mode default` saves the values on confirm; `--mode session` does not save them for boot.
- The CLI carries numeric controls only; the fan policy and V/F points are applied from the panel.

## Uninstall

```bash
sudo bin/uninstall
```

- Resets CPU and GPU to factory values through the running daemon.
- Stops `omatuner-fan.service` first, so the helper hands the fan back to the driver.
- Removes both units, `/usr/local/bin/omatuner`, `/usr/local/lib/omatuner`, `/var/lib/omatuner`, `/run/omatuner`, `/run/omatuner-fan`, `/etc/omatuner/mode`, the `msr` boot config, the sleep hook and the udev rules.
- Removes the service user and group, and the desktop user's plugin.

The `msr` module stays loaded until the next boot because other tools may use it.
`sudo modprobe -r msr` unloads it now.

If the daemon cannot be started, the script cannot reset the offsets and says they will clear at the next power cycle.

For a user-mode install, run `bin/uninstall-ui` as the desktop user.
It removes the user daemon, the plugin, the bar entry, the state and the socket.
If it finds system-mode files, it prints the root commands instead of failing.

## Safety model

- The recovery record with the previous values reaches disk before any hardware write. A failed write attempts to restore the previous values and reports an uncertain state if the restore also fails.
- An unconfirmed trial reverts when its 5-second deadline expires. The deadline runs in the daemon, not in the panel.
- An interrupted apply is discarded on the next boot and never reapplied. If a pending record is malformed, the daemon drops it and journals the rejection.
- After an unclean shutdown the daemon treats both components as paused: an orderly stop writes `clean-shutdown`; a missing marker means abrupt. It shows a notice with a 5-second countdown. At the end of the countdown the defaults return; **Keep paused** or Esc keeps them paused. A first run with no state is treated as clean.
- A kernel Xid or machine-check error after a confirmed adjustment suspends that component's saved default as a precaution and records the evidence. The message says the error is suspected, not proven.
- The thermal guard samples CPU package and GPU temperature once per second during a trial and for 30 seconds after a confirm. The CPU ceiling is the lower of 95 °C and the hwmon limit minus 2 °C. The GPU ceiling is at least 88 °C and at least the target plus 2 °C, and the driver slowdown minus 2 °C always wins when it is lower. A breach reverts the trial, or restores the previous values after a confirm.
- Every value is validated against the active mode's range in the daemon. The panel only mirrors the same limits.
- State files are validated when loaded: a malformed shape is journaled and replaced by the fallback, and a saved control record with a value that is not a whole number is dropped. A bad file cannot crash-loop the service.
- Another tuning program managing the same component blocks an apply until it is stopped or disabled. OmaTuner never stops it for you.

## Control matrix

| Control | Backend | Unit | Normal range | Expert range |
| --- | --- | --- | --- | --- |
| CPU core offset | MSR 0x150 | mV | -250 … +100 | -500 … +200 |
| CPU cache offset | MSR 0x150 | mV | -250 … +100 | -500 … +200 |
| GPU core clock offset | NVML | MHz | -300 … +300, intersected with the driver window | driver window (fallback -600 … +600) |
| GPU memory clock offset | NVML | MHz | -1000 … +1500, intersected with the driver window | driver window (fallback -2000 … +3000) |
| GPU temperature target | NVML threshold API | °C | 50 … 90, intersected with the driver window | 45 … 95, intersected with the driver window |
| GPU fan, fixed duty | NVML through the root helper | % | at least 25 … driver maximum | driver minimum … driver maximum |
| GPU fan, curve | NVML through the root helper | % at °C | 2 … 12 points, 30 … 90 °C, rising duty, last point at least 90% | same, without the last-point floor |

<details>
<summary>Where the clock and temperature ranges come from</summary>

The clock ranges come from `nvmlDeviceGetGpcClkMinMaxVfOffset` and `nvmlDeviceGetMemClkMinMaxVfOffset`.

- A normal clock range is intersected with the driver window and never widened.
- In expert mode the driver window becomes the range.
- The temperature target is intersected with the driver window when the driver reports one.
- The constants above are the fallbacks when the driver does not answer.

</details>

## Privileges

### omatuner.service

| Property | Value |
| --- | --- |
| User | the unprivileged `omatuner` user |
| Capabilities | only `CAP_SYS_ADMIN` and `CAP_SYS_RAWIO` |
| Sandbox | systemd hardening, listed below |
| State and runtime | `/var/lib/omatuner` and `/run/omatuner` |
| Socket and mode | `/run/omatuner/omatuner.sock`, mode 0660, group `omatuner` |

<details>
<summary>Sandbox options</summary>

- `NoNewPrivileges=yes`
- `ProtectSystem=strict`
- `ProtectHome=yes`
- `PrivateTmp=yes`
- `ProtectKernelTunables=yes`, `ProtectKernelModules=yes`, `ProtectKernelLogs=yes`
- `ProtectControlGroups=yes`, `ProtectClock=yes`, `ProtectHostname=yes`
- `RestrictAddressFamilies=AF_UNIX`

</details>

The installer adds your user to that group.
The daemon also grants the installing session a named ACL entry (`rw` on the socket, `x` on the runtime directory), so no new login is needed.
Anyone in the `omatuner` group can apply tuning.

### omatuner-fan.service

`omatuner-fan.service` is the only installed service that runs as root.
Driver 580 refuses the fan setters and the temperature threshold to any other UID, even with the same capabilities.

| Property | Value |
| --- | --- |
| User | root |
| Capabilities | `CAP_SYS_ADMIN` and `CAP_SYS_RAWIO` |
| Sandbox | the same options as `omatuner.service` |
| Skipped when | `/dev/nvidiactl` does not exist |
| Socket and mode | `/run/omatuner-fan/fan.sock` (`root:omatuner`, mode 0660) |

### Helper protocol

Every helper connection is checked with `SO_PEERCRED` in the accept loop.
Only root and the `omatuner` user are served; group membership alone is not enough.

The helper accepts:

- `hello` (fan limits, speed, temperature target and range)
- `set-duty`
- `auto`
- `set-temp`

It caps a request at 64 KiB and serves at most 8 clients at once.
It hands the fan back to the driver when its client disconnects or when it receives SIGTERM.
A daemon crash therefore restores the fan by itself.

### Daemon socket

The daemon accepts newline-delimited JSON on its socket, with a 1 MiB buffer per client.
It has no network socket.

The `msr` nodes belong to the `omatuner` user in mode 0600, not to the group; writing still requires `CAP_SYS_RAWIO`.

## Troubleshooting

- **The panel says the service is offline.**
  - Check `systemctl status omatuner.service` and `journalctl -u omatuner.service -n 30 --no-pager`.
  - For a user-mode install, use `systemctl --user status omatuner.service`.
  - The CLI prints the same distinction: no daemon is listening.
- **CPU controls are unavailable.**
  - `omatuner doctor` reports whether `msr` is loaded.
  - If `/dev/cpu/0/msr` is missing, run `sudo modprobe msr`; the daemon notices the node without a restart.
  - If the node exists but belongs to another user, re-run `sudo bin/install` to apply the udev ownership rule.
  - A module that refuses to load under Secure Boot must be signed, or Secure Boot disabled.
- **Fan and temperature-target writes are unavailable.**
  - Check `systemctl status omatuner-fan.service` and `journalctl -u omatuner-fan -n 30`.
  - Without `/dev/nvidiactl` the unit is skipped; without the helper the daemon keeps reading the fan but reports the writes as unavailable with the cause.
- **The GPU is not detected.**
  - `omatuner doctor` reports the device names and the backend cause.
  - Install the NVIDIA proprietary driver. Without `libnvidia-ml.so.1` the daemon reports `libnvidia-ml not found`, and without a device it reports `no NVIDIA device`.
- **Apply is refused because of a conflict.**
  LACT, CoreCtrl, GWE or tuxclockerd is running and manages the same component. Stop or disable it, then apply again.
- **The chip is in the bar but offline.**
  Run `omarchy-shell shell rescanPlugins` in the desktop session, or re-run `bin/install-ui`. The plugin and the daemon use the same socket.

## Known limitations

- The GPU power limit is read-only; OmaTuner reports it but does not change it.
- The NVIDIA driver requires root for fan control and the temperature target, which is why those writes go through a root helper. Clock offsets go through the unprivileged daemon.
- The V/F curve uses undocumented NvAPI functions from `libnvidia-api.so`. If a driver changes the call IDs or structures, the curve reports unavailable with a cause. When the driver refuses `SetControl`, the curve remains readable but read-only.
- The 30-second thermal guard lives in the daemon's memory. Restarting the daemon immediately after a confirm loses that remaining window.
- Only `/dev/cpu/0/msr` is written. On a dual-socket machine the second CPU package keeps its stock voltage.
- The factory temperature target is the constant 83 °C, not a per-card read; the driver exposes no query for the original acoustic target.
- The aggressive-zone mark is a fixed 10% beyond the factory reference and is informational only. It does not block a value.

## Development

```bash
PYTHONPATH=daemon python3 -m unittest discover -s tests -v
node tests/test_model.mjs
bin/validate
```

The GitHub workflow is manual (`workflow_dispatch`), and `bin/validate` is the local gate.
`bin/validate` runs every check the machine can run and reports skips.
The real-hardware tests skip outside the reference machine, and the QML harness needs `qmltestrunner`.

Repository layout:

```text
daemon/omatuner/   daemon, state machine, backends, fan policy, root helper
ui/ash.omatuner/   Qt Quick bar plugin
bin/               install, uninstall, CLI wrapper and validation scripts
tests/             Python tests, QML harnesses and Model.js checks
.github/           CI workflow
```

Release notes are in [CHANGELOG.md](CHANGELOG.md).

## Security

The daemon runs unprivileged; the root helper serves only root and the `omatuner` user over `SO_PEERCRED`.
The threat model, deliberate limits and reporting instructions are in [SECURITY.md](SECURITY.md).

## License

MIT. See [LICENSE](LICENSE) for the full text.
