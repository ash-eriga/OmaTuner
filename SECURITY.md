# Security

OmaTuner writes to hardware: CPU voltage offsets through MSR and clocks,
fan duty and thermal target through NVML. This document says who can do what,
where the limits are, and what is out of scope.

## Trust model

- **Daemon (`omatuner.service`)** — runs as the `omatuner` system user, never
  as UID 0, with exactly `CAP_SYS_ADMIN` (NVML clock offsets, fan) and
  `CAP_SYS_RAWIO` (MSR), Nice −20, and a sandboxed unit
  (`ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`, `NoNewPrivileges`,
  `StateDirectory=omatuner`, `RestrictAddressFamilies=AF_UNIX`).
- **Root helper (`omatuner-fan.service`)** — runs as root with the same two
  capabilities. It exists because driver 580 refuses
  `nvmlDeviceSetFanSpeed_v2`, `nvmlDeviceSetDefaultFanSpeed_v2` and
  `nvmlDeviceSetTemperatureThreshold` from any UID other than 0 (measured on
  the reference machine). Its surface is minimal: count fans, read limits and
  speed, write a duty, hand the fan back to auto, write the thermal target.
- **Control socket** (`/run/omatuner/omatuner.sock`) — owned by `omatuner`,
  mode 0660, group `omatuner`. `sudo bin/install` adds the installing user to
  that group and, for the session already open (which does not inherit the new
  group), the daemon adds a named ACL entry on the socket and the runtime
  directory. The entry is reapplied on every start and stops applying once the
  user leaves the group. **Anyone in the `omatuner` group can apply tuning**:
  the group is the authority, not a sandbox.
- **Helper socket** (`/run/omatuner-fan/fan.sock`) — `root:omatuner` 0660, and
  every connection is checked by `SO_PEERCRED`: only root and the `omatuner`
  user are served. The check runs in the accept loop, before any worker
  thread exists, so a refused peer costs a bounded drain and nothing more; at
  most `MAX_CLIENTS` (8) clients are served at once. Group membership alone
  does not drive the helper.

## Surface and deliberate limits

- The daemon accepts newline-delimited JSON only, with a 1 MiB buffer per
  client; a malformed line, valid JSON that is not an object, or a request
  whose `params` is not an object is dropped without taking the service down.
- The helper caps a request at 64 KiB, refuses frames that are not objects,
  answers a frame that is not UTF-8 with a `bad-request` error instead of
  dropping the connection, and bounds how long it drains a refused peer.
- The protocol has no authentication beyond the socket: authorization comes
  from the filesystem and from `SO_PEERCRED`, and the daemon process is the
  trust boundary.
- The udev rule hands `/dev/cpu/*/msr` to the `omatuner` user (the node stays
  0600; the group gains no read access). Whoever controls the daemon can write
  arbitrary MSRs: that is inherent to the product, not a permission bug.
- No sensitive state is stored: the files in `/var/lib/omatuner` are tuning
  values, the event journal and recovery markers.

## Out of scope

- local root, a compromised kernel, a malicious NVIDIA driver or firmware;
- Omarchy and the shell that hosts the plugin;
- other tuning software competing for the same hardware (the helper only hands
  the fan back to the driver when it was the one that set it to manual).

## Reporting

Open a **private security advisory** on the GitHub repository (*Security* tab →
*Advisories* → *Report a vulnerability*). Do not open a public issue with
exploit details. Include the driver, GPU, distribution, the output of
`omatuner doctor` and the steps to reproduce.

Supported versions: 0.1.x. Fixes land on the default branch and in a new release;
there is no backport to older versions.
