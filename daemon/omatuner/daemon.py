"""Daemon: a single-threaded select loop serving the UI and CLI over one
unix socket, plus the timers that keep a pending trial honest even when the
UI is gone.

Protocol: newline-delimited JSON. Requests carry an id and get one reply
({"id", "ok", "result"|"error"}); the daemon pushes {"event", "data"} frames
(state snapshots and telemetry) to every connected client. Telemetry frames
flow only while at least one client subscribed, matching the design rule
that collection pauses when the popup is closed.
"""

from __future__ import annotations

import json
import os
import select
import signal
import socket
import sys
import time
from typing import Any

from . import access, config
from .core import CoreError, TunerCore
from .statestore import Journal

# A client that sends this much without a newline is not speaking the
# protocol; drop it before its buffer eats the daemon's memory.
MAX_CLIENT_BUFFER = 1024 * 1024


class TimerWheel:
    def __init__(self) -> None:
        self.timers: list[dict[str, Any]] = []

    def schedule(self, delay_ms: int, callback) -> Any:
        entry = {"deadline": time.monotonic() + delay_ms / 1000.0, "callback": callback, "cancelled": False}
        self.timers.append(entry)
        return lambda: entry.__setitem__("cancelled", True)

    def due(self) -> float | None:
        pending = [t["deadline"] for t in self.timers if not t["cancelled"]]
        return min(pending) if pending else None

    def poll(self) -> None:
        now = time.monotonic()
        fired = [t for t in self.timers if not t["cancelled"] and t["deadline"] <= now]
        self.timers = [t for t in self.timers if not t["cancelled"] and t["deadline"] > now]
        for entry in fired:
            try:
                entry["callback"]()
            except Exception:
                pass


