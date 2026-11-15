"""Live hardware pickup: a running daemon must not need a restart.

`probe()` runs once at startup, but the environment it judges can change
while the daemon runs: `sudo bin/install` loads the `msr` module into the
live kernel, and the NVIDIA driver can come up after the unit starts. These
tests cover the re-probe path — one cheap stat per second, an actual probe
only when the answer the daemon holds is stale, and the state push that
makes the UI show the controls immediately.

Run from the project root: python3 -m unittest tests.test_refresh -v
"""

from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "daemon"))

from omatuner import config
from omatuner.backends import cpu_msr
from omatuner.backends.cpu_msr import CpuMsrBackend
from omatuner.backends.fake import FakeBackend
from omatuner.core import TunerCore
from omatuner.daemon import Server
from omatuner.statestore import Journal


class PathCpu(CpuMsrBackend):
    """CPU backend bound to a temporary file instead of /dev/cpu/0/msr."""

    def __init__(self, path: str):
        super().__init__({"intel": True, "name": "test CPU"})
        self.path = path
        self.probes = 0

    def _msr_path(self) -> str:
        return self.path

    def probe(self) -> None:
        self.probes += 1
        super().probe()


class CpuRefreshTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="omatuner-msr-")
        self.path = os.path.join(self.tmp, "msr")
        self.backend = PathCpu(self.path)
        self.backend.probe()

    def tearDown(self):
        if self.backend.fd is not None:
            os.close(self.backend.fd)

    def test_absent_device_is_not_re_probed(self):
        self.assertFalse(self.backend.available)
        self.assertIn("msr kernel module not loaded", self.backend.cause)
        self.assertFalse(self.backend.refresh())
        self.assertEqual(self.backend.probes, 1)

    def test_module_appearing_is_picked_up_once(self):
        with open(self.path, "wb") as fh:
            fh.write(b"\0" * 8)
        self.assertTrue(self.backend.refresh())
        self.assertEqual(self.backend.probes, 2)
        self.assertTrue(self.backend.available)
        self.assertIsNone(self.backend.cause)
        # Steady state costs nothing more.
        self.assertFalse(self.backend.refresh())
        self.assertEqual(self.backend.probes, 2)

    def test_present_but_denied_device_is_not_re_opened(self):
        with open(self.path, "wb") as fh:
            fh.write(b"\0" * 8)
        # What a permission refusal leaves behind: the node exists, the
        # verdict is unavailable, and re-probing every second would fail
        # every second. The signature is unchanged, so refresh stays quiet.
        self.backend.probe()
        stat = os.stat(self.path)
        self.backend.device_signature = (
            stat.st_uid, stat.st_gid, stat.st_mode, stat.st_ino, stat.st_rdev)
        self.backend.available = False
        self.backend.cause = "no permission for /dev/cpu/0/msr"
        probes = self.backend.probes
        self.assertFalse(self.backend.refresh())
        self.assertEqual(self.backend.probes, probes)

    def test_module_vanishing_is_reported(self):
        with open(self.path, "wb") as fh:
            fh.write(b"\0" * 8)
        self.assertEqual(self.backend.refresh(), True)
        self.assertTrue(self.backend.available)
        os.unlink(self.path)
        self.assertTrue(self.backend.refresh())
        self.assertFalse(self.backend.available)
        self.assertIn("msr kernel module not loaded", self.backend.cause)
        self.assertIsNone(self.backend.fd)

    def test_recreated_node_is_noticed_even_with_the_same_owner(self):
        # A module reload recreates the node: same owner, group and mode, new
        # inode. Keeping the old file descriptor would make every later write
        # fail while the daemon still reported the controls as available.
        with open(self.path, "wb") as fh:
            fh.write(b"\0" * 8)
        self.assertTrue(self.backend.refresh())
        self.assertTrue(self.backend.available)
        # Force the identity a reload would have: same owner/mode, new inode.
        signature = self.backend.device_signature
        self.backend.device_signature = (
            signature[0], signature[1], signature[2], signature[3] + 1, signature[4])
        probes = self.backend.probes
        # The verdict (available/cause) is unchanged, so refresh() reports
        # False; what matters is that it re-probed and reopened the node.
        self.backend.refresh()
        self.assertEqual(self.backend.probes, probes + 1)
        self.assertEqual(os.fstat(self.backend.fd).st_ino, os.stat(self.path).st_ino)

    def test_ownership_change_is_picked_up(self):
        # The install applies a udev rule that hands the node to the service
        # user; a daemon that probed while it was still root-owned must not
        # keep reporting "no permission" until someone restarts it.
        with open(self.path, "wb") as fh:
            fh.write(b"\0" * 8)
        self.backend.probe()
        stat = os.stat(self.path)
        # Simulate the probe having seen a root-owned 0600 node.
        self.backend.device_signature = (
            0, stat.st_gid, 0o600, stat.st_ino, stat.st_rdev)
        self.backend.available = False
        self.backend.cause = "no permission for /dev/cpu/0/msr"
        probes = self.backend.probes
        self.assertTrue(self.backend.refresh())
        self.assertEqual(self.backend.probes, probes + 1)
        self.assertTrue(self.backend.available)
        self.backend.probe()
        stat = os.stat(self.path)
        self.backend.device_signature = (
            stat.st_uid + 1, stat.st_gid, stat.st_mode, stat.st_ino, stat.st_rdev)
        self.backend.available = False
        self.backend.cause = "no permission"
        os.chmod(self.path, 0o600)
        stat = os.stat(self.path)
        self.assertTrue(self.backend.refresh())
        self.assertEqual(
            self.backend.device_signature,
            (stat.st_uid, stat.st_gid, stat.st_mode, stat.st_ino, stat.st_rdev))

    def test_probe_failure_does_not_outlive_a_successful_re_probe(self):
        with open(self.path, "wb") as fh:
            fh.write(b"\0" * 8)
        self.backend.probe_failed = "driver hiccup at boot"
        self.backend.device_present = False
        self.assertTrue(self.backend.refresh())
        self.assertIsNone(self.backend.probe_failed)
        self.assertTrue(self.backend.available)

    def test_a_second_probe_closes_the_previous_descriptor(self):
        with open(self.path, "wb") as fh:
            fh.write(b"\0" * 8)
        self.backend.probe()
        first = self.backend.fd
        self.assertIsNotNone(first)
        with mock.patch.object(cpu_msr.os, "close", wraps=os.close) as close:
            self.backend.probe()
        close.assert_any_call(first)

    def test_a_second_probe_drops_a_stale_verdict(self):
        with open(self.path, "wb") as fh:
            fh.write(b"\0" * 8)
        self.backend.probe()
        self.assertTrue(self.backend.available)
        os.unlink(self.path)
        self.backend.probe()
        self.assertFalse(self.backend.available)

    def test_interface_is_uniform(self):
        for backend in (FakeBackend("cpu"), FakeBackend("gpu")):
            self.assertFalse(backend.refresh())


