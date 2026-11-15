#!/usr/bin/env bash
# Wait for a systemd unit to be really running after `systemctl restart`.
#
# Type=exec reports "started" as soon as the process is executed, so a single
# `is-active --quiet` right after the restart answers about the wrong moment:
# a helper that exits at once ("nvmlInit failed") and one that systemd is
# already restarting (Restart=on-failure holds it in "activating
# (auto-restart)" for RestartSec) both read as inactive. Checking again after
# the first interval rules out the second case; only a unit that is still down
# then is worth a warning.
#
# Sourced by bin/install. `systemctl` and `sleep` are looked up on PATH, so a
# test runs the real loop without systemd.
#
# Usage: wait_for_active_unit <unit> [delay-seconds...]

wait_for_active_unit() {
  local unit="$1"
  shift
  local delay
  # No delays means one 1 s check: the default keeps the caller short.
  for delay in "${@:-1}"; do
    sleep "$delay"
    if systemctl is-active --quiet "$unit"; then
      return 0
    fi
  done
  return 1
}
