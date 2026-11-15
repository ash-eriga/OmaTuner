"""Root helper: the hardware writes the unprivileged daemon cannot make.

Driver 580 gates `nvmlDeviceSetFanSpeed_v2`,
`nvmlDeviceSetDefaultFanSpeed_v2` and `nvmlDeviceSetTemperatureThreshold` on
the caller being uid 0. A process with CAP_SYS_ADMIN + CAP_SYS_RAWIO but a
nonzero uid gets NVML error 4, while uid 0 with exactly those two
capabilities succeeds (measured on the reference GTX 1060). LACT gets the
same writes by running its daemon as root; OmaTuner keeps the tuning daemon
on `omatuner` and asks this helper instead.

The surface is deliberately tiny: read the fan count/bounds/speed, set a
duty, hand the fan back to the driver, and write the acoustic temperature
target. Nothing else.

Ownership: the helper sets the fan to manual only while a client asks it to,
and restores the driver's curve when that client disconnects or the helper
stops. A daemon crash therefore returns the fan to the driver by itself.

The socket lives at /run/omatuner-fan/fan.sock, owned root:omatuner mode
0660, and every connection is checked with SO_PEERCRED: only root and the
`omatuner` user are served. Being in the group is not enough, so a desktop
user cannot drive the fan through it.
"""

from __future__ import annotations

import ctypes
import json
import math
import os
import pwd
import select
import signal
import socket
import struct
import sys
import threading
import time
from typing import Any

from . import nvml_defs
from .nvml_defs import (
    NVML_ERROR_NO_PERMISSION,
    NVML_SUCCESS,
    NVML_TEMP_ACOUSTIC_CURR,
    NVML_TEMP_ACOUSTIC_MAX,
    NVML_TEMP_ACOUSTIC_MIN,
)

FAN_SOCKET = "/run/omatuner-fan/fan.sock"
OMATUNER_USER = "omatuner"
# Every frame this protocol carries is a few hundred bytes. A client that
# streams bytes without a newline is cut off instead of growing the buffer.
MAX_REQUEST_BYTES = 64 * 1024
# Threads serving allowed clients. The daemon holds one long-lived link and a
# second client only gets a hello, so this is generous.
MAX_CLIENTS = 8
# A refused peer is answered in the accept loop, without a thread. The frame
# goes out on a non-blocking socket and the peer's unread input is drained
# only while select() reports it readable, within this total deadline: a peer
# that sends nothing costs one select() timeout, one that keeps sending is cut
# off at the same deadline, and neither delays the daemon's own reconnect.
REFUSAL_DRAIN_SECONDS = 0.05
# At most one "refused a client" line per interval. A local user who
# reconnects in a loop would otherwise fill the journal; the count that was
# held back is reported with the next line.
REFUSAL_LOG_SECONDS = 5.0


class FanDeviceError(RuntimeError):
    pass


class _LinkLost(FanDeviceError):
    """The socket died mid-request; the helper may have restarted."""


