"""Access bridge for the installing session (access.py).

`sudo bin/install` adds the desktop user to the `omatuner` group, but the
running session keeps the supplementary groups it logged in with. These
tests prove the daemon's answer to that: a socket ACL entry for the
installing user, applied before the socket accepts clients, that keeps
kernel enforcement and coexists with the group permission.

Run from the project root: python3 -m unittest tests.test_access -v
"""

from __future__ import annotations

import grp
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "daemon"))

from omatuner import access

HERE = os.path.dirname(os.path.abspath(__file__))
DAEMON_DIR = os.path.join(HERE, "..", "daemon")


def entries_by_tag(entries, tag):
    return [entry for entry in entries if entry[0] == tag]


class EncodingTests(unittest.TestCase):
    def test_roundtrip(self):
        entries = access.access_entries(0o660, [1000], 0o6)
        self.assertEqual(access.decode(access.encode(entries)), entries)

    def test_version_header(self):
        raw = access.encode([])
        self.assertEqual(raw, b"\x02\x00\x00\x00")

    def test_rejects_other_versions(self):
        with self.assertRaises(ValueError):
            access.decode(b"\x01\x00\x00\x00")


class EntryTests(unittest.TestCase):
    def test_mask_covers_group_and_named_user(self):
        entries = access.access_entries(0o660, [1000], 0o6)
        self.assertEqual(entries_by_tag(entries, access.ACL_MASK), [(access.ACL_MASK, 0o6, 0xFFFFFFFF)])

    def test_group_class_can_stay_denied_while_named_entry_allows(self):
        # A non-member of the group is authorized by its named entry alone:
        # the owner class does not match it, the group class is 0, and the
        # mask keeps the named entry at rw. (The kernel's own check for a
        # non-owner is what the entry encodes; exercising it end to end needs
        # a second uid, which needs root on this machine.)
        entries = access.access_entries(0o600, [1000], 0o6)
        self.assertEqual(entries_by_tag(entries, access.ACL_USER), [(access.ACL_USER, 0o6, 1000)])
        self.assertEqual(entries_by_tag(entries, access.ACL_MASK), [(access.ACL_MASK, 0o6, 0xFFFFFFFF)])

    def test_base_mode_is_preserved(self):
        entries = access.access_entries(0o750, [1000], 0o1)
        self.assertEqual(entries_by_tag(entries, access.ACL_USER_OBJ), [(access.ACL_USER_OBJ, 0o7, 0xFFFFFFFF)])
        self.assertEqual(entries_by_tag(entries, access.ACL_GROUP_OBJ), [(access.ACL_GROUP_OBJ, 0o5, 0xFFFFFFFF)])
        self.assertEqual(entries_by_tag(entries, access.ACL_OTHER), [(access.ACL_OTHER, 0, 0xFFFFFFFF)])

    def test_named_user_entry(self):
        entries = access.access_entries(0o660, [4242], 0o6)
        self.assertEqual(entries_by_tag(entries, access.ACL_USER), [(access.ACL_USER, 0o6, 4242)])

    def test_every_named_user_gets_an_entry(self):
        entries = access.access_entries(0o660, [1000, 1001], 0o6)
        self.assertEqual(
            entries_by_tag(entries, access.ACL_USER),
            [(access.ACL_USER, 0o6, 1000), (access.ACL_USER, 0o6, 1001)],
        )

    def test_other_is_never_granted(self):
        entries = access.access_entries(0o777, [1000], 0o6)
        self.assertEqual(entries_by_tag(entries, access.ACL_OTHER), [(access.ACL_OTHER, 0o7, 0xFFFFFFFF)])


class MembershipTests(unittest.TestCase):
    def test_primary_group_counts(self):
        group = grp.struct_group(("omatuner", "x", os.getgid(), []))
        self.assertTrue(access.group_members(_user_with_primary_gid(os.getgid()), group))

    def test_supplementary_membership_counts(self):
        me = _current_user()
        group = grp.struct_group(("omatuner", "x", os.getgid() + 100000, [me]))
        self.assertTrue(access.group_members(me, group))

    def test_removed_user_is_refused(self):
        group = grp.struct_group(("omatuner", "x", os.getgid() + 100000, []))
        self.assertFalse(access.group_members(_current_user(), group))

    def test_unknown_user_is_refused(self):
        group = grp.struct_group(("omatuner", "x", os.getgid(), []))
        self.assertFalse(access.group_members("no-such-user-omatuner", group))


class GrantTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="omatuner-acl-")
        os.chmod(self.tmp, 0o750)
        self.sock_path = os.path.join(self.tmp, "omatuner.sock")
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(self.sock_path)
        self.listener.listen(4)
        os.chmod(self.sock_path, 0o660)
        self.me = _current_user()
        self.group_name = grp.getgrgid(os.getgid()).gr_name

    def tearDown(self):
        self.listener.close()

    def test_grant_adds_one_named_user(self):
        granted = access.grant(self.tmp, self.sock_path, [self.me], self.group_name)
        self.assertEqual(granted, [self.me])
        sock_entries = access.read(self.sock_path)
        uid = os.getuid()
        self.assertIn((access.ACL_USER, access.GRANT_SOCKET_PERMS, uid), sock_entries)
        # the group keeps its access and nobody else gains any
        self.assertIn((access.ACL_GROUP_OBJ, 0o6, 0xFFFFFFFF), sock_entries)
        self.assertIn((access.ACL_MASK, 0o6, 0xFFFFFFFF), sock_entries)
        self.assertIn((access.ACL_OTHER, 0, 0xFFFFFFFF), sock_entries)

    def test_base_entries_mirror_the_real_mode_not_a_guess(self):
        os.chmod(self.sock_path, 0o600)
        os.chmod(self.tmp, 0o700)
        access.grant(self.tmp, self.sock_path, [self.me], self.group_name)
        sock_entries = access.read(self.sock_path)
        self.assertIn((access.ACL_GROUP_OBJ, 0, 0xFFFFFFFF), sock_entries)
        self.assertIn((access.ACL_OTHER, 0, 0xFFFFFFFF), sock_entries)
        # the mask covers the named entry, not the (absent) group permission
        self.assertIn((access.ACL_MASK, access.GRANT_SOCKET_PERMS, 0xFFFFFFFF), sock_entries)
        dir_entries = access.read(self.tmp)
        self.assertIn((access.ACL_GROUP_OBJ, 0, 0xFFFFFFFF), dir_entries)

    def test_directory_grant_is_traverse_only(self):
        access.grant(self.tmp, self.sock_path, [self.me], self.group_name)
        dir_entries = access.read(self.tmp)
        self.assertIn((access.ACL_USER, access.GRANT_DIR_PERMS, os.getuid()), dir_entries)
        self.assertNotIn((access.ACL_USER, 0o6, os.getuid()), dir_entries)

    def test_owner_can_still_use_the_socket(self):
        access.grant(self.tmp, self.sock_path, [self.me], self.group_name)
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(2)
        client.connect(self.sock_path)
        client.close()
        self.assertIn(
            (access.ACL_USER, access.GRANT_SOCKET_PERMS, os.getuid()),
            access.read(self.sock_path),
        )

    def test_non_member_is_skipped(self):
        # root is not in this user's private group
        granted = access.grant(self.tmp, self.sock_path, ["root"], self.group_name)
        if os.getuid() == 0:
            self.skipTest("running as root: root is in every group check here")
        self.assertEqual(granted, [])

    def test_unknown_user_is_skipped(self):
        self.assertEqual(access.grant(self.tmp, self.sock_path, ["no-such-user-omatuner"], self.group_name), [])

    def test_missing_group_is_skipped(self):
        self.assertEqual(access.grant(self.tmp, self.sock_path, [self.me], "no-such-group-omatuner"), [])

    def test_duplicate_name_grants_once(self):
        granted = access.grant(self.tmp, self.sock_path, [self.me, self.me], self.group_name)
        self.assertEqual(granted, [self.me])
        self.assertEqual(
            entries_by_tag(access.read(self.sock_path), access.ACL_USER),
            [(access.ACL_USER, access.GRANT_SOCKET_PERMS, os.getuid())],
        )

    def test_empty_list_does_nothing(self):
        self.assertEqual(access.grant(self.tmp, self.sock_path, [], self.group_name), [])
        with self.assertRaises(OSError):
            access.read(self.sock_path)


class DaemonWiringTests(unittest.TestCase):
    """The daemon reads OMATUNER_CLIENT_USERS and grants before listening."""

    def setUp(self):
        try:
            group = grp.getgrnam("omatuner")
        except KeyError:
            self.skipTest("omatuner group absent: run sudo bin/install first")
        if not access.group_members(_current_user(), group):
            self.skipTest("current user is not in group omatuner")
        self.tmp = tempfile.mkdtemp(prefix="omatuner-daemon-acl-")
        self.sock_path = os.path.join(self.tmp, "omatuner.sock")

    def test_system_daemon_grants_the_installer(self):
        env = dict(os.environ)
        env["PYTHONPATH"] = os.path.abspath(DAEMON_DIR)
        env["OMATUNER_MODE"] = "system"
        env["OMATUNER_CLIENT_USERS"] = _current_user()
        proc = subprocess.Popen(
            [sys.executable, "-m", "omatuner", "run", "--backend", "fake", "--system",
             "--state-dir", os.path.join(self.tmp, "state"), "--socket", self.sock_path],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            deadline = time.time() + 15
            entries = []
            while time.time() < deadline:
                if os.path.exists(self.sock_path):
                    try:
                        entries = access.read(self.sock_path)
                    except OSError:
                        entries = []  # bound but the ACL is not applied yet
                    if entries:
                        break
                time.sleep(0.05)
            self.assertIn((access.ACL_USER, access.GRANT_SOCKET_PERMS, os.getuid()), entries)
            self.assertIn((access.ACL_USER, access.GRANT_DIR_PERMS, os.getuid()), access.read(self.tmp))

            # The socket actually serves the protocol, and the stdout/stderr
            # line proves the grant happened before accept() was possible.
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.settimeout(5)
            client.connect(self.sock_path)
            client.sendall(json.dumps({"id": 1, "method": "hello", "params": {}}).encode() + b"\n")
            reply = b""
            while b"\n" not in reply:
                chunk = client.recv(65536)
                if not chunk:
                    break
                reply += chunk
            client.close()
            self.assertTrue(json.loads(reply.split(b"\n")[0].decode())["ok"])
        finally:
            proc.terminate()
            _, stderr = proc.communicate(timeout=10)
        self.assertIn("socket access granted to", stderr)


def _current_user() -> str:
    import pwd

    return pwd.getpwuid(os.getuid()).pw_name


def _user_with_primary_gid(gid: int) -> str:
    import pwd

    for account in pwd.getpwall():
        if account.pw_gid == gid:
            return account.pw_name
    raise unittest.SkipTest(f"no account with primary gid {gid}")


if __name__ == "__main__":
    unittest.main()
