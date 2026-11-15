"""State machine tests for the OmaTuner daemon core, against fake hardware.

Every policy row from the design document gets a test: trial confirm and
revert paths, reset without the 5-second check, boot policies for clean and
abrupt shutdowns, interrupted-apply discard, preventive suspension, resume
reapply, conflict blocking, and the failure paths that must restore previous
values. Run with: PYTHONPATH=daemon python3 -m unittest discover -s tests -v
(from the repository root).
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "daemon"))

from omatuner import config
from omatuner.backends.base import BackendError
from omatuner.backends.fake import FakeBackend
from omatuner.core import CoreError, TunerCore
from omatuner.statestore import Journal, now_ms, read_json, write_json

try:
    from tests.support import make_core
except ImportError:  # discover -s tests puts the tests directory on sys.path
    from support import make_core


class TrialFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="omatuner-test-")
        self.core, self.cpu, self.gpu = make_core(
            self.tmp, confirm_ms=5000, clean_marker=True
        )
        # boot with clean marker reapplies nothing (no defaults yet)

    def test_confirm_saves_default_and_clears_pause(self):
        self.core.paused = {"cpu": True, "gpu": False}
        result = self.core.apply("cpu", {"core": -50, "cache": -40}, "default")
        self.assertEqual(result["trial"]["component"], "cpu")
        self.assertEqual(self.cpu.values, {"core": -50, "cache": -40})
        pending = read_json(os.path.join(self.tmp, "pending.json"))
        self.assertEqual(pending["previous"], {"core": 0, "cache": 0})
        self.core.confirm()
        state = self.core.snapshot()
        self.assertFalse(state["paused"]["cpu"])
        self.assertTrue(state["defaults"]["cpu"])
        self.assertIsNone(state["trial"])
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "pending.json")))

    def test_session_apply_confirm_does_not_save_default(self):
        self.core.apply("cpu", {"core": -50})
        self.core.confirm()
        state = self.core.snapshot()
        self.assertFalse(state["defaults"]["cpu"])
        self.assertEqual(state["applied"]["cpu"]["core"], -50)

    def test_deadline_revert_restores_previous(self):
        self.core.apply("gpu", {"core": 80, "memory": 200})
        self.assertEqual(self.gpu.values["core"], 80)
        result = self.core.revert("deadline")
        self.assertTrue(result["restored"])
        self.assertEqual(self.gpu.values["core"], 0)
        self.assertEqual(self.gpu.values["memory"], 0)
        state = self.core.snapshot()
        self.assertEqual(state["applied"]["gpu"]["core"], 0)

    def test_apply_failure_restores_previous(self):
        self.core.apply("cpu", {"core": -30})
        self.core.confirm()
        self.cpu.fail_next_apply = True
        with self.assertRaises(CoreError):
            self.core.apply("cpu", {"core": -70})
        self.assertEqual(self.cpu.values["core"], -30)
        state = self.core.snapshot()
        self.assertEqual(state["applied"]["cpu"]["core"], -30)
        self.assertEqual(state["notice"]["type"], "apply-failed")
        self.assertFalse(state["notice"]["uncertain"])

    def test_double_apply_blocked(self):
        self.core.apply("cpu", {"core": -30})
        with self.assertRaises(CoreError) as ctx:
            self.core.apply("cpu", {"core": -40})
        self.assertEqual(ctx.exception.code, "trial-active")

    def test_reset_applies_factory_without_trial(self):
        self.core.apply("cpu", {"core": -50}, "default")
        self.core.confirm()
        result = self.core.apply("cpu", {}, "reset")
        self.assertNotIn("trial", result)
        self.assertEqual(self.cpu.values["core"], 0)
        state = self.core.snapshot()
        self.assertIsNone(state["trial"])
        self.assertEqual(state["applied"]["cpu"]["core"], 0)

    def test_reset_save_default(self):
        self.core.apply("gpu", {"core": 100}, "default")
        self.core.confirm()
        self.core.apply("gpu", {}, "reset", save_default=True)
        state = self.core.snapshot()
        self.assertTrue(state["defaults"]["gpu"])
        self.assertEqual(read_json(os.path.join(self.tmp, "defaults.json"))["gpu"]["core"], 0)

    def test_range_validation(self):
        with self.assertRaises(CoreError) as ctx:
            self.core.apply("cpu", {"core": -999})
        self.assertEqual(ctx.exception.code, "bad-value")
        with self.assertRaises(CoreError) as ctx:
            self.core.apply("cpu", {"nonsense": 5})
        self.assertEqual(ctx.exception.code, "bad-key")

    def test_unavailable_control_rejected(self):
        tmp = tempfile.mkdtemp(prefix="omatuner-test-")
        core, _, _ = make_core(tmp, cpu={"unavailable": True, "cause": "msr not loaded"})
        with self.assertRaises(CoreError) as ctx:
            core.apply("cpu", {"core": -30})
        self.assertEqual(ctx.exception.code, "unavailable")
        self.assertIn("msr", ctx.exception.message)


class SaveDefaultTests(unittest.TestCase):
    """`saveDefault` means "write this as the boot default"; only a reset,
    which has nothing left to confirm, can honour it. Anywhere else the flag
    is refused before the hardware is touched."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="omatuner-test-")
        # A prior boot marker keeps this a normal boot: no restart notice
        # would otherwise block the applies below before saveDefault is read.
        with open(os.path.join(self.tmp, "events.jsonl"), "w") as fh:
            fh.write('{"kind": "boot", "tsMs": 1}\n')
        write_json(os.path.join(self.tmp, "clean-shutdown"), {"at": now_ms()})
        self.core, self.cpu, self.gpu = make_core(self.tmp)
        self.defaults_path = os.path.join(self.tmp, "defaults.json")
        self.pending_path = os.path.join(self.tmp, "pending.json")

    def test_rejected_in_session_mode(self):
        with self.assertRaises(CoreError) as ctx:
            self.core.apply("cpu", {"core": -50}, "session", save_default=True)
        self.assertEqual(ctx.exception.code, "bad-value")
        self.assertIn("saveDefault", ctx.exception.message)
        self.assertIn("reset", ctx.exception.message)

    def test_rejected_in_default_mode(self):
        with self.assertRaises(CoreError) as ctx:
            self.core.apply("cpu", {"core": -50}, "default", save_default=True)
        self.assertEqual(ctx.exception.code, "bad-value")
        self.assertIn("saveDefault", ctx.exception.message)

    def test_rejected_request_changes_nothing(self):
        before_state = self.core.snapshot()
        with self.assertRaises(CoreError):
            self.core.apply("cpu", {"core": -50}, "session", save_default=True)
        self.assertEqual(self.cpu.values, {})
        self.assertFalse(os.path.exists(self.pending_path))
        self.assertFalse(os.path.exists(self.defaults_path))
        state = self.core.snapshot()
        self.assertIsNone(state["trial"])
        self.assertEqual(state["applied"], before_state["applied"])
        self.assertEqual(state["defaults"], before_state["defaults"])

    def test_reset_accepts_it(self):
        result = self.core.apply("cpu", {}, "reset", save_default=True)
        self.assertNotIn("trial", result)
        self.assertEqual(self.cpu.values["core"], 0)
        self.assertEqual(read_json(self.defaults_path)["cpu"]["core"], 0)

    def test_session_accepts_false_and_absent(self):
        self.core.apply("cpu", {"core": -50}, "session", save_default=False)
        self.assertEqual(self.cpu.values["core"], -50)
        self.core.confirm()
        self.core.apply("cpu", {"core": -60}, "session")
        self.assertEqual(self.cpu.values["core"], -60)
        self.assertFalse(self.core.snapshot()["defaults"]["cpu"])

    def test_default_mode_accepts_false_and_absent(self):
        # The flag is redundant here: "default" mode saves the values when
        # they are confirmed, so the request itself must still be accepted.
        self.core.apply("cpu", {"core": -50}, "default", save_default=False)
        self.core.confirm()
        self.assertEqual(read_json(self.defaults_path)["cpu"]["core"], -50)
        self.core.apply("cpu", {"core": -60}, "default")
        self.core.confirm()
        self.assertEqual(read_json(self.defaults_path)["cpu"]["core"], -60)

    def test_socket_request_is_rejected_before_any_write(self):
        from omatuner.daemon import Server

        server = Server(self.core, self.core.dirs)
        with self.assertRaises(CoreError) as ctx:
            server.handle_request(
                "apply",
                {"component": "cpu", "settings": {"core": -50}, "mode": "session", "saveDefault": True},
            )
        self.assertEqual(ctx.exception.code, "bad-value")
        self.assertEqual(self.cpu.values, {})
        self.assertFalse(os.path.exists(self.pending_path))

    def test_socket_request_without_the_flag_is_served(self):
        from omatuner.daemon import Server

        server = Server(self.core, self.core.dirs)
        result = server.handle_request(
            "apply", {"component": "cpu", "settings": {"core": -50}, "mode": "session"}
        )
        self.assertEqual(result["trial"]["component"], "cpu")
        self.assertEqual(self.cpu.values["core"], -50)


