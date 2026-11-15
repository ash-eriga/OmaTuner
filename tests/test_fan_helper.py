"""The root helper: protocol, peer check, and the daemon's delegation.

Driver 580 refuses the fan writes and the temperature-target write from any
non-root uid, so the daemon hands them to `omatuner fan-helper` (root) over a
unix socket. These tests run the real server loop against a fake device and
the real client against a temporary socket; nothing here touches NVML or the
fan.

Run from the project root: python3 -m unittest tests.test_fan_helper -v
"""

from __future__ import annotations

import ctypes
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

from omatuner import fanhelper, nvml_defs
from omatuner.backends import gpu_nvml
from omatuner.backends.gpu_nvml import GpuNvmlBackend

UID = os.getuid()


class FakeDevice:
    """Stands in for FanDevice: records the writes, never touches NVML."""

    count = 1
    min = 0
    max = 100

    def __init__(self):
        self.duty: int | None = None
        self.temperature = 83
        self.calls: list[tuple] = []

    def info(self) -> dict:
        return {
            "count": self.count,
            "min": self.min,
            "max": self.max,
            "speed": 30,
            "fan": True,
            "temperature": self.temperature,
            "temperatureRange": [60, 92],
        }

    def set_duty(self, percent: int) -> int:
        duty = max(self.min, min(self.max, int(percent)))
        self.calls.append(("duty", duty))
        self.duty = duty
        return duty

    def set_auto(self) -> None:
        self.calls.append(("auto",))
        self.duty = None

    def set_temperature(self, celsius: int) -> int:
        target = int(celsius)
        self.calls.append(("temp", target))
        self.temperature = target
        return target

    def close(self) -> None:
        pass


class FakeNvml:
    device = None

    def __init__(self):
        self.device = object()

    def has(self, name: str) -> bool:
        return False


class FakeLink:
    def __init__(self):
        self.calls: list[tuple] = []
        self.applied = 0
        self.temperature = 83
        self.available_calls = 0
        self.available_verdict = (True, None)

    def available(self) -> tuple[bool, str | None]:
        self.available_calls += 1
        return self.available_verdict

    def set_duty(self, percent: int) -> int:
        self.calls.append(("duty", percent))
        self.applied = percent
        return percent

    def set_auto(self) -> None:
        self.calls.append(("auto",))

    def set_temperature(self, celsius: int) -> int:
        self.calls.append(("temp", celsius))
        self.temperature = celsius
        return celsius


class HelperServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "fan.sock")
        self.device = FakeDevice()
        self.helpers: list[tuple[fanhelper.FanHelper, threading.Thread]] = []
        self.links: list[fanhelper.FanHelperLink] = []
        self.raws: list[socket.socket] = []

    def tearDown(self):
        for raw in self.raws:
            raw.close()
        for link in self.links:
            link.close()
        for helper, thread in self.helpers:
            helper.stop()
            thread.join(timeout=2)
        self.tmp.cleanup()

    def start(self, allowed=None, log=None):
        helper = fanhelper.FanHelper(
            self.device, self.path,
            allowed_uids={UID} if allowed is None else allowed,
            log=log or (lambda message: None),
            accept_timeout=0.02,
        )
        helper.bind()
        thread = threading.Thread(target=helper.serve_forever, daemon=True)
        thread.start()
        self.helpers.append((helper, thread))
        return helper, thread

    def link(self) -> fanhelper.FanHelperLink:
        link = fanhelper.FanHelperLink(self.path, timeout=2.0)
        self.links.append(link)
        return link

    def test_hello_and_duty_roundtrip(self):
        self.start()
        link = self.link()
        self.assertEqual(link.available(), (True, None))
        self.assertEqual(link.set_duty(55), 55)
        self.assertEqual(self.device.calls, [("duty", 55)])
        link.set_auto()
        self.assertEqual(self.device.calls, [("duty", 55), ("auto",)])
        self.assertIsNone(link.sock, "auto closes the link; the policy is over")

    def test_a_restarted_helper_is_reconnected_once(self):
        # A helper restart drops the link and hands the fan back to the
        # driver. The next write must reconnect instead of failing forever.
        first, first_thread = self.start()
        link = self.link()
        self.assertEqual(link.set_duty(55), 55)
        first.stop()
        first_thread.join(timeout=2)
        self.start()
        self.assertEqual(link.set_duty(65), 65)
        self.assertEqual(self.device.calls[-1], ("duty", 65))

    def test_temperature_roundtrip(self):
        self.start()
        link = self.link()
        self.assertEqual(link.set_temperature(80), 80)
        self.assertEqual(self.device.calls, [("temp", 80)])
        self.assertEqual(self.device.temperature, 80)

    def test_disconnect_while_manual_returns_the_fan_to_the_driver(self):
        self.start()
        link = self.link()
        link.set_duty(70)
        link.close()
        for _ in range(50):
            if ("auto",) in self.device.calls:
                break
            time.sleep(0.02)
        self.assertIn(("auto",), self.device.calls, "a dropped client must restore the driver curve")

    def test_hello_alone_never_touches_the_fan(self):
        helper, _ = self.start()
        link = self.link()
        link.available()
        link.close()
        time.sleep(0.1)
        self.assertEqual(self.device.calls, [], "a hello is read-only")
        self.assertFalse(helper.owned)

    def test_another_uid_is_refused(self):
        self.start(allowed={UID + 1})
        link = self.link()
        ok, cause = link.available()
        self.assertFalse(ok)
        self.assertIn("only the omatuner service", cause)
        self.assertEqual(self.device.calls, [])

    def test_a_refused_peer_drops_the_link_instead_of_reusing_it(self):
        # The helper answers a peer it does not serve and closes. Keeping that
        # socket made the next request fail with "closed the connection"
        # instead of the refusal, and the daemon's fan probe lost the cause.
        self.start(allowed={UID + 1})
        link = self.link()
        with self.assertRaises(fanhelper.FanDeviceError) as caught:
            link.request("hello")
        self.assertIn("only the omatuner service", str(caught.exception))
        self.assertIsNone(link.sock, "the refused connection must not be reused")

    def test_missing_helper_names_the_service(self):
        link = fanhelper.FanHelperLink(os.path.join(self.tmp.name, "absent.sock"), timeout=1.0)
        ok, cause = link.available()
        self.assertFalse(ok)
        self.assertIn("omatuner-fan.service", cause)

    def test_a_second_client_is_refused_while_the_fan_is_owned(self):
        # The daemon holds its link for the life of a policy; a second
        # process (a user-mode daemon, a test) must get an immediate refusal
        # instead of sitting in the backlog behind that link.
        self.start()
        owner = self.link()
        owner.set_duty(60)
        other = self.link()
        self.assertTrue(other.available()[0], "hello is read-only and always answers")
        with self.assertRaises(fanhelper.FanDeviceError) as caught:
            other.set_duty(40)
        self.assertIn("already owned", str(caught.exception))
        self.assertEqual(self.device.duty, 60, "the owner's duty stands")
        owner.close()
        for _ in range(50):
            if ("auto",) in self.device.calls:
                break
            time.sleep(0.02)
        self.assertIn(("auto",), self.device.calls)

    def test_a_non_object_frame_keeps_the_connection(self):
        self.start()
        raw = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        raw.settimeout(2.0)
        raw.connect(self.path)
        self.raws.append(raw)
        raw.sendall(b"[1, 2]\n")
        reply = json.loads(raw.recv(4096).split(b"\n")[0])
        self.assertFalse(reply["ok"])
        raw.sendall(b'{"id": 7, "method": "hello"}\n')
        reply = json.loads(raw.recv(4096).split(b"\n")[0])
        self.assertTrue(reply["ok"], "a malformed frame must not end the session")

    def test_a_non_utf8_frame_is_refused_and_keeps_the_connection(self):
        # json.loads raises UnicodeDecodeError (not JSONDecodeError) for a
        # line that is not UTF-8; that used to escape and drop the client.
        self.start()
        raw = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        raw.settimeout(2.0)
        raw.connect(self.path)
        self.raws.append(raw)
        raw.sendall(b"\xff\n")
        reply = json.loads(raw.recv(4096).split(b"\n")[0])
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["error"]["code"], "bad-request")
        raw.sendall(b'{"id": 7, "method": "hello"}\n')
        reply = json.loads(raw.recv(4096).split(b"\n")[0])
        self.assertTrue(reply["ok"], "a non-UTF-8 frame must not end the session")

    def test_an_unprivileged_peer_is_refused_without_a_thread(self):
        helper, _ = self.start(allowed={UID + 1})
        raw = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        raw.settimeout(2.0)
        raw.connect(self.path)
        self.raws.append(raw)
        reply = json.loads(raw.recv(4096).split(b"\n")[0])
        self.assertFalse(reply["ok"])
        self.assertIn("only the omatuner service", reply["error"]["message"])
        self.assertFalse(helper.owned)
        for _ in range(50):
            if not helper.threads:
                break
            time.sleep(0.02)
        self.assertEqual(helper.threads, [], "a refused peer never gets a worker thread")

    def refused_peer(self, raw: socket.socket) -> dict:
        raw.settimeout(2.0)
        raw.connect(self.path)
        self.raws.append(raw)
        return json.loads(raw.recv(4096).split(b"\n")[0])

    def test_a_silent_refused_peer_costs_the_accept_loop_little(self):
        # The refusal used to drain with blocking reads for 0.3 s per peer,
        # even when the peer had sent nothing: the accept loop was held for
        # every refused connection, delaying the daemon's own reconnect.
        self.start(allowed={UID + 1})
        started = time.monotonic()
        first = self.refused_peer(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM))
        # The second refusal cannot be accepted before the first drain ended,
        # so its arrival time measures what a silent peer costs the loop.
        second = self.refused_peer(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM))
        elapsed = time.monotonic() - started
        self.assertFalse(first["ok"])
        self.assertFalse(second["ok"])
        self.assertLess(elapsed, 0.25, f"two silent refusals took {elapsed:.3f} s")

    def test_the_refusal_log_line_is_rate_limited(self):
        lines = []
        helper, _ = self.start(allowed={UID + 1}, log=lines.append)
        for _ in range(4):
            self.assertFalse(self.refused_peer(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM))["ok"])
        refused = [line for line in lines if "refused a client with uid" in line]
        self.assertEqual(len(refused), 1, lines)
        self.assertNotIn("more since the last line", refused[0])

        # Past the interval the next line names what was held back.
        helper.refusal_log_ms -= fanhelper.REFUSAL_LOG_SECONDS + 1
        self.refused_peer(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM))
        refused = [line for line in lines if "refused a client with uid" in line]
        self.assertEqual(len(refused), 2, lines)
        self.assertIn("(3 more since the last line)", refused[1])

    def test_concurrent_clients_are_capped(self):
        self.start()
        clients = []
        try:
            for _ in range(fanhelper.MAX_CLIENTS):
                raw = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                raw.settimeout(2.0)
                raw.connect(self.path)
                raw.sendall(b'{"id": 1, "method": "hello"}\n')
                self.assertTrue(json.loads(raw.recv(4096).split(b"\n")[0])["ok"])
                clients.append(raw)
            extra = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            extra.settimeout(2.0)
            extra.connect(self.path)
            self.raws.append(extra)
            reply = json.loads(extra.recv(4096).split(b"\n")[0])
            self.assertFalse(reply["ok"])
            self.assertIn("maximum number of clients", reply["error"]["message"])
        finally:
            for raw in clients:
                raw.close()


    def test_an_oversized_request_drops_the_client(self):
        self.start()
        raw = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        raw.settimeout(2.0)
        raw.connect(self.path)
        self.raws.append(raw)
        raw.sendall(b"x" * (fanhelper.MAX_REQUEST_BYTES + 1))
        self.assertEqual(raw.recv(4096), b"", "the helper closes a runaway request")

    def test_a_failed_write_does_not_wedge_ownership(self):
        # set_duty walks the fans one by one, so a failure can leave the first
        # ones manual. The helper used to keep the owner without `owned`, and
        # restore() then refused, blocking every later client until a restart.
        self.start()
        real = self.device.set_duty
        first_call = {"done": False}

        def flaky(percent):
            if not first_call["done"]:
                first_call["done"] = True
                raise fanhelper.FanDeviceError("the driver refused the write")
            return real(percent)

        self.device.set_duty = flaky
        first = self.link()
        with self.assertRaises(fanhelper.FanDeviceError):
            first.set_duty(50)
        first.close()
        for _ in range(50):
            if ("auto",) in self.device.calls:
                break
            time.sleep(0.02)
        self.assertIn(("auto",), self.device.calls, "the disconnect must restore the driver")
        second = self.link()
        self.assertEqual(second.set_duty(60), 60)
        self.assertEqual(self.device.duty, 60)

    def test_duplicate_hello_keeps_one_link(self):
        self.start()
        link = self.link()
        self.assertTrue(link.available()[0])
        sock = link.sock
        self.assertTrue(link.available()[0])
        self.assertIs(link.sock, sock, "the second hello reuses the open connection")


