"""Tuning state machine: drafts live in the UI, everything applied or
recoverable lives here.

Invariants from the design document:
- The recovery record (previous values) is written to disk *before* the
  hardware write, so any interruption leaves enough state to restore.
- An unconfirmed trial always resolves: a fresh Enter confirms, Esc, popup
  close or the 5 s deadline reverts. The daemon owns the deadline, not the UI.
- An interrupted apply is discarded and never reapplied.
- After an abrupt restart both components' defaults stay paused until the
  user lets the 5 s notice reapply them or keeps them paused.
- A confirmed apply clears that component's pause and preventive suspension.
"""

from __future__ import annotations

import math
import os
import time
from typing import Any

from . import config, diagnose, fan
from .backends.base import Backend, BackendError
from .statestore import Journal, now_ms, read_json, remove, write_json

COMPONENTS = ("cpu", "gpu")
_MISSING = object()

# Reported next to thermalGuard when a guarded component has no reading. The
# guard only protects what it can see; presenting an unreadable sensor as
# armed would tell the user a change is being watched when it is not.
_THERMAL_SENSOR_CAUSE = {
    "cpu": "the CPU package temperature sensor is unreadable; the thermal guard cannot watch the CPU",
    "gpu": "the GPU temperature is unreadable; the thermal guard cannot watch the GPU",
}


def _bool_map(value: Any) -> dict[str, bool] | None:
    """Shape check for paused.json: one boolean per component."""
    if not isinstance(value, dict):
        return None
    out: dict[str, bool] = {}
    for component in COMPONENTS:
        flag = value.get(component, False)
        if not isinstance(flag, bool):
            return None
        out[component] = flag
    return out


def _record_map(value: Any, *, settings: bool = False) -> dict[str, dict | None] | None:
    """Shape check for the per-component state maps.

    With ``settings`` the entry must be a last-applied record carrying a
    settings object; without it, any object (a default, a suspension) or
    null passes.
    """
    if not isinstance(value, dict):
        return None
    out: dict[str, dict | None] = {}
    for component in COMPONENTS:
        record = value.get(component)
        if record is None:
            out[component] = None
            continue
        if not isinstance(record, dict):
            return None
        if settings and not isinstance(record.get("settings"), dict):
            return None
        out[component] = record
    return out


def _control_record_ok(record: dict) -> bool:
    """A persisted settings record: every control is a whole number."""
    for key, value in record.items():
        if key in ("points", "fan", "curveScale"):
            continue
        if isinstance(value, bool) or not isinstance(value, int):
            return False
    return True


def _str_list(value: Any) -> list[str] | None:
    """Shape check for dismissed.json: the answered notice ids."""
    if not isinstance(value, list):
        return None
    if not all(isinstance(item, str) for item in value):
        return None
    return value


def _optional_dict(value: Any) -> dict | None:
    """Shape check for last-reverted.json."""
    return value if isinstance(value, dict) else None


def migrate_curve_settings(settings: dict | None, curve) -> tuple[dict | None, bool]:
    """Bring persisted curve points into the units the live curve reports.

    Older builds stored the driver's raw frequencies (driver 580 reports the
    Pascal V/F table at twice the real SM clock). Applying that record after
    the scale fix would move every point to the wrong frequency, so the
    stored ``curveScale`` marker decides the conversion; records without one
    are recognised by a maximum well above what the current curve can
    address. A record that cannot be converted confidently loses only its
    points; the scalar controls are always kept.

    Returns ``(settings, changed)``.
    """
    if not isinstance(settings, dict):
        return settings, False
    out = dict(settings)
    points = settings.get("points")
    if not points:
        if "curveScale" in out:
            out.pop("curveScale", None)
            return out, True
        return out, False
    if curve is None or not getattr(curve, "supported", False):
        out.pop("points", None)
        out.pop("curveScale", None)
        return out, True

    try:
        current = float(getattr(curve, "scale", 1.0) or 1.0)
    except (TypeError, ValueError):
        current = 1.0
    if current <= 0:
        current = 1.0

    stored = settings.get("curveScale")
    try:
        stored = float(stored) if stored is not None else None
    except (TypeError, ValueError):
        stored = None
    if stored is not None and stored <= 0:
        stored = None

    max_saved = 0.0
    for point in points:
        try:
            max_saved = max(max_saved, float(point["f"]))
        except (KeyError, TypeError, ValueError):
            out.pop("points", None)
            out.pop("curveScale", None)
            return out, True

    if stored is None:
        # No marker: point values above anything the current descriptor can
        # address were written against the raw driver table.
        if max_saved > float(curve.max_frequency) * 1.3:
            factor = 1.0 / current
        else:
            factor = 1.0
    else:
        factor = stored / current

    if factor == 1.0:
        # The units are right; the record can still be corrupt (a hand-edited
        # file, or one written by a future build). Out-of-range points are
        # dropped rather than handed to the driver.
        try:
            valid = all(
                float(curve.min_frequency) - 1 <= float(point["f"]) <= float(curve.max_frequency) + 1
                for point in points
            )
        except (KeyError, TypeError, ValueError):
            valid = False
        if valid:
            out["curveScale"] = current
            return out, settings.get("curveScale") != current
        out.pop("points", None)
        out.pop("curveScale", None)
        return out, True

    converted: list[dict] = []
    previous = -1.0
    for point in points:
        try:
            voltage = float(point["v"])
            frequency = float(point["f"]) * factor
        except (KeyError, TypeError, ValueError):
            out.pop("points", None)
            out.pop("curveScale", None)
            return out, True
        if frequency < 0 or frequency < float(curve.min_frequency) - 1:
            if float(point["f"]) * factor < 1:
                frequency = 1.0
            else:
                out.pop("points", None)
                out.pop("curveScale", None)
                return out, True
        if frequency > float(curve.max_frequency) + 1:
            out.pop("points", None)
            out.pop("curveScale", None)
            return out, True
        frequency = max(frequency, previous)
        previous = frequency
        converted.append({"v": voltage, "f": frequency})
    out["points"] = converted
    out["curveScale"] = current
    return out, True


class CoreError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _event_id(kind: str) -> str:
    return f"{kind}-{now_ms()}"


class Trial:
    def __init__(self, component: str, mode: str, previous: dict, candidate: dict, deadline_ms: int):
        self.component = component
        self.mode = mode
        self.previous = previous
        self.candidate = candidate
        self.deadline_ms = deadline_ms


