"""Thin client for the daemon socket, shared by the CLI and tests."""

from __future__ import annotations

import json
import os
import socket
from typing import Any

from . import config

# How long to wait for the daemon's reply. A daemon that accepts and never
# answers raises TimeoutError, which the CLI reports as a one-line failure.
TIMEOUT_S = 10


def socket_path() -> str:
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    user_path = os.path.join(runtime, "omatuner.sock")
    if os.path.exists(user_path):
        return user_path
    if os.path.exists(config.SYSTEM_SOCKET):
        return config.SYSTEM_SOCKET
    return user_path


def request(method: str, params: dict[str, Any] | None = None) -> Any:
    targets = [socket_path()]
    if targets[0] != config.SYSTEM_SOCKET:
        targets.append(config.SYSTEM_SOCKET)
    for index, target in enumerate(targets):
        try:
            return _exchange(target, method, params)
        except (ConnectionRefusedError, FileNotFoundError):
            # A leftover socket file with no daemon behind it must not stop
            # us from trying the system service before giving up.
            if index + 1 == len(targets):
                raise


def _exchange(target: str, method: str, params: dict[str, Any] | None) -> Any:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(TIMEOUT_S)
        sock.connect(target)
        payload = {"id": 1, "method": method, "params": params or {}}
        sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))
        buffer = b""
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                raise ConnectionError("daemon closed the connection")
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                reply = json.loads(line)
                if reply.get("id") != 1:
                    continue  # pushed event frame, not our reply
                if reply.get("ok"):
                    return reply.get("result")
                error = reply.get("error") or {}
                raise RuntimeError(f"{error.get('code')}: {error.get('message')}")


if __name__ == "__main__":
    print(request("hello"))