class LateBackend(FakeBackend):
    """Fake backend that becomes usable the way a late msr load does."""

    def __init__(self, component: str):
        super().__init__(component, unavailable=True, cause="msr kernel module not loaded")
        self.arrived = False

    def refresh(self) -> bool:
        if not (self.arrived and self.unavailable):
            return False
        self.unavailable = False
        self.fixed_cause = None
        return True


def make_core(tmp: str, cpu, gpu) -> tuple[TunerCore, list]:
    dirs = config.Dirs(system=False, state_dir=tmp, socket_path=os.path.join(tmp, "test.sock"))
    core = TunerCore(dirs, {"cpu": cpu, "gpu": gpu}, Journal(dirs.events_file))
    core.schedule_timer = lambda delay_ms, callback: (lambda: None)
    core.boot()
    changes: list = []
    core.on_change.append(changes.append)
    return core, changes


class CoreRefreshTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="omatuner-refresh-")

    def test_control_becomes_available_and_state_is_pushed(self):
        cpu, gpu = LateBackend("cpu"), FakeBackend("gpu")
        core, changes = make_core(self.tmp, cpu, gpu)
        before = core.snapshot()["controls"]["cpu"]
        self.assertFalse(any(control["available"] for control in before))

        cpu.arrived = True
        self.assertTrue(core.refresh_backends())
        self.assertTrue(changes, "a change must push a fresh state to the UI")
        self.assertTrue(changes[-1]["controls"]["cpu"][0]["available"])
        self.assertIsNone(changes[-1]["controls"]["cpu"][0]["cause"])

        self.assertFalse(core.refresh_backends())
        self.assertEqual(len(changes), 1, "steady state pushes nothing")

    def test_applied_values_are_re_read_when_a_control_appears(self):
        cpu, gpu = LateBackend("cpu"), FakeBackend("gpu")
        core, _ = make_core(self.tmp, cpu, gpu)
        cpu.values = {"core": -25, "cache": -10}

        cpu.arrived = True
        core.refresh_backends()
        self.assertEqual(core.applied["cpu"], {"core": -25, "cache": -10})

    def test_trial_is_never_resynced_underneath(self):
        cpu, gpu = FakeBackend("cpu"), LateBackend("gpu")
        core, _ = make_core(self.tmp, cpu, gpu)
        core.apply("cpu", {"core": -30})
        self.assertIsNotNone(core.trial)
        applied_before = dict(core.applied["cpu"])

        calls: list = []
        core._resync_applied = lambda component: calls.append(component)
        gpu.arrived = True
        self.assertTrue(core.refresh_backends())
        self.assertEqual(calls, [], "a trial owns the applied state until it is answered")
        self.assertEqual(core.applied["cpu"], applied_before)

    def test_backend_exceptions_do_not_break_the_loop(self):
        class Broken(FakeBackend):
            def refresh(self):
                raise RuntimeError("boom")

        cpu, gpu = Broken("cpu"), FakeBackend("gpu")
        core, _ = make_core(self.tmp, cpu, gpu)
        self.assertFalse(core.refresh_backends())