class BootPolicyTests(unittest.TestCase):
    def test_clean_boot_reapplies_defaults(self):
        tmp = tempfile.mkdtemp(prefix="omatuner-test-")
        core, cpu, _ = make_core(tmp)
        core.apply("cpu", {"core": -50}, "default")
        core.confirm()
        core.mark_clean_shutdown()

        core2, cpu2, _ = make_core(tmp)
        state = core2.snapshot()
        self.assertEqual(cpu2.values["core"], -50)
        self.assertEqual(state["applied"]["cpu"]["core"], -50)
        self.assertFalse(state["paused"]["cpu"])

    def test_abrupt_boot_pauses_and_notice_after_client(self):
        tmp = tempfile.mkdtemp(prefix="omatuner-test-")
        core, _, _ = make_core(tmp)
        core.apply("cpu", {"core": -50}, "default")
        core.confirm()
        # no clean marker -> abrupt

        core2, cpu2, _ = make_core(tmp)
        state = core2.snapshot()
        self.assertTrue(state["paused"]["cpu"])
        self.assertTrue(state["paused"]["gpu"])
        self.assertTrue(state["abruptPending"])
        self.assertIsNone(state["notice"])

        core2.client_connected()
        state = core2.snapshot()
        self.assertEqual(state["notice"]["type"], "abrupt-restart")
        self.assertIn("deadlineMs", state["notice"])

    def test_boot_notice_timeout_reapplies_defaults(self):
        tmp = tempfile.mkdtemp(prefix="omatuner-test-")
        core, _, _ = make_core(tmp)
        core.apply("cpu", {"core": -50}, "default")
        core.confirm()

        core2, _, _ = make_core(tmp)
        core2.client_connected()
        core2._boot_notice_timeout()
        state = core2.snapshot()
        self.assertFalse(state["paused"]["cpu"])
        self.assertEqual(state["applied"]["cpu"]["core"], -50)
        self.assertIsNone(state["notice"])

    def test_dismissing_the_restart_notice_never_clobbers_a_session_apply(self):
        # Dismissing the notice used to leave the boot countdown armed: a
        # session apply confirmed afterwards was overwritten by the default
        # when the timer finally fired.
        tmp = tempfile.mkdtemp(prefix="omatuner-test-")
        core, _, _ = make_core(tmp)
        core.apply("cpu", {"core": -50}, "default")
        core.confirm()

        core2, cpu2, _ = make_core(tmp)
        core2.client_connected()
        core2.dismiss_notice()
        core2.apply("cpu", {"core": -80})
        core2.confirm()
        self.assertEqual(cpu2.values["core"], -80)
        self.assertFalse(core2.abrupt_boot_pending)
        self.assertIsNone(core2.boot_notice_deadline)
        core2._boot_notice_timeout()
        self.assertEqual(cpu2.values["core"], -80)
        self.assertEqual(core2.snapshot()["applied"]["cpu"]["core"], -80)

    def test_keep_paused_holds(self):
        tmp = tempfile.mkdtemp(prefix="omatuner-test-")
        core, _, _ = make_core(tmp)
        core2, cpu2, _ = make_core(tmp)
        core2.client_connected()
        core2.keep_paused()
        state = core2.snapshot()
        self.assertTrue(state["paused"]["cpu"])
        self.assertTrue(state["paused"]["gpu"])
        self.assertIsNone(state["notice"])
        self.assertEqual(cpu2.values, {})

    def test_boot_survives_probe_exception(self):
        tmp = tempfile.mkdtemp(prefix="omatuner-test-")
        dirs = config.Dirs(system=False, state_dir=tmp, socket_path=os.path.join(tmp, "test.sock"))

        class ExplodingBackend(FakeBackend):
            def probe(self):
                raise BackendError("nvml query failed", cause="driver")

        cpu = ExplodingBackend("cpu")
        core = TunerCore(dirs, {"cpu": cpu, "gpu": FakeBackend("gpu")}, Journal(dirs.events_file))
        core.boot()  # must not raise

        controls = {c.key: c for c in cpu.controls()}
        self.assertFalse(controls["core"].available)
        self.assertIn("nvml query failed", controls["core"].cause)
        # boot continued past the broken backend: the gpu side is live
        self.assertIn("core", core.applied["gpu"])

    def test_interrupted_apply_discarded_never_reapplied(self):
        tmp = tempfile.mkdtemp(prefix="omatuner-test-")
        core, _, _ = make_core(tmp)
        core.apply("cpu", {"core": -30})
        core.confirm()
        # simulate a crash mid-trial: pending.json remains on disk
        write_json(
            os.path.join(tmp, "pending.json"),
            {"component": "cpu", "mode": "default", "previous": {"core": -30, "cache": 0},
             "candidate": {"core": -120, "cache": 0}, "writtenAt": now_ms()},
        )
        with open(os.path.join(tmp, "clean-shutdown"), "w") as fh:
            fh.write("clean\n")

        core2, cpu2, _ = make_core(tmp)
        self.assertEqual(cpu2.values["core"], -30)
        state = core2.snapshot()
        self.assertEqual(state["applied"]["cpu"]["core"], -30)
        self.assertFalse(os.path.exists(os.path.join(tmp, "pending.json")))
        kinds = [r["kind"] for r in core2.journal.tail(20)]
        self.assertIn("apply-interrupted", kinds)
        # the -120 candidate is never applied anywhere
        self.assertNotEqual(cpu2.values.get("core"), -120)