class Server:
    def __init__(self, core: TunerCore, dirs: config.Dirs):
        self.core = core
        self.dirs = dirs
        self.wheel = TimerWheel()
        self.clients: dict[socket.socket, dict[str, Any]] = {}
        self.listener: socket.socket | None = None
        self.running = False
        self._last_suspend_delta = self._suspend_delta()
        core.schedule_timer = self.wheel.schedule
        core.on_change.append(lambda snapshot: self.broadcast("state", snapshot))

    @staticmethod
    def _suspend_delta() -> float:
        try:
            boottime = time.clock_gettime(time.CLOCK_BOOTTIME)
            monotonic = time.clock_gettime(time.CLOCK_MONOTONIC)
            return boottime - monotonic
        except (OSError, AttributeError):
            return 0.0

    def start(self) -> None:
        path = self.dirs.socket_path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.exists(path):
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                probe.connect(path)
            except OSError:
                os.unlink(path)  # nothing is listening behind it: stale file
            else:
                probe.close()
                raise CoreError("already-running", f"another daemon is listening on {path}")
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(path)
        os.chmod(path, 0o660)
        # Before listen(), so a client can never race the grant. The socket
        # is group-readable, but the installer's session still has the
        # supplementary groups it logged in with; the ACL entry lets that
        # session use the service without logging out first (access.py).
        self._grant_installer_access(path)
        self.listener.listen(8)
        self.listener.setblocking(False)
        self.running = True
        self.core.boot()
        self.core._changed()

    def _grant_installer_access(self, path: str) -> None:
        if not self.dirs.client_users:
            return
        try:
            granted = access.grant(os.path.dirname(path), path, self.dirs.client_users)
        except OSError as exc:
            # A filesystem without ACL support only costs the immediate
            # grant; the group still applies from the next login on.
            print(f"omatuner: could not grant socket access ({exc})", file=sys.stderr)
            return
        for name in granted:
            print(f"omatuner: socket access granted to {name}", file=sys.stderr)

    def stop(self) -> None:
        self.running = False

    def broadcast(self, event: str, data: Any) -> None:
        if not self.clients:
            return
        frame = json.dumps({"event": event, "data": data}) + "\n"
        dead = []
        for client, state in self.clients.items():
            try:
                client.sendall(frame.encode("utf-8"))
            except OSError:
                dead.append(client)
        for client in dead:
            self._drop(client)

    def _drop(self, client: socket.socket) -> None:
        self.clients.pop(client, None)
        try:
            client.close()
        except OSError:
            pass

    def _telemetry_subscribed(self) -> bool:
        return any(state.get("telemetry") for state in self.clients.values())

    def handle_request(self, method: str, params: dict[str, Any]) -> Any:
        core = self.core
        if method == "hello":
            core.client_connected()
            return {"state": core.snapshot()}
        if method == "subscribe":
            return {"ok": True}
        if method == "doctor":
            return self.doctor()
        if method == "apply":
            return core.apply(
                params.get("component", ""),
                params.get("settings", {}),
                params.get("mode", "session"),
                params.get("saveDefault", False),
            )
        if method == "confirm":
            return core.confirm()
        if method == "revert":
            return core.revert(params.get("reason", "requested"))
        if method == "keep-paused":
            return core.keep_paused()
        if method == "dismiss-notice":
            return core.dismiss_notice()
        if method == "ack-expert":
            return core.ack_expert()
        if method == "internal/resume":
            core.on_resume()
            return {"ok": True}
        raise CoreError("bad-method", f"unknown method {method!r}")

    def doctor(self) -> dict[str, Any]:
        state = self.core.snapshot()
        cpu = self.core.backends.get("cpu")
        gpu = self.core.backends.get("gpu")
        return {
            "state": state,
            "tuningMode": self.core.tuning_mode,
            "msrLoaded": os.path.exists("/dev/cpu/0/msr"),
            "devices": {
                "cpu": cpu.device_name if cpu else "",
                "gpu": gpu.device_name if gpu else "",
            },
        }

    def _on_line(self, client: socket.socket, line: str) -> None:
        try:
            request = json.loads(line)
        except (ValueError, RecursionError):
            return
        if not isinstance(request, dict):
            # Only an object is a request; anything else is dropped.
            return
        method = request.get("method")
        request_id = request.get("id")
        params = request.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            # A request carries an object of parameters; anything else is dropped.
            return
        state = self.clients.setdefault(client, {})
        try:
            if method == "subscribe":
                state["telemetry"] = bool(params.get("telemetry"))
            result = self.handle_request(method, params)
            reply = {"id": request_id, "ok": True, "result": result}
        except CoreError as exc:
            reply = {"id": request_id, "ok": False, "error": {"code": exc.code, "message": exc.message}}
        except Exception as exc:  # a broken handler must not kill the daemon
            reply = {"id": request_id, "ok": False, "error": {"code": "internal", "message": str(exc)}}
        try:
            client.sendall((json.dumps(reply) + "\n").encode("utf-8"))
        except OSError:
            self._drop(client)

    def run(self) -> None:
        self.start()
        assert self.listener is not None
        listeners = [self.listener]

        def _stop(*_args: Any) -> None:
            # systemd (or the user) stops services with SIGTERM exactly when
            # the stop is orderly; a power loss or crash never reaches here,
            # which is what makes the missing marker mean "abrupt".
            try:
                self.core.mark_clean_shutdown()
            except Exception:
                pass
            self.stop()

        try:
            signal.signal(signal.SIGTERM, _stop)
            signal.signal(signal.SIGINT, _stop)
        except ValueError:
            # signal handlers only exist in the main thread; tests run the
            # server loop on a worker thread and stop it via Server.stop().
            pass

        last_telemetry = 0.0
        # Cheap once-a-second stat of the hardware device nodes, so a module
        # loaded while this daemon runs becomes usable without a restart.
        last_refresh = time.monotonic()
        while self.running:
            timeout = 0.5
            due = self.wheel.due()
            if due is not None:
                timeout = max(0.0, min(timeout, due - time.monotonic()))
            try:
                readable, _, _ = select.select(listeners + list(self.clients.keys()), [], [], timeout)
            except InterruptedError:
                continue
            for source in readable:
                if source is self.listener:
                    try:
                        client, _ = self.listener.accept()
                        client.setblocking(False)
                        self.clients[client] = {"telemetry": False}
                    except OSError:
                        continue
                else:
                    try:
                        chunk = source.recv(65536)
                    except BlockingIOError:
                        continue
                    except OSError:
                        self._drop(source)
                        continue
                    if not chunk:
                        self._drop(source)
                        continue
                    state = self.clients.setdefault(source, {})
                    state["buffer"] = state.get("buffer", b"") + chunk
                    if len(state["buffer"]) > MAX_CLIENT_BUFFER:
                        self._drop(source)
                        continue
                    while b"\n" in state["buffer"]:
                        raw, state["buffer"] = state["buffer"].split(b"\n", 1)
                        try:
                            self._on_line(source, raw.decode("utf-8", "replace"))
                        except Exception as exc:
                            # One client's line must never end the server loop.
                            self.core.journal.log("client-line-failed", error=str(exc))

            self.wheel.poll()

            delta = self._suspend_delta()
            if self._last_suspend_delta and delta > self._last_suspend_delta + 5.0:
                self._last_suspend_delta = delta
                self.core.on_resume()
            elif delta != 0.0:
                self._last_suspend_delta = delta

            now = time.monotonic()
            if now - last_refresh >= 1.0:
                last_refresh = now
                self.core.refresh_backends()
            if self._telemetry_subscribed() and now - last_telemetry >= config.TELEMETRY_INTERVAL_S:
                last_telemetry = now
                try:
                    self.broadcast("telemetry", self.core.telemetry())
                except Exception as exc:
                    self.core.journal.log("telemetry-failed", error=str(exc))

        for client in list(self.clients):
            self._drop(client)
        try:
            self.listener.close()
        except OSError:
            pass
        try:
            os.unlink(self.dirs.socket_path)
        except OSError:
            pass