class FanPolicyConstantTests(unittest.TestCase):
    """nvml.h: TEMPERATURE_CONTINOUS_SW = 0 (automatic), MANUAL = 1."""

    def test_the_constants_match_the_header(self):
        self.assertEqual(nvml_defs.NVML_FAN_POLICY_AUTO, 0)
        self.assertEqual(nvml_defs.NVML_FAN_POLICY_MANUAL, 1)

    def test_set_auto_passes_the_drivers_curve_policy_not_manual(self):
        # A driver that only exports SetFanControlPolicy takes this fallback.
        lib = type("Lib", (), {})()
        written = []

        def set_policy(device, fan, policy):
            written.append(policy)
            return 0

        lib.nvmlInit_v2 = lambda: 0
        lib.nvmlShutdown = lambda: 0
        lib.nvmlDeviceGetHandleByIndex_v2 = lambda index, out: 0
        lib.nvmlDeviceGetNumFans = lambda dev, out: 0
        lib.nvmlDeviceGetFanSpeed_v2 = lambda dev, fan, out: 0
        lib.nvmlDeviceSetFanSpeed_v2 = lambda dev, fan, speed: 0
        lib.nvmlDeviceSetFanControlPolicy = set_policy
        device = fanhelper.FanDevice(lib)
        self.assertEqual(device.count, 0)  # the fake reports a zero count
        device.count = 1
        device.set_auto()
        self.assertEqual(written, [0], "auto is policy 0, never MANUAL (1)")