class SuspensionTests(unittest.TestCase):
    def test_xid_after_confirmed_gpu_tuning_suspends(self):
        tmp = tempfile.mkdtemp(prefix="omatuner-test-")
        core, _, gpu = make_core(tmp)
        core.apply("gpu", {"core": 90}, "default")
        core.confirm()
        with open(os.path.join(tmp, "clean-shutdown"), "w") as fh:
            fh.write("clean\n")

        import omatuner.diagnose as diagnose

        confirmed = core.journal.last_of("apply-confirmed")
        expected_since = int(confirmed["tsMs"] / 1000)

        def fake_scan(since_s):
            self.assertEqual(since_s, expected_since)
            return {"kind": "xid", "line": "NVRM: Xid 13 at ...", "component": "gpu"}

        original = diagnose.scan_kernel_errors
        diagnose.scan_kernel_errors = fake_scan
        try:
            core2, _, gpu2 = make_core(tmp)
        finally:
            diagnose.scan_kernel_errors = original

        state = core2.snapshot()
        self.assertIsNotNone(state["suspended"]["gpu"])
        self.assertEqual(state["suspended"]["gpu"]["cause"], "suspected")
        self.assertEqual(state["notice"]["type"], "component-error")
        # suspended component's default is not reapplied
        self.assertEqual(gpu2.values, {})

    def test_new_confirmed_apply_clears_suspension(self):
        tmp = tempfile.mkdtemp(prefix="omatuner-test-")
        core, _, _ = make_core(tmp)
        core.suspended = {"gpu": {"eventId": "e1", "cause": "suspected"}, "cpu": None}
        write_json(core.dirs.suspended_file, core.suspended)
        core.apply("gpu", {"core": 20})
        core.confirm()
        state = core.snapshot()
        self.assertIsNone(state["suspended"]["gpu"])

    def test_dismiss_notice_does_not_reactivate(self):
        tmp = tempfile.mkdtemp(prefix="omatuner-test-")
        core, _, _ = make_core(tmp)
        core.suspended = {"gpu": {"eventId": "e2", "cause": "suspected"}, "cpu": None}
        core.notice = {"type": "component-error", "component": "gpu", "eventId": "e2"}
        core.dismiss_notice()
        state = core.snapshot()
        self.assertIsNone(state["notice"])
        self.assertIsNotNone(state["suspended"]["gpu"])
        self.assertIn("e2", read_json(core.dirs.dismissed_file))


class DiagnosisTests(unittest.TestCase):
    def test_scan_kernel_errors_queries_journalctl_by_epoch(self):
        import re
        import subprocess as py_subprocess

        import omatuner.diagnose as diagnose

        captured = {}

        def fake_run(command, **kwargs):
            captured["command"] = command
            return py_subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        original = diagnose.subprocess.run
        diagnose.subprocess.run = fake_run
        try:
            self.assertIsNone(diagnose.scan_kernel_errors(1730000000))
        finally:
            diagnose.subprocess.run = original

        since = captured["command"][captured["command"].index("--since") + 1]
        self.assertRegex(since, r"^@\d+$")
        self.assertIn("@1730000000", captured["command"])

    def test_detect_conflicts_reports_only_a_running_program(self):
        import omatuner.diagnose as diagnose

        tmp = tempfile.mkdtemp(prefix="omatuner-proc-")
        os.makedirs(os.path.join(tmp, "4242"))
        with open(os.path.join(tmp, "4242", "comm"), "w", encoding="ascii") as fh:
            fh.write("lactd\n")

        original_which = diagnose.shutil.which
        diagnose.shutil.which = lambda name: "/usr/bin/lactd" if name == "lactd" else None
        try:
            conflicts = diagnose.detect_conflicts(proc_root=tmp)
        finally:
            diagnose.shutil.which = original_which
        self.assertEqual([conflict["name"] for conflict in conflicts], ["lactd"])

    def test_detect_conflicts_ignores_unreadable_proc_entries(self):
        import omatuner.diagnose as diagnose

        tmp = tempfile.mkdtemp(prefix="omatuner-proc-")
        os.makedirs(os.path.join(tmp, "7"))  # a pid whose comm vanished
        os.makedirs(os.path.join(tmp, "self"))
        os.makedirs(os.path.join(tmp, "42"))
        with open(os.path.join(tmp, "42", "comm"), "w", encoding="ascii") as fh:
            fh.write("corectrl\n")

        original_which = diagnose.shutil.which
        diagnose.shutil.which = lambda name: "/usr/bin/corectrl" if name == "corectrl" else None
        try:
            conflicts = diagnose.detect_conflicts(proc_root=tmp)
        finally:
            diagnose.shutil.which = original_which
        self.assertEqual([conflict["name"] for conflict in conflicts], ["corectrl"])