def build_backends(kind: str) -> dict:
    if kind == "fake":
        from .backends.fake import FakeBackend

        return {"cpu": FakeBackend("cpu"), "gpu": FakeBackend("gpu")}
    from .backends.cpu_msr import CpuMsrBackend
    from .backends.gpu_nvml import GpuNvmlBackend

    def cpu_info() -> dict[str, Any]:
        info: dict[str, Any] = {"name": ""}
        try:
            with open("/proc/cpuinfo", "r", encoding="ascii") as fh:
                for line in fh:
                    if line.startswith("model name"):
                        info["name"] = line.split(":", 1)[1].strip()
                    if line.startswith("vendor_id"):
                        info["intel"] = "Intel" in line
        except OSError:
            pass
        return info

    return {"cpu": CpuMsrBackend(cpu_info()), "gpu": GpuNvmlBackend()}


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="omatuner", description="OmaTuner tuning daemon")
    parser.add_argument("--version", action="version", version=config.VERSION)
    sub = parser.add_subparsers(dest="command")

    run_parser = sub.add_parser("run", help="run the daemon")
    run_parser.add_argument("--backend", choices=["real", "fake"], default="real")
    run_parser.add_argument("--state-dir")
    run_parser.add_argument("--socket")
    run_parser.add_argument("--system", action="store_true", help="force system mode paths")
    run_parser.add_argument("--user", action="store_true", help="force user mode paths")

    status_parser = sub.add_parser("status", help="query the running daemon")
    status_parser.add_argument("--json", action="store_true")

    sub.add_parser("doctor", help="diagnosis report")
    sub.add_parser("confirm", help="confirm the pending application")
    sub.add_parser("revert", help="revert the pending application")
    sub.add_parser("keep-paused", help="keep defaults paused after an abrupt restart")
    sub.add_parser("dismiss-notice", help="dismiss the current notice")

    apply_parser = sub.add_parser("apply", help="apply settings for a component")
    apply_parser.add_argument("--component", choices=["cpu", "gpu"], required=True)
    apply_parser.add_argument("--mode", choices=["session", "default", "reset"], default="session")
    apply_parser.add_argument("settings", nargs="*", help="key=value pairs, e.g. core=-50")

    internal = sub.add_parser("internal")
    internal_sub = internal.add_subparsers(dest="internal_command", required=True)
    internal_sub.add_parser("mark-clean")
    internal_sub.add_parser("resume")

    # The root helper parses its own flags (it is routed to
    # fanhelper.main below); they are declared here so the parent parser
    # accepts the command line the unit passes.
    helper_parser = sub.add_parser("fan-helper", help="root helper for the privileged writes (systemd unit)")
    helper_parser.add_argument("--socket", default=None)
    helper_parser.add_argument("--allow-uid", type=int, action="append", default=None)
    helper_parser.add_argument("--dry-run", action="store_true")

    raw = list(argv if argv is not None else sys.argv[1:])
    args = parser.parse_args(raw)
    if args.command == "fan-helper":
        from . import fanhelper

        return fanhelper.main(raw[1:])
    if args.command == "run":
        system = args.system or (not args.user and config.is_system_mode())
        dirs = config.Dirs(system, state_dir=args.state_dir, socket_path=args.socket)
        backends = build_backends(args.backend)
        system_telemetry = None
        if args.backend == "real":
            from .backends.telemetry import SystemTelemetry

            system_telemetry = SystemTelemetry()
        else:
            system_telemetry = _FakeSystemTelemetry()
        journal = Journal(dirs.events_file)
        core = TunerCore(dirs, backends, journal, system_telemetry=system_telemetry)
        server = Server(core, dirs)
        try:
            server.run()
        except CoreError as exc:
            print(f"omatuner: {exc.message}", file=sys.stderr)
            return 1
        return 0

    from .client import request

    def ask(method: str, params: dict[str, Any] | None = None) -> Any:
        # A daemon-side refusal (a control unavailable, a conflict, nothing
        # listening) is an answer, not a crash: print it and fail quietly so
        # scripts like bin/uninstall can treat the exit code as the truth.
        try:
            return request(method, params)
        except TimeoutError:
            print(
                "omatuner: the daemon did not answer within the timeout.",
                file=sys.stderr,
            )
            raise SystemExit(1)
        except (FileNotFoundError, ConnectionRefusedError, ConnectionError):
            print(
                "omatuner: no daemon is listening. Start it with 'sudo bin/install' "
                "(system service) or 'systemctl --user start omatuner.service' (user mode).",
                file=sys.stderr,
            )
            raise SystemExit(1)
        except RuntimeError as exc:
            print(f"omatuner: {exc}", file=sys.stderr)
            raise SystemExit(1)

    if args.command == "status":
        result = ask("hello")
        if args.json:
            print(json.dumps(result, indent=2, sort_keys=True))
        else:
            state = result["state"]
            _print_status(state)
        return 0
    if args.command == "doctor":
        result = ask("doctor")
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    if args.command == "apply":
        settings: dict[str, Any] = {}
        for pair in args.settings:
            key, sep, value = pair.partition("=")
            if not sep or not key:
                print(
                    f"omatuner: settings are key=value pairs, e.g. core=-50 (got {pair!r})",
                    file=sys.stderr,
                )
                return 2
            try:
                settings[key] = int(value)
            except ValueError:
                # The CLI only carries the numeric controls; the fan policy
                # and the V/F points are structured and belong to the plugin.
                print(
                    f"omatuner: {key} takes a whole number (got {value!r}). "
                    "The fan policy and the V/F curve are applied from the plugin.",
                    file=sys.stderr,
                )
                return 2
        result = ask("apply", {"component": args.component, "settings": settings, "mode": args.mode})
        applied = result.get("applied") if isinstance(result, dict) else None
        if isinstance(applied, dict) and isinstance(applied.get("points"), list):
            # 80 V/F points would bury the three values the reader wants.
            shown = dict(applied, points=f"{len(applied['points'])} points")
            result = dict(result, applied=shown)
        print(json.dumps(result, indent=2))
        if args.mode == "reset":
            print("Factory values applied; reset needs no confirmation.")
        else:
            print("Confirm within 5 seconds with: omatuner confirm")
            print("Revert with: omatuner revert")
        return 0
    if args.command == "confirm":
        print(json.dumps(ask("confirm"), indent=2))
        return 0
    if args.command == "revert":
        print(json.dumps(ask("revert", {"reason": "cli"}), indent=2))
        return 0
    if args.command == "keep-paused":
        print(json.dumps(ask("keep-paused"), indent=2))
        return 0
    if args.command == "dismiss-notice":
        print(json.dumps(ask("dismiss-notice"), indent=2))
        return 0
    if args.command == "internal":
        dirs = config.Dirs(config.is_system_mode())
        if args.internal_command == "mark-clean":
            journal = Journal(dirs.events_file)
            TunerCore(dirs, {"cpu": None, "gpu": None}, journal).mark_clean_shutdown()
            return 0
        if args.internal_command == "resume":
            ask("internal/resume")
            return 0
    parser.print_help()
    return 2


