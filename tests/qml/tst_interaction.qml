import QtQuick
import QtTest
import qs.Commons
import qs.Commons as Commons
import "../../ui/ash.omatuner" as Tuner
import "../../ui/ash.omatuner/Model.js" as Model
import "RealGpuState.js" as Real

// Real rendered interaction test for the GPU tab. It loads the shipped
// TuneControl/TuneSlider/CurveView (not copies), uses the GPU state captured
// read-only from the reference GTX 1060, and drives it with QtTest mouse and
// keyboard events. The controller mirrors Panel.qml's draft functions; the
// Python test also checks that Panel.qml uses the same Model.copyDraft touch.
TestCase {
  id: test
  name: "GpuTabInteraction"
  when: windowShown
  width: 440
  height: 980
  visible: true

  property var baseState: Real.state

  QtObject {
    id: daemon
    property bool online: true
    property var state: test.baseState
    property var telemetry: ({ gpuFrequency: 1125, gpuVoltage: 0.806 })
    property var calls: []
    signal pushed()
    signal ticked()

    // The real daemon re-emits its snapshot; the panel reconciles drafts on
    // every push. Same state object, as the brief asks.
    function pushState() {
      pushed()
    }

    function tick() {
      telemetry = { gpuFrequency: 1125 + Math.round(Math.random() * 20), gpuVoltage: 0.806 }
      ticked()
    }

    function call(method, params, callback) {
      calls.push({ method: method, params: params })
      if (callback) callback({ ok: true, result: {} })
    }
  }

  QtObject {
    id: controller
    property var drafts: ({})
    property bool curveMode: false
    readonly property string component: "gpu"
    readonly property var controlsNow: daemon.state && daemon.state.controls
      ? daemon.state.controls.gpu || [] : []

    function componentOf(which) {
      return which === "cpu" ? "cpu" : "gpu"
    }

    function draftFor(which) {
      return drafts[componentOf(which)]
    }

    function controlByKey(key) {
      for (var i = 0; i < controlsNow.length; i++) {
        if (controlsNow[i].key === key) return controlsNow[i]
      }
      return null
    }

    function appliedFor(control) {
      if (!control) return 0
      if (!daemon.state) return control.factory
      return Model.appliedValue(daemon.state, component, control.key, control)
    }

    function ensureDrafts() {
      if (!daemon.state) return
      var next = { gpu: drafts.gpu }
      next.gpu = next.gpu
        ? Model.reconcileDraft(next.gpu, daemon.state, "gpu")
        : Model.newDraft(daemon.state, "gpu")
      drafts = next
    }

    function reconcileDrafts() {
      if (!daemon.state) return
      ensureDrafts()
    }

    // Must match Panel.qml: drafts are plain JS objects mutated in place, so
    // reassigning the same object notifies nothing. Copy the mutated draft.
    function touchDrafts() {
      drafts = { gpu: Model.copyDraft(drafts.gpu) }
    }

    function setControl(key, value) {
      var draft = drafts[component]
      if (!draft || !daemon.state) return
      var control = controlByKey(key)
      if (!control || !control.available) return
      var result = Model.sliderChange(draft, control, value, daemon.state, component)
      draft.values[key] = result.value
      if (result.points) draft.points = result.points
      draft.reset = false
      draft.reverted = false
      touchDrafts()
    }

    function changePoint(index, frequency, flatten, anchorList) {
      var draft = drafts.gpu
      if (!draft || !daemon.state) return
      var result = Model.pointChange(draft, index, frequency, flatten,
        daemon.state, "gpu", anchorList)
      draft.points = result.points
      draft.reset = false
      draft.reverted = false
      touchDrafts()
    }

    function resetCurve() {
      var draft = drafts.gpu
      if (!draft || !daemon.state.curves.gpu) return
      draft.points = Model.factoryCurve(daemon.state.curves.gpu)
      draft.reset = false
      draft.reverted = false
      touchDrafts()
    }

    // Mirrors Panel.addCurveAnchor: a pinned table index beside the
    // automatic spread, kept with the draft and never sent to the daemon.
    function addAnchor(index) {
      var draft = drafts.gpu
      if (!draft || !draft.points) return
      var pins = Model.sanitizeAnchors((draft.anchors || []).concat([index]), draft.points)
      if (pins.length === (draft.anchors || []).length) return
      var next = Model.copyDraft(draft)
      next.anchors = pins
      drafts = { gpu: next }
    }

    function removeAnchor(index) {
      var draft = drafts.gpu
      if (!draft || !draft.anchors || draft.anchors.indexOf(index) < 0) return
      var next = Model.copyDraft(draft)
      next.anchors = Model.withoutAnchor(draft.anchors, index)
      drafts = { gpu: next }
    }

    function stageFactory() {
      var draft = drafts[component]
      if (!draft || !daemon.state) return
      Model.stageFactory(draft, daemon.state, component)
      touchDrafts()
    }
  }

  Connections {
    target: daemon
    function onPushed() { controller.reconcileDrafts() }
  }

  Timer {
    interval: 1000
    running: true
    repeat: true
    onTriggered: daemon.tick()
  }

  Column {
    id: ui
    x: 20
    y: 20
    // The popup's content width: 400 px card minus 16 px padding each side.
    width: 368
    spacing: 16

    Repeater {
      model: controller.controlsNow.length

      Tuner.TuneControl {
        readonly property var modelData: controller.controlsNow.length > index
          ? controller.controlsNow[index] : null
        objectName: "row-" + modelData.key
        width: ui.width
        control: modelData
        draft: controller.draftFor("gpu")
        applied: controller.appliedFor(modelData)
        helpText: ""
        cursorHere: false
        trialRunning: false
        rowForeground: Commons.Color.foreground
        showCurveToggle: modelData.key === "core"
        curveToggleOn: modelData.key === "core" && controller.curveMode
        curveToggleEnabled: modelData.key === "core"
        curveToggleTooltip: "Per-point curve"
        curveMode: modelData.key === "core" && controller.curveMode
        curveData: daemon.state.curves.gpu
        curveDaemon: daemon
        onCommitted: function(key, value) { controller.setControl(key, value) }
        onCurvePointEdited: function(index, frequency, flatten, anchorList) {
          controller.changePoint(index, frequency, flatten, anchorList)
        }
        onCurveExactSubmitted: function(index, frequency, anchorList) {
          controller.changePoint(index, frequency, false, anchorList)
        }
        onCurveAnchorAdded: function(index) { controller.addAnchor(index) }
        onCurveAnchorRemoved: function(index) { controller.removeAnchor(index) }
        onCurveResetRequested: controller.resetCurve()
      }
    }

    Text {
      id: factoryAction
      objectName: "factory-action"
      text: "Factory"
      color: Commons.Color.accent
      font.family: "monospace"
      font.pixelSize: 12

      MouseArea {
        anchors.fill: parent
        cursorShape: Qt.PointingHandCursor
        onClicked: controller.stageFactory()
      }
    }
  }

  function initTestCase() {
    controller.drafts = {}
    controller.curveMode = false
    controller.ensureDrafts()
    wait(50)
  }

  function init() {
    daemon.calls = []
    daemon.state = baseState
    controller.drafts = {}
    controller.curveMode = false
    controller.ensureDrafts()
    wait(50)
  }

  function row(key) {
    var item = findChild(test, "row-" + key)
    verify(item !== null, "row " + key + " exists")
    return item
  }

  function test_exact_value_survives_enter_and_state_pushes() {
    var memory = row("memory")
    compare(memory.valueText, "0")

    mouseClick(memory.valueItem, memory.valueItem.width / 2,
      memory.valueItem.height / 2)
    wait(20)
    verify(memory.exactShown, "clicking the value opens the exact editor")
    keyClick("5")
    keyClick("0")
    keyClick("0")
    keyClick(Qt.Key_Return)
    wait(20)

    compare(controller.drafts.gpu.values.memory, 500)
    compare(memory.valueText, "+500", "the rendered value follows Enter")
    compare(memory.exactShown, false)
    compare(daemon.calls.length, 0, "Enter does not reach an apply path")

    for (var i = 0; i < 3; i++) {
      daemon.tick()
      daemon.pushState()
      wait(30)
      compare(controller.drafts.gpu.values.memory, 500)
      compare(memory.valueText, "+500", "state push " + (i + 1) + " keeps the value")
    }
    verify(memory.wasShown)
    compare(memory.wasText, "0 MHz")
    compare(memory.knobX > memory.factoryKnobX, true, "the knob follows the value")
  }

  function test_was_line_restores_the_memory_row() {
    var memory = row("memory")
    controller.setControl("memory", 500)
    wait(20)

    compare(memory.valueText, "+500")
    verify(memory.wasShown, "the restore line appears when the draft differs")
    verify(memory.wasItem.width > 0 && memory.wasItem.height > 0,
      "the restore line has a hit area")
    compare(memory.wasText, "0 MHz")

    mouseClick(memory.wasItem, memory.wasItem.width / 2, memory.wasItem.height / 2)
    wait(20)
    compare(controller.drafts.gpu.values.memory, 0)
    compare(memory.valueText, "0")
    verify(!memory.wasShown)
  }

  function test_slider_drag_updates_value_and_knob() {
    var memory = row("memory")
    var slider = memory.sliderItem
    var factoryX = memory.factoryKnobX

    mouseDrag(slider, memory.knobX, slider.height / 2, 30, 0)
    wait(30)
    var value = controller.drafts.gpu.values.memory
    compare(value > 0, true, "the drag reaches the draft")
    compare(memory.valueText, "+" + value, "the readout follows the drag")
    compare(memory.knobX > factoryX, true, "the knob follows the drag")
    verify(memory.wasShown)
  }

  function test_factory_restores_drafts_and_readouts() {
    var memory = row("memory")
    var core = row("core")
    controller.setControl("memory", 500)
    controller.setControl("core", 50)
    wait(20)

    compare(memory.valueText, "+500")
    compare(core.valueText, "+50")
    compare(memory.knobX > memory.factoryKnobX, true)

    mouseClick(factoryAction, factoryAction.width / 2, factoryAction.height / 2)
    wait(20)

    compare(controller.drafts.gpu.values.memory, 0)
    compare(controller.drafts.gpu.values.core, 0)
    compare(memory.valueText, "0")
    compare(core.valueText, "0")
    compare(Math.abs(memory.knobX - memory.factoryKnobX) < 0.5, true)
    verify(!memory.wasShown)
  }

  function test_curve_drag_updates_the_rendered_curve() {
    controller.curveMode = true
    wait(50)
    var core = row("core")
    var curve = core.curveViewItem
    verify(curve.visible, "curve mode renders the plot")
    verify(curve.draftPoints.length === 80, "the real 80-point table is loaded")

    var handles = curve.handlePositions()
    compare(handles.length, 6)
    // Anchor 2 (v 0.7125, 1328 MHz) has its next neighbour at 1620 MHz, so a
    // small upward drag stays below the clamp and the +1 keyboard step is
    // observable. A big drag would clamp to the neighbour and hide it.
    var handle = handles[2]
    var beforeDraft = controller.drafts.gpu.points[handle.index].f
    var beforeRendered = curve.draftPoints[handle.index].f
    var beforePaints = curve.paintCount

    mouseDrag(curve, handle.x, handle.y, 0, -5)
    wait(30)

    compare(controller.drafts.gpu.points[handle.index].f > beforeDraft, true,
      "the drag reaches the draft")
    compare(curve.draftPoints[handle.index].f > beforeRendered, true,
      "the rendered curve follows the drag")
    compare(curve.paintCount > beforePaints, true, "the canvas repainted")
    compare(curve.selected, handle.index)

    var afterDrag = curve.draftPoints[handle.index].f
    keyClick(Qt.Key_Up)
    wait(20)
    compare(curve.draftPoints[handle.index].f, afterDrag + 1, "Up steps the frequency up")
    keyClick(Qt.Key_Down)
    wait(20)
    compare(curve.draftPoints[handle.index].f, afterDrag, "Down steps it back")

    keyClick(Qt.Key_Right)
    wait(20)
    compare(curve.selected, handles[3].index, "Right selects the next anchor")
    keyClick(Qt.Key_Left)
    wait(20)
    compare(curve.selected, handle.index, "Left selects the previous anchor")

    var edited = curve.draftPoints[handle.index].f
    daemon.pushState()
    wait(30)
    compare(curve.draftPoints[handle.index].f, edited,
      "a daemon state push keeps the edited point")
  }

  function test_anchor_is_added_by_double_click_and_removed_by_delete() {
    controller.curveMode = true
    wait(50)
    var core = row("core")
    var curve = core.curveViewItem
    verify(curve.visible, "curve mode renders the plot")

    var handles = curve.handlePositions()
    compare(handles.length, 6)
    var left = handles[2]
    var right = handles[3]
    var middleX = (left.x + right.x) / 2
    var middleY = (left.y + right.y) / 2

    mouseDoubleClickSequence(curve, middleX, middleY)
    wait(40)

    var widened = curve.handlePositions()
    compare(widened.length, 7, "the double-click adds a handle")
    var added = curve.selected
    verify(added > left.index && added < right.index,
      "the new handle sits between its neighbours: " + added
      + " (neighbours " + left.index + " and " + right.index + ")")
    verify(controller.drafts.gpu.anchors.indexOf(added) >= 0,
      "the handle is pinned in the draft")
    verify(curve.visible, "the plot is still there")

    // Pins are view state: the daemon keeps receiving the full table.
    compare(curve.draftPoints.length, 80)

    keyClick(Qt.Key_Delete)
    wait(40)
    compare(curve.handlePositions().length, 6, "Delete removes the pinned handle")
    compare(controller.drafts.gpu.anchors.length, 0)

    // An automatic handle is derived from the plot width, so it cannot be
    // removed: Delete there leaves the set alone.
    curve.selectDefault()
    wait(20)
    var auto = curve.selected
    verify(controller.drafts.gpu.anchors.indexOf(auto) < 0, "selection is an automatic handle")
    keyClick(Qt.Key_Delete)
    wait(20)
    compare(curve.handlePositions().length, 6)
  }

  function test_unavailable_control_is_muted_with_its_cause() {
    var temp = row("temp")
    verify(temp.unavailable)
    compare(temp.opacity < 1, true)
    verify(temp.causeText.length > 0, "the cause is rendered")
    verify(temp.causeText.indexOf("refuses temperature changes") >= 0,
      "the cause is the driver's, not a generic label")
  }

  function test_rows_survive_a_state_replacement() {
    var before = row("memory")
    // Every daemon frame is a fresh object graph, so the controls array is a
    // new array too. The row must keep its delegate, and with it any value
    // being typed.
    daemon.state = JSON.parse(JSON.stringify(baseState))
    wait(40)

    var after = row("memory")
    verify(after === before, "the same delegate object serves the new state")
    compare(after.control.key, "memory", "the delegate reads the new control")
  }
}
