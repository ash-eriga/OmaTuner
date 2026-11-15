"""Atomic JSON state files and the append-only event journal.

Every state file is the recovery source for a specific failure mode, so
writes are rename-atomic: a reader either sees the old record or the new one,
never a partial file.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any


def now_ms() -> int:
    return int(time.time() * 1000)


def read_json(path: str) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _fsync_dir(path: str) -> None:
    """Make the rename itself durable, not just the file contents.

    Without this a power loss can undo the rename even though the hardware
    write that followed it happened, which is exactly the case the recovery
    record exists for. Best effort: a filesystem that refuses the directory
    fd must not turn a state write into a failure.
    """
    try:
        fd = os.open(os.path.dirname(path) or ".", os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def write_json(path: str, data: Any) -> None:
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    _fsync_dir(path)


def remove(path: str) -> None:
    # Cleanup is best effort: a permission or I/O failure deleting a stale
    # marker must not abort the state transition that is already in flight
    # (a confirm or a revert after the trial's hardware write).
    try:
        os.unlink(path)
    except OSError:
        pass


class Journal:
    """Append-only record of every tuning decision, for diagnosis."""

    def __init__(self, path: str):
        self.path = path

    def log(self, kind: str, **data: Any) -> None:
        record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "tsMs": now_ms(), "kind": kind}
        record.update(data)
        try:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, sort_keys=True) + "\n")
        except OSError:
            pass

    def tail(self, count: int = 50) -> list[dict[str, Any]]:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                lines = fh.readlines()[-count:]
        except (OSError, ValueError):
            return []
        records = []
        for line in lines:
            try:
                records.append(json.loads(line))
            except ValueError:
                continue
        return records

    def last_of(self, kind: str) -> dict[str, Any] | None:
        for record in reversed(self.tail(500)):
            if record.get("kind") == kind:
                return record
        return None