class TunerCore:
    def __init__(
        self,
        dirs: config.Dirs,
        backends: dict[str, Backend],
        journal: Journal,
        system_telemetry=None,
        confirm_ms: int = config.CONFIRM_WINDOW_MS,
    ):
        self.dirs = dirs
        self.backends = backends
        self.journal = journal
        self.system_telemetry = system_telemetry
        self.confirm_ms = confirm_ms
        self.applied: dict[str, dict[str, Any]] = {}
        self.defaults: dict[str, dict | None] = {"cpu": None, "gpu": None}
        self.last_applied: dict[str, dict | None] = {"cpu": None, "gpu": None}
        self.paused: dict[str, bool] = {"cpu": False, "gpu": False}
        self.suspended: dict[str, dict | None] = {"cpu": None, "gpu": None}
        self.dismissed: list[str] = []
        self.trial: Trial | None = None
        self.notice: dict | None = None
        # Notices raised while the abrupt-restart question is on screen. That
        # question is modal and its countdown answers it, so a notice that
        # would replace it is held here and shown once it is answered.
        self.notice_queue: list[dict] = []
        self.conflicts: list[dict] = []
        self.abrupt_boot_pending = False
        self.boot_notice_deadline: int | None = None
        self.tuning_mode = config.TUNING_MODE_NORMAL
        self.expert_warned = False
        # The last reverted candidate. The UI keeps that draft after a revert
        # and needs to know it was applied once and rolled back, so it can
        # show "not applied · reverted" and offer a one-click retry.
        self.last_reverted: dict | None = None
        # Thermal verification: component -> {previous, until, mode}. Populated
        # at confirm and cleared by a breach or when the window expires.
        self.thermal_guard: dict[str, dict] = {}
        # The sample the guard last evaluated. A guard is only reported as
        # armed for a component whose sensor produced a reading here.
        self.thermal_sample: dict[str, Any] | None = None
        self.thermal_guard_ms = config.THERMAL_GUARD_MS
        self.thermal_cpu_ceiling_c = config.CPU_THERMAL_CEILING_C
        self.thermal_gpu_ceiling_c = config.GPU_THERMAL_CEILING_C
        self.thermal_hard_margin_c = config.THERMAL_HARD_LIMIT_MARGIN_C
        # Live fan policy. `fan_loop` is set while a fixed or curve fan
        # configuration is being enforced; it is not part of `applied` because
        # it only exists in the running daemon. The configuration it follows
        # lives in applied["gpu"]["fan"] and follows the normal save/recover
        # path.
        self.fan_loop: fan.FanLoop | None = None
        # True when a stopped loop could not hand the fan back to the driver
        # (the helper was gone). NVML keeps the last manual duty, so the UI
        # must not claim the driver controls the fan in that state.
        self.fan_handback_failed = False
        self.on_change: list = []  # callbacks fired after every state change

        # Set by the server loop; the core stays timer-free and testable.
        self.schedule_timer = None  # fn(delay_ms, callback) -> cancel fn
        self._timers: dict[str, Any] = {}

    # ------------------------------------------------------------- lifecycle

    def _load_state(self, name: str, path: str, validate, fallback):
        """Read one state file, rejecting a value whose shape is wrong.

        A malformed file (hand-edited, or written by another build) must not
        crash-loop boot: the fallback is used and the rejection is journaled.
        The file stays on disk only until this daemon next writes that state
        (paused, defaults, last-applied, dismissed and the rest are rewritten
        on their next change); a rejection a user needs to inspect is the one
        the journal keeps, not the file.
        """
        raw = read_json(path)
        if raw is None:
            return fallback
        checked = validate(raw)
        if checked is None:
            self.journal.log("state-rejected", file=name, reason="unexpected JSON shape")
            return fallback
        return checked

    def _validated_records(self, name: str, records: dict, *, settings: bool = False) -> dict:
        """Drop a component record whose contents are not valid controls.

        The shape check in ``_load_state`` accepts any object; the values it
        carries must still be applyable. A record that fails here is journaled
        and ignored instead of crashing boot in the middle of a write.
        """
        out = dict(records)
        for component in COMPONENTS:
            record = out.get(component)
            if record is None:
                continue
            payload = record.get("settings") if settings else record
            reason = self._record_rejection(component, payload)
            if reason is None:
                continue
            out[component] = None
            self.journal.log("state-rejected", file=name, component=component, reason=reason)
        return out

    def _record_rejection(self, component: str, payload: dict) -> str | None:
        if not _control_record_ok(payload):
            return "a control value is not a whole number"
        fan_settings = payload.get("fan")
        if fan_settings is None or component != "gpu":
            return None
        gpu = self.backends.get("gpu")
        if gpu is None:
            return None
        try:
            info = gpu.fan_info()
        except Exception:
            # A transient backend failure is not a verdict on the record:
            # keep it, and let the next apply report the real error.
            return None
        try:
            fan.validate(
                fan_settings,
                info,
                self.tuning_mode == config.TUNING_MODE_EXPERT,
            )
        except (ValueError, TypeError, KeyError) as exc:
            return f"fan settings are invalid: {exc}"
        return None

    def boot(self) -> None:
        first_run = not os.path.exists(self.dirs.events_file)
        self.dirs.ensure()
        self.defaults = self._load_state(
            "defaults", self.dirs.defaults_file, _record_map, {"cpu": None, "gpu": None}
        )
        self.last_applied = self._load_state(
            "last-applied", self.dirs.last_applied_file,
            lambda value: _record_map(value, settings=True),
            {"cpu": None, "gpu": None},
        )
        self.paused = self._load_state(
            "paused", self.dirs.paused_file, _bool_map, {"cpu": False, "gpu": False}
        )
        self.suspended = self._load_state(
            "suspended", self.dirs.suspended_file, _record_map, {"cpu": None, "gpu": None}
        )
        self.dismissed = self._load_state("dismissed", self.dirs.dismissed_file, _str_list, [])
        self.last_reverted = self._load_state(
            "last-reverted", self.dirs.last_reverted_file, _optional_dict, None
        )
        self.tuning_mode = config.read_tuning_mode(self.dirs)
        self.expert_warned = os.path.exists(self.dirs.expert_warned_file)

        for component, backend in self.backends.items():
            if backend is None:
                continue
            # Controls report the limits of the mode the daemon runs in, so
            # every consumer (UI, CLI, server-side validation) sees one table.
            backend.tuning_mode = self.tuning_mode
            try:
                backend.probe()
            except Exception as exc:
                # A broken probe (NVML query failure, missing symbol) must
                # degrade the backend, not crash-loop the unit.
                backend.probe_failed = str(exc)
        self.defaults = self._validated_records("defaults", self.defaults)
        self.last_applied = self._validated_records(
            "last-applied", self.last_applied, settings=True
        )
        self.conflicts = diagnose.detect_conflicts()

        for component in COMPONENTS:
            backend = self.backends.get(component)
            if backend is None:
                continue
            try:
                values = backend.read()
                applied = {k: v for k, v in values.items()}
                curve = backend.curve()
                if curve is not None and curve.supported:
                    applied.update(self._curve_record(curve))
                self.applied[component] = applied
            except (BackendError, OSError):
                # The hardware did not answer. The last confirmed settings are
                # the best remaining evidence of what is on the machine; the
                # factory table is only the fallback when no record exists.
                # The record is migrated here as well: _migrate_curve_state
                # runs after this loop, so copying the stored points as-is
                # would report (and then reapply) raw driver frequencies.
                record = self.last_applied.get(component) or {}
                settings = record.get("settings")
                if settings:
                    migrated, _ = migrate_curve_settings(settings, self._curve_for(component))
                    self.applied[component] = dict(migrated or {})
                else:
                    self.applied[component] = self._factory_settings(component)

        # A reboot hands the fan back to the driver: nvmlDeviceSetFanSpeed_v2
        # does not survive it. Report the truth at boot; a saved default that
        # includes a fan config is reapplied by _reapply_defaults below, and
        # an interrupted apply restores its previous config even when that is
        # a custom one.
        gpu_applied = self.applied.get("gpu")
        if isinstance(gpu_applied, dict):
            gpu_applied["fan"] = {"mode": "auto"}

        self._migrate_curve_state()
        self._handle_interrupted_apply()
        self._scan_failure_evidence()

        clean = False
        try:
            with open(self.dirs.clean_shutdown_file, "r", encoding="ascii"):
                clean = True
            remove(self.dirs.clean_shutdown_file)
        except OSError:
            clean = False

        if first_run and not clean:
            # A fresh install has no prior shutdown to judge; treating it as
            # abrupt would pause tuning that was never applied. Birth is clean.
            clean = True
            self.journal.log("first-run", note="no previous state; treating boot as clean")

        if clean:
            self.journal.log("boot", clean=True)
            self._reapply_defaults(automatic=True)
        else:
            self.journal.log("boot", clean=False, note="no clean shutdown marker; treating as abrupt")
            self.paused = {"cpu": True, "gpu": True}
            write_json(self.dirs.paused_file, self.paused)
            self.abrupt_boot_pending = True

    def mark_clean_shutdown(self) -> None:
        # An orderly stop gives the fan back to the driver. A crash does not
        # reach here; the fan keeps its last duty until the reboot clears it,
        # which is why the ceiling override exists while the loop is running.
        self.shutdown()
        self.dirs.ensure()
        try:
            with open(self.dirs.clean_shutdown_file, "w", encoding="ascii") as fh:
                fh.write(f"{now_ms()}\n")
        except OSError:
            pass

    def refresh_backends(self) -> bool:
        """Let a running daemon notice hardware that appeared after startup.

        Loading the `msr` module, or the NVIDIA driver coming up late, must
        not require restarting the service: `sudo bin/install` loads the
        module into the live kernel, and intel-undervolt/NVML work the same
        way — write now, no reboot. The server loop calls this once a second;
        a change pushes a fresh state to the UI.
        """
        changed = False
        for component, backend in self.backends.items():
            try:
                if not backend.refresh():
                    continue
            except Exception:
                continue
            changed = True
            # The values the daemon reports as applied were read from the
            # hardware at boot. With a control that just became reachable,
            # read them again — but never mid-trial, where `applied` is the
            # state under confirmation.
            if self.trial is None:
                self._resync_applied(component)
        if changed:
            self._changed()
        return changed

    def _resync_applied(self, component: str, fallback: dict | None = None) -> None:
        """Set `applied` from the hardware, merging what the backend omits.

        A backend that cannot read one control must not erase it from the
        snapshot: `fallback` (normally the candidate or the previous state) is
        the base, and a successful read overlays the keys the hardware really
        reports. Reading also captures driver clamping, so the UI shows what
        the device accepted instead of the raw request.
        """
        backend = self.backends[component]
        base = dict(fallback or self.applied.get(component) or self._factory_settings(component))
        try:
            values = backend.read()
        except (BackendError, OSError) as exc:
            # The device did not answer the read-back. Kept as the base (the
            # write itself succeeded), but the journal says the confirmation
            # never came, so `doctor` does not present a guess as fact.
            self.journal.log("read-back-failed", component=component, error=str(exc))
            self.applied[component] = base
            return
        applied = dict(base)
        applied.update({k: v for k, v in values.items()})
        curve = backend.curve()
        if curve is not None and curve.supported:
            applied.update(self._curve_record(curve))
        self.applied[component] = applied

    @staticmethod
    def _curve_record(curve, points=None) -> dict[str, Any]:
        """The record fields for a V/F curve: its points and the units stamp."""
        source = curve.points if points is None else points
        return {
            "points": [{"v": p.voltage, "f": p.frequency} for p in source],
            "curveScale": curve.scale,
        }

    def _factory_settings(self, component: str) -> dict[str, Any]:
        settings: dict[str, Any] = {
            c.key: c.factory for c in self.backends[component].controls()
        }
        curve = self.backends[component].curve()
        if curve is not None and curve.supported:
            settings.update(self._curve_record(curve, curve.factory_points))
        if component == "gpu":
            info = self.backends[component].fan_info()
            if info.get("supported"):
                settings["fan"] = {"mode": "auto"}
        return settings

    def _writable(self, component: str, settings: dict | None) -> dict[str, Any]:
        """Drop controls the backend cannot write and unsupported curves.

        A restore carries the whole previous snapshot, which includes controls
        that are unavailable on this driver. Calling the backend with them
        made a successful revert of the other controls report a failed
        restore.
        """
        if not settings:
            return {}
        controls = {c.key: c for c in self.backends[component].controls()}
        curve = self.backends[component].curve()
        out: dict[str, Any] = {}
        for key, value in settings.items():
            if key == "points":
                if curve is not None and curve.supported and curve.writable:
                    out[key] = value
                continue
            control = controls.get(key)
            if control is not None and control.available:
                out[key] = value
        return out

    def _range_violation(self, control, value: int) -> str | None:
        """The message for a value outside the active range, or None.

        apply() raises it for a request; the replay paths use it to decide
        whether a persisted record may still be written in this mode.
        """
        if value < control.minimum or value > control.maximum:
            return (
                f"{control.key}={value} {control.unit} is outside "
                f"{control.minimum}..{control.maximum} "
                f"{control.unit} for {self.tuning_mode} mode"
                + ("; expert mode (sudo bin/install --expert) unlocks the wider range"
                   if self.tuning_mode != config.TUNING_MODE_EXPERT else "")
            )
        return None

    def _replay_violation(self, component: str, settings: dict[str, Any]) -> str | None:
        """Why a persisted record cannot be replayed in the active mode.

        A saved record outlives the tuning mode it was saved in: reinstalling
        without `--expert` switches back to normal, and the boot default (or
        the last confirmed session) may hold values only expert mode accepts.
        The replay paths therefore pass the same gate apply() applies to a
        request. Returns the sentence to journal and show, or None.
        """
        backend = self.backends.get(component)
        if backend is None:
            return None
        controls = {c.key: c for c in backend.controls()}
        for key, value in settings.items():
            if key in ("points", "fan", "curveScale"):
                continue
            control = controls.get(key)
            if control is None:
                # Not a control this backend exposes; _writable drops it, so
                # it cannot reach the hardware.
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return f"{key}={value!r} is not a number"
            if isinstance(value, float) and not math.isfinite(value):
                return f"{key}={value!r} is not a number"
            violation = self._range_violation(control, int(value))
            if violation is not None:
                return violation
        if component == "gpu" and "fan" in settings:
            gpu = self.backends.get("gpu")
            if gpu is not None:
                try:
                    info = gpu.fan_info()
                except Exception:
                    # A transient backend failure is not a verdict on the
                    # record; the normal replay path reports it where it
                    # happens.
                    return None
                try:
                    fan.validate(
                        settings["fan"],
                        info,
                        self.tuning_mode == config.TUNING_MODE_EXPERT,
                    )
                except (ValueError, TypeError, KeyError) as exc:
                    return f"fan settings are invalid: {exc}"
        return None

    def _reject_replay(self, component: str, settings: dict[str, Any], reason: str) -> None:
        """Keep a saved record the active mode does not allow off the hardware."""
        self.journal.log(
            "replay-out-of-range",
            component=component,
            values=dict(settings),
            reason=reason,
        )
        self._raise_notice(
            "apply-failed",
            component=component,
            uncertain=False,
            message=(
                f"The saved {component.upper()} values exceed the {self.tuning_mode} mode range "
                f"({reason}) and were not applied; they must be applied again in "
                f"{self.tuning_mode} mode."
            ),
        )

    def _curve_for(self, component: str | None):
        backend = self.backends.get(component or "")
        if backend is None:
            return None
        try:
            return backend.curve()
        except Exception:
            return None

    # ------------------------------------------------------------------ fan

    def _validate_fan(self, raw: Any) -> dict[str, Any]:
        """Server-side fan validation. The UI mirrors these limits only."""
        gpu = self.backends.get("gpu")
        if gpu is None:
            raise CoreError("unavailable", "there is no GPU backend for fan control")
        info = gpu.fan_info()
        if not info.get("supported"):
            raise CoreError("unavailable", info.get("cause") or "fan control is unavailable")
        # Auto is the safe direction and the driver already owns the fan, so
        # it is accepted even when this process cannot write: there is
        # nothing to write. Fixed and curve genuinely need the capability.
        if isinstance(raw, dict) and raw.get("mode") == "auto":
            return {"mode": "auto"}
        if not info.get("writable"):
            # A backends' verdict is cached from the last probe; the fan
            # helper can be started after the daemon, so ask once more before
            # refusing. `check_fan_write` only re-probes, it never writes a
            # duty the user did not ask for.
            checker = getattr(gpu, "check_fan_write", None)
            if checker is not None:
                checker()
                info = gpu.fan_info()
        if not info.get("writable"):
            raise CoreError(
                "unavailable",
                info.get("cause")
                or "the driver refuses fan changes from this process; the OmaTuner "
                "system service is required (install with bin/install)",
            )
        expert = self.tuning_mode == config.TUNING_MODE_EXPERT
        try:
            return fan.validate(raw, info, expert)
        except ValueError as exc:
            raise CoreError("bad-value", str(exc)) from exc

    def _set_fan(self, settings: dict[str, Any] | None) -> None:
        """Start, change or stop the live fan loop for `settings`.

        Called only after a hardware write succeeded. A write failure raises
        BackendError so the caller's recovery path restores the previous fan
        configuration along with the other controls.
        """
        self._cancel_timer("fan")
        self.fan_loop = None
        gpu = self.backends.get("gpu")
        if gpu is None or settings is None:
            return
        mode = settings.get("mode", "auto")
        info = gpu.fan_info()
        if mode == "auto":
            # No write is needed when this process cannot write: the driver
            # already owns the fan. On a writable backend this returns a
            # manual fan to the driver's own curve.
            if info.get("writable"):
                gpu.set_fan_auto()
                self.fan_handback_failed = False
                self.journal.log("fan-auto")
            else:
                self.journal.log("fan-auto-skipped", cause=info.get("cause"))
            return
        if not info.get("supported") or not info.get("writable"):
            # The root helper starts independently of the daemon (the units
            # share multi-user.target, with no ordering), so a fixed duty or a
            # curve re-asks it once here, exactly as _validate_fan does before
            # refusing the request. Auto above needs no write and skips this.
            checker = getattr(gpu, "check_fan_write", None)
            if checker is not None:
                checker()
                info = gpu.fan_info()
        if not info.get("supported") or not info.get("writable"):
            raise BackendError(
                info.get("cause") or "fan control is unavailable", cause="driver"
            )
        self.fan_loop = fan.FanLoop(settings, info)
        # A running loop owns the duty again, whether or not an earlier
        # hand-back failed.
        self.fan_handback_failed = False
        try:
            self._fan_apply(now_ms(), initial=True)
        except (BackendError, OSError):
            # The first write failed. Leaving the loop object behind would
            # claim a duty that is not applied, with its timer already
            # cancelled: the UI would show "active" while the fan runs on the
            # driver. Drop the loop and hand the fan back, best effort.
            self.fan_loop = None
            self._cancel_timer("fan")
            try:
                gpu.set_fan_auto()
                self.fan_handback_failed = False
            except (BackendError, OSError):
                # The fan may be left at a manual duty the driver does not
                # own; the descriptor says so instead of claiming auto.
                self.fan_handback_failed = True
            raise
        self.journal.log("fan-mode", mode=mode, config=dict(settings))
        self._changed()

    def _fan_ceiling(self, info: dict[str, Any]) -> int:
        return fan.thermal_ceiling(info.get("targetC"), info.get("slowdownC"))

    def _fan_apply(self, now: int, initial: bool = False) -> None:
        loop = self.fan_loop
        if loop is None:
            return
        gpu = self.backends.get("gpu")
        if gpu is None:
            self._stop_fan(set_auto=False)
            return
        try:
            info = gpu.fan_info()
        except (BackendError, OSError) as exc:
            if initial:
                raise
            self._fan_fail(f"fan control stopped: the GPU reading failed ({exc})")
            return
        if not info.get("supported"):
            if initial:
                raise BackendError(info.get("cause") or "fan control is unavailable", cause="driver")
            self._fan_fail(info.get("cause") or "fan control stopped")
            return
        temperature = info.get("temperatureC")
        if temperature is None:
            loop.failures += 1
            if loop.failures >= config.FAN_FAILURE_LIMIT:
                if initial:
                    raise BackendError(
                        "the GPU temperature is unreadable, so the fan curve cannot be enforced",
                        cause="driver",
                    )
                self._fan_fail(
                    "the GPU temperature stayed unreadable for "
                    f"{loop.failures} samples; the fan returned to automatic control"
                )
                return
            self._arm_fan_timer()
            return
        loop.failures = 0
        ceiling = self._fan_ceiling(info)
        duty, forced, changed = loop.target(int(temperature), ceiling, now)
        if forced and not loop.was_forced:
            self.journal.log(
                "fan-ceiling", temperatureC=temperature, ceilingC=ceiling, duty=duty
            )
        loop.was_forced = forced
        if changed:
            try:
                applied = gpu.set_fan_duty(duty)
            except (BackendError, OSError) as exc:
                # A restarted helper makes the link drop for a moment; the
                # duty it held is gone with it. Retry a few samples before
                # declaring the policy dead, and forget the duty so the next
                # sample writes again instead of believing the old one holds.
                loop.forget()
                loop.write_failures += 1
                if initial:
                    raise
                if loop.write_failures >= config.FAN_WRITE_FAILURE_LIMIT:
                    self._fan_fail(f"fan control stopped: the driver refused a fan write ({exc})")
                    return
                self._arm_fan_timer()
                return
            loop.write_failures = 0
            if not loop.reassert:
                self.journal.log(
                    "fan-duty", duty=applied, requested=duty, temperatureC=temperature, forced=forced
                )
                if not initial:
                    self._changed()
        self._arm_fan_timer()

    def _arm_fan_timer(self) -> None:
        self._cancel_timer("fan")
        if self.fan_loop is None or self.schedule_timer is None:
            return
        self._timers["fan"] = self.schedule_timer(config.FAN_SAMPLE_MS, self._fan_tick)

    def _fan_tick(self) -> None:
        self._fan_apply(now_ms())

    def _fan_fail(self, message: str) -> None:
        self._stop_fan(set_auto=True)
        self.journal.log("fan-failed", error=message)
        self._raise_notice("apply-failed", component="gpu", uncertain=False, message=message)

    def _stop_fan(self, set_auto: bool) -> None:
        self._cancel_timer("fan")
        was_active = self.fan_loop is not None
        self.fan_loop = None
        if set_auto and was_active:
            gpu = self.backends.get("gpu")
            if gpu is not None:
                try:
                    gpu.set_fan_auto()
                    self.fan_handback_failed = False
                    self.journal.log("fan-auto", reason="loop-stopped")
                except (BackendError, OSError) as exc:
                    # NVML keeps the last manual duty when the hand-back
                    # cannot be made: the descriptor must not let the UI say
                    # the driver controls the fan.
                    self.fan_handback_failed = True
                    self.journal.log("fan-auto-failed", error=str(exc))

    def shutdown(self) -> None:
        """Orderly stop: the fan belongs to the driver again."""
        self._stop_fan(set_auto=True)

    def _apply_settings(self, component: str, settings: dict[str, Any] | None) -> None:
        """Write every control in `settings`, fan included when present.

        The fan is not a Control: it is a policy with its own loop. Keeping
        the two paths together here means a revert, a boot reapply and a
        thermal restore all put the fan back exactly where the rest of the
        component goes.
        """
        if not settings:
            return
        writable = self._writable(component, settings)
        if writable:
            self.backends[component].apply(writable)
        if component == "gpu" and "fan" in settings:
            self._set_fan(settings["fan"])

    def _migrate_curve_state(self) -> None:
        """Convert persisted curve points to the units the live curve uses.

        A `curveScale` marker is written with every point-bearing record from
        this build on. Older records carry none; a maximum above what the
        current descriptor can address identifies them as raw-scale.
        """
        rewrites: list[str] = []
        migrated_count = 0
        for component in COMPONENTS:
            curve = self._curve_for(component)
            record = self.defaults.get(component)
            if isinstance(record, dict):
                migrated, did = migrate_curve_settings(record, curve)
                if did:
                    self.defaults[component] = migrated
                    rewrites.append("defaults")
                    migrated_count += 1
            record = self.last_applied.get(component)
            if isinstance(record, dict) and isinstance(record.get("settings"), dict):
                migrated, did = migrate_curve_settings(record["settings"], curve)
                if did:
                    updated = dict(record)
                    updated["settings"] = migrated
                    self.last_applied[component] = updated
                    rewrites.append("last-applied")
                    migrated_count += 1
        if self.last_reverted:
            component = self.last_reverted.get("component")
            candidate = self.last_reverted.get("candidate")
            if isinstance(candidate, dict):
                migrated, did = migrate_curve_settings(candidate, self._curve_for(component))
                if did:
                    self.last_reverted["candidate"] = migrated
                    rewrites.append("last-reverted")
                    migrated_count += 1
        if not rewrites:
            return
        try:
            if "defaults" in rewrites:
                write_json(self.dirs.defaults_file, self.defaults)
            if "last-applied" in rewrites:
                write_json(self.dirs.last_applied_file, self.last_applied)
            if "last-reverted" in rewrites and self.last_reverted:
                write_json(self.dirs.last_reverted_file, self.last_reverted)
        except OSError as exc:
            self.journal.log("curve-scale-migration-write-failed", error=str(exc))
        self.journal.log("curve-scale-migrated", records=migrated_count, files=sorted(set(rewrites)))

    def _handle_interrupted_apply(self) -> None:
        pending = read_json(self.dirs.pending_file)
        if not pending:
            return
        component = pending.get("component")
        previous = pending.get("previous")
        candidate = pending.get("candidate")
        if component not in COMPONENTS or not isinstance(previous, dict) or not isinstance(candidate, dict):
            # The recovery record cannot be trusted: restoring from it would
            # write whatever the broken file happens to hold. The invariant
            # still holds because nothing is applied, and the state on the
            # machine is read back at boot as usual.
            remove(self.dirs.pending_file)
            self.journal.log(
                "apply-interrupted-rejected",
                reason="pending record has an unexpected shape",
            )
            return
        curve = self._curve_for(component)
        previous, _ = migrate_curve_settings(previous, curve)
        candidate, _ = migrate_curve_settings(candidate, curve)
        previous = previous or {}
        candidate = candidate or {}
        remove(self.dirs.pending_file)
        self.journal.log(
            "apply-interrupted",
            component=component,
            previous=previous,
            candidate=candidate,
            note="attempt discarded; the candidate is never reapplied",
        )
        backend = self.backends.get(component)
        if backend is None:
            return
        try:
            self._apply_settings(component, previous)
            self._resync_applied(component, previous or None)
        except (BackendError, OSError) as exc:
            self.journal.log("apply-interrupted-restore-failed", component=component, error=str(exc))
            self._raise_notice(
                "apply-failed",
                component=component,
                uncertain=True,
                message=(
                    "An application was interrupted and restoring the previous "
                    f"values failed ({exc}). Current hardware state is uncertain."
                ),
            )

    def _scan_failure_evidence(self) -> None:
        last = self.journal.last_of("apply-confirmed")
        since = int(last["tsMs"] / 1000) if last and last.get("tsMs") else None
        evidence = diagnose.scan_kernel_errors(since)
        if not evidence:
            return
        component = evidence.get("component")
        related = bool(last and last.get("component") == component)
        if not related:
            self.journal.log("evidence-unrelated", evidence=evidence, since=since)
            return
        event_id = _event_id("evidence")
        self.suspended[component] = {
            "eventId": event_id,
            "cause": "suspected",
            "evidence": evidence,
            "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "message": (
                f"Kernel log shows {evidence['kind'].upper()} errors after your last "
                f"confirmed {component.upper()} adjustment. The error alone does not prove "
                "the adjustment caused it, so the saved default is suspended as a "
                "precaution and its values were preserved."
            ),
        }
        write_json(self.dirs.suspended_file, self.suspended)
        self._raise_notice(
            "component-error",
            component=component,
            eventId=event_id,
            message=self.suspended[component]["message"],
            evidence=evidence,
        )
        self.journal.log("suspended", component=component, evidence=evidence, eventId=event_id)

    # ------------------------------------------------------------- apply flow

    def _raise_notice(self, kind: str, **fields: Any) -> None:
        notice = {"type": kind}
        notice.update(fields)
        if notice.get("eventId") in self.dismissed:
            return
        if self.notice is not None and self.notice.get("type") == "abrupt-restart":
            # The restart question must be answered first: its countdown
            # reapplies the defaults and clears the screen, so a notice that
            # replaced it now would be lost unseen.
            self.notice_queue.append(notice)
            return
        self.notice = notice
        self._changed()

    def _clear_notice(self, *, release: bool = True) -> None:
        """Take the shown notice down and tell the clients what replaced it.

        Releasing is separate from nulling on purpose: the boot timeout keeps
        the queue for after it reapplies the defaults, so a failure raised
        there takes the screen instead of the notice it would have released.
        """
        self.notice = None
        if release and not self._release_queued_notice():
            self._changed()

    def _release_queued_notice(self) -> bool:
        """Show a notice that waited behind the answered restart question.

        Pushes a snapshot when it shows one: a release that only changed
        `self.notice` left connected clients showing the notice that had
        already been answered.
        """
        while self.notice_queue:
            notice = self.notice_queue.pop(0)
            if notice.get("eventId") in self.dismissed:
                continue
            self.notice = notice
            self._changed()
            return True
        return False

    def _answer_abrupt_boot(self) -> None:
        """The user answered the restart question: keep the pause, stop the countdown."""
        self._cancel_timer("boot")
        self.abrupt_boot_pending = False
        self.boot_notice_deadline = None
        write_json(self.dirs.paused_file, self.paused)

    def _note_apply_accepted(self) -> None:
        """An accepted apply answers a pending restart question.

        A CLI client never sends `hello`, so the countdown may not have
        started when the values are applied; without this its later timeout
        would replace them with the saved defaults.
        """
        if not self.abrupt_boot_pending:
            return
        self._answer_abrupt_boot()
        if self.notice is not None and self.notice.get("type") == "abrupt-restart":
            self._clear_notice()

    def _write_pending(self, component: str, pending: dict, mode: str | None = None) -> None:
        """Persist the recovery record before any hardware write."""
        try:
            write_json(self.dirs.pending_file, pending)
        except OSError as exc:
            fields: dict[str, Any] = {"component": component, "error": str(exc)}
            if mode is not None:
                fields["mode"] = mode
            self.journal.log("pending-write-failed", **fields)
            raise CoreError(
                "state-write-failed",
                f"cannot write the recovery record for {component.upper()}: {exc}. "
                "Nothing was written to the hardware.",
            ) from exc

    def apply(self, component: str, settings: dict[str, Any], mode: str = "session", save_default: bool = False) -> dict:
        if not isinstance(component, str):
            raise CoreError("bad-value", "component must be a string")
        if not isinstance(settings, dict):
            raise CoreError("bad-value", "settings must be an object")
        if save_default is not None and not isinstance(save_default, bool):
            raise CoreError("bad-value", "saveDefault must be true or false")
        save_default = bool(save_default)
        if component not in COMPONENTS:
            raise CoreError("bad-component", f"unknown component {component!r}")
        if mode not in ("session", "default", "reset"):
            raise CoreError("bad-mode", f"unknown mode {mode!r}")
        if save_default and mode != "reset":
            # Only a reset has nothing left to confirm, so it is the one mode
            # that writes the default straight away. Accepting the flag in the
            # other modes and dropping it would tell a client its values were
            # saved for the next boot when they were not: "default" mode saves
            # them on confirm, "session" mode never saves them.
            raise CoreError(
                "bad-value",
                f"saveDefault is only accepted with mode 'reset', not {mode!r}: "
                "'default' mode saves the values when they are confirmed, and "
                "'session' mode never saves them",
            )
        if self.trial is not None:
            raise CoreError("trial-active", "another application is awaiting its 5-second check")
        if self.notice and self.notice.get("type") == "abrupt-restart" and self.boot_notice_deadline:
            raise CoreError("notice-active", "answer the restart notice first")

        backend = self.backends[component]
        controls = {c.key: c for c in backend.controls()}
        fan_requested = settings.get("fan", _MISSING)
        if fan_requested is not _MISSING and component != "gpu":
            raise CoreError("bad-key", "fan settings belong to the gpu component")

        if mode == "reset":
            if not any(c.available for c in controls.values()):
                raise CoreError("unavailable", f"no {component} control is available to reset")
            previous = dict(self.applied.get(component) or self._factory_settings(component))
            return self._apply_now(component, previous, self._factory_settings(component), "reset", save_default)

        candidate: dict[str, Any] = {}
        for key, value in settings.items():
            if key in ("points", "fan"):
                continue
            control = controls.get(key)
            if control is None:
                raise CoreError("bad-key", f"unknown control {key!r} for {component}")
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise CoreError("bad-value", f"{key} must be a number")
            if isinstance(value, float) and (
                not math.isfinite(value) or value != int(value)
            ):
                raise CoreError("bad-value", f"{key} must be a whole number")
            value = int(value)
            violation = self._range_violation(control, value)
            if violation is not None:
                raise CoreError("bad-value", violation)
            if not control.available:
                # A hand-written request may resend a control this driver
                # cannot write, at the value the machine already runs. That
                # is not a request to change it, so accept it and leave the
                # control out.
                current = (self.applied.get(component) or {}).get(key, control.factory)
                if value == current:
                    continue
                if component == "gpu" and key == "temp":
                    # The root helper can start after the daemon probed; ask
                    # it once more before refusing a real change.
                    checker = getattr(backend, "check_temp_write", None)
                    if checker is not None:
                        checker()
                        control = {c.key: c for c in backend.controls()}[key]
                if not control.available:
                    raise CoreError("unavailable", f"{key} is unavailable: {control.cause}")
            candidate[key] = value
        curve = backend.curve()
        if "points" in settings and (curve is None or not curve.supported):
            raise CoreError("unavailable", f"{component.upper()} curve control is not available")
        if "points" in settings and curve is not None and not curve.writable:
            raise CoreError(
                "unavailable",
                curve.cause or f"{component.upper()} curve control is read-only",
            )
        if "points" in settings and curve is not None:
            points = settings["points"]
            if not isinstance(points, list) or len(points) != len(curve.factory_points):
                raise CoreError("bad-value", "curve points must match the factory point count")
            checked = []
            for index, point in enumerate(points):
                try:
                    voltage = float(point["v"])
                    frequency = float(point["f"])
                except (KeyError, TypeError, ValueError):
                    raise CoreError("bad-value", "curve points must be {v, f} pairs")
                # The X axis (voltage) is fixed by the factory table; editing
                # only moves a point horizontally. Reject a client that tries
                # to invent voltages or frequencies the curve cannot address.
                factory_point = curve.factory_points[index]
                if abs(voltage - factory_point.voltage) > 1e-6:
                    raise CoreError("bad-value", "curve point voltages are fixed by the factory curve")
                if frequency < curve.min_frequency or frequency > curve.max_frequency:
                    raise CoreError(
                        "bad-value",
                        f"curve point {index} frequency {frequency} outside "
                        f"{curve.min_frequency}..{curve.max_frequency} MHz",
                    )
                checked.append({"v": voltage, "f": frequency})
            candidate["points"] = checked
            # Stamp the units this record was written in, so a later build
            # that detects a different driver scale can convert it instead of
            # replaying doubled frequencies.
            candidate["curveScale"] = curve.scale

        # Fan is a GPU policy, not a Control. A request that does not mention
        # it keeps the running configuration in the snapshot; a request that
        # does mention it is validated here, server-side, and started below.
        if component == "gpu":
            if fan_requested is not _MISSING:
                candidate["fan"] = self._validate_fan(fan_requested)
            else:
                current_fan = (self.applied.get(component) or {}).get("fan")
                if isinstance(current_fan, dict):
                    candidate["fan"] = dict(current_fan)

        self.conflicts = diagnose.detect_conflicts()
        for conflict in self.conflicts:
            if component in conflict.get("components", []):
                raise CoreError("conflict", conflict["detail"])

        previous = dict(self.applied.get(component) or self._factory_settings(component))
        if not previous:
            previous = self._factory_settings(component)

        pending = {
            "component": component,
            "mode": mode,
            "previous": previous,
            "candidate": candidate,
            "writtenAt": now_ms(),
        }
        # The recovery record must reach disk before the hardware write;
        # without it an interruption could not restore the old values.
        self._write_pending(component, pending)
        try:
            backend.apply(self._writable(component, candidate))
            if component == "gpu":
                if fan_requested is not _MISSING:
                    self._set_fan(candidate["fan"])
                elif self.fan_loop is None and (candidate.get("fan") or {}).get("mode") in ("fixed", "curve"):
                    # The candidate carries the applied policy even when the
                    # request leaves the fan out, so a recorded curve whose
                    # loop is gone (a helper outage, a failed apply) must be
                    # started again here instead of being trusted.
                    self._set_fan(candidate["fan"])
        except (BackendError, OSError) as exc:
            remove(self.dirs.pending_file)
            self.journal.log("apply-failed", component=component, error=str(exc))
            self._try_restore(component, previous)
            self._raise_notice(
                "apply-failed",
                component=component,
                uncertain=False,
                message=f"Applying {component.upper()} settings failed: {exc}. "
                "The previous values were restored; the saved default was kept.",
            )
            raise CoreError("apply-failed", str(exc)) from exc

        # Read back what the hardware accepted. A partial request (the CLI can
        # send one key) must keep the component's other applied values instead
        # of dropping them to the factory defaults in the next snapshot.
        self._resync_applied(component, candidate)
        self._clear_last_reverted()
        self._note_apply_accepted()
        self.trial = Trial(component, mode, previous, dict(candidate), now_ms() + self.confirm_ms)
        self.journal.log("apply-started", component=component, mode=mode, candidate=candidate)
        self._arm_trial_timer()
        self._arm_thermal_timer()
        self._changed()
        return {"trial": self._trial_descriptor()}

    def _apply_now(self, component: str, previous: dict, candidate: dict, mode: str, save_default: bool = False) -> dict:
        """Reset path: factory values applied without the 5-second check."""
        backend = self.backends[component]
        self._write_pending(
            component,
            {"component": component, "mode": mode, "previous": previous, "candidate": candidate, "writtenAt": now_ms()},
            mode,
        )
        try:
            self._apply_settings(component, candidate)
        except (BackendError, OSError) as exc:
            remove(self.dirs.pending_file)
            self.journal.log("apply-failed", component=component, mode=mode, error=str(exc))
            self._try_restore(component, previous)
            self._raise_notice(
                "apply-failed",
                component=component,
                uncertain=False,
                message=f"Resetting {component.upper()} to factory values failed: {exc}. "
                "Previous values were restored where possible.",
            )
            raise CoreError("apply-failed", str(exc)) from exc
        remove(self.dirs.pending_file)
        self._note_apply_accepted()
        self.thermal_guard.pop(component, None)
        self._resync_applied(component, candidate)
        actual = dict(self.applied.get(component) or candidate)
        problems = [self._record_confirmed(component, actual, mode)]
        if save_default:
            # After the last-applied record, for the reason given in confirm().
            self.defaults[component] = dict(actual)
            problems.append(self._save_defaults(component))
        self._report_persist_failures(component, [p for p in problems if p])
        self._clear_pause(component)
        self._clear_last_reverted()
        self._changed()
        return {"applied": dict(actual)}

    def _try_restore(self, component: str, previous: dict) -> None:
        try:
            self._apply_settings(component, previous)
            self._resync_applied(component, previous)
            self.journal.log("restored", component=component)
        except (BackendError, OSError) as exc:
            self.journal.log("restore-failed", component=component, error=str(exc))
            self._raise_notice(
                "apply-failed",
                component=component,
                uncertain=True,
                message=(
                    f"Restoring previous {component.upper()} values after a failed "
                    f"application did not succeed ({exc}). Current hardware state is uncertain."
                ),
            )

    def _arm_trial_timer(self) -> None:
        self._cancel_timer("trial")
        if self.schedule_timer is None:
            return
        delay = max(0, self.trial.deadline_ms - now_ms())
        self._timers["trial"] = self.schedule_timer(delay, lambda: self.revert("deadline"))

    def _cancel_timer(self, name: str) -> None:
        cancel = self._timers.pop(name, None)
        if cancel:
            cancel()

    def confirm(self) -> dict:
        trial = self.trial
        if trial is None:
            raise CoreError("no-trial", "no application is waiting for confirmation")
        self._cancel_timer("trial")
        self.trial = None
        remove(self.dirs.pending_file)
        # The snapshot holds what the hardware accepted, which may differ
        # from the request when the driver clamps; save and record that.
        self._resync_applied(trial.component, trial.candidate)
        actual = dict(self.applied.get(trial.component) or trial.candidate)
        # What the boot default was before this confirm. The thermal guard
        # needs it: a breach inside the guard window must put the default
        # back to what it was instead of leaving the overheated candidate
        # saved for the next boot.
        previous_default = self.defaults.get(trial.component)
        problems = [self._record_confirmed(trial.component, actual, trial.mode)]
        if trial.mode == "default":
            # After the last-applied record on purpose: when both writes fail,
            # the user sees one notice naming both consequences, in this
            # order, instead of the second replacing the first.
            self.defaults[trial.component] = dict(actual)
            problems.append(self._save_defaults(trial.component))
        self._report_persist_failures(trial.component, [p for p in problems if p])
        self._clear_pause(trial.component)
        self._clear_last_reverted()
        # Keep the pre-trial values for a short guard window: a temperature
        # breach just after confirm must undo the change without asking the
        # user to find the popup.
        self.thermal_guard[trial.component] = {
            "previous": dict(trial.previous),
            "until": now_ms() + self.thermal_guard_ms,
            "mode": trial.mode,
            "previousDefault": dict(previous_default) if isinstance(previous_default, dict) else None,
            "hadDefault": previous_default is not None,
        }
        # Start the guard from a fresh reading: a sensor that never reported
        # must not be presented as watching this component.
        self._refresh_thermal_sample()
        self.journal.log(
            "apply-confirmed",
            component=trial.component,
            mode=trial.mode,
            settings=actual,
        )
        self._arm_thermal_timer()
        self._changed()
        return {"confirmed": trial.component}

    def revert(self, reason: str) -> dict:
        trial = self.trial
        if trial is None:
            raise CoreError("no-trial", "no application is waiting for confirmation")
        self._cancel_timer("trial")
        self.trial = None
        self.thermal_guard.pop(trial.component, None)
        remove(self.dirs.pending_file)
        # Record the rolled-back candidate before touching the hardware: the
        # UI keeps the draft the user built, marks it "not applied · reverted"
        # and can retry it with one click.
        restored = self._restore_previous(
            trial.component,
            reason,
            mode=trial.mode,
            candidate=trial.candidate,
            previous=trial.previous,
            failure_event="revert-failed",
            failure_notice=lambda exc: (
                f"Reverting {trial.component.upper()} after '{reason}' failed ({exc}). "
                "Current hardware state is uncertain."
            ),
        )
        self.journal.log(
            "apply-reverted",
            component=trial.component,
            reason=reason,
            restored=restored,
        )
        self._arm_thermal_timer()
        self._changed()
        return {"reverted": trial.component, "reason": reason, "restored": restored}

    def _restore_previous(self, component: str, reason: str, *, mode: str,
                          candidate: dict, previous: dict, failure_event: str,
                          failure_notice) -> bool:
        """Persist the rollback and restore `previous` on the hardware.

        Shared by revert() and the thermal guard: both write the last-reverted
        record, apply the previous values, read the hardware back and raise the
        uncertain-state notice when the restore is refused. The journal kind
        and notice text stay with the caller.
        """
        self.last_reverted = {
            "component": component,
            "reason": reason,
            "mode": mode,
            "candidate": dict(candidate),
            "atMs": now_ms(),
        }
        try:
            write_json(self.dirs.last_reverted_file, self.last_reverted)
        except OSError as exc:
            # The marker only feeds the UI's "reverted" label. A full disk
            # must never block the hardware restore below.
            self.journal.log("last-reverted-write-failed", error=str(exc))
        restored = True
        try:
            self._apply_settings(component, previous)
            self._resync_applied(component, previous)
        except (BackendError, OSError) as exc:
            restored = False
            # Never claim the previous values are live when the restore was
            # refused; report what the hardware still says (the candidate,
            # when it cannot be read) and flag the state as uncertain.
            self._resync_applied(component)
            self.journal.log(failure_event, component=component, error=str(exc))
            self._raise_notice(
                "apply-failed",
                component=component,
                uncertain=True,
                message=failure_notice(exc),
            )
        return restored

    def _save_defaults(self, component: str) -> str | None:
        """Persist the saved defaults without failing a live confirmation.

        A full disk must not undo a change the hardware already accepted, but
        the user asked for "keep on boot" and it did not happen, so the
        journal records it and the caller says so. Returns the sentence for
        the user, or None when the write succeeded.
        """
        try:
            write_json(self.dirs.defaults_file, self.defaults)
        except OSError as exc:
            self.journal.log("default-save-failed", component=component, error=str(exc))
            return (
                f"The settings are live, but the saved {component.upper()} default "
                f"could not be written ({exc}). They will not return after a restart."
            )
        return None

    def _record_confirmed(self, component: str, settings: dict, mode: str) -> str | None:
        """Write the last-applied record; returns a message when it failed."""
        self.last_applied[component] = {"settings": dict(settings), "at": now_ms(), "mode": mode}
        try:
            write_json(self.dirs.last_applied_file, self.last_applied)
        except OSError as exc:
            self.journal.log("last-applied-write-failed", component=component, error=str(exc))
            return (
                f"The {component.upper()} settings are live, but the record of them "
                f"could not be written ({exc}). After a restart or resume they may "
                "not be reapplied."
            )
        return None

    def _report_persist_failures(self, component: str, problems: list[str]) -> None:
        """Raise one notice naming every failed write of one confirm."""
        if not problems:
            return
        self._raise_notice(
            "apply-failed",
            component=component,
            uncertain=False,
            message=" ".join(problems),
        )

    def _clear_last_reverted(self) -> None:
        if self.last_reverted is None:
            return
        self.last_reverted = None
        remove(self.dirs.last_reverted_file)

    def ack_expert(self) -> dict:
        """Dismiss the one-time expert-mode warning, persisted on disk."""
        self.expert_warned = True
        try:
            with open(self.dirs.expert_warned_file, "w", encoding="ascii") as fh:
                fh.write(f"{now_ms()}\n")
        except OSError:
            pass
        self.journal.log("expert-warning-acknowledged")
        self._changed()
        return {"expertWarned": True}

    def _clear_pause(self, component: str) -> None:
        changed = False
        if self.paused.get(component):
            self.paused[component] = False
            write_json(self.dirs.paused_file, self.paused)
            changed = True
        if self.suspended.get(component):
            self.suspended[component] = None
            write_json(self.dirs.suspended_file, self.suspended)
            if self.notice and self.notice.get("type") == "component-error" and self.notice.get("component") == component:
                # Clear it through the one helper: a queued notice must take
                # the screen instead of being orphaned behind a notice that
                # no longer exists.
                self._clear_notice()
            changed = True
        if changed:
            self.journal.log("pause-cleared", component=component)

    # ---------------------------------------------------- boot notice & pause

    def client_connected(self) -> None:
        """Start the abrupt-restart countdown when the popup first appears."""
        if self.abrupt_boot_pending and self.boot_notice_deadline is None:
            if self.notice is not None:
                # boot() may already have raised a notice (an interrupted
                # restore that failed, a suspended component). Raising the
                # restart notice over it used to discard it for good; hold it
                # and show it again once the restart question is answered.
                self.notice_queue.append(self.notice)
                self.notice = None
            self.boot_notice_deadline = now_ms() + config.BOOT_NOTICE_MS
            self._raise_notice(
                "abrupt-restart",
                deadlineMs=self.boot_notice_deadline,
                eventId=_event_id("abrupt"),
                message=(
                    "The last shutdown was not clean. CPU and GPU tuning are paused. "
                    "Defaults return automatically unless you keep them paused."
                ),
            )
            self._cancel_timer("boot")
            if self.schedule_timer:
                self._timers["boot"] = self.schedule_timer(
                    config.BOOT_NOTICE_MS, self._boot_notice_timeout
                )

    def _boot_notice_timeout(self) -> None:
        if not self.abrupt_boot_pending:
            return
        self.abrupt_boot_pending = False
        self.boot_notice_deadline = None
        # Not released yet: a default that fails to reapply must be able to
        # take the screen, and the queued notice is released below when it
        # did not.
        self._clear_notice(release=False)
        # The expired notice counts as the user's assent: defaults come back
        # and the pause they held ends with them.
        self.paused = {"cpu": False, "gpu": False}
        write_json(self.dirs.paused_file, self.paused)
        self.journal.log("boot-notice-timeout", note="defaults reapplied without user action")
        self._reapply_defaults(automatic=True)
        if self.notice is None:
            self._release_queued_notice()

    def keep_paused(self) -> dict:
        if not self.abrupt_boot_pending:
            raise CoreError("no-notice", "no restart notice is pending")
        self._answer_abrupt_boot()
        self._clear_notice()
        self.journal.log("boot-notice-kept-paused")
        self._changed()
        return {"paused": dict(self.paused)}

    def _reapply_defaults(self, automatic: bool) -> None:
        for component in COMPONENTS:
            if self.paused.get(component):
                self.journal.log("default-skipped", component=component, reason="paused")
                continue
            if self.suspended.get(component):
                self.journal.log("default-skipped", component=component, reason="suspended")
                continue
            default = self.defaults.get(component)
            if not default:
                self.journal.log("default-none", component=component)
                continue
            conflict = next((c for c in self.conflicts if component in c.get("components", [])), None)
            if conflict:
                self.journal.log("default-skipped", component=component, reason="conflict", conflict=conflict["name"])
                continue
            # The default may have been saved in a wider mode than the one
            # this daemon runs in (a reinstall without --expert). Never replay
            # a value the active mode refuses; the record stays on disk.
            reason = self._replay_violation(component, default)
            if reason is not None:
                self._reject_replay(component, default, reason)
                continue
            try:
                if component == "gpu" and "fan" in default:
                    # The controls are written before the fan policy; if the
                    # fan write fails, the snapshot must still say the
                    # controls are live.
                    controls = {k: v for k, v in default.items() if k != "fan"}
                    self._apply_settings(component, controls)
                    try:
                        self._set_fan(default["fan"])
                    except (BackendError, OSError, ValueError, TypeError, KeyError) as exc:
                        self._resync_applied(component, controls)
                        self.journal.log(
                            "default-reapply-failed", component=component, error=str(exc)
                        )
                        self._raise_notice(
                            "apply-failed",
                            component=component,
                            uncertain=False,
                            message=f"Reapplying the saved {component.upper()} default failed: {exc}.",
                        )
                        continue
                else:
                    self._apply_settings(component, default)
                self._resync_applied(component, default)
                problem = self._record_confirmed(
                    component, dict(self.applied.get(component) or default), "boot"
                )
                self._report_persist_failures(component, [problem] if problem else [])
                self.journal.log("default-reapplied", component=component, automatic=automatic)
            except (BackendError, OSError, ValueError, TypeError, KeyError) as exc:
                self.journal.log("default-reapply-failed", component=component, error=str(exc))
                self._raise_notice(
                    "apply-failed",
                    component=component,
                    uncertain=False,
                    message=f"Reapplying the saved {component.upper()} default failed: {exc}.",
                )
        self._changed()

    # -------------------------------------------------------------- resume

    def on_resume(self) -> None:
        self.journal.log("resume")
        if self.trial is not None and now_ms() >= self.trial.deadline_ms:
            # The deadline is wall-clock and the timer wheel is monotonic: a
            # long suspend can leave the trial past its deadline with the
            # revert still waiting for the remaining monotonic time.
            self.revert("deadline")
        for component in COMPONENTS:
            if self.trial is not None and self.trial.component == component:
                # An unconfirmed trial owns this component: `applied` is the
                # candidate under test, and last_applied still holds the old
                # values. Reapplying them here would silently undo the
                # candidate while the trial continued to describe it, so the
                # trial decides: its deadline, the user, or a thermal breach
                # resolves it. refresh_backends skips the same resync for the
                # same reason. Skipping is the safe choice: the trial started
                # no later than the suspend, so nothing the user confirmed is
                # at stake yet.
                self.journal.log("resume-skipped", component=component, reason="trial")
                continue
            if self.paused.get(component) or self.suspended.get(component):
                continue
            record = self.last_applied.get(component)
            if not record or not record.get("settings"):
                continue
            settings = record["settings"]
            # last-applied has the same lifetime problem as a boot default:
            # it can hold expert values after the daemon is reinstalled in
            # normal mode. Do not write them; apply() would refuse them.
            reason = self._replay_violation(component, settings)
            if reason is not None:
                self._reject_replay(component, settings, reason)
                continue
            backend = self.backends[component]
            try:
                current = backend.read()
            except (BackendError, OSError):
                current = None
            if current is not None:
                lost = any(
                    key in current and current[key] != value
                    for key, value in settings.items()
                    if key != "points"
                )
                curve = backend.curve()
                if not lost and curve is not None and curve.supported and "points" in settings:
                    live = {round(p.voltage, 3): p.frequency for p in curve.points}
                    lost = any(
                        live.get(round(float(p["v"]), 3)) != p["f"] for p in settings["points"]
                    )
                if not lost:
                    # Fan control does not survive suspend/resume even when
                    # the clocks did: the driver takes the fan back. Re-enter
                    # the configured mode without touching the clock offsets.
                    if component == "gpu" and "fan" in settings:
                        try:
                            self._set_fan(settings["fan"])
                        except (BackendError, OSError) as exc:
                            self.journal.log(
                                "resume-fan-failed", component=component, error=str(exc)
                            )
                            self._raise_notice(
                                "apply-failed",
                                component=component,
                                uncertain=False,
                                message=(
                                    f"Reapplying the saved fan configuration after "
                                    f"resume failed: {exc}."
                                ),
                            )
                    continue
            try:
                self._apply_settings(component, settings)
                self._resync_applied(component, settings)
                if component in self.thermal_guard:
                    # The component is still on probation from its confirm.
                    # Re-applying outside the daemon's control means the
                    # samples it just lost cannot count, so give the restored
                    # values a full window again instead of the remainder:
                    # the guard is the only thing that would undo them.
                    self.thermal_guard[component]["until"] = now_ms() + self.thermal_guard_ms
                self.journal.log("resume-reapplied", component=component)
            except (BackendError, OSError) as exc:
                self.journal.log("resume-reapply-failed", component=component, error=str(exc))
                self._raise_notice(
                    "apply-failed",
                    component=component,
                    uncertain=True,
                    message=f"Reapplying the session {component.upper()} settings after resume failed: {exc}.",
                )
        self._changed()

    # -------------------------------------------------------------- notices

    def dismiss_notice(self) -> dict:
        if not self.notice:
            raise CoreError("no-notice", "nothing to dismiss")
        notice = self.notice
        event_id = notice.get("eventId")
        if event_id:
            self.dismissed.append(event_id)
            write_json(self.dirs.dismissed_file, self.dismissed)
        if notice.get("type") == "abrupt-restart" and self.abrupt_boot_pending:
            # Dismissing the restart notice answers it the same way "keep
            # paused" does; the boot timer must not reapply defaults over a
            # session the user has already answered for.
            self._answer_abrupt_boot()
            self.journal.log("boot-notice-dismissed-kept-paused")
        self.journal.log("notice-dismissed", eventId=event_id, noticeType=notice.get("type"))
        self._clear_notice()
        return {"dismissed": True}

    # -------------------------------------------------------------- snapshot

    def _trial_descriptor(self) -> dict | None:
        if not self.trial:
            return None
        return {
            "component": self.trial.component,
            "mode": self.trial.mode,
            "deadlineMs": self.trial.deadline_ms,
        }

    def fan_descriptor(self) -> dict[str, Any]:
        """Fan capability, live reading and the applied policy for the UI."""
        gpu = self.backends.get("gpu")
        if gpu is None:
            return {"supported": False, "cause": "there is no GPU backend"}
        try:
            info = dict(gpu.fan_info())
        except Exception as exc:
            return {"supported": False, "cause": f"fan reading failed: {exc}"}
        applied = self.applied.get("gpu") or {}
        config_ = applied.get("fan")
        if not isinstance(config_, dict):
            config_ = {"mode": "auto"}
        info["config"] = dict(config_)
        # The editor places points anywhere in this window: `temps` is only the
        # factory anchor set, drawn as the reference curve.
        info["curve"] = {
            "temps": list(config.FAN_CURVE_TEMPS_C),
            "factory": fan.default_curve(),
            "minP": int(info.get("min", 0) or 0),
            "maxP": int(info.get("max", 100) or 100),
            "fixedMinP": fan.fixed_floor(info, self.tuning_mode == config.TUNING_MODE_EXPERT),
            "lastMinP": config.FAN_CURVE_LAST_MIN_PERCENT,
            "minT": config.FAN_CURVE_TEMP_MIN_C,
            "maxT": config.FAN_CURVE_TEMP_MAX_C,
            "minPoints": config.FAN_CURVE_MIN_POINTS,
            "maxPoints": config.FAN_CURVE_MAX_POINTS,
        }
        info["loopActive"] = self.fan_loop is not None
        # A stopped policy whose hand-back failed: NVML may still hold the
        # last duty, so "the driver controls the fan" would be a guess.
        info["handBackFailed"] = bool(self.fan_handback_failed and self.fan_loop is None)
        temperature = info.get("temperatureC")
        info["override"] = bool(
            self.fan_loop is not None
            and temperature is not None
            and int(temperature) >= self._fan_ceiling(info)
        )
        return info

    def snapshot(self) -> dict:
        devices = {}
        controls = {}
        curves = {}
        for component in COMPONENTS:
            backend = self.backends.get(component)
            if backend is None:
                continue
            devices[component] = backend.device_name
            applied = self.applied.get(component) or {}
            controls[component] = [
                c.descriptor(applied.get(c.key, c.factory)) for c in backend.controls()
            ]
            curve = backend.curve()
            if curve is not None:
                curves[component] = curve.descriptor()

        notice = dict(self.notice) if self.notice else None
        if notice and notice.get("type") == "abrupt-restart" and self.boot_notice_deadline:
            notice["deadlineMs"] = self.boot_notice_deadline
        thermal_guard, thermal_guard_cause = self._thermal_guard_view()

        return {
            "version": config.VERSION,
            "mode": "system" if self.dirs.system else "user",
            "tuningMode": self.tuning_mode,
            "expertWarned": self.expert_warned,
            "lastReverted": dict(self.last_reverted) if self.last_reverted else None,
            "devices": devices,
            "controls": controls,
            "curves": curves,
            "fans": {"gpu": self.fan_descriptor()},
            "applied": self.applied,
            "defaults": {c: bool(d) for c, d in self.defaults.items()},
            "paused": dict(self.paused),
            "suspended": self.suspended,
            "trial": self._trial_descriptor(),
            "notice": notice,
            "conflicts": self.conflicts,
            "abruptPending": self.abrupt_boot_pending,
            "thermalGuard": thermal_guard,
            "thermalGuardCause": thermal_guard_cause,
            "thermalCeilings": {
                "cpuC": self.thermal_cpu_ceiling_c,
                "gpuC": self.thermal_gpu_ceiling_c,
            },
        }

    def _changed(self) -> None:
        for callback in self.on_change:
            callback(self.snapshot())

    def telemetry(self) -> dict:
        data: dict[str, Any] = {}
        for component in COMPONENTS:
            backend = self.backends.get(component)
            if backend is None:
                continue
            try:
                data.update(backend.telemetry())
            except Exception:
                continue
        if self.system_telemetry is not None:
            try:
                data.update(self.system_telemetry.sample())
            except Exception:
                pass
        return data

    # ------------------------------------------------------------ thermal

    def _thermal_sample(self) -> dict[str, Any]:
        cpu = None
        cpu_limit = None
        if self.system_telemetry is not None:
            try:
                cpu = self.system_telemetry.cpu_package_temp()
                cpu_limit = self.system_telemetry.cpu_package_limit()
            except Exception:
                cpu = None
        gpu_info: dict[str, Any] = {}
        backend = self.backends.get("gpu")
        if backend is not None:
            try:
                gpu_info = backend.thermal_info() or {}
            except Exception:
                gpu_info = {}
        return {
            "cpuC": cpu,
            "cpuLimitC": cpu_limit,
            "gpuC": gpu_info.get("currentC"),
            "gpuTargetC": gpu_info.get("targetC"),
            "gpuSlowdownC": gpu_info.get("slowdownC"),
            "gpuShutdownC": gpu_info.get("shutdownC"),
        }

    def _refresh_thermal_sample(self) -> dict[str, Any]:
        """Sample the guard sensors and report a component that has none."""
        self.thermal_sample = self._thermal_sample()
        self._note_missing_thermal_sensors()
        return self.thermal_sample

    def _note_missing_thermal_sensors(self) -> None:
        """Journal an unreadable sensor once per guard window.

        The guard cannot fire for a component with no reading, so the journal
        and the snapshot must say so instead of letting it look armed.
        """
        sample = self.thermal_sample
        if sample is None:
            return
        for component, key in (("cpu", "cpuC"), ("gpu", "gpuC")):
            guard = self.thermal_guard.get(component)
            if guard is None or guard.get("missingReported"):
                continue
            if sample.get(key) is not None:
                continue
            guard["missingReported"] = True
            self.journal.log(
                "thermal-sensor-missing",
                component=component,
                cause=_THERMAL_SENSOR_CAUSE[component],
            )

    def _thermal_guard_view(self) -> tuple[dict[str, bool], dict[str, str | None]]:
        """The guard fields for the snapshot: armed only where it can fire."""
        if self.thermal_guard and self.thermal_sample is None:
            self._refresh_thermal_sample()
        sample = self.thermal_sample or {}
        armed: dict[str, bool] = {}
        causes: dict[str, str | None] = {}
        for component, key in (("cpu", "cpuC"), ("gpu", "gpuC")):
            if component not in self.thermal_guard:
                armed[component] = False
                causes[component] = None
            elif sample.get(key) is None:
                armed[component] = False
                causes[component] = _THERMAL_SENSOR_CAUSE[component]
            else:
                armed[component] = True
                causes[component] = None
        return armed, causes

    def _thermal_ceilings(self, sample: dict[str, Any]) -> tuple[int, int]:
        cpu_ceiling = self.thermal_cpu_ceiling_c
        cpu_hard = sample.get("cpuLimitC")
        if cpu_hard is not None:
            cpu_ceiling = min(cpu_ceiling, int(cpu_hard) - self.thermal_hard_margin_c)
        # Raising the target above the default ceiling must not make the
        # guard fire below the target the user just chose; fan.thermal_ceiling
        # carries that rule for both the guard and the fan loop.
        gpu_ceiling = fan.thermal_ceiling(
            sample.get("gpuTargetC"),
            sample.get("gpuSlowdownC"),
            ceiling_c=self.thermal_gpu_ceiling_c,
            margin_c=self.thermal_hard_margin_c,
        )
        return int(cpu_ceiling), int(gpu_ceiling)

    def _thermal_breaches(self, sample: dict[str, Any]) -> list[dict[str, Any]]:
        cpu_ceiling, gpu_ceiling = self._thermal_ceilings(sample)
        breaches = []
        for component, key, sensor, ceiling in (
            ("cpu", "cpuC", "CPU package", cpu_ceiling),
            ("gpu", "gpuC", "GPU", gpu_ceiling),
        ):
            value = sample.get(key)
            if value is not None and int(value) >= ceiling:
                breaches.append({
                    "component": component, "sensor": sensor,
                    "temperatureC": int(value), "ceilingC": ceiling,
                })
        return breaches

    def _arm_thermal_timer(self) -> None:
        self._cancel_timer("thermal")
        if self.schedule_timer is None:
            return
        if self.trial is None and not self.thermal_guard:
            return
        self._timers["thermal"] = self.schedule_timer(
            config.THERMAL_SAMPLE_MS, self._thermal_tick
        )

    @staticmethod
    def _thermal_reason(breach: dict[str, Any]) -> str:
        return (
            f"thermal: {breach['sensor']} reached {breach['temperatureC']} °C "
            f"(ceiling {breach['ceilingC']} °C)"
        )

    def _thermal_tick(self) -> None:
        now = now_ms()
        for component in list(self.thermal_guard):
            if now >= self.thermal_guard[component]["until"]:
                self.thermal_guard.pop(component, None)
        if self.trial is None and not self.thermal_guard:
            return
        sample = self._refresh_thermal_sample()
        breaches = self._thermal_breaches(sample)
        if not breaches:
            self._arm_thermal_timer()
            return
        if self.trial is not None:
            component = self.trial.component
            # Only a breach of the component under test undoes the trial; a
            # hot CPU must not discard an unconfirmed GPU draft.
            mine = [b for b in breaches if b["component"] == component]
            if mine:
                detail = "; ".join(self._thermal_reason(b) for b in mine)
                result = self.revert(detail)
                self.journal.log(
                    "thermal-revert",
                    component=component,
                    reason=detail,
                    restored=result.get("restored"),
                    thermal=sample,
                )
                self._changed()
                return
        for breach in breaches:
            component = breach["component"]
            if component in self.thermal_guard:
                self._thermal_restore(component, breach, sample)
        self._arm_thermal_timer()

    def _thermal_restore(self, component: str, breach: dict, sample: dict) -> None:
        guard = self.thermal_guard.pop(component, None)
        if guard is None:
            return
        detail = self._thermal_reason(breach)
        previous = guard.get("previous") or {}
        restored = self._restore_previous(
            component,
            detail,
            mode=guard.get("mode", "session"),
            candidate=self.applied.get(component) or {},
            previous=previous,
            failure_event="thermal-restore-failed",
            failure_notice=lambda exc: (
                f"Restoring {component.upper()} after a thermal breach failed ({exc}). "
                "Current hardware state is uncertain."
            ),
        )
        self._record_thermal_restore(component, guard, previous)
        self.journal.log(
            "thermal-revert",
            component=component,
            reason=detail,
            restored=restored,
            thermal=sample,
        )
        self._changed()

    def _record_thermal_restore(self, component: str, guard: dict, previous: dict) -> None:
        """Make the persisted records match the restored hardware.

        The guard popped the candidate from the running state, but
        defaults.json and last-applied.json still described it: the next boot
        (_reapply_defaults) and on_resume replayed the values that overheated,
        with no guard left to catch them. Do this even when the restore
        failed: dropping the overheated default is the safe direction.
        """
        if guard.get("mode") == "default":
            if guard.get("hadDefault"):
                self.defaults[component] = dict(guard.get("previousDefault") or {})
            else:
                self.defaults[component] = None
            try:
                write_json(self.dirs.defaults_file, self.defaults)
            except OSError as exc:
                self.journal.log("default-restore-write-failed", component=component, error=str(exc))
        self.last_applied[component] = {
            "settings": dict(previous),
            "at": now_ms(),
            "mode": "thermal-revert",
        }
        try:
            write_json(self.dirs.last_applied_file, self.last_applied)
        except OSError as exc:
            self.journal.log("last-applied-write-failed", component=component, error=str(exc))