class RootFanProbeTests(unittest.TestCase):
    """Root proves the fan write and must hand the fan back before saying so."""

    def lib(self, restore: str | None):
        lib = type("Lib", (), {})()
        lib.calls = []
        lib.restore_rc = 0

        def set_speed(device, fan, speed):
            lib.calls.append(("set", fan, speed))
            return 0

        def default_speed(device, fan):
            lib.calls.append(("default", fan))
            return lib.restore_rc

        def policy(device, fan, value):
            lib.calls.append(("policy", fan, value))
            return lib.restore_rc

        def get_speed(device, fan, out):
            out._obj.value = 40
            return 0

        lib.nvmlDeviceSetFanSpeed_v2 = set_speed
        lib.nvmlDeviceGetFanSpeed_v2 = get_speed
        if restore == "default":
            lib.nvmlDeviceSetDefaultFanSpeed_v2 = default_speed
        elif restore == "policy":
            lib.nvmlDeviceSetFanControlPolicy = policy
        return lib

    def probe(self, lib) -> GpuNvmlBackend:
        nvml = FakeNvml()
        nvml.lib = lib
        nvml.has = lambda name: hasattr(lib, name)
        backend = GpuNvmlBackend(nvml=nvml)
        backend.present = True
        backend.fan_supported = True
        backend.fan_count = 1
        with mock.patch.object(gpu_nvml.os, "geteuid", return_value=0):
            backend._probe_fan_write()
        return backend

    def test_the_probe_hands_the_fan_back_to_the_driver(self):
        lib = self.lib("default")
        backend = self.probe(lib)
        self.assertTrue(backend.fan_writable)
        self.assertTrue(backend.fan_direct)
        self.assertEqual(lib.calls, [("set", 0, 40), ("default", 0)])

    def test_a_failed_hand_back_is_not_a_writable_fan(self):
        lib = self.lib("policy")
        lib.restore_rc = 5
        backend = self.probe(lib)
        self.assertFalse(backend.fan_writable)
        self.assertFalse(backend.fan_direct)
        self.assertIn("NVML error 5", backend.fan_write_cause or "")
        self.assertEqual(lib.calls[-1], ("policy", 0, nvml_defs.NVML_FAN_POLICY_AUTO))

    def test_no_restore_symbol_means_no_probe_write_at_all(self):
        lib = self.lib(None)
        backend = self.probe(lib)
        self.assertFalse(backend.fan_writable)
        self.assertFalse(backend.fan_direct)
        self.assertEqual(
            backend.fan_write_cause,
            "this driver cannot return the fan to automatic control",
        )
        self.assertEqual(lib.calls, [], "without a hand-back the fan is never made manual")