class FanDevice:
    """Minimal NVML wrapper: the fan reads/writes and the temperature target."""

    def __init__(self, lib: Any | None = None):
        if lib is None:
            lib, err = nvml_defs.load_library(nvml_defs.NVML_LIB_CANDIDATES)
            if lib is None:
                raise OSError(f"libnvidia-ml not found: {err}")
        self.lib = lib
        self.device = None
        self.count = 0
        self.min = 0
        self.max = 100
        self.fan_supported = False
        self.temp_supported = False
        self._declare()
        self._require(("nvmlInit_v2", "nvmlShutdown", "nvmlDeviceGetHandleByIndex_v2"))
        rc = self.lib.nvmlInit_v2()
        if rc != NVML_SUCCESS:
            raise FanDeviceError(f"nvmlInit failed ({rc})")
        handle = ctypes.c_void_p()
        rc = self.lib.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(handle))
        if rc != NVML_SUCCESS:
            self.lib.nvmlShutdown()
            raise FanDeviceError(f"no NVIDIA device ({rc})")
        self.device = handle
        # A card without a controllable fan can still be served for the
        # temperature target, so the fan symbols are optional.
        self.fan_supported = self._has(
            "nvmlDeviceGetNumFans", "nvmlDeviceGetFanSpeed_v2",
            "nvmlDeviceSetFanSpeed_v2",
        )
        if self.fan_supported:
            fans = ctypes.c_uint(0)
            rc = self.lib.nvmlDeviceGetNumFans(self.device, ctypes.byref(fans))
            if rc != NVML_SUCCESS:
                raise FanDeviceError(f"fan count unavailable ({rc})")
            self.count = int(fans.value)
            low = ctypes.c_uint(0)
            high = ctypes.c_uint(0)
            if self._has("nvmlDeviceGetMinMaxFanSpeed") and (
                self.lib.nvmlDeviceGetMinMaxFanSpeed(
                    self.device, ctypes.byref(low), ctypes.byref(high)
                ) == NVML_SUCCESS
            ) and low.value < high.value:
                self.min = int(low.value)
                self.max = int(high.value)
        self.temp_supported = (
            self._has("nvmlDeviceGetTemperatureThreshold", "nvmlDeviceSetTemperatureThreshold")
            and self.temperature_threshold(NVML_TEMP_ACOUSTIC_CURR) is not None
            and self.temperature_threshold(NVML_TEMP_ACOUSTIC_MIN) is not None
            and self.temperature_threshold(NVML_TEMP_ACOUSTIC_MAX) is not None
        )

    def _declare(self) -> None:
        nvml_defs.declare(self.lib)

    def _require(self, names: tuple[str, ...]) -> None:
        for name in names:
            if not hasattr(self.lib, name):
                raise FanDeviceError(f"this driver does not export {name}")

    def _has(self, *names: str) -> bool:
        return all(hasattr(self.lib, name) for name in names)

    def speed(self) -> int | None:
        if not self.fan_supported:
            return None
        value = ctypes.c_uint(0)
        rc = self.lib.nvmlDeviceGetFanSpeed_v2(self.device, 0, ctypes.byref(value))
        return int(value.value) if rc == NVML_SUCCESS else None

    def temperature_threshold(self, kind: int) -> int | None:
        if not self._has("nvmlDeviceGetTemperatureThreshold"):
            return None
        value = ctypes.c_uint(0)
        rc = self.lib.nvmlDeviceGetTemperatureThreshold(
            self.device, kind, ctypes.byref(value)
        )
        return int(value.value) if rc == NVML_SUCCESS else None

    def info(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "count": self.count,
            "min": self.min,
            "max": self.max,
            "speed": self.speed(),
            "fan": self.fan_supported,
        }
        if self.temp_supported:
            out["temperature"] = self.temperature_threshold(NVML_TEMP_ACOUSTIC_CURR)
            out["temperatureRange"] = [
                self.temperature_threshold(NVML_TEMP_ACOUSTIC_MIN),
                self.temperature_threshold(NVML_TEMP_ACOUSTIC_MAX),
            ]
        return out

    def clamp(self, percent: Any) -> int:
        try:
            number = float(percent)
        except (TypeError, ValueError):
            raise FanDeviceError("fan duty must be a number")
        if not math.isfinite(number):
            raise FanDeviceError("fan duty must be a number")
        value = int(round(number))
        return max(self.min, min(self.max, value))

    def set_duty(self, percent: Any) -> int:
        if not self.fan_supported:
            raise FanDeviceError("this driver does not expose fan control")
        duty = self.clamp(percent)
        for fan in range(self.count):
            rc = self.lib.nvmlDeviceSetFanSpeed_v2(self.device, fan, duty)
            if rc != NVML_SUCCESS:
                if rc == NVML_ERROR_NO_PERMISSION:
                    raise FanDeviceError(
                        "the NVIDIA driver refused the fan write: it requires uid 0"
                    )
                raise FanDeviceError(f"fan write rejected (NVML error {rc})")
        return duty

    def set_auto(self) -> None:
        if not self.fan_supported:
            raise FanDeviceError("this driver does not expose fan control")
        for fan in range(self.count):
            rc = nvml_defs.set_fan_auto(self.lib, self.device, fan)
            if rc == nvml_defs.NVML_NO_AUTO_CONTROL:
                raise FanDeviceError("this driver cannot return the fan to automatic control")
            if rc != NVML_SUCCESS:
                raise FanDeviceError(f"returning the fan to the driver failed (NVML error {rc})")

    def set_temperature(self, celsius: Any) -> int:
        if not self.temp_supported:
            raise FanDeviceError("this driver does not expose a writable temperature target")
        try:
            target = int(celsius)
        except (TypeError, ValueError):
            raise FanDeviceError("temperature target must be a number")
        low = self.temperature_threshold(NVML_TEMP_ACOUSTIC_MIN)
        high = self.temperature_threshold(NVML_TEMP_ACOUSTIC_MAX)
        if low is not None and high is not None and not (low <= target <= high):
            raise FanDeviceError(
                f"temperature target {target} °C is outside the driver range {low}..{high} °C"
            )
        value = ctypes.c_int(target)
        rc = self.lib.nvmlDeviceSetTemperatureThreshold(
            self.device, NVML_TEMP_ACOUSTIC_CURR, ctypes.byref(value)
        )
        if rc == NVML_ERROR_NO_PERMISSION:
            raise FanDeviceError(
                "the NVIDIA driver refused the temperature write: it requires uid 0"
            )
        if rc != NVML_SUCCESS:
            raise FanDeviceError(f"temperature write rejected (NVML error {rc})")
        actual = self.temperature_threshold(NVML_TEMP_ACOUSTIC_CURR)
        if actual is not None and actual != target:
            raise FanDeviceError(
                f"driver reports temperature target {actual} °C after requesting {target} °C"
            )
        return target

    def close(self) -> None:
        if self.device is not None:
            try:
                self.lib.nvmlShutdown()
            except Exception:
                pass
            self.device = None