class ResumeTests(unittest.TestCase):
    def test_resume_reapplies_only_when_lost(self):
        tmp = tempfile.mkdtemp(prefix="omatuner-test-")
        core, cpu, gpu = make_core(tmp)
        core.apply("cpu", {"core": -40}, "session")
        core.confirm()
        # S3 reset: hardware lost it
        cpu.values = {}
        core.on_resume()
        self.assertEqual(cpu.values["core"], -40)
        # resume with values intact does not rewrite
        writes = len(cpu.apply_log)
        core.on_resume()
        self.assertEqual(len(cpu.apply_log), writes)

    def test_resume_uses_last_applied_not_defaults(self):
        tmp = tempfile.mkdtemp(prefix="omatuner-test-")
        core, cpu, _ = make_core(tmp)
        core.apply("cpu", {"core": -40}, "default")
        core.confirm()
        core.apply("cpu", {"core": -10}, "session")
        core.confirm()
        cpu.values = {}
        core.on_resume()
        self.assertEqual(cpu.values["core"], -10)

    def test_resume_leaves_an_active_trial_alone(self):
        # refresh_backends refuses to resync under a trial for the same
        # reason: reapplying last_applied would undo the candidate the trial
        # still describes.
        tmp = tempfile.mkdtemp(prefix="omatuner-test-")
        core, cpu, _ = make_core(tmp)
        core.apply("cpu", {"core": -40}, "session")
        cpu.values = {}
        core.on_resume()
        self.assertEqual(cpu.values, {}, "the trial's partner values are not written")
        self.assertIsNotNone(core.trial)
        self.assertEqual(core.snapshot()["applied"]["cpu"]["core"], -40)
        self.assertIn("resume-skipped", [r["kind"] for r in core.journal.tail(20)])

    def test_resume_restarts_the_guard_window_when_it_reapplies(self):
        tmp = tempfile.mkdtemp(prefix="omatuner-test-")
        core, cpu, _ = make_core(tmp)
        core.apply("cpu", {"core": -40}, "session")
        core.confirm()
        core.thermal_guard["cpu"]["until"] = now_ms() + 1000
        cpu.values = {}
        core.on_resume()
        self.assertEqual(cpu.values["core"], -40)
        self.assertGreater(core.thermal_guard["cpu"]["until"], now_ms() + 1000,
                           "the reapplied values get a full probation window")


class ConflictTests(unittest.TestCase):
    def test_conflicting_program_blocks_apply(self):
        import omatuner.diagnose as diagnose

        original = diagnose.detect_conflicts
        diagnose.detect_conflicts = lambda: [
            {"name": "lactd", "components": ["cpu", "gpu"], "detail": "lactd is running"}
        ]
        try:
            tmp = tempfile.mkdtemp(prefix="omatuner-test-")
            core, cpu, _ = make_core(tmp)
            with self.assertRaises(CoreError) as ctx:
                core.apply("cpu", {"core": -30})
            self.assertEqual(ctx.exception.code, "conflict")
            self.assertEqual(cpu.values, {})
        finally:
            diagnose.detect_conflicts = original

    def test_conflicting_component_only_blocks_its_side(self):
        tmp = tempfile.mkdtemp(prefix="omatuner-test-")
        core, _, gpu = make_core(tmp)
        core.conflicts = [{"name": "lactd", "components": ["cpu"], "detail": "lactd is running"}]
        core.apply("gpu", {"core": 30})
        self.assertEqual(gpu.values["core"], 30)

    def test_conflict_skips_boot_reapply(self):
        tmp = tempfile.mkdtemp(prefix="omatuner-test-")
        core, _, _ = make_core(tmp)
        core.apply("cpu", {"core": -50}, "default")
        core.confirm()
        with open(os.path.join(tmp, "clean-shutdown"), "w") as fh:
            fh.write("clean\n")

        import omatuner.diagnose as diagnose

        original = diagnose.detect_conflicts
        diagnose.detect_conflicts = lambda: [{"name": "lactd", "components": ["cpu"], "detail": "running"}]
        try:
            core2, cpu2, _ = make_core(tmp)
        finally:
            diagnose.detect_conflicts = original
        self.assertEqual(cpu2.values, {})


class FirstRunTests(unittest.TestCase):
    def test_fresh_install_boot_is_clean_not_abrupt(self):
        tmp = tempfile.mkdtemp(prefix="omatuner-test-")
        core, _, _ = make_core(tmp)
        state = core.snapshot()
        self.assertFalse(state["paused"]["cpu"])
        self.assertFalse(state["abruptPending"])

    def test_second_run_without_marker_is_abrupt(self):
        tmp = tempfile.mkdtemp(prefix="omatuner-test-")
        core, _, _ = make_core(tmp)
        with open(os.path.join(tmp, "events.jsonl"), "a") as fh:
            fh.write('{"kind": "boot", "tsMs": 2}\n')
        core2, _, _ = make_core(tmp)
        state = core2.snapshot()
        self.assertTrue(state["paused"]["cpu"])
        self.assertTrue(state["abruptPending"])