class RestoreFailureTests(unittest.TestCase):
    def test_a_failed_restore_keeps_ownership_for_a_second_attempt(self):
        class Flaky(FakeDevice):
            def __init__(self):
                super().__init__()
                self.fail = True

            def set_auto(self):
                if self.fail:
                    self.fail = False
                    raise fanhelper.FanDeviceError("the driver refused the restore")
                super().set_auto()

        device = Flaky()
        helper = fanhelper.FanHelper(device, path="/nonexistent/fan.sock", log=lambda message: None,
                                     accept_timeout=0.02)
        token = object()
        helper.owned = True
        helper.owner = token
        self.assertFalse(helper.restore(token))
        self.assertTrue(helper.owned, "a failed restore must not drop ownership")
        self.assertIsNone(helper.owner, "the dead client's claim is released")
        # The owner was the dead client's token: a reconnecting client may
        # take the fan instead of being refused until the helper restarts.
        self.assertEqual(helper._dispatch(object(), "set-duty", {"percent": 55}), {"applied": 55})
        self.assertTrue(helper.restore())
        self.assertFalse(helper.owned)
        self.assertIsNone(helper.owner)
        self.assertEqual(device.calls, [("duty", 55), ("auto",)])


class HelperRecheckTests(unittest.TestCase):
    """The helper starts independently of the daemon (no unit ordering)."""

    def backend(self) -> GpuNvmlBackend:
        backend = GpuNvmlBackend(nvml=FakeNvml())
        backend.present = True
        backend.device_present = True
        backend.fan_supported = True
        backend.fan_count = 1
        backend.fan_writable = False
        backend.fan_direct = False
        backend.temp_supported = False
        backend.helper = FakeLink()
        return backend

    def test_refresh_picks_up_the_helper_when_it_starts_late(self):
        backend = self.backend()
        with mock.patch.object(gpu_nvml, "driver_visible", return_value=True), \
                mock.patch.object(gpu_nvml.os, "geteuid", return_value=1000):
            self.assertTrue(backend.refresh(), "the changed verdict is reported")
            self.assertTrue(backend.fan_writable)
            self.assertFalse(backend.refresh(), "an answered verdict is not re-probed")

    def test_the_helper_recheck_is_rate_limited(self):
        backend = self.backend()
        with mock.patch.object(gpu_nvml, "driver_visible", return_value=True), \
                mock.patch.object(gpu_nvml.os, "geteuid", return_value=1000):
            self.assertTrue(backend.refresh())
            calls = backend.helper.available_calls
            backend.fan_writable = False
            backend.fan_direct = False
            self.assertFalse(backend.refresh(), "inside the interval nothing is asked")
            self.assertEqual(backend.helper.available_calls, calls)
            backend._helper_recheck_at = 0.0
            self.assertTrue(backend.refresh())
            self.assertGreater(backend.helper.available_calls, calls)

    def test_a_root_process_never_asks_the_helper(self):
        backend = self.backend()
        with mock.patch.object(gpu_nvml, "driver_visible", return_value=True), \
                mock.patch.object(gpu_nvml.os, "geteuid", return_value=0):
            self.assertFalse(backend.refresh())
        self.assertEqual(backend.helper.available_calls, 0)


