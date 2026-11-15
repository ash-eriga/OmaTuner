"""Fan policy: validation, curve interpolation, hysteresis and the loop.

The hardware paths are exercised against the fake GPU. The point of the
suite is to pin the rules that make a fan setting safe and honest:
normal mode refuses a fixed duty below 25% and a curve that ends cold;
expert mode only widens the window, never the validation itself; the loop
ramps up immediately, delays spindown, and forces 100% at the thermal
ceiling regardless of the curve.

Run from the project root: python3 -m unittest tests.test_fan -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "daemon"))

from omatuner import config, fan
from omatuner.backends.base import BackendError
from omatuner.backends.fake import FakeBackend
from omatuner.core import CoreError, TunerCore
from omatuner.statestore import Journal

try:
    from tests.support import make_core
except ImportError:  # discover -s tests puts the tests directory on sys.path
    from support import make_core

ANCHORS = list(config.FAN_CURVE_TEMPS_C)
DEFAULT = fan.default_curve()
INFO = {"min": 0, "max": 100}


class CurveMathTests(unittest.TestCase):
    def test_default_curve_matches_the_fixed_anchors(self):
        self.assertEqual([point["t"] for point in DEFAULT], ANCHORS)
        self.assertEqual([point["p"] for point in DEFAULT], list(config.FAN_CURVE_DEFAULT))

    def test_interpolation_hits_anchors_midpoints_and_both_ends(self):
        self.assertEqual(fan.duty_at(DEFAULT, 40), 30)
        self.assertEqual(fan.duty_at(DEFAULT, 50), 35)
        self.assertEqual(fan.duty_at(DEFAULT, 60), 50)
        self.assertEqual(fan.duty_at(DEFAULT, 70), 75)
        self.assertEqual(fan.duty_at(DEFAULT, 80), 100)
        self.assertEqual(fan.duty_at(DEFAULT, 65), 62)
        self.assertEqual(fan.duty_at(DEFAULT, 20), 30)
        self.assertEqual(fan.duty_at(DEFAULT, 90), 100)

    def test_ceiling_follows_target_and_slowdown(self):
        self.assertEqual(fan.thermal_ceiling(83, 99), 88)
        self.assertEqual(fan.thermal_ceiling(90, 99), 92)
        self.assertEqual(fan.thermal_ceiling(83, 91), 88)
        self.assertEqual(fan.thermal_ceiling(90, 93), 91)


class ValidationTests(unittest.TestCase):
    def test_auto_is_always_valid(self):
        self.assertEqual(fan.validate({"mode": "auto"}, INFO, False), {"mode": "auto"})

    def test_fixed_normal_floor_and_expert_unlock(self):
        self.assertEqual(fan.validate({"mode": "fixed", "speed": 25}, INFO, False)["speed"], 25)
        with self.assertRaisesRegex(ValueError, "expert mode unlocks 0%"):
            fan.validate({"mode": "fixed", "speed": 24}, INFO, False)
        self.assertEqual(fan.validate({"mode": "fixed", "speed": 0}, INFO, True)["speed"], 0)

    def test_fixed_stays_inside_the_driver_window(self):
        info = {"min": 20, "max": 90}
        self.assertEqual(fan.validate({"mode": "fixed", "speed": 20}, info, True)["speed"], 20)
        with self.assertRaisesRegex(ValueError, "outside 25..90%"):
            fan.validate({"mode": "fixed", "speed": 10}, info, False)
        with self.assertRaisesRegex(ValueError, "outside 20..90%"):
            fan.validate({"mode": "fixed", "speed": 95}, info, True)

    def test_curve_accepts_any_point_count_inside_the_window(self):
        self.assertEqual(len(fan.validate({"mode": "curve", "points": DEFAULT}, INFO, False)["points"]), 5)
        sparse = [{"t": 40, "p": 30}, {"t": 75, "p": 100}]
        self.assertEqual(fan.validate({"mode": "curve", "points": sparse}, INFO, False), {
            "mode": "curve", "points": sparse,
        })
        placed = [{"t": 33, "p": 40}, {"t": 58, "p": 55}, {"t": 67, "p": 80}, {"t": 89, "p": 100}]
        self.assertEqual(fan.validate({"mode": "curve", "points": placed}, INFO, False)["points"], placed)
        with self.assertRaisesRegex(ValueError, "needs 2 to 12 points"):
            fan.validate({"mode": "curve", "points": [{"t": 40, "p": 30}]}, INFO, False)
        too_many = [{"t": 30 + i, "p": min(100, 30 + i * 5)} for i in range(13)]
        with self.assertRaisesRegex(ValueError, "needs 2 to 12 points"):
            fan.validate({"mode": "curve", "points": too_many}, INFO, False)

    def test_curve_temperatures_must_rise_inside_the_window(self):
        backwards = [{"t": 60, "p": 40}, {"t": 50, "p": 90}]
        with self.assertRaisesRegex(ValueError, "temperatures must rise"):
            fan.validate({"mode": "curve", "points": backwards}, INFO, False)
        doubled = [{"t": 50, "p": 40}, {"t": 50, "p": 90}]
        with self.assertRaisesRegex(ValueError, "temperatures must rise"):
            fan.validate({"mode": "curve", "points": doubled}, INFO, False)
        cold = [{"t": 20, "p": 40}, {"t": 80, "p": 100}]
        with self.assertRaisesRegex(ValueError, "outside 30..90"):
            fan.validate({"mode": "curve", "points": cold}, INFO, False)
        hot = [{"t": 40, "p": 40}, {"t": 95, "p": 100}]
        with self.assertRaisesRegex(ValueError, "outside 30..90"):
            fan.validate({"mode": "curve", "points": hot}, INFO, False)

    def test_curve_must_be_monotonic(self):
        points = [dict(point) for point in DEFAULT]
        points[2]["p"] = 20
        with self.assertRaisesRegex(ValueError, "never decrease"):
            fan.validate({"mode": "curve", "points": points}, INFO, False)

    def test_curve_hot_end_floor_only_in_normal_mode(self):
        cool = [dict(point) for point in DEFAULT]
        cool[-1]["p"] = 80
        with self.assertRaisesRegex(ValueError, "must reach at least 90%"):
            fan.validate({"mode": "curve", "points": cool}, INFO, False)
        self.assertEqual(
            fan.validate({"mode": "curve", "points": cool}, INFO, True)["points"][-1]["p"], 80
        )

    def test_unknown_mode_and_non_numeric_speed_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "unknown fan mode"):
            fan.validate({"mode": "turbo"}, INFO, False)
        with self.assertRaisesRegex(ValueError, "must be a number"):
            fan.validate({"mode": "fixed", "speed": "60"}, INFO, False)
        with self.assertRaisesRegex(ValueError, "must be a number"):
            fan.validate({"mode": "fixed", "speed": float("inf")}, INFO, False)


class LoopTests(unittest.TestCase):
    def loop(self, config_=None):
        return fan.FanLoop(config_ or {"mode": "curve", "points": DEFAULT}, INFO)

    def test_first_sample_is_applied_and_ramp_up_is_immediate(self):
        loop = self.loop()
        duty, forced, changed = loop.target(40, 88, 0)
        self.assertEqual((duty, forced, changed), (30, False, True))
        duty, _, changed = loop.target(65, 88, 500)
        self.assertEqual((duty, changed), (62, True))

    def test_small_changes_are_ignored(self):
        loop = self.loop()
        loop.target(60, 88, 0)
        duty, _, changed = loop.target(60, 88, 2000)
        self.assertEqual((duty, changed), (50, False))

    def test_a_small_upward_move_is_held_too(self):
        # The hysteresis is symmetric: a rise smaller than one duty step
        # within the temperature window waits, exactly like the fall does.
        loop = fan.FanLoop(
            {"mode": "curve", "points": [{"t": 40, "p": 20}, {"t": 60, "p": 60}]},
            {"min": 0, "max": 100},
        )
        self.assertEqual(loop.target(50, 88, 0)[0], 40)
        duty, _, changed = loop.target(51, 88, 2_000)
        self.assertEqual((duty, changed), (40, False), "two points up inside the window")
        duty, _, changed = loop.target(52, 88, 30_000)
        self.assertEqual(duty, 44, "four points up crosses the step limit")

    def test_the_hysteresis_is_measured_from_the_last_change(self):
        # The curve moves 3 points per °C here, so a sample-by-sample
        # comparison would let the fan hunt between two duties at a point.
        loop = fan.FanLoop(
            {"mode": "curve", "points": [{"t": 60, "p": 50}, {"t": 70, "p": 79}]},
            {"min": 0, "max": 100},
        )
        self.assertEqual(loop.target(67, 88, 0)[0], 70)
        duty, _, changed = loop.target(66, 88, 5_000)
        self.assertEqual((duty, changed), (70, False))
        duty, _, changed = loop.target(65, 88, 15_000)
        self.assertEqual((duty, changed), (64, True))

    def test_an_unchanged_duty_is_reasserted_after_the_interval(self):
        # The helper hands the fan back to the driver when it restarts. A loop
        # that only wrote on change would leave the curve unenforced without
        # noticing until the target happened to move.
        loop = self.loop()
        loop.target(70, 88, 0)
        duty, _, changed = loop.target(70, 88, config.FAN_REASSERT_MS + 1)
        self.assertEqual((duty, changed, loop.reassert), (75, True, True))
        self.assertEqual(loop.last_change_ms, 0, "a re-assert is not a change")
        duty, _, changed = loop.target(70, 88, config.FAN_REASSERT_MS + 2)
        self.assertEqual((duty, changed, loop.reassert), (75, False, False))

    def test_spindown_waits_then_applies(self):
        loop = self.loop()
        loop.target(70, 88, 0)
        duty, _, changed = loop.target(60, 88, 1000)
        self.assertEqual((duty, changed), (75, False))
        duty, _, changed = loop.target(60, 88, config.FAN_SPINDOWN_MS + 1)
        self.assertEqual((duty, changed), (50, True))

    def test_ceiling_forces_full_speed_even_in_fixed_mode(self):
        loop = self.loop({"mode": "fixed", "speed": 25})
        loop.target(60, 88, 0)
        duty, forced, changed = loop.target(88, 88, 1000)
        self.assertEqual((duty, forced, changed), (100, True, True))
        # The way down is still protected by the spindown delay...
        duty, forced, changed = loop.target(60, 88, 1100)
        self.assertEqual((duty, forced, changed), (100, False, False))
        # ...and it applies once the delay has elapsed.
        duty, forced, changed = loop.target(60, 88, 1000 + config.FAN_SPINDOWN_MS + 1)
        self.assertEqual((duty, forced, changed), (25, False, True))


class FlakyFanBackend(FakeBackend):
    """Fails the next N duty writes, the way a restarted helper does."""

    def __init__(self):
        super().__init__("gpu")
        self.fan_failures = 0

    def set_fan_duty(self, percent):
        if self.fan_failures > 0:
            self.fan_failures -= 1
            raise BackendError("the root helper did not answer: broken pipe", cause="driver")
        return super().set_fan_duty(percent)


class NoHandBackBackend(FakeBackend):
    """Fails the next N hand-backs, the way a helper that went away does."""

    def __init__(self):
        super().__init__("gpu")
        self.auto_failures = 0

    def set_fan_auto(self):
        if self.auto_failures > 0:
            self.auto_failures -= 1
            raise BackendError("the root helper did not answer: broken pipe", cause="driver")
        super().set_fan_auto()


class CoreFanTests(unittest.TestCase):
    def setUp(self):
        self.tmp_obj = tempfile.TemporaryDirectory()
        self.core, _, self.gpu = make_core(self.tmp_obj.name)

    def tearDown(self):
        self.tmp_obj.cleanup()

    def test_fixed_fan_applies_through_the_trial_and_confirm_keeps_it(self):
        result = self.core.apply("gpu", {"fan": {"mode": "fixed", "speed": 55}})
        self.assertIn("trial", result)
        self.assertFalse(self.gpu.fan_auto)
        self.assertEqual(self.gpu.fan_speed, 55)
        state = self.core.snapshot()
        self.assertEqual(state["fans"]["gpu"]["config"], {"mode": "fixed", "speed": 55})
        self.assertTrue(state["fans"]["gpu"]["loopActive"])

        self.core.confirm()
        self.assertFalse(self.gpu.fan_auto)
        self.assertEqual(self.core.snapshot()["applied"]["gpu"]["fan"], {"mode": "fixed", "speed": 55})

    def test_revert_returns_the_fan_to_the_driver(self):
        self.core.apply("gpu", {"fan": {"mode": "fixed", "speed": 70}})
        self.assertFalse(self.gpu.fan_auto)
        self.core.revert("deadline")
        self.assertTrue(self.gpu.fan_auto)
        self.assertEqual(self.core.snapshot()["applied"]["gpu"]["fan"], {"mode": "auto"})
        self.assertFalse(self.core.snapshot()["fans"]["gpu"]["loopActive"])

    def test_fan_below_the_normal_floor_is_refused_before_hardware(self):
        with self.assertRaises(CoreError) as caught:
            self.core.apply("gpu", {"fan": {"mode": "fixed", "speed": 10}})
        self.assertEqual(caught.exception.code, "bad-value")
        self.assertTrue(self.gpu.fan_auto)
        self.assertEqual(self.gpu.fan_log, [])

    def test_fan_is_unavailable_without_a_writable_driver(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            gpu = FakeBackend("gpu", unavailable=True, cause="driver refused")
            core, _, _ = make_core(tmp.name, gpu=gpu)
            with self.assertRaises(CoreError) as caught:
                core.apply("gpu", {"fan": {"mode": "fixed", "speed": 60}})
            self.assertEqual(caught.exception.code, "unavailable")
        finally:
            tmp.cleanup()

    def test_auto_is_accepted_when_the_driver_refuses_writes(self):
        # Readable but unwritable (no service privilege): Fixed and Curve are
        # refused with the driver's cause, Auto still applies because the
        # driver already owns the fan and there is nothing to write.
        tmp = tempfile.TemporaryDirectory()
        try:
            gpu = FakeBackend("gpu")
            gpu.fan_writable = False
            core, _, _ = make_core(tmp.name, gpu=gpu)
            info = core.snapshot()["fans"]["gpu"]
            self.assertTrue(info["supported"])
            self.assertFalse(info["writable"])
            self.assertIn("refuses fan changes", info["cause"])

            with self.assertRaises(CoreError) as caught:
                core.apply("gpu", {"fan": {"mode": "fixed", "speed": 60}})
            self.assertEqual(caught.exception.code, "unavailable")
            self.assertTrue(gpu.fan_auto)

            core.apply("gpu", {"fan": {"mode": "auto"}})
            self.assertTrue(gpu.fan_auto)
            self.assertEqual(gpu.fan_log, [], "auto with no write access writes nothing")
            self.assertEqual(core.snapshot()["fans"]["gpu"]["config"], {"mode": "auto"})
        finally:
            tmp.cleanup()

    def test_a_read_only_verdict_is_reprobed_before_it_is_reported(self):
        # The fan helper can be started after the daemon: the cached verdict
        # must not refuse forever. check_fan_write() re-probes (it never
        # writes a duty on its own), and a still-read-only driver reports the
        # cause.
        self.gpu.fan_writable = False
        probes = []
        self.gpu.check_fan_write = lambda: probes.append(1)
        with self.assertRaises(CoreError) as caught:
            self.core.apply("gpu", {"fan": {"mode": "fixed", "speed": 60}})
        self.assertEqual(caught.exception.code, "unavailable")
        self.assertEqual(len(probes), 1)

    def test_set_fan_rechecks_the_helper_before_refusing_a_policy(self):
        # _set_fan is reached by a boot reapply and by resume, not only by
        # apply(): a helper that started after the probe must still serve the
        # first fixed duty instead of failing the whole GPU default.
        gpu = self.gpu
        gpu.fan_writable = False
        calls = []

        def check():
            calls.append(1)
            gpu.fan_writable = True

        gpu.check_fan_write = check
        self.core._set_fan({"mode": "fixed", "speed": 55})
        self.assertIsNotNone(self.core.fan_loop)
        self.assertEqual(gpu.fan_speed, 55)
        self.assertEqual(calls, [1])

    def test_curve_change_and_delayed_spindown_are_visible_in_the_fan_log(self):
        self.core.apply("gpu", {"fan": {"mode": "curve", "points": DEFAULT}})
        self.assertEqual(self.gpu.fan_log[-1]["percent"], 41)  # 54 °C on the default curve
        self.core.confirm()
        self.gpu.temperature_c = 70.0
        self.core._fan_tick()
        self.assertEqual(self.gpu.fan_log[-1]["percent"], 75)
        before = len(self.gpu.fan_log)
        self.gpu.temperature_c = 60.0
        self.core._fan_tick()
        self.assertEqual(len(self.gpu.fan_log), before, "a downshift inside the delay is held")
        # Pretend the last change is older than the spindown window.
        self.core.fan_loop.last_change_ms -= config.FAN_SPINDOWN_MS + 1
        self.core._fan_tick()
        self.assertEqual(self.gpu.fan_log[-1]["percent"], 50)

    def test_a_transient_write_failure_is_retried_not_fatal(self):
        # A helper restart drops the link once; the loop must write the same
        # duty again on the next sample instead of stopping the policy.
        tmp = tempfile.TemporaryDirectory()
        try:
            gpu = FlakyFanBackend()
            core, _, _ = make_core(tmp.name, gpu=gpu)
            core.apply("gpu", {"fan": {"mode": "curve", "points": DEFAULT}})
            core.confirm()
            gpu.temperature_c = 70.0
            gpu.fan_failures = 1
            core._fan_tick()
            self.assertTrue(core.snapshot()["fans"]["gpu"]["loopActive"])
            self.assertEqual(core.fan_loop.write_failures, 1)
            core._fan_tick()
            self.assertEqual(gpu.fan_speed, 75)
            self.assertFalse(gpu.fan_auto)
            self.assertEqual(core.fan_loop.write_failures, 0)
        finally:
            tmp.cleanup()

    def test_repeated_write_failures_stop_the_loop_and_free_the_fan(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            gpu = FlakyFanBackend()
            core, _, _ = make_core(tmp.name, gpu=gpu)
            core.apply("gpu", {"fan": {"mode": "curve", "points": DEFAULT}})
            core.confirm()
            gpu.temperature_c = 70.0
            gpu.fan_failures = config.FAN_WRITE_FAILURE_LIMIT
            for _ in range(config.FAN_WRITE_FAILURE_LIMIT):
                core._fan_tick()
            state = core.snapshot()
            self.assertFalse(state["fans"]["gpu"]["loopActive"])
            self.assertTrue(gpu.fan_auto)
            self.assertEqual(state["notice"]["type"], "apply-failed")
        finally:
            tmp.cleanup()

    def test_a_partial_apply_restarts_a_policy_that_lost_its_loop(self):
        self.core.apply("gpu", {"fan": {"mode": "fixed", "speed": 55}})
        self.core.confirm()
        self.assertEqual(self.gpu.fan_speed, 55)
        # The applied record keeps the policy when the loop stops (a helper
        # outage, a failed apply). A later apply that does not mention the fan
        # must start it again instead of trusting that record.
        self.core._stop_fan(set_auto=True)
        self.core.apply("gpu", {"memory": 600})
        self.core.confirm()
        self.assertIsNotNone(self.core.fan_loop)
        self.assertEqual(self.gpu.fan_speed, 55)

    def test_a_failed_initial_fan_apply_leaves_no_dead_loop(self):
        # A write refused during the first sample used to leave a loop object
        # with its timer cancelled: loopActive stayed true while the fan ran
        # on the driver's curve.
        tmp = tempfile.TemporaryDirectory()
        try:
            gpu = FlakyFanBackend()
            core, _, _ = make_core(tmp.name, gpu=gpu)
            gpu.fan_failures = 1
            with self.assertRaises(BackendError):
                core._set_fan({"mode": "fixed", "speed": 55})
            self.assertIsNone(core.fan_loop)
            self.assertFalse(core.snapshot()["fans"]["gpu"]["loopActive"])
            self.assertTrue(gpu.fan_auto)
        finally:
            tmp.cleanup()

    def test_a_failed_hand_back_is_reported_in_the_fan_descriptor(self):
        # A stopped loop that could not hand the fan back leaves the last
        # manual duty to NVML: the descriptor must not let the UI claim the
        # driver controls the fan.
        tmp = tempfile.TemporaryDirectory()
        try:
            gpu = NoHandBackBackend()
            core, _, _ = make_core(tmp.name, gpu=gpu)
            core.apply("gpu", {"fan": {"mode": "fixed", "speed": 55}})
            core.confirm()
            gpu.auto_failures = 1
            core._stop_fan(set_auto=True)
            info = core.snapshot()["fans"]["gpu"]
            self.assertFalse(info["loopActive"])
            self.assertTrue(info["handBackFailed"])
            self.assertFalse(gpu.fan_auto)
            self.assertEqual(gpu.fan_speed, 55, "the manual duty outlives the failed hand-back")
            kinds = [r["kind"] for r in core.journal.tail(20)]
            self.assertIn("fan-auto-failed", kinds)
        finally:
            tmp.cleanup()

    def test_a_running_loop_or_a_successful_auto_clears_the_hand_back_failure(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            gpu = NoHandBackBackend()
            core, _, _ = make_core(tmp.name, gpu=gpu)
            core.apply("gpu", {"fan": {"mode": "fixed", "speed": 55}})
            core.confirm()
            gpu.auto_failures = 1
            core._stop_fan(set_auto=True)
            self.assertTrue(core.snapshot()["fans"]["gpu"]["handBackFailed"])

            core._set_fan({"mode": "fixed", "speed": 60})
            self.assertFalse(core.snapshot()["fans"]["gpu"]["handBackFailed"])
            gpu.auto_failures = 1
            core._stop_fan(set_auto=True)
            self.assertTrue(core.snapshot()["fans"]["gpu"]["handBackFailed"])

            core._set_fan({"mode": "auto"})
            self.assertFalse(core.snapshot()["fans"]["gpu"]["handBackFailed"])
            self.assertTrue(gpu.fan_auto)
        finally:
            tmp.cleanup()

    def test_ceiling_override_is_forced_and_reported(self):
        self.core.apply("gpu", {"fan": {"mode": "fixed", "speed": 25}})
        self.core.confirm()
        self.gpu.temperature_c = 95.0
        self.core._fan_tick()
        self.assertEqual(self.gpu.fan_speed, 100)
        self.assertTrue(self.core.snapshot()["fans"]["gpu"]["override"])

    def test_saved_default_is_reapplied_on_a_clean_boot(self):
        self.core.apply("gpu", {"fan": {"mode": "fixed", "speed": 45}}, "default")
        self.core.confirm()
        self.core.mark_clean_shutdown()
        # mark_clean_shutdown handed the fake fan back to auto...
        self.assertTrue(self.gpu.fan_auto)

        gpu = FakeBackend("gpu")
        core, _, _ = make_core(self.tmp_obj.name, gpu=gpu)
        # ...and the clean boot reapplied the saved fixed duty.
        self.assertFalse(gpu.fan_auto)
        self.assertEqual(gpu.fan_speed, 45)
        self.assertEqual(core.snapshot()["applied"]["gpu"]["fan"], {"mode": "fixed", "speed": 45})

    def test_reset_returns_the_fan_to_auto(self):
        self.core.apply("gpu", {"fan": {"mode": "curve", "points": DEFAULT}}, "default")
        self.core.confirm()
        self.core.apply("gpu", {}, "reset")
        self.assertTrue(self.gpu.fan_auto)
        self.assertEqual(self.core.snapshot()["applied"]["gpu"]["fan"], {"mode": "auto"})

    def test_expert_widens_the_fixed_floor_in_the_descriptor(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            core, _, _ = make_core(tmp.name, mode="expert")
            self.assertEqual(core.snapshot()["fans"]["gpu"]["curve"]["fixedMinP"], 0)
            core.apply("gpu", {"fan": {"mode": "fixed", "speed": 0}})
            self.assertEqual(core.snapshot()["fans"]["gpu"]["config"]["speed"], 0)
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