class NoticeQueueTests(unittest.TestCase):
    """The abrupt-restart question is modal; a notice raised around it must
    survive it instead of being replaced unseen."""

    class RestoreRefused(FakeBackend):
        def apply(self, values):
            raise BackendError("simulated refusal", cause="driver")

    def boot_with_a_failed_interrupted_restore(self) -> tuple[str, TunerCore]:
        tmp = tempfile.mkdtemp(prefix="omatuner-test-")
        # A previous boot makes the next one abrupt (no clean marker).
        make_core(tmp)
        write_json(
            os.path.join(tmp, "pending.json"),
            {"component": "cpu", "mode": "session", "previous": {"core": -30, "cache": 0},
             "candidate": {"core": -120, "cache": 0}, "writtenAt": now_ms()},
        )
        dirs = config.Dirs(system=False, state_dir=tmp, socket_path=os.path.join(tmp, "t.sock"))
        core = TunerCore(
            dirs,
            {"cpu": self.RestoreRefused("cpu"), "gpu": FakeBackend("gpu")},
            Journal(dirs.events_file),
        )
        core.schedule_timer = lambda delay, callback: (lambda: None)
        core.boot()
        self.assertTrue(core.abrupt_boot_pending)
        self.assertEqual(core.snapshot()["notice"]["type"], "apply-failed")
        return tmp, core

    def test_a_boot_notice_reappears_after_the_restart_notice_is_answered(self):
        _, core = self.boot_with_a_failed_interrupted_restore()
        core.client_connected()
        self.assertEqual(core.snapshot()["notice"]["type"], "abrupt-restart")
        core.dismiss_notice()
        state = core.snapshot()
        self.assertEqual(state["notice"]["type"], "apply-failed")
        self.assertTrue(state["notice"]["uncertain"])
        self.assertIn("interrupted", state["notice"]["message"])

    def test_a_notice_raised_during_the_restart_question_waits(self):
        _, core = self.boot_with_a_failed_interrupted_restore()
        core.dismiss_notice()
        core.client_connected()
        self.assertEqual(core.snapshot()["notice"]["type"], "abrupt-restart")
        core._raise_notice("component-error", component="cpu", eventId="e-1", message="evidence")
        self.assertEqual(core.snapshot()["notice"]["type"], "abrupt-restart")
        core.keep_paused()
        self.assertEqual(core.snapshot()["notice"]["type"], "component-error")

    def test_the_boot_timeout_pushes_the_notice_it_releases(self):
        # `_release_queued_notice` used to set self.notice without _changed():
        # connected clients kept the answered restart question on screen.
        _, core = self.boot_with_a_failed_interrupted_restore()
        seen = []
        core.on_change.append(lambda state: seen.append(state["notice"]))
        core.client_connected()
        self.assertEqual(seen[-1]["type"], "abrupt-restart")
        core._boot_notice_timeout()
        self.assertEqual(seen[-1]["type"], "apply-failed")
        self.assertIn("interrupted", seen[-1]["message"])

    def test_clearing_a_pause_shows_the_notice_queued_behind_it(self):
        # Two notices wait behind the restart question. Dropping the one on
        # screen (its suspension is gone) must show the next one instead of
        # orphaning it in the queue with nothing to dismiss.
        _, core = self.boot_with_a_failed_interrupted_restore()
        core.client_connected()
        core._raise_notice("component-error", component="cpu", eventId="e-1", message="first")
        core.suspended["cpu"] = {"eventId": "e-1", "cause": "suspected", "message": "first"}
        core._raise_notice("component-error", component="cpu", eventId="e-2", message="second")
        self.assertEqual(core.snapshot()["notice"]["type"], "abrupt-restart")

        core.dismiss_notice()  # releases the interrupted-restore notice
        core.dismiss_notice()  # releases "first"
        self.assertEqual(core.snapshot()["notice"]["eventId"], "e-1")

        core._clear_pause("cpu")
        state = core.snapshot()
        self.assertIsNone(state["suspended"]["cpu"])
        self.assertEqual(state["notice"]["eventId"], "e-2", "the queued notice must be shown")
        self.assertEqual(core.notice_queue, [])
        core.dismiss_notice()
        self.assertIsNone(core.snapshot()["notice"])


class StateShapeTests(unittest.TestCase):
    """A malformed state file must be rejected, journaled and replaced by the
    default, not crash-loop the service."""

    def test_malformed_state_files_are_rejected_instead_of_fatal(self):
        tmp = tempfile.mkdtemp(prefix="omatuner-test-")
        with open(os.path.join(tmp, "events.jsonl"), "w") as fh:
            fh.write('{"kind": "boot", "tsMs": 1}\n')
        with open(os.path.join(tmp, "clean-shutdown"), "w") as fh:
            fh.write("clean\n")
        for name in ("paused.json", "suspended.json", "last-reverted.json", "defaults.json"):
            write_json(os.path.join(tmp, name), [])
        write_json(os.path.join(tmp, "dismissed.json"), {"cpu": True})
        write_json(os.path.join(tmp, "last-applied.json"), {"cpu": {"aggressive": True}})

        core, _, _ = make_core(tmp)  # must not raise
        state = core.snapshot()
        self.assertFalse(state["paused"]["cpu"])
        self.assertIsNone(state["suspended"]["gpu"])
        self.assertIsNone(state["lastReverted"])
        self.assertEqual(core.dismissed, [])
        self.assertIsNone(core.defaults["gpu"])
        self.assertIsNone(core.last_applied["gpu"])
        rejected = {
            record["file"] for record in core.journal.tail(60)
            if record["kind"] == "state-rejected"
        }
        self.assertEqual(
            rejected,
            {"paused", "suspended", "last-reverted", "defaults", "dismissed", "last-applied"},
        )

    def test_a_malformed_pending_record_is_discarded_without_a_restore(self):
        tmp = tempfile.mkdtemp(prefix="omatuner-test-")
        with open(os.path.join(tmp, "events.jsonl"), "w") as fh:
            fh.write('{"kind": "boot", "tsMs": 1}\n')
        write_json(
            os.path.join(tmp, "pending.json"),
            {"component": "cpu", "mode": "session", "previous": [],
             "candidate": {"core": -120}, "writtenAt": now_ms()},
        )
        with open(os.path.join(tmp, "clean-shutdown"), "w") as fh:
            fh.write("clean\n")
        core, cpu, _ = make_core(tmp)
        self.assertFalse(os.path.exists(os.path.join(tmp, "pending.json")))
        self.assertEqual(cpu.values, {}, "nothing is applied from a broken record")
        kinds = [r["kind"] for r in core.journal.tail(20)]
        self.assertIn("apply-interrupted-rejected", kinds)
        self.assertNotIn("apply-interrupted", kinds)