class FanDeviceTests(unittest.TestCase):
    """The helper's own NVML wrapper, without touching the driver."""

    def device(self, fan: bool = True):
        lib = type("Lib", (), {})()
        state = {"value": 83, "written": [], "order": []}

        def get_handle(index, out):
            out._obj.value = 1
            return 0

        def num_fans(dev, out):
            out._obj.value = 1
            return 0

        def get_speed(dev, fan, out):
            out._obj.value = 40
            return 0

        def set_speed(dev, fan, speed):
            return 0

        def set_default(dev, fan):
            return 0

        def get_threshold(dev, kind, out):
            values = {4: 60, 5: state["value"], 6: 92}
            if kind not in values:
                return 3
            out._obj.value = values[kind]
            return 0

        def set_threshold(dev, kind, temp):
            # FanDevice declares `int *temp`; reading the pointer here fails
            # if it is ever called with the int by value.
            value = ctypes.cast(temp, ctypes.POINTER(ctypes.c_int)).contents.value
            state["written"].append(value)
            state["value"] = value
            return 0

        lib.nvmlInit_v2 = lambda: 0
        lib.nvmlShutdown = lambda: 0
        lib.nvmlDeviceGetHandleByIndex_v2 = get_handle
        if fan:
            lib.nvmlDeviceGetNumFans = num_fans
            lib.nvmlDeviceGetFanSpeed_v2 = get_speed
            lib.nvmlDeviceSetFanSpeed_v2 = set_speed
            lib.nvmlDeviceSetDefaultFanSpeed_v2 = set_default
        lib.nvmlDeviceGetTemperatureThreshold = get_threshold
        lib.nvmlDeviceSetTemperatureThreshold = set_threshold
        return fanhelper.FanDevice(lib), state

    def test_temperature_write_passes_a_pointer(self):
        device, state = self.device()
        self.assertTrue(device.temp_supported)
        self.assertEqual(device.set_temperature(80), 80)
        self.assertEqual(state["written"], [80])

    def test_out_of_range_temperature_never_reaches_the_driver(self):
        device, state = self.device()
        with self.assertRaises(fanhelper.FanDeviceError):
            device.set_temperature(99)
        self.assertEqual(state["written"], [])

    def test_a_card_without_fan_symbols_still_serves_the_temperature(self):
        device, _ = self.device(fan=False)
        self.assertFalse(device.fan_supported)
        self.assertTrue(device.temp_supported)
        self.assertEqual(device.set_temperature(80), 80)

    def test_set_duty_clamps_to_the_device_max_and_refuses_non_numeric(self):
        lib = type("Lib", (), {})()
        written = []

        def get_handle(index, out):
            out._obj.value = 1
            return 0

        def num_fans(dev, out):
            out._obj.value = 1
            return 0

        def min_max(dev, low, high):
            low._obj.value = 20
            high._obj.value = 80
            return 0

        def set_speed(dev, fan, speed):
            written.append(speed)
            return 0

        lib.nvmlInit_v2 = lambda: 0
        lib.nvmlShutdown = lambda: 0
        lib.nvmlDeviceGetHandleByIndex_v2 = get_handle
        lib.nvmlDeviceGetNumFans = num_fans
        lib.nvmlDeviceGetMinMaxFanSpeed = min_max
        lib.nvmlDeviceGetFanSpeed_v2 = lambda dev, fan, out: 0
        lib.nvmlDeviceSetFanSpeed_v2 = set_speed
        device = fanhelper.FanDevice(lib)
        self.assertEqual(device.set_duty(150), 80)
        self.assertEqual(written, [80])
        for value in ("quiet", float("inf")):
            with self.assertRaises(fanhelper.FanDeviceError):
                device.set_duty(value)
        self.assertEqual(written, [80])


