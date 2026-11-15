#!/usr/bin/env bash
# Service account lifecycle for the OmaTuner system install.
#
# `useradd --user-group` refuses to run when a group with that name already
# exists: "group omatuner exists - if you want to add this user to that
# group, use -g". That state is what a previous uninstall leaves behind —
# userdel removes a private group only when USERGROUPS_ENAB=yes *and* the
# group has no members left, and the desktop user is a member (that is how
# it reaches the control socket). Failing the whole install over it, or
# telling the user to clean up by hand, would be the same "you must fix your
# machine first" answer this project exists to avoid: reuse the group.
#
# Sourced by bin/install and bin/uninstall. When the caller defines `run`
# (both do, for --dry-run), it goes through it, so a dry run stays a dry run.

_omatuner_runner() {
  if declare -F run >/dev/null 2>&1; then
    run "$@"
  else
    "$@"
  fi
}

ensure_service_user() {
  local user="${1:-omatuner}" home="${2:-/var/lib/omatuner}"

  if getent passwd "$user" >/dev/null 2>&1; then
    echo "user $user already exists"
    return 0
  fi

  if getent group "$user" >/dev/null 2>&1; then
    # Reuse the existing group: creating a second one is impossible, and
    # picking a different gid would split socket ownership from the members
    # the socket permission is granted to. --gid keeps the leftover gid.
    echo "group $user exists without its user (left by a previous uninstall); reusing it"
    _omatuner_runner useradd --system --gid "$user" --home-dir "$home" \
      --create-home --shell /usr/sbin/nologin "$user"
    return 0
  fi

  _omatuner_runner useradd --system --user-group --home-dir "$home" \
    --create-home --shell /usr/sbin/nologin "$user"
}

remove_service_user() {
  local user="${1:-omatuner}" member="${2:-}"

  # Drop the desktop user from the group first: while it is a member the
  # group cannot disappear, and a group that outlives its user is exactly
  # what breaks the next install.
  if [[ -n "$member" && "$member" != root ]] && getent group "$user" >/dev/null 2>&1; then
    _omatuner_runner gpasswd -d "$member" "$user" >/dev/null 2>&1 || true
  fi

  if getent passwd "$user" >/dev/null 2>&1; then
    # A userdel that fails (a process still running as the account) must not
    # abort the rest of the uninstall; say so and carry on.
    if ! _omatuner_runner userdel -r "$user"; then
      echo "NOTE: could not remove user $user (a process may still use it); the group is left too." >&2
      return 0
    fi
  else
    echo "user $user already absent"
  fi

  if getent group "$user" >/dev/null 2>&1; then
    # groupdel refuses when another account uses this gid as its primary
    # group; that is a real ownership conflict, not an uninstall failure.
    if _omatuner_runner groupdel "$user" 2>/dev/null; then
      echo "group $user removed"
    else
      echo "NOTE: group $user is still another account's primary group; left in place." >&2
    fi
  fi
}
