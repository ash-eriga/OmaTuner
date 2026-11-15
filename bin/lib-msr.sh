#!/usr/bin/env bash
# Load the msr kernel module into the running kernel.
#
# Sourced by bin/install. Kept in its own file so the load and its
# diagnostics can be exercised without root (the module only loads for real
# when the caller has privileges). The paths are arguments so a test can
# point them at a temporary directory.
#
# modprobe needs no reboot: this is how intel-undervolt and every other MSR
# tool is used. modules-load.d only covers later boots; this call is what
# makes the controls work now.
#
# The module's stderr is captured in a variable, never in a file: a fixed
# /tmp path is refused by fs.protected_regular when the file already exists
# and belongs to someone else, and a failed redirection means the command
# never runs — which is exactly how a real `sudo bin/install` reported a
# stale "Module msr not found" while the module had never been attempted.
#
# Usage: load_msr [sysfs-path] [device-path]

load_msr() {
  local sysfs="${1:-/sys/module/msr}" device="${2:-/dev/cpu/0/msr}"
  local err status

  if [[ -e $sysfs ]]; then
    echo "msr already loaded"
  else
    err="$(modprobe msr 2>&1 >/dev/null)"
    status=$?
    if [[ $status -ne 0 ]]; then
      echo "WARN: could not load the msr module${err:+: $err}" >&2
      local why=1
      if ! modinfo -F filename msr >/dev/null 2>&1; then
        echo "      The kernel package does not ship msr.ko for $(uname -r); install it, then: sudo modprobe msr" >&2
        why=0
      fi
      if [[ ! -d /lib/modules/$(uname -r) ]]; then
        echo "      /lib/modules/$(uname -r) is missing: the running kernel was updated under it." >&2
        echo "      Boot the installed kernel once; nothing else in this install needs a reboot." >&2
        why=0
      fi
      if [[ $why -eq 1 ]] && command -v mokutil >/dev/null 2>&1 && mokutil --sb-state 2>/dev/null | grep -qi enabled; then
        echo "      Secure Boot refuses unsigned modules: sign msr.ko or disable Secure Boot, then: sudo modprobe msr" >&2
        why=0
      fi
      if [[ $why -eq 1 ]]; then
        case "$err" in
          *"Operation not permitted"*|*"Permission denied"*)
            echo "      This process lacks the privilege to load modules. Re-run with root:" >&2
            echo "      sudo modprobe msr   (the service picks it up on its own)" >&2
            ;;
          *"Module msr not found"*)
            echo "      modprobe cannot find msr.ko for $(uname -r); check /lib/modules/$(uname -r)/modules.dep." >&2
            ;;
          *)
            echo "      Retry now with: sudo modprobe msr   (the service picks it up on its own)" >&2
            ;;
        esac
      fi
      return 1
    fi
    echo "msr loaded"
  fi

  # The device nodes follow the module; udev can lag a few milliseconds.
  local i
  for i in $(seq 1 20); do
    [[ -e $device ]] && return 0
    sleep 0.05
  done
  echo "WARN: msr loaded but $device did not appear; check: ls $(dirname "$device")" >&2
  return 1
}
