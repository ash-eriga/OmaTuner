import QtQuick
import qs.Commons
import qs.Commons as Commons
import "Model.js" as Model

// Thin offset slider: a 4 px track, a 15 px knob, a tick where the machine
// currently runs and a fill from the factory value to the knob. Keys work
// only while it holds focus (click it, or press Enter on its row); the wheel
// is ignored otherwise so scrolling the panel never tunes the machine.
Item {
  id: root

  property var control: null
  property int value: 0
  property int applied: 0
  property bool unavailable: false
  property bool risky: false
  property bool cursorHere: false
  property color foreground: Commons.Color.foreground
  property color accent: Commons.Color.accent
  property color riskColor: Commons.Color.urgent

  readonly property bool focused: activeFocus && !root.unavailable
  readonly property bool dragging: mouse.dragging
  readonly property bool hovered: mouse.containsMouse
  readonly property int trackHeight: 4
  readonly property int knobSize: 15
  // Room for the focus ring around the knob at either end, so the body's
  // clip never cuts it.
  readonly property real padX: (knobSize + 6) / 2
  readonly property real span: Math.max(1, width - knobSize)
  readonly property real factoryPos: root.control ? root.pos(root.control.factory) : 0
  readonly property real appliedPos: root.control ? root.pos(root.applied) : 0
  readonly property real knobPos: root.control ? root.pos(root.value) : 0
  readonly property real fillFrom: Math.min(root.factoryPos, root.knobPos)
  readonly property real fillWidth: Math.abs(root.knobPos - root.factoryPos)

  signal preview(int liveValue)
  signal committed(int value)
  signal keyboardFocusChanged(bool focused)
  signal enterPressed()
  signal escapePressed()
  signal tabRequested(int direction)

  implicitHeight: knobSize + Style.space(10)
  focus: true
  activeFocusOnTab: false
  onActiveFocusChanged: root.keyboardFocusChanged(activeFocus)
  Accessible.role: Accessible.Slider
  Accessible.name: root.control ? root.control.label : ""
  Accessible.description: root.control ? root.value + " " + root.control.unit : ""

  function pos(v) {
    var span = root.control ? (root.control.max - root.control.min) : 0
    if (span <= 0) return root.padX
    return root.padX + ((v - root.control.min) / span) * root.span
  }

  function valueAt(x) {
    var fraction = Math.min(1, Math.max(0, (x - root.padX) / root.span))
    return Math.round(root.control.min + fraction * (root.control.max - root.control.min))
  }

  function step(direction, shift) {
    root.committed(Model.stepValue(root.control, root.value, direction, shift))
  }

  Rectangle {
    id: track
    anchors.left: parent.left
    anchors.right: parent.right
    anchors.leftMargin: root.padX
    anchors.rightMargin: root.padX
    anchors.verticalCenter: parent.verticalCenter
    height: root.trackHeight
    radius: height / 2
    color: Util.alpha(root.foreground, 0.2)
  }

  Rectangle {
    anchors.verticalCenter: track.verticalCenter
    x: root.fillFrom
    width: Math.max(2, root.fillWidth)
    height: root.trackHeight
    radius: height / 2
    // Flat, like the red zone: a gradient brightened the stock end on
    // negative values and the knob end on positive ones.
    color: root.accent
  }

  // The aggressive zone: past the risk edge the track turns red, stronger
  // toward the extreme, so the warning is visible before the knob gets there.
  // Drawn over the fill, so the boundary stays visible wherever the knob is.
  readonly property real riskEdge: root.control && root.control.risk && root.control.risk.ref
    ? Math.floor((root.control.risk.ref * (root.control.risk.percent || 5)) / 100) : 0
  readonly property bool riskUp: root.riskEdge > 0 && root.control.risk.direction !== "down"
    && root.control.factory + root.riskEdge < root.control.max
  readonly property bool riskDown: root.riskEdge > 0 && root.control.risk.direction === "both"
    && root.control.factory - root.riskEdge > root.control.min

  // The whole zone is a faint red; only the part the value has entered is
  // solid, so solid red on the track always means "this value is risky".
  readonly property real upEdgePos: root.riskUp ? root.pos(root.control.factory + root.riskEdge) : 0
  readonly property real downEdgePos: root.riskDown ? root.pos(root.control.factory - root.riskEdge) : 0

  Rectangle {
    visible: root.riskUp && !root.unavailable
    anchors.verticalCenter: track.verticalCenter
    x: root.upEdgePos
    width: Math.max(0, root.width - root.padX - x)
    height: root.trackHeight
    radius: height / 2
    color: Util.alpha(root.riskColor, 0.3)
  }

  Rectangle {
    visible: root.riskDown && !root.unavailable
    anchors.verticalCenter: track.verticalCenter
    x: root.padX
    width: Math.max(0, root.downEdgePos - root.padX)
    height: root.trackHeight
    radius: height / 2
    color: Util.alpha(root.riskColor, 0.3)
  }

  Rectangle {
    visible: !root.unavailable && ((root.riskUp && root.knobPos > root.upEdgePos)
      || (root.riskDown && root.knobPos < root.downEdgePos))
    anchors.verticalCenter: track.verticalCenter
    x: root.knobPos > root.upEdgePos && root.riskUp ? root.upEdgePos : root.knobPos
    width: root.knobPos > root.upEdgePos && root.riskUp
      ? root.knobPos - root.upEdgePos : root.downEdgePos - root.knobPos
    height: root.trackHeight
    radius: height / 2
    color: root.riskColor
  }

  // Where "aggressive" starts: a red tick rising only above the track at
  // each zone edge, unlike the factory notch, which runs through it.
  Rectangle {
    // Tall enough to rise above the knob, so the edge stays visible when the
    // knob sits on it.
    visible: root.riskUp && !root.unavailable
    anchors.bottom: track.verticalCenter
    x: root.upEdgePos - 1
    width: 2
    height: root.knobSize / 2 + Style.space(4)
    radius: 1
    color: root.riskColor
  }

  Rectangle {
    visible: root.riskDown && !root.unavailable
    anchors.bottom: track.verticalCenter
    x: root.downEdgePos - 1
    width: 2
    height: root.knobSize / 2 + Style.space(4)
    radius: 1
    color: root.riskColor
  }

  // Factory reference: a notch through the track, taller than the knob, so
  // the fill visibly starts from "stock" and the notch still shows above and
  // below the knob when the value sits at stock.
  Rectangle {
    anchors.verticalCenter: track.verticalCenter
    x: root.factoryPos - width / 2
    width: 2
    height: root.knobSize + Style.space(6)
    radius: 1
    color: Util.alpha(root.foreground, 0.65)
  }

  // Where the machine runs right now. Distinct from the knob, which is the
  // draft: the gap between the two is the unapplied change.
  Rectangle {
    anchors.verticalCenter: track.verticalCenter
    visible: root.appliedPos !== root.knobPos
    x: root.appliedPos - width / 2
    width: 2
    height: Style.space(10)
    radius: 1
    color: Commons.Color.accent
  }

  Rectangle {
    visible: root.focused || root.cursorHere
    width: root.knobSize + 6
    height: width
    radius: width / 2
    color: "transparent"
    border.width: 2
    border.color: root.accent
    anchors.centerIn: knob
  }

  Rectangle {
    id: knob
    width: root.knobSize
    height: root.knobSize
    radius: width / 2
    anchors.verticalCenter: track.verticalCenter
    x: root.knobPos - width / 2
    color: root.unavailable ? Util.alpha(root.foreground, 0.3)
      : root.risky ? root.riskColor
      : root.foreground
    opacity: root.unavailable ? 0.5 : 1
  }

  // Drags write nothing to knob.x: its declarative x binding is the only
  // writer, so keyboard and wheel changes keep moving it after a drag.
  MouseArea {
    id: mouse
    anchors.fill: parent
    enabled: !root.unavailable
    cursorShape: root.unavailable ? Qt.ForbiddenCursor : Qt.PointingHandCursor
    acceptedButtons: Qt.LeftButton
    hoverEnabled: true

    property bool dragging: false
    property bool wasDrag: false

    onPressed: function(m) {
      root.forceActiveFocus()
      dragging = false
      wasDrag = false
    }
    onPositionChanged: function(m) {
      if (!pressed) return
      dragging = true
      root.preview(root.valueAt(m.x))
    }
    onReleased: function(m) {
      if (dragging) {
        wasDrag = true
        root.committed(root.valueAt(m.x))
      }
      dragging = false
    }
    onClicked: function(m) { if (!wasDrag) root.committed(root.valueAt(m.x)) }
    onWheel: function(wheel) {
      // A panel is scrolled often enough that an accidental wheel over a
      // slider must not tune the machine. Only a focused slider takes the
      // wheel, like a keyboard control.
      if (!root.focused) {
        wheel.accepted = false
        return
      }
      root.step(wheel.angleDelta.y > 0 ? 1 : -1, false)
      wheel.accepted = true
    }
  }

  Keys.onPressed: function(event) {
    if (event.key === Qt.Key_Left || event.key === Qt.Key_Down) {
      root.step(-1, (event.modifiers & Qt.ShiftModifier) !== 0)
      event.accepted = true
    } else if (event.key === Qt.Key_Right || event.key === Qt.Key_Up) {
      root.step(1, (event.modifiers & Qt.ShiftModifier) !== 0)
      event.accepted = true
    } else if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter) {
      if (!event.isAutoRepeat) root.enterPressed()
      event.accepted = true
    } else if (event.key === Qt.Key_Escape) {
      root.escapePressed()
      event.accepted = true
    } else if (event.key === Qt.Key_Tab || event.key === Qt.Key_Backtab) {
      root.tabRequested((event.modifiers & Qt.ShiftModifier) !== 0 || event.key === Qt.Key_Backtab ? -1 : 1)
      event.accepted = true
    }
  }
}