class StateWriteFailureTests(unittest.TestCase):
    def test_a_failed_default_write_keeps_the_values_and_tells_the_user(self):
        import omatuner.core as core_module

        tmp = tempfile.mkdtemp(prefix="omatuner-test-")
        with open(os.path.join(tmp, "events.jsonl"), "w") as fh:
            fh.write('{"kind": "boot", "tsMs": 1}\n')
        with open(os.path.join(tmp, "clean-shutdown"), "w") as fh:
            fh.write("clean\n")
        core, cpu, _ = make_core(tmp)
        core.apply("cpu", {"core": -30}, "default")
        real = core_module.write_json

        def flaky(path, data):
            if path in (core.dirs.defaults_file, core.dirs.last_applied_file):
                raise OSError(28, "No space left on device")
            return real(path, data)

        with mock.patch.object(core_module, "write_json", side_effect=flaky):
            core.confirm()  # non-fatal

        state = core.snapshot()
        self.assertEqual(cpu.values["core"], -30)
        self.assertEqual(state["applied"]["cpu"]["core"], -30)
        self.assertEqual(state["notice"]["type"], "apply-failed")
        self.assertIn("will not return after a restart", state["notice"]["message"])
        kinds = [r["kind"] for r in core.journal.tail(20)]
        self.assertIn("default-save-failed", kinds)
        self.assertIn("last-applied-write-failed", kinds)
        self.assertFalse(os.path.exists(core.dirs.defaults_file))

    def test_both_failed_writes_are_reported_in_one_notice(self):
        # The second notice used to replace the first: on a full disk the user
        # was told only about "keep on boot" and never about the lost
        # last-applied record.
        import omatuner.core as core_module

        tmp = tempfile.mkdtemp(prefix="omatuner-test-")
        with open(os.path.join(tmp, "events.jsonl"), "w") as fh:
            fh.write('{"kind": "boot", "tsMs": 1}\n')
        with open(os.path.join(tmp, "clean-shutdown"), "w") as fh:
            fh.write("clean\n")
        core, _, _ = make_core(tmp)
        core.apply("cpu", {"core": -30}, "default")
        real = core_module.write_json

        def flaky(path, data):
            if path in (core.dirs.defaults_file, core.dirs.last_applied_file):
                raise OSError(28, "No space left on device")
            return real(path, data)

        with mock.patch.object(core_module, "write_json", side_effect=flaky):
            core.confirm()

        message = core.snapshot()["notice"]["message"]
        self.assertIn("the record of them could not be written", message)
        self.assertIn("saved CPU default", message)
        self.assertLess(
            message.index("the record of them"),
            message.index("saved CPU default"),
            "the consequences are named in the order the writes happened",
        )


class ProtocolTests(unittest.TestCase):
    """End-to-end over the real unix socket with fake hardware."""

    def test_server_roundtrip_and_events(self):
        import threading

        from omatuner.backends.fake import FakeBackend as FB
        from omatuner.daemon import Server
        from omatuner.statestore import Journal

        tmp = tempfile.mkdtemp(prefix="omatuner-test-")
        # A prior run marker makes this boot non-first; without it the fresh
        # install is treated as a clean first boot and no notice appears.
        with open(os.path.join(tmp, "events.jsonl"), "w") as fh:
            fh.write('{"kind": "boot", "tsMs": 1}\n')
        dirs = config.Dirs(system=False, state_dir=tmp, socket_path=os.path.join(tmp, "s.sock"))
        core = TunerCore(dirs, {"cpu": FB("cpu"), "gpu": FB("gpu")}, Journal(dirs.events_file), confirm_ms=300)
        server = Server(core, dirs)

        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        time.sleep(0.4)

        import json
        import socket as pysocket

        client = pysocket.socket(pysocket.AF_UNIX, pysocket.SOCK_STREAM)
        client.connect(dirs.socket_path)
        client.settimeout(3)

        def request(method, params=None):
            client.sendall(json.dumps({"id": 7, "method": method, "params": params or {}}).encode() + b"\n")
            buffer = b""
            while True:
                while b"\n" not in buffer:
                    buffer += client.recv(65536)
                line, _, buffer = buffer.partition(b"\n")
                frame = json.loads(line)
                if frame.get("id") == 7:
                    return frame
                # pushed events are skipped until our reply arrives

        reply = request("hello")
        self.assertTrue(reply["ok"])
        state = reply["result"]["state"]
        self.assertIn("cpu", state["controls"])
        # fresh state dir = abrupt boot: the notice blocks applies until answered
        blocked = request("apply", {"component": "gpu", "settings": {"core": 10}})
        self.assertFalse(blocked["ok"])
        self.assertEqual(blocked["error"]["code"], "notice-active")
        self.assertTrue(request("keep-paused")["ok"])

        reply = request("apply", {"component": "gpu", "settings": {"core": 40}, "mode": "session"})
        self.assertTrue(reply["ok"], reply)
        reply = request("confirm")
        self.assertTrue(reply["ok"])
        reply = request("apply", {"component": "gpu", "settings": {"core": 90}})
        self.assertTrue(reply["ok"])
        time.sleep(0.6)  # deadline passes
        reply = request("hello")
        state = reply["result"]["state"]
        self.assertIsNone(state["trial"])
        self.assertEqual(state["applied"]["gpu"]["core"], 40)

        client.close()
        server.stop()
        thread.join(timeout=3)

    def test_second_server_refused_on_live_socket(self):
        import socket as pysocket
        import threading

        from omatuner.daemon import Server

        tmp = tempfile.mkdtemp(prefix="omatuner-test-")
        dirs = config.Dirs(system=False, state_dir=tmp, socket_path=os.path.join(tmp, "dup.sock"))
        core = TunerCore(dirs, {"cpu": FakeBackend("cpu"), "gpu": FakeBackend("gpu")}, Journal(dirs.events_file))
        server = Server(core, dirs)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        try:
            deadline = time.time() + 3
            while time.time() < deadline:
                probe = pysocket.socket(pysocket.AF_UNIX, pysocket.SOCK_STREAM)
                try:
                    probe.connect(dirs.socket_path)
                except OSError:
                    probe.close()
                    time.sleep(0.02)
                else:
                    probe.close()
                    break
            core2 = TunerCore(dirs, {"cpu": FakeBackend("cpu"), "gpu": FakeBackend("gpu")}, Journal(dirs.events_file))
            with self.assertRaises(CoreError) as ctx:
                Server(core2, dirs).start()
            self.assertEqual(ctx.exception.code, "already-running")
        finally:
            server.stop()
            thread.join(timeout=3)

    def test_oversized_client_buffer_dropped(self):
        import socket as pysocket
        import threading

        from omatuner.daemon import MAX_CLIENT_BUFFER, Server

        tmp = tempfile.mkdtemp(prefix="omatuner-test-")
        dirs = config.Dirs(system=False, state_dir=tmp, socket_path=os.path.join(tmp, "big.sock"))
        core = TunerCore(dirs, {"cpu": FakeBackend("cpu"), "gpu": FakeBackend("gpu")}, Journal(dirs.events_file))
        server = Server(core, dirs)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        client = pysocket.socket(pysocket.AF_UNIX, pysocket.SOCK_STREAM)
        try:
            # The socket file exists as soon as bind() runs; listen() comes a
            # few microseconds later, and a connect in that window is refused.
            deadline = time.time() + 3
            while True:
                try:
                    client.connect(dirs.socket_path)
                    break
                except OSError:
                    if time.time() >= deadline:
                        raise
                    time.sleep(0.02)
            try:
                client.sendall(b"x" * (MAX_CLIENT_BUFFER + 1))
            except OSError:
                pass  # the server may already have dropped us mid-send
            client.settimeout(5)
            try:
                data = client.recv(4096)
            except OSError:
                data = b""  # a reset is also the server dropping us
            self.assertEqual(data, b"")
        finally:
            client.close()
            server.stop()
            thread.join(timeout=3)