class BackendDelegationTests(unittest.TestCase):
    def backend(self):
        backend = GpuNvmlBackend(nvml=FakeNvml())
        backend.present = True
        backend.fan_supported = True
        backend.fan_count = 1
        backend.fan_min = 0
        backend.fan_max = 100
        backend.fan_writable = True
        backend.fan_direct = False
        backend.temp_supported = True
        backend.temp_api = "threshold"
        backend.temp_min_c = 60
        backend.temp_max_c = 92
        backend.temp_limit_c = 83
        backend.helper = FakeLink()
        return backend

    def test_duty_goes_through_the_helper_when_not_root(self):
        backend = self.backend()
        self.assertEqual(backend.set_fan_duty(60), 60)
        self.assertEqual(backend.helper.calls, [("duty", 60)])

    def test_auto_goes_through_the_helper(self):
        backend = self.backend()
        backend.set_fan_auto()
        self.assertEqual(backend.helper.calls, [("auto",)])

    def test_read_only_reports_the_helper_cause(self):
        backend = self.backend()
        backend.fan_writable = False
        backend.fan_write_cause = "the helper is not running"
        with self.assertRaises(Exception) as caught:
            backend.set_fan_duty(60)
        self.assertIn("not running", str(caught.exception))
        self.assertEqual(backend.helper.calls, [])

    def test_helper_failure_is_surfaced_and_the_link_dropped(self):
        class Broken(FakeLink):
            def set_duty(self, percent):
                raise fanhelper.FanDeviceError("the fan helper did not answer: broken pipe")

        backend = self.backend()
        backend.helper = Broken()
        with self.assertRaises(Exception) as caught:
            backend.set_fan_duty(60)
        self.assertIn("did not answer", str(caught.exception))
        self.assertIn("did not answer", backend.fan_write_cause)

    def test_temperature_probe_goes_through_the_helper_when_not_root(self):
        backend = self.backend()
        backend._get_temp_target = lambda: backend.helper.temperature
        with mock.patch.object(gpu_nvml.os, "geteuid", return_value=1000):
            backend._probe_temp_write()
        self.assertTrue(backend.temp_writable)
        self.assertFalse(backend.temp_direct)
        self.assertEqual(backend.helper.calls, [("temp", 83)])

    def test_temperature_apply_goes_through_the_helper(self):
        backend = self.backend()
        backend.temp_writable = True
        backend._get_temp_target = lambda: backend.helper.temperature
        backend.apply({"temp": 80})
        self.assertEqual(backend.helper.calls, [("temp", 80)])

    def test_temperature_refusal_is_reported(self):
        class Refusing(FakeLink):
            def set_temperature(self, celsius):
                raise fanhelper.FanDeviceError(
                    "the NVIDIA driver refused the temperature write: it requires uid 0"
                )

        backend = self.backend()
        backend.helper = Refusing()
        backend._get_temp_target = lambda: 83
        with mock.patch.object(gpu_nvml.os, "geteuid", return_value=1000):
            backend._probe_temp_write()
        self.assertFalse(backend.temp_writable)
        self.assertIn("uid 0", backend.temp_write_cause or "")
        controls = {c.key: c for c in backend.controls()}
        self.assertFalse(controls["temp"].available)


    def test_a_successful_fan_write_clears_an_older_refusal(self):
        class OnceBroken(FakeLink):
            def __init__(self):
                super().__init__()
                self.broken = True

            def set_duty(self, percent):
                if self.broken:
                    self.broken = False
                    raise fanhelper.FanDeviceError("the root helper is not running")
                return super().set_duty(percent)

        backend = self.backend()
        backend.helper = OnceBroken()
        with self.assertRaises(Exception):
            backend.set_fan_duty(60)
        self.assertIn("not running", backend.fan_write_cause or "")
        self.assertEqual(backend.set_fan_duty(60), 60)
        self.assertIsNone(backend.fan_write_cause, "a write that went through clears the refusal")
        self.assertTrue(backend.fan_writable)

    def test_a_successful_temperature_write_clears_an_older_refusal(self):
        class OnceBroken(FakeLink):
            def __init__(self):
                super().__init__()
                self.broken = True

            def set_temperature(self, celsius):
                if self.broken:
                    self.broken = False
                    raise fanhelper.FanDeviceError("the root helper is not running")
                return super().set_temperature(celsius)

        backend = self.backend()
        backend.helper = OnceBroken()
        backend.temp_writable = True
        backend._get_temp_target = lambda: backend.helper.temperature
        with self.assertRaises(Exception):
            backend.apply({"temp": 80})
        self.assertIn("not running", backend.temp_write_cause or "")
        backend.temp_writable = True
        backend.apply({"temp": 80})
        self.assertIsNone(backend.temp_write_cause)
        self.assertTrue(backend.temp_writable)
        self.assertEqual(backend.helper.temperature, 80)


if __name__ == "__main__":
    unittest.main()