def _peer_uid(conn: socket.socket) -> int:
    raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    _pid, uid, _gid = struct.unpack("3i", raw)
    return uid


class FanHelper:
    """JSON-lines server around FanDevice.

    Every allowed client gets its own thread, so the daemon's long-lived link
    never queues behind a second client; peers that are not served are refused
    in the accept loop without one.
    """

    def __init__(self, device: FanDevice, path: str = FAN_SOCKET, allowed_uids: set[int] | None = None,
                 log=None, accept_timeout: float = 1.0):
        self.device = device
        self.path = path
        self.allowed_uids = allowed_uids if allowed_uids is not None else {0, _user_uid(OMATUNER_USER)}
        self.log = log or (lambda message: print(f"omatuner-fan: {message}", file=sys.stderr, flush=True))
        # Both the accept loop and a client thread wake at this interval to
        # notice a stop. Tests lower it so teardown does not wait a full
        # second per helper.
        self.accept_timeout = accept_timeout
        self.stopping = False
        # A manual duty this helper set and still owns. Only then does a
        # disconnect or a stop hand the fan back to the driver: restoring
        # auto over another tool's manual setting would be rude.
        self.owned = False
        self.owner: object | None = None
        # The daemon repeats the same duty every few seconds in curve mode.
        # Logging every write filled the journal, so only a change is logged;
        # None after a hand-back means the next manual write is logged again.
        self._last_logged_duty: int | None = None
        self._last_logged_temperature: int | None = None
        self.lock = threading.Lock()
        self.threads: list[threading.Thread] = []
        self.server: socket.socket | None = None
        self.refusal_log_ms = float("-inf")
        self.refusal_suppressed = 0

    def bind(self) -> None:
        if os.path.exists(self.path):
            os.unlink(self.path)
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.settimeout(self.accept_timeout)
        server.bind(self.path)
        try:
            os.chmod(self.path, 0o660)
        except OSError:
            pass
        self._chown_socket()
        server.listen(4)
        self.server = server

    def _chown_socket(self) -> None:
        try:
            entry = pwd.getpwnam(OMATUNER_USER)
        except KeyError:
            # Without the daemon user the socket stays root-only: the
            # helper still runs, and the daemon reports the cause.
            self.log(f"user {OMATUNER_USER} does not exist; the socket is root-only")
            return
        try:
            # The systemd unit creates its runtime directory setgid with the
            # omatuner group, so the socket already has the right gid and
            # this is a no-op. A manual run as plain root chowns it here.
            if os.stat(self.path).st_gid != entry.pw_gid:
                os.chown(self.path, 0, entry.pw_gid)
        except OSError as exc:
            self.log(f"cannot set the socket group to {OMATUNER_USER}: {exc}")

    def serve_forever(self) -> int:
        if self.server is None:
            self.bind()
        assert self.server is not None
        self.log(
            f"listening on {self.path} (uid 0, fans {self.device.count}, "
            f"temperature {getattr(self.device, 'temp_supported', False)})"
        )
        try:
            while not self.stopping:
                try:
                    conn, _ = self.server.accept()
                except socket.timeout:
                    continue
                except OSError:
                    if self.stopping:
                        break
                    raise
                # The peer is checked before any thread is spawned: a local
                # user who is not served must not cost a root thread per
                # connection, and the refusal drain needs a total deadline.
                self.threads = [thread for thread in self.threads if thread.is_alive()]
                try:
                    uid = _peer_uid(conn)
                except OSError as exc:
                    self.log(f"cannot read the peer credentials: {exc}")
                    conn.close()
                    continue
                if uid not in self.allowed_uids:
                    self._log_refusal(uid)
                    self._refuse(conn, "only the omatuner service may use this helper")
                    continue
                if len(self.threads) >= MAX_CLIENTS:
                    self._log_refusal(uid, f": {MAX_CLIENTS} already connected")
                    self._refuse(conn, "the helper is busy with the maximum number of clients")
                    continue
                # One thread per client: a second client must get an answer
                # (a "busy" refusal or a hello) instead of sitting in the
                # backlog behind the daemon's long-lived link.
                thread = threading.Thread(target=self._client, args=(conn, uid), daemon=True)
                thread.start()
                self.threads.append(thread)
        finally:
            self.restore()
            for thread in self.threads:
                thread.join(timeout=2)
            if self.server is not None:
                self.server.close()
                self.server = None
            try:
                os.unlink(self.path)
            except OSError:
                pass
        return 0

    def _log_refusal(self, uid: int, reason: str = "") -> None:
        """Report a refusal at most once per REFUSAL_LOG_SECONDS.

        The accept loop refuses peers it will not serve; a user reconnecting in
        a loop would otherwise write one journal line per attempt.
        """
        now = time.monotonic()
        if now - self.refusal_log_ms < REFUSAL_LOG_SECONDS:
            self.refusal_suppressed += 1
            return
        suppressed = self.refusal_suppressed
        self.refusal_suppressed = 0
        self.refusal_log_ms = now
        held = f" ({suppressed} more since the last line)" if suppressed else ""
        self.log(f"refused a client with uid {uid}{reason}{held}")

    def _refuse(self, conn: socket.socket, message: str) -> None:
        """Answer a connection this helper will not serve, then hang up.

        Called from the accept loop, so no thread is created for a peer that
        was never allowed and nothing here may block. The frame goes out on a
        non-blocking socket before SHUT_WR: the client reads the refusal, then
        EOF, instead of blocking on a socket the helper still owns. The peer's
        unread input is drained only while select() reports it readable, with
        a short total deadline, so a peer that sent nothing costs one select()
        timeout instead of the full deadline in blocking reads.
        """
        try:
            conn.setblocking(False)
        except OSError:
            pass
        _reply(conn, 0, error=("forbidden", message))
        try:
            conn.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        deadline = time.monotonic() + REFUSAL_DRAIN_SECONDS
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                readable, _, _ = select.select([conn], [], [], remaining)
                if not readable:
                    break
                if not conn.recv(4096):
                    break
        except OSError:
            pass
        finally:
            conn.close()

    def _client(self, conn: socket.socket, uid: int | None = None) -> None:
        token = object()
        try:
            # The peer was already checked in the accept loop; by the time a
            # thread exists the connection belongs to an allowed uid.
            self.log(f"client uid {uid} connected")
            conn.settimeout(self.accept_timeout)
            buffer = b""
            while not self.stopping:
                try:
                    chunk = conn.recv(4096)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not chunk:
                    break
                buffer += chunk
                if len(buffer) > MAX_REQUEST_BYTES:
                    self.log("dropping a client that sent an oversized request")
                    break
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    try:
                        request = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    except UnicodeDecodeError:
                        # A line that is not UTF-8 is not JSON either, but it
                        # raises a different error; letting it escape ended
                        # the connection instead of answering the client.
                        _reply(conn, 0, error=("bad-request", "a request must be UTF-8 JSON"))
                        continue
                    if not isinstance(request, dict):
                        # Keep the client; a malformed frame is not a reason
                        # to kill the thread or the helper.
                        _reply(conn, 0, error=("bad-request", "a request must be a JSON object"))
                        continue
                    request_id = request.get("id")
                    method = request.get("method")
                    params = request.get("params") or {}
                    try:
                        result = self._dispatch(token, method, params)
                    except FanDeviceError as exc:
                        _reply(conn, request_id, error=("fan-error", str(exc)))
                        continue
                    except Exception as exc:  # a bad request must not kill the helper
                        _reply(conn, request_id, error=("internal", str(exc)))
                        continue
                    _reply(conn, request_id, result)
        finally:
            # The client went away while it owned a manual duty: the driver
            # gets the fan back rather than leaving it pinned after a crash.
            if self.restore(token):
                self.log("client disconnected while the fan was manual; restoring the driver curve")
            conn.close()

    def _dispatch(self, token: object, method: str | None, params: dict[str, Any]) -> Any:
        if method == "hello":
            with self.lock:
                return self.device.info()
        if method == "set-duty":
            with self.lock:
                if self.owner is not None and self.owner is not token:
                    raise FanDeviceError("the fan is already owned by another client")
                self.owner = token
                # Claim ownership before the write: set_duty walks the fans one
                # by one, so a failure can leave the first ones manual. With
                # `owned` still False, restore() refuses and the stale owner
                # blocks every later client until the helper restarts.
                self.owned = True
                duty = self.device.set_duty(params.get("percent"))
            if duty != self._last_logged_duty:
                self.log(f"set-duty {duty}%")
                self._last_logged_duty = duty
            return {"applied": duty}
        if method == "auto":
            with self.lock:
                if self.owner is not None and self.owner is not token:
                    raise FanDeviceError("another client owns the fan; stop it first")
                self.device.set_auto()
                self.owner = None
                self.owned = False
                self._last_logged_duty = None
            self.log("auto")
            return {"auto": True}
        if method == "set-temp":
            with self.lock:
                target = self.device.set_temperature(params.get("celsius"))
            if target != self._last_logged_temperature:
                self.log(f"set-temp {target} °C")
                self._last_logged_temperature = target
            return {"applied": target}
        raise FanDeviceError(f"unknown method {method!r}")

    def restore(self, token: object | None = None) -> bool:
        """Hand the fan back to the driver if this helper set a manual duty.

        With a token, only that client's ownership is restored (its
        connection died); without one, anything this helper set is undone
        (shutdown). Returns whether a restore was performed.
        """
        with self.lock:
            if not self.owned:
                return False
            if token is not None and self.owner is not token:
                return False
            try:
                self.device.set_auto()
            except FanDeviceError as exc:
                self.log(f"restoring the driver fan curve failed: {exc}")
                if token is not None:
                    # The client's link died, so release its claim: a client
                    # that reconnects can take the fan. `owned` stays True so
                    # the shutdown restore() (token None) still retries.
                    self.owner = None
                return False
            self.log("restored the driver fan curve")
            self.owned = False
            self.owner = None
            self._last_logged_duty = None
            return True

    def stop(self) -> None:
        self.stopping = True
        if self.server is not None:
            try:
                self.server.close()
            except OSError:
                pass
            self.server = None


