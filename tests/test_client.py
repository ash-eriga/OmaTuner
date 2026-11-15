"""Client exchange tests over a real unix socket.

The daemon pushes event frames on the same connection as the reply, and a
daemon that accepts without answering must surface as a timeout the CLI can
report in one line.
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

from omatuner import client


class ExchangeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="omatuner-client-")
        self.path = os.path.join(self.tmp, "omatuner.sock")
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(self.path)
        self.server.listen(1)
        self.thread = None

    def tearDown(self):
        self.server.close()
        if self.thread is not None:
            self.thread.join(timeout=2)

    def start(self, handler):
        self.thread = threading.Thread(target=handler, daemon=True)
        self.thread.start()

    def test_an_event_frame_before_the_reply_is_skipped(self):
        def serve():
            connection, _ = self.server.accept()
            with connection:
                connection.recv(65536)
                event = {"event": "telemetry", "data": {"cpuTemp": 50}}
                connection.sendall(json.dumps(event).encode() + b"\n")
                reply = {"id": 1, "ok": True, "result": {"state": "ok"}}
                connection.sendall(json.dumps(reply).encode() + b"\n")

        self.start(serve)
        with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": self.tmp}):
            result = client.request("hello")
        self.assertEqual(result, {"state": "ok"})

    def test_a_daemon_that_never_answers_times_out(self):
        release = threading.Event()

        def serve():
            connection, _ = self.server.accept()
            with connection:
                connection.recv(65536)
                release.wait(2)

        self.start(serve)
        try:
            with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": self.tmp}), \
                    mock.patch.object(client, "TIMEOUT_S", 0.3):
                started = time.monotonic()
                with self.assertRaises(TimeoutError):
                    client.request("hello")
                self.assertLess(time.monotonic() - started, 1.5)
        finally:
            release.set()


if __name__ == "__main__":
    unittest.main()