class DaemonLoopRefreshTests(unittest.TestCase):
    """The real server loop: a device that appears is served, no restart."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="omatuner-loop-")
        self.msr = os.path.join(self.tmp, "msr")
        self.sock = os.path.join(self.tmp, "omatuner.sock")
        self.dirs = config.Dirs(system=False, state_dir=os.path.join(self.tmp, "state"), socket_path=self.sock)
        self.cpu = PathCpu(self.msr)
        self.core = TunerCore(self.dirs, {"cpu": self.cpu, "gpu": FakeBackend("gpu")},
                              Journal(self.dirs.events_file))
        self.server = Server(self.core, self.dirs)

    def tearDown(self):
        self.server.stop()

    def test_state_pushes_availability_after_the_device_appears(self):
        thread = threading.Thread(target=self.server.run, daemon=True)
        thread.start()
        client = _connect(self.sock)
        try:
            client.sendall(b'{"id": 1, "method": "hello", "params": {}}\n')
            reply = _read_frame(client)
            cpu_controls = reply["result"]["state"]["controls"]["cpu"]
            self.assertFalse(any(control["available"] for control in cpu_controls))

            # What `sudo bin/install` does: the module arrives while the
            # daemon is already serving.
            with open(self.msr, "wb") as fh:
                fh.write(b"\0" * 8)

            frame = _wait_for_state(client, lambda state: state["controls"]["cpu"][0]["available"], timeout=6)
            self.assertIsNone(frame["data"]["controls"]["cpu"][0]["cause"])
        finally:
            client.close()


    def test_non_object_params_do_not_kill_the_daemon(self):
        # `subscribe` reads params before the request handler's try block, so
        # a string there used to escape and end the server loop.
        thread = threading.Thread(target=self.server.run, daemon=True)
        thread.start()
        client = _connect(self.sock)
        try:
            client.sendall(b'{"id": 1, "method": "subscribe", "params": "notadict"}\n')
            client.sendall(b'{"id": 2, "method": "hello", "params": {}}\n')
            reply = _read_frame(client)
            self.assertTrue(reply["ok"])
            self.assertEqual(reply["id"], 2)
            self.assertTrue(thread.is_alive())
        finally:
            client.close()

    def test_a_non_object_frame_does_not_kill_the_daemon(self):
        # `[1]` is valid JSON but has no .get(); it used to escape the line
        # handler and take the whole server loop down.
        thread = threading.Thread(target=self.server.run, daemon=True)
        thread.start()
        client = _connect(self.sock)
        try:
            client.sendall(b"[1, 2]\n")
            client.sendall(b'{"id": 2, "method": "hello", "params": {}}\n')
            reply = _read_frame(client)
            self.assertTrue(reply["ok"])
            self.assertIn("state", reply["result"])
            self.assertTrue(thread.is_alive())
        finally:
            client.close()


def _connect(path: str, timeout: float = 5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(path):
            try:
                client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                client.settimeout(timeout)
                client.connect(path)
                return client
            except OSError:
                pass
        time.sleep(0.02)
    raise AssertionError(f"daemon never listened on {path}")


def _read_frame(client) -> dict:
    buffer = b""
    while b"\n" not in buffer:
        chunk = client.recv(65536)
        if not chunk:
            raise AssertionError("daemon closed the connection")
        buffer += chunk
    line, _ = buffer.split(b"\n", 1)
    return json.loads(line)


def _wait_for_state(client, predicate, timeout: float) -> dict:
    deadline = time.time() + timeout
    client.settimeout(0.5)
    while time.time() < deadline:
        try:
            frame = _read_frame(client)
        except (socket.timeout, TimeoutError):
            continue
        if frame.get("event") == "state" and predicate(frame["data"]):
            return frame
    raise AssertionError("no state frame reported the control as available")


if __name__ == "__main__":
    unittest.main()
