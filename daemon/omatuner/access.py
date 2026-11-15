"""Immediate socket access for the session that ran the installer.

`sudo bin/install` adds the desktop user to the `omatuner` group, but a
running login session keeps the supplementary groups it started with: the
kernel only learns about a new group membership at the next login. The
control socket is group-readable, so without help the plugin stays offline
(and the design's "no reboot needed" promise fails) until the user logs out
and back in — exactly the restart the install is supposed to avoid.

The group is the authority; this module only saves the session. For each
user named at install time and still present in the group database, the
daemon adds one POSIX ACL entry (`user:<name>:rw-` on the socket,
`user:<name>:--x` on the runtime directory) right after it binds the socket
and before it listens. Access stays kernel-enforced: the entry names exactly
one user, it is reapplied on every start because the socket is recreated,
and removing the user from the group withdraws the grant at the next start.

Nothing here runs in user mode: the socket already belongs to the desktop
user there.
"""

from __future__ import annotations

import grp
import os
import pwd
import stat
import struct

# Linux's system.posix_acl_access encoding: a version-2 header followed by
# (tag, perm, id) entries in the order the kernel requires.
ACL_VERSION = 2
ACL_USER_OBJ = 0x01
ACL_USER = 0x02
ACL_GROUP_OBJ = 0x04
ACL_MASK = 0x10
ACL_OTHER = 0x20
ACL_XATTR = "system.posix_acl_access"

# The named user only needs to traverse the directory to reach the socket,
# and read/write the socket itself. The base owner/group/other entries come
# from the real file mode (stat), never from a hardcoded guess: the ACL must
# not loosen what systemd or the daemon chose.
GRANT_DIR_PERMS = 0o1
GRANT_SOCKET_PERMS = 0o6


def encode(entries: list[tuple[int, int, int]]) -> bytes:
    """Pack access-ACL entries into the xattr value the kernel expects."""
    raw = struct.pack("<I", ACL_VERSION)
    for tag, perm, ident in entries:
        raw += struct.pack("<HHI", tag, perm, ident)
    return raw


def decode(raw: bytes) -> list[tuple[int, int, int]]:
    """Inverse of encode(), used by tests and diagnostics."""
    if len(raw) < 4:
        return []
    version = struct.unpack_from("<I", raw)[0]
    if version != ACL_VERSION:
        raise ValueError(f"unsupported ACL version {version}")
    entries = []
    for offset in range(4, len(raw) - 7, 8):
        entries.append(struct.unpack_from("<HHI", raw, offset))
    return entries


def access_entries(base_mode: int, uids: list[int], perms: int) -> list[tuple[int, int, int]]:
    """Entries for `base_mode` plus one named entry per uid with `perms`.

    The mask has to cover both the group class and the named entries,
    otherwise the kernel silently caps the new grant (or the existing group
    access).
    """
    user_obj = (base_mode >> 6) & 0o7
    group_obj = (base_mode >> 3) & 0o7
    other = base_mode & 0o7
    entries = [(ACL_USER_OBJ, user_obj, 0xFFFFFFFF)]
    entries += [(ACL_USER, perms, uid) for uid in uids]
    entries += [
        (ACL_GROUP_OBJ, group_obj, 0xFFFFFFFF),
        (ACL_MASK, group_obj | perms, 0xFFFFFFFF),
        (ACL_OTHER, other, 0xFFFFFFFF),
    ]
    return entries


def _apply(path: str, entries: list[tuple[int, int, int]]) -> None:
    os.setxattr(path, ACL_XATTR, encode(entries))


def group_members(name: str, group: grp.struct_group) -> bool:
    """True while the group database still lists `name` in `group`."""
    try:
        account = pwd.getpwnam(name)
    except KeyError:
        return False
    return account.pw_gid == group.gr_gid or name in group.gr_mem


def grant(runtime_dir: str, socket_path: str, user_names: list[str],
          group_name: str = "omatuner") -> list[str]:
    """Grant each allowed user access to `socket_path` via `runtime_dir`.

    Returns the names actually granted. A name the group no longer lists is
    skipped; a filesystem without ACL support raises only for the caller to
    decide, since the group path still works after the next login.
    """
    if not user_names:
        return []
    try:
        group = grp.getgrnam(group_name)
    except KeyError:
        return []

    granted: list[str] = []
    uids: list[int] = []
    for name in user_names:
        if name in granted or not group_members(name, group):
            continue
        granted.append(name)
        uids.append(pwd.getpwnam(name).pw_uid)
    if not uids:
        return []

    # One ACL with every named entry: applying them one by one would leave
    # only the last user in the list. The base entries mirror the real modes.
    dir_mode = stat.S_IMODE(os.stat(runtime_dir).st_mode)
    socket_mode = stat.S_IMODE(os.stat(socket_path).st_mode)
    _apply(runtime_dir, access_entries(dir_mode, uids, GRANT_DIR_PERMS))
    _apply(socket_path, access_entries(socket_mode, uids, GRANT_SOCKET_PERMS))
    return granted


def read(path: str) -> list[tuple[int, int, int]]:
    """Current access ACL of `path`, as decoded entries (tests/diagnostics)."""
    return decode(os.getxattr(path, ACL_XATTR))