class UndecodableStateTests(unittest.TestCase):
    """A state file that is not UTF-8 means "no record", not a boot loop."""

    def test_read_json_returns_none_for_non_utf8_bytes(self):
        tmp = tempfile.mkdtemp(prefix="omatuner-test-")
        path = os.path.join(tmp, "defaults.json")
        with open(path, "wb") as fh:
            fh.write(b"\xff\xfe")
        self.assertIsNone(read_json(path))

    def test_read_tuning_mode_falls_back_to_normal_for_non_utf8_bytes(self):
        tmp = tempfile.mkdtemp(prefix="omatuner-test-")
        dirs = config.Dirs(system=False, state_dir=tmp, socket_path=os.path.join(tmp, "t.sock"))
        with open(dirs.mode_file, "wb") as fh:
            fh.write(b"\xff\xfe")
        self.assertEqual(config.read_tuning_mode(dirs), config.TUNING_MODE_NORMAL)


class ServerLineTests(unittest.TestCase):
    def test_a_deeply_nested_line_is_dropped_without_raising(self):
        from omatuner.daemon import Server

        tmp = tempfile.mkdtemp(prefix="omatuner-test-")
        core, _, _ = make_core(tmp)
        server = Server(core, core.dirs)
        server._on_line(None, "[" * 100000 + "]" * 100000)
        # A dropped line leaves no client state behind (and did not raise).
        self.assertEqual(server.clients, {})


class TelemetryTests(unittest.TestCase):
    def test_a_missing_package_sensor_is_not_rescanned(self):
        from omatuner.backends.telemetry import SystemTelemetry

        telemetry = SystemTelemetry()
        with mock.patch(
            "omatuner.backends.telemetry.os.listdir", return_value=[]
        ) as listdir:
            self.assertIsNone(telemetry.cpu_package_temp())
            self.assertIsNone(telemetry.cpu_package_temp())
            self.assertEqual(listdir.call_count, 1)

    def test_a_failing_system_sample_still_returns_telemetry(self):
        class BrokenSample:
            def sample(self):
                raise OSError("sensor failed")

        tmp = tempfile.mkdtemp(prefix="omatuner-test-")
        dirs = config.Dirs(system=False, state_dir=tmp, socket_path=os.path.join(tmp, "t.sock"))
        core = TunerCore(
            dirs,
            {"cpu": FakeBackend("cpu"), "gpu": FakeBackend("gpu")},
            Journal(dirs.events_file),
            system_telemetry=BrokenSample(),
        )
        core.schedule_timer = lambda delay, callback: (lambda: None)
        core.boot()
        data = core.telemetry()
        self.assertIsInstance(data, dict)
        self.assertIn("cpuUsage", data)


class AbruptApplyTests(unittest.TestCase):
    def test_an_apply_without_hello_answers_the_boot_question(self):
        tmp = tempfile.mkdtemp(prefix="omatuner-test-")
        core, _, _ = make_core(tmp)
        core.apply("cpu", {"core": -30}, "default")
        core.confirm()

        core2, cpu2, _ = make_core(tmp)
        self.assertTrue(core2.abrupt_boot_pending)
        # The CLI never sends `hello`, so no notice or timer exists yet.
        core2.apply("cpu", {"core": -80})
        core2.confirm()
        core2.client_connected()
        core2._boot_notice_timeout()
        self.assertFalse(core2.abrupt_boot_pending)
        self.assertEqual(cpu2.values["core"], -80)
        self.assertEqual(core2.snapshot()["applied"]["cpu"]["core"], -80)


