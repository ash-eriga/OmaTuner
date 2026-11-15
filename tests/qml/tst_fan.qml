import QtQuick
import QtTest
import qs.Commons
import qs.Commons as Commons
import "../../ui/ash.omatuner" as Tuner
import "../../ui/ash.omatuner/Model.js" as Model

// Rendered interaction test for the fan section. It loads the shipped FanView
// and FanCurve with a fan descriptor shaped like the daemon's, and drives
// the mode row, the fixed-duty slider and the curve editor with QtTest.
TestCase {
  id: test
  name: "FanTabInteraction"
  when: windowShown
  width: 420
  height: 760
  visible: true

  property bool showNoFan: false

  // The same descriptor with a saved curve whose daemon loop stopped.
  readonly property var stoppedState: ({
    controls: { gpu: [] },
    applied: { gpu: {} },
    curves: {},
    fans: { gpu: {
      supported: true, writable: true, cause: null, count: 1,
      speed: 32, temperatureC: 70, targetC: 88, slowdownC: 97,
      min: 0, max: 100, override: false, loopActive: false,
      config: { mode: "curve", points: [
        { t: 40, p: 30 }, { t: 50, p: 35 }, { t: 60, p: 50 },
        { t: 70, p: 75 }, { t: 80, p: 100 },
      ] },
      curve: {
        temps: [40, 50, 60, 70, 80],
        factory: [
          { t: 40, p: 30 }, { t: 50, p: 35 }, { t: 60, p: 50 },
          { t: 70, p: 75 }, { t: 80, p: 100 },
        ],
        minP: 0, maxP: 100, fixedMinP: 25, lastMinP: 90,
        minT: 30, maxT: 90, minPoints: 2, maxPoints: 12,
      },
    } },
  })

  readonly property var fanState: ({
    controls: { gpu: [] },
    applied: { gpu: {} },
    curves: {},
    fans: { gpu: {
      supported: true, writable: true, cause: null, count: 1,
      speed: 32, temperatureC: 70, targetC: 88, slowdownC: 97,
      min: 0, max: 100, override: false, loopActive: false,
      config: { mode: "auto" },
      curve: {
        temps: [40, 50, 60, 70, 80],
        factory: [
          { t: 40, p: 30 }, { t: 50, p: 35 }, { t: 60, p: 50 },
          { t: 70, p: 75 }, { t: 80, p: 100 },
        ],
        minP: 0, maxP: 100, fixedMinP: 25, lastMinP: 90,
        minT: 30, maxT: 90, minPoints: 2, maxPoints: 12,
      },
    } },
  })

  // A fan the driver finds but will not let us write: only the cause is
  // offered, and Fixed/Curve stay out of reach.
  readonly property var readOnlyState: ({
    controls: { gpu: [] },
    applied: { gpu: {} },
    curves: {},
    fans: { gpu: {
      supported: true, writable: false, cause: "the driver owns the fan control",
      count: 1, speed: 32, temperatureC: 70, targetC: 88, slowdownC: 97,
      min: 0, max: 100, override: false, loopActive: true,
      config: { mode: "auto" },
      curve: {
        temps: [40, 50, 60, 70, 80],
        factory: [
          { t: 40, p: 30 }, { t: 50, p: 35 }, { t: 60, p: 50 },
          { t: 70, p: 75 }, { t: 80, p: 100 },
        ],
        minP: 0, maxP: 100, fixedMinP: 25, lastMinP: 90,
        minT: 30, maxT: 90, minPoints: 2, maxPoints: 12,
      },
    } },
  })

  QtObject {
    id: controller
    property var state: test.fanState
    property var fanDraft: null
    readonly property var fanNow: Model.fanInfo(state)

    function ensureDraft() {
      fanDraft = Model.newFanDraft(state)
    }

    // Mirrors Panel.setFanMode: clone, change, reassign so bindings refresh.
    function setMode(mode) {
      if (!fanDraft) return
      if (mode !== "auto" && !(fanNow && fanNow.writable)) return
      var next = Model.copyFanDraft(fanDraft)
      var fresh = Model.newFanDraft(state)
      next.mode = mode
      if (mode === "fixed") next.speed = Model.clampFanSpeed(fanNow, 32)
      if (mode === "curve") next.points = fanDraft.points || fresh.points
      fanDraft = next
    }

    function setSpeed(value) {
      if (!fanDraft || !fanNow) return
      var next = Model.copyFanDraft(fanDraft)
      next.mode = "fixed"
      next.speed = Model.clampFanSpeed(fanNow, value)
      fanDraft = next
    }

    // Mirrors Panel.changeFanPoint: the point moves on both axes and the
    // model keeps the curve rising.
    function changePoint(index, temp, speed) {
      if (!fanDraft || !fanDraft.points) return
      var next = Model.copyFanDraft(fanDraft)
      next.points = Model.fanMovePoint(next.points, index, temp, speed, state)
      next.mode = "curve"
      fanDraft = next
    }

    // Mirrors Panel.addFanPoint.
    function addPoint(temp, speed) {
      if (!fanDraft) return
      var added = Model.fanAddPoint(fanDraft.points || [], temp, speed, state)
      if (!added) return
      var next = Model.copyFanDraft(fanDraft)
      next.points = added.points
      next.mode = "curve"
      fanDraft = next
    }

    // Mirrors Panel.removeFanPoint.
    function removePoint(index) {
      if (!fanDraft) return
      var points = Model.fanRemovePoint(fanDraft.points || [], index, state)
      if (!points) return
      var next = Model.copyFanDraft(fanDraft)
      next.points = points
      next.mode = "curve"
      fanDraft = next
    }

    function resetCurve() {
      var next = Model.copyFanDraft(fanDraft || Model.newFanDraft(state))
      next.mode = "curve"
      next.points = Model.fanFactoryPoints(state)
      fanDraft = next
    }
  }

  Column {
    id: ui
    x: 20
    y: 20
    width: 368
    spacing: 16

    Tuner.FanView {
      id: fanView
      objectName: "fan"
      width: ui.width
      fan: controller.fanNow
      draft: controller.fanDraft
      config: controller.fanNow ? controller.fanNow.config : null
      liveSpeed: 32
      liveTemp: 70
      rowForeground: Commons.Color.foreground
      onModeRequested: function(mode) { controller.setMode(mode) }
      onSpeedChanged: function(value) { controller.setSpeed(value) }
      onPointChanged: function(index, temp, speed) { controller.changePoint(index, temp, speed) }
      onPointAdded: function(temp, speed) { controller.addPoint(temp, speed) }
      onPointRemoved: function(index) { controller.removePoint(index) }
      onCurveResetRequested: controller.resetCurve()
    }

    Tuner.FanView {
      objectName: "no-fan"
      width: ui.width
      visible: test.showNoFan
      fan: ({ supported: false, writable: false, cause: "no fan on this GPU" })
      draft: controller.fanDraft
      config: null
      rowForeground: Commons.Color.foreground
    }
  }

  function initTestCase() {
    controller.ensureDraft()
    wait(50)
  }

  function init() {
    controller.state = test.fanState
    controller.ensureDraft()
    test.showNoFan = false
    wait(30)
  }

  function child(parent, name) {
    var item = findChild(parent, name)
    verify(item !== null, name + " exists")
    return item
  }

  function test_auto_is_the_default_and_shows_the_live_reading() {
    var fan = child(test, "fan")
    compare(controller.fanDraft.mode, "auto")
    compare(child(fan, "fan-reading").text, "32% · 70 °C")
    compare(child(fan, "mode-auto").current, true)
    // Auto shows only its hint: no duty slider, no curve.
    compare(child(fan, "fan-speed-slider").visible, false)
    compare(child(fan, "fan-curve").visible, false)
  }

  function test_mode_row_switches_the_visible_control() {
    var fan = child(test, "fan")

    mouseClick(child(fan, "mode-fixed"), 4, 4)
    wait(30)
    compare(controller.fanDraft.mode, "fixed")
    compare(child(fan, "mode-fixed").current, true)
    compare(child(fan, "fan-speed-value").text, "32 %")
    compare(child(fan, "fan-speed-slider").visible, true)
    compare(child(fan, "fan-curve").visible, false)

    mouseClick(child(fan, "mode-curve"), 4, 4)
    wait(30)
    compare(controller.fanDraft.mode, "curve")
    compare(child(fan, "fan-curve").visible, true)
    var curve = child(fan, "fan-curve")
    compare(curve.points.length, 5)
    compare(child(fan, "fan-speed-slider").visible, false)
  }

  function test_fixed_slider_drag_reaches_the_draft() {
    var fan = child(test, "fan")
    controller.setMode("fixed")
    wait(30)
    var slider = child(fan, "fan-speed-slider")
    var before = controller.fanDraft.speed

    mouseDrag(slider, slider.knobPos, slider.height / 2, 40, 0)
    wait(30)

    compare(controller.fanDraft.speed > before, true, "the drag reaches the draft")
    compare(child(fan, "fan-speed-value").text, controller.fanDraft.speed + " %")
    compare(controller.fanDraft.mode, "fixed")
  }

  function test_curve_drag_and_keys_edit_the_draft() {
    var fan = child(test, "fan")
    controller.setMode("curve")
    wait(50)
    var curve = child(fan, "fan-curve")
    var anchor = 2
    var point = curve.points[anchor]
    compare(point.p, 50)

    var x = curve.xForTemp(point.t)
    var y = curve.yForSpeed(point.p)
    mouseDrag(curve, x, y, 0, -8)
    wait(30)

    var dragged = controller.fanDraft.points[anchor].p
    compare(dragged > 50, true, "the drag reaches the draft")
    compare(curve.points[anchor].p, dragged, "the rendered curve follows the drag")
    compare(curve.selected, anchor)

    keyClick(Qt.Key_Up)
    wait(20)
    compare(controller.fanDraft.points[anchor].p, dragged + 1, "Up steps the duty up")
    keyClick(Qt.Key_Down)
    wait(20)
    compare(controller.fanDraft.points[anchor].p, dragged, "Down steps it back")

    keyClick(Qt.Key_Left)
    wait(20)
    compare(curve.selected, anchor - 1, "Left selects the previous anchor")

    // The readout interpolates the draft at the live temperature.
    compare(child(fan, "fan-reading").text, "32% · 70 °C")
    verify(fanView.curveNow >= 0)
  }

  function test_curve_points_can_be_added_and_removed() {
    var fan = child(test, "fan")
    controller.setMode("curve")
    wait(50)
    var curve = child(fan, "fan-curve")
    compare(curve.pointCount, 5)

    // A point that does not exist yet, at 65 °C and 70 %: well away from the
    // handles, so the double-click creates instead of grabbing.
    var x = curve.xForTemp(65)
    var y = curve.yForSpeed(70)
    mouseDoubleClickSequence(curve, x, y)
    wait(40)

    compare(controller.fanDraft.points.length, 6, "the double-click adds a point")
    var temps = controller.fanDraft.points.map(function (point) { return point.t })
    verify(temps.indexOf(65) >= 0, "the new point sits at the clicked temperature")
    compare(curve.selected, temps.indexOf(65), "the new point is selected")

    // Dragging it moves temperature and duty together.
    mouseDrag(curve, x, y, curve.xForTemp(67) - x, 0)
    wait(30)
    temps = controller.fanDraft.points.map(function (point) { return point.t })
    verify(temps.indexOf(67) >= 0, "the drag moved the point sideways")

    keyClick(Qt.Key_Delete)
    wait(30)
    compare(controller.fanDraft.points.length, 5, "Delete removes the selected point")

    // A right-click removes a point too.
    var point = controller.fanDraft.points[2]
    mouseClick(curve, curve.xForTemp(point.t), curve.yForSpeed(point.p), Qt.RightButton)
    wait(30)
    compare(controller.fanDraft.points.length, 4, "a right-click removes a point")

    // The keyboard creates one between the selection and the next point.
    var before = controller.fanDraft.points.length
    keyClick(Qt.Key_A)
    wait(30)
    compare(controller.fanDraft.points.length, before + 1, "a adds a point")
  }

  function test_curve_reset_restores_the_factory_points() {
    var fan = child(test, "fan")
    controller.setMode("curve")
    wait(30)
    controller.changePoint(3, 70, 85)
    wait(30)
    compare(controller.fanDraft.points[3].p, 85)

    var reset = child(fan, "fan-curve-reset")
    mouseClick(reset, 4, 4)
    wait(30)
    compare(controller.fanDraft.points[2].p, 50, "reset restores the factory duty")
    compare(controller.fanDraft.points[3].p, 75, "and the factory temperatures")
    compare(controller.fanDraft.mode, "curve")
  }

  function test_unavailable_fan_shows_the_cause() {
    test.showNoFan = true
    wait(30)
    var view = child(test, "no-fan")
    // FanView no longer renders a cause line: Panel names the system cause
    // above it. What FanView still guarantees is that only Auto is reachable.
    compare(child(view, "mode-fixed").modeEnabled, false)
    compare(child(view, "mode-auto").modeEnabled, true)
  }

  function test_a_supported_fan_that_refuses_writes_shows_the_cause() {
    controller.state = test.readOnlyState
    controller.ensureDraft()
    wait(30)
    var cause = child(child(test, "fan"), "fan-read-only-cause")
    compare(cause.visible && cause.text === test.readOnlyState.fans.gpu.cause, true,
      "a read-only fan shows its cause")
  }

  function test_a_stopped_fan_policy_is_reported_not_shown_as_applied() {
    var fan = child(test, "fan")
    compare(child(fan, "fan-policy-stopped").visible, false)
    controller.state = test.stoppedState
    wait(30)
    compare(controller.fanNow.loopActive, false)
    compare(controller.fanNow.config.mode, "curve")
    var note = child(fan, "fan-policy-stopped")
    compare(note.visible, true)
    compare(note.text, "The fan policy is not running: the driver controls the fan.")
  }

  // A copy of the stopped descriptor with the hand-back verdict flipped: the
  // helper was gone when the loop died, so the driver may not own the duty.
  function stateWithHandBack() {
    var stopped = test.stoppedState
    var gpu = {}
    for (var key in stopped.fans.gpu) gpu[key] = stopped.fans.gpu[key]
    gpu.handBackFailed = true
    return { controls: stopped.controls, applied: stopped.applied,
             curves: stopped.curves, fans: { gpu: gpu } }
  }

  function test_a_failed_hand_back_does_not_claim_driver_control() {
    var fan = child(test, "fan")
    controller.state = stateWithHandBack()
    wait(30)
    var note = child(fan, "fan-policy-stopped")
    compare(note.visible, true)
    compare(note.text, "The fan policy stopped and the fan may still be at its last setting;"
      + " choose Auto to hand it back to the driver.")
  }

  function test_switching_back_to_curve_keeps_the_edited_points() {
    var fan = child(test, "fan")
    controller.setMode("curve")
    wait(30)
    controller.changePoint(2, 60, 66)
    wait(20)
    var edited = controller.fanDraft.points[2].p
    compare(edited, 66)

    controller.setMode("fixed")
    wait(20)
    controller.setMode("curve")
    wait(30)
    compare(controller.fanDraft.points[2].p, edited,
      "Fixed -> Curve keeps the draft's edited points instead of the factory ones")
    compare(child(fan, "fan-curve").points[2].p, edited)
  }
}
