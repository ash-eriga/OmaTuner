"""Shared construction for the core test suites.

The four suites that drive TunerCore build the same object graph: a pair of
fake backends, a journal under a temporary state directory, a timer stub and a
boot. The differences are parameters here, not four copies of the setup.
"""

from __future__ import annotations

import os

from omatuner import config
from omatuner.backends.fake import FakeBackend
from omatuner.core import TunerCore
from omatuner.statestore import Journal


def _backend(component: str, provided, kwargs) -> FakeBackend:
    if provided is None:
        return FakeBackend(component, **(kwargs or {}))
    if isinstance(provided, dict):
        return FakeBackend(component, **provided)
    return provided


def make_core(
    tmp: str,
    mode: str | None = None,
    *,
    confirm_ms: int | None = None,
    cpu=None,
    gpu=None,
    cpu_kwargs: dict | None = None,
    gpu_kwargs: dict | None = None,
    system_telemetry=None,
    clean_marker: bool = False,
    schedule=None,
) -> tuple[TunerCore, FakeBackend, FakeBackend]:
    """Boot a TunerCore on fake hardware.

    `cpu`/`gpu` take a backend instance or a FakeBackend keyword dict;
    `clean_marker` writes the orderly-shutdown file that boot looks for.
    """
    dirs = config.Dirs(
        system=False, state_dir=tmp, socket_path=os.path.join(tmp, "test.sock")
    )
    if mode == config.TUNING_MODE_EXPERT:
        with open(dirs.mode_file, "w", encoding="utf-8") as fh:
            fh.write("expert\n")
    if clean_marker:
        with open(os.path.join(tmp, "clean-shutdown"), "w") as fh:
            fh.write("clean\n")
    cpu_backend = _backend("cpu", cpu, cpu_kwargs)
    gpu_backend = _backend("gpu", gpu, gpu_kwargs)
    core = TunerCore(
        dirs,
        {"cpu": cpu_backend, "gpu": gpu_backend},
        Journal(dirs.events_file),
        system_telemetry=system_telemetry,
        confirm_ms=confirm_ms if confirm_ms is not None else config.CONFIRM_WINDOW_MS,
    )
    core.schedule_timer = schedule or (lambda delay, callback: (lambda: None))
    core.boot()
    return core, cpu_backend, gpu_backend
