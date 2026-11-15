"""Failure and conflict diagnosis.

Combines OmaTuner's own journal with what the system exposes (kernel log via
journalctl). Diagnosis never touches hardware: it only explains why
something is unavailable or suspended, and it separates confirmed causes
from suspicions.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import Any

# Programs known to reapply CPU/GPU tuning. Present ones block application on
# the components they own; OmaTuner never stops or reconfigures them.
CONFLICT_PROGRAMS: dict[str, list[str]] = {
    "lactd": ["cpu", "gpu"],
    "corectrl": ["cpu", "gpu"],
    "tuxclockerd": ["gpu"],
    "gwe": ["gpu"],
}

XID_PATTERN = "NVRM: Xid"
MCE_PATTERNS = ("mce: [Hardware Error]", "Machine check events logged")
PROC_ROOT = "/proc"


def _running(names: set[str], proc_root: str) -> set[str]:
    """Which of `names` have a process with that exact comm name."""
    running: set[str] = set()
    try:
        entries = os.listdir(proc_root)
    except OSError:
        return running
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(os.path.join(proc_root, entry, "comm"), "r", encoding="utf-8", errors="replace") as fh:
                name = fh.read().strip()
        except OSError:
            continue
        if name in names:
            running.add(name)
    return running


def detect_conflicts(proc_root: str = PROC_ROOT) -> list[dict[str, Any]]:
    installed = [program for program in CONFLICT_PROGRAMS if shutil.which(program) is not None]
    if not installed:
        return []
    running = _running(set(installed), proc_root)
    conflicts = []
    for program in installed:
        if program not in running:
            continue
        conflicts.append(
            {
                "name": program,
                "components": CONFLICT_PROGRAMS[program],
                "detail": (
                    f"{program} is running and configured to manage the same hardware. "
                    "Resolve the conflict (stop or disable it) before applying."
                ),
            }
        )
    return conflicts


def scan_kernel_errors(since_s: float | None) -> dict[str, Any] | None:
    """Return the first Xid/MCE evidence after the epoch second `since_s`, or None.

    Returns None as well when the journal is not readable by this process;
    absent evidence is reported as such, never as "no errors happened".
    """
    journalctl = shutil.which("journalctl")
    if not journalctl:
        return None
    # journalctl rejects ISO timestamps with numeric-zone offsets like
    # "...-0300"; its epoch form is the one format it always parses.
    since = f"@{int(since_s)}" if since_s is not None else "-2h"
    command = [journalctl, "-k", "--since", since, "-n", "200", "--no-pager", "-o", "short"]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    evidence = None
    for line in result.stdout.splitlines():
        if XID_PATTERN in line:
            evidence = {"kind": "xid", "line": line.strip()[:300], "component": "gpu"}
            break
        for pattern in MCE_PATTERNS:
            if pattern in line:
                evidence = {"kind": "mce", "line": line.strip()[:300], "component": "cpu"}
                break
        if evidence:
            break
    return evidence