def _format_control_value(value: int, unit: str) -> str:
    """A temperature reads as a plain value; an offset keeps its sign."""
    return str(int(value)) if unit == "°C" else f"{int(value):+d}"


def _print_status(state: dict[str, Any]) -> None:
    tuning = state.get("tuningMode", config.TUNING_MODE_NORMAL)
    label = (
        "expert mode: widest ranges"
        if tuning == config.TUNING_MODE_EXPERT
        else "normal ranges"
    )
    print(f"OmaTuner {state['version']} ({state['mode']} mode; {label})")
    for component in ("cpu", "gpu"):
        device = state["devices"].get(component, "")
        flags = []
        if state["paused"].get(component):
            flags.append("PAUSED")
        if state.get("suspended", {}).get(component):
            flags.append("SUSPENDED")
        suffix = f" [{', '.join(flags)}]" if flags else ""
        print(f"{component.upper()}: {device}{suffix}")
        for control in state["controls"].get(component, []):
            value = control["value"]
            unit = control["unit"]
            status = "" if control["available"] else f"  (unavailable: {control['cause']})"
            print(f"  {control['label']}: {_format_control_value(value, unit)} {unit}{status}")
        curve = state.get("curves", {}).get(component)
        if curve and curve.get("supported"):
            access = "writable" if curve.get("writable") else "read-only"
            cause = f" ({curve['cause']})" if curve.get("cause") else ""
            print(f"  Curve: {len(curve['points'])} points, {access}{cause}")
        fan = state.get("fans", {}).get(component)
        if fan and fan.get("supported"):
            cfg = fan.get("config") or {}
            mode = cfg.get("mode", "auto")
            detail = ""
            if mode == "fixed":
                detail = f" at {cfg.get('speed')}%"
            elif mode == "curve":
                detail = f" with {len(cfg.get('points') or [])} points"
            speed = fan.get("speed")
            rpm = fan.get("rpm")
            reading = f"{speed}%" if speed is not None else "speed unavailable"
            if rpm:
                reading += f" · {rpm} RPM"
            access = "writable" if fan.get("writable") else "read-only"
            cause = f" ({fan['cause']})" if fan.get("cause") else ""
            print(f"  Fan: {reading} · {mode}{detail} · {access}{cause}")
    if state.get("trial"):
        trial = state["trial"]
        remaining = max(0, (trial["deadlineMs"] - int(time.time() * 1000)) / 1000)
        print(f"Trial: {trial['component']} {trial['mode']}, {remaining:.1f}s left")
    if state.get("notice"):
        print(f"Notice: {state['notice']['type']}: {state['notice'].get('message', '')}")
    for conflict in state.get("conflicts", []):
        print(f"Conflict: {conflict['name']}: {conflict['detail']}")


class _FakeSystemTelemetry:
    def cpu_package_temp(self) -> int:
        return 52

    def cpu_package_limit(self) -> int:
        return 100

    def sample(self) -> dict[str, Any]:
        import math

        from .backends.fake import cpu_telemetry

        t = time.time()
        data = cpu_telemetry(t)
        data["ramUsage"] = round(47 + 2 * math.sin(t * 0.07))
        return data


if __name__ == "__main__":
    raise SystemExit(main())