class RecordContentTests(unittest.TestCase):
    def prepare(self, defaults: dict) -> str:
        tmp = tempfile.mkdtemp(prefix="omatuner-test-")
        with open(os.path.join(tmp, "events.jsonl"), "w") as fh:
            fh.write('{"kind": "boot", "tsMs": 1}\n')
        with open(os.path.join(tmp, "clean-shutdown"), "w") as fh:
            fh.write("clean\n")
        write_json(os.path.join(tmp, "defaults.json"), defaults)
        return tmp

    def test_a_default_with_an_invalid_fan_is_rejected_instead_of_fatal(self):
        tmp = self.prepare(
            {"gpu": {"core": 0, "fan": {"mode": "fixed", "speed": "high"}}}
        )
        core, _, _ = make_core(tmp)  # must not raise
        self.assertIsNone(core.defaults["gpu"])
        rejected = [
            record for record in core.journal.tail(40)
            if record["kind"] == "state-rejected"
        ]
        self.assertTrue(
            any(
                record.get("file") == "defaults" and record.get("component") == "gpu"
                for record in rejected
            )
        )

    def test_a_gpu_whose_fan_info_fails_keeps_the_saved_record(self):
        # A transient backend failure is not a bad record: dropping it would
        # lose the user's saved fan settings on a flaky NVML read.
        tmp = self.prepare(
            {"gpu": {"core": 0, "fan": {"mode": "fixed", "speed": 60}}}
        )
        gpu = FakeBackend("gpu")
        gpu.fan_info = mock.Mock(
            side_effect=BackendError("NVML is transiently unavailable", cause="driver")
        )
        dirs = config.Dirs(system=False, state_dir=tmp, socket_path=os.path.join(tmp, "t.sock"))
        core = TunerCore(dirs, {"cpu": FakeBackend("cpu"), "gpu": gpu}, Journal(dirs.events_file))
        core.schedule_timer = lambda delay, callback: (lambda: None)
        core.boot()
        self.assertEqual(core.defaults["gpu"]["fan"], {"mode": "fixed", "speed": 60})

    def test_a_default_with_a_non_integer_control_is_rejected(self):
        tmp = self.prepare({"cpu": {"core": True, "cache": 0}})
        core, cpu, _ = make_core(tmp)
        self.assertIsNone(core.defaults["cpu"])
        self.assertEqual(cpu.values, {})


class BootFanWriteFailureTests(unittest.TestCase):
    def test_a_failed_fan_write_still_records_the_applied_controls(self):
        tmp = tempfile.mkdtemp(prefix="omatuner-test-")
        core, _, _ = make_core(tmp)
        core.apply("gpu", {"core": 50, "fan": {"mode": "fixed", "speed": 60}}, "default")
        core.confirm()
        core.mark_clean_shutdown()

        dirs = config.Dirs(system=False, state_dir=tmp, socket_path=os.path.join(tmp, "t.sock"))
        gpu = FakeBackend("gpu")
        gpu.fan_writable = False
        core2 = TunerCore(
            dirs,
            {"cpu": FakeBackend("cpu"), "gpu": gpu},
            Journal(dirs.events_file),
        )
        core2.schedule_timer = lambda delay, callback: (lambda: None)
        core2.boot()
        state = core2.snapshot()
        self.assertEqual(state["applied"]["gpu"]["core"], 50)
        self.assertEqual(state["notice"]["type"], "apply-failed")


class ResumeDeadlineTests(unittest.TestCase):
    def test_resume_reverts_a_trial_whose_deadline_passed(self):
        tmp = tempfile.mkdtemp(prefix="omatuner-test-")
        core, cpu, _ = make_core(tmp)
        core.apply("cpu", {"core": -40})
        core.trial.deadline_ms = now_ms() - 1
        core.on_resume()
        self.assertIsNone(core.trial)
        self.assertEqual(cpu.values["core"], 0)


class ApplyValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="omatuner-test-")
        self.core, self.cpu, self.gpu = make_core(self.tmp)

    def test_settings_must_be_an_object(self):
        with self.assertRaises(CoreError) as ctx:
            self.core.apply("cpu", [])
        self.assertEqual(ctx.exception.code, "bad-value")
        self.assertEqual(ctx.exception.message, "settings must be an object")

    def test_component_must_be_a_string(self):
        with self.assertRaises(CoreError) as ctx:
            self.core.apply(1, {})
        self.assertEqual(ctx.exception.code, "bad-value")

    def test_a_fractional_control_is_rejected(self):
        with self.assertRaises(CoreError) as ctx:
            self.core.apply("cpu", {"core": -30.5})
        self.assertEqual(ctx.exception.code, "bad-value")
        self.assertEqual(ctx.exception.message, "core must be a whole number")
        self.assertEqual(self.cpu.values, {})

    def test_a_non_finite_control_is_rejected(self):
        for value in (float("nan"), float("inf")):
            with self.assertRaises(CoreError) as ctx:
                self.core.apply("cpu", {"core": value})
            self.assertEqual(ctx.exception.code, "bad-value")
        self.assertEqual(self.cpu.values, {})

    def test_a_whole_float_control_is_accepted(self):
        self.core.apply("cpu", {"core": -30.0})
        self.assertEqual(self.cpu.values["core"], -30)

    def test_save_default_must_be_a_boolean(self):
        for value in (0, 1, "yes"):
            with self.assertRaises(CoreError) as ctx:
                self.core.apply("cpu", {"core": -30}, save_default=value)
            self.assertEqual(ctx.exception.code, "bad-value")
            self.assertEqual(ctx.exception.message, "saveDefault must be true or false")

    def test_handle_request_does_not_coerce_save_default(self):
        from omatuner.daemon import Server

        server = Server(self.core, self.core.dirs)
        with self.assertRaises(CoreError) as ctx:
            server.handle_request(
                "apply",
                {"component": "cpu", "settings": {"core": -30}, "saveDefault": 1},
            )
        self.assertEqual(ctx.exception.code, "bad-value")
        self.assertEqual(ctx.exception.message, "saveDefault must be true or false")


if __name__ == "__main__":
    unittest.main()