HELPER_MISSING = (
    "the NVIDIA driver only accepts these hardware writes from root, and the "
    "OmaTuner root helper is not running (sudo bin/install installs it; "
    "sudo systemctl start omatuner-fan.service starts it)"
)


class FanHelperLink:
    """Client the unprivileged daemon uses for the privileged writes.

    The connection stays open for the life of the fan policy: the helper
    returns the fan to the driver when that connection closes, so a daemon
    crash hands the fan back without any extra bookkeeping. The temperature
    target rides the same link.
    """

    def __init__(self, path: str = FAN_SOCKET, timeout: float = 5.0):
        self.path = path
        self.timeout = timeout
        self.sock: socket.socket | None = None
        self._next_id = 0

    def _connect(self) -> tuple[socket.socket | None, str | None]:
        if self.sock is not None:
            return self.sock, None
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(self.path)
        except OSError:
            sock.close()
            return None, HELPER_MISSING
        self.sock = sock
        return sock, None

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Send one request, reconnecting once when the helper restarted.

        A restart drops the link and hands the fan back to the driver. Every
        method here is idempotent, so repeating it on a fresh link cannot
        apply anything twice, and the second failure is the honest one
        (usually "the root helper is not running").
        """
        try:
            return self._exchange(method, params)
        except _LinkLost:
            self.close()
            return self._exchange(method, params)

    def _exchange(self, method: str, params: dict[str, Any] | None = None) -> Any:
        sock, cause = self._connect()
        if sock is None:
            raise FanDeviceError(cause or HELPER_MISSING)
        self._next_id += 1
        request_id = self._next_id
        payload = {"id": request_id, "method": method, "params": params or {}}
        try:
            sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))
            buffer = b""
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    raise ConnectionError("the root helper closed the connection")
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    try:
                        frame = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    # id 0 is the helper's pre-handshake refusal: it never
                    # read a request, so the reply carries no matching id.
                    if frame.get("id") not in (0, request_id):
                        continue
                    if frame.get("ok"):
                        return frame.get("result")
                    if frame.get("id") == 0:
                        # The helper refuses a peer it does not serve before
                        # reading the request, and closes. The socket cannot
                        # carry a second request: drop it with the refusal.
                        self.close()
                    error = frame.get("error") or {}
                    raise FanDeviceError(error.get("message") or "the root helper refused the request")
        except (OSError, ConnectionError) as exc:
            # The helper died or the peer check refused us: drop the link so
            # the next probe reports the cause instead of reusing a dead
            # socket, and surface the honest reason now.
            self.close()
            raise _LinkLost(f"the root helper did not answer: {exc}") from exc

    def available(self) -> tuple[bool, str | None]:
        """Cheap capability check that never writes: a hello over the link.

        `request()` already reconnects once, which covers a helper that was
        restarted since the last probe.
        """
        try:
            self.request("hello")
            return True, None
        except FanDeviceError as exc:
            self.close()
            return False, str(exc)

    def set_duty(self, percent: int) -> int:
        result = self.request("set-duty", {"percent": percent})
        return int(result["applied"])

    def set_auto(self) -> None:
        self.request("auto")
        self.close()

    def set_temperature(self, celsius: int) -> int:
        result = self.request("set-temp", {"celsius": celsius})
        return int(result["applied"])


def _reply(conn: socket.socket, request_id: Any, result: Any = None,
           error: tuple[str, str] | None = None) -> None:
    if error is not None:
        frame = {"id": request_id, "ok": False, "error": {"code": error[0], "message": error[1]}}
    else:
        frame = {"id": request_id, "ok": True, "result": result}
    try:
        conn.sendall((json.dumps(frame) + "\n").encode("utf-8"))
    except OSError:
        pass


def _user_uid(name: str) -> int:
    try:
        return pwd.getpwnam(name).pw_uid
    except KeyError:
        return -1


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="omatuner fan-helper", description=__doc__.splitlines()[0])
    parser.add_argument("--socket", default=FAN_SOCKET)
    parser.add_argument("--allow-uid", type=int, action="append", default=None,
                        help="extra uid allowed to make the privileged writes (tests)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the resolved socket and device, then exit")
    args = parser.parse_args(argv)

    if os.geteuid() != 0:
        print("omatuner fan-helper: must run as root (the NVIDIA driver refuses the fan "
              "and temperature writes from any other uid); install it with sudo bin/install",
              file=sys.stderr)
        return 1

    try:
        device = FanDevice()
    except (OSError, FanDeviceError) as exc:
        print(f"omatuner fan-helper: {exc}", file=sys.stderr)
        return 1

    allowed = {0, _user_uid(OMATUNER_USER)}
    for uid in args.allow_uid or []:
        allowed.add(uid)
    if args.dry_run:
        print(json.dumps({"socket": args.socket, "device": device.info(),
                          "allowedUids": sorted(allowed)}, indent=2))
        device.close()
        return 0

    helper = FanHelper(device, args.socket, allowed)

    def _stop(_signum, _frame):
        helper.stop()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    try:
        return helper.serve_forever()
    finally:
        device.close()


if __name__ == "__main__":
    raise SystemExit(main())
