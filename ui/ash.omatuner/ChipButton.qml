import QtQuick
import qs.Ui
import qs.Commons
import qs.Commons as Commons

// Compact text button for the panel's secondary actions (discard, restore
// factory, reset curve, revert). Bordered and quiet. Colours come from the
// theme so it follows Omarchy like the rest of the panel.
Rectangle {
  id: root

  property string text: ""
  property string iconText: ""
  property string tooltipText: ""
  // Body-size text and wider padding, to stand beside the primary button.
  property bool large: false
  property color foreground: Commons.Color.foreground
  readonly property bool hovered: mouse.containsMouse
  readonly property color tone: Commons.Color.accent

  signal clicked()

  implicitWidth: row.implicitWidth + (root.large ? Style.space(28) : Style.space(16))
  implicitHeight: Style.space(24)
  radius: Style.cornerRadius > 0 ? Math.min(Style.cornerRadius, height / 2) : Style.space(4)
  opacity: root.enabled ? 1 : 0.4
  color: {
    if (root.hovered || root.activeFocus) return Util.alpha(root.tone, 0.14)
    return "transparent"
  }
  border.width: 1
  border.color: root.hovered || root.activeFocus
    ? root.tone
    : Util.alpha(root.foreground, 0.3)
  activeFocusOnTab: false
  Accessible.role: Accessible.Button
  Accessible.name: root.text

  Row {
    id: row
    anchors.centerIn: parent
    spacing: Style.space(5)

    OpticalGlyph {
      visible: root.iconText !== ""
      anchors.verticalCenter: parent.verticalCenter
      width: visible ? Style.space(12) : 0
      height: Style.space(12)
      text: root.iconText
      fontSize: Style.font.body
      paintedSize: Style.space(11)
      color: label.color
    }

    Text {
      id: label
      anchors.verticalCenter: parent.verticalCenter
      textFormat: Text.PlainText
      text: root.text
      color: root.hovered ? root.tone : Util.alpha(root.foreground, 0.85)
      font.family: Style.font.family
      font.pixelSize: root.large ? Style.font.body : Style.font.bodySmall
    }
  }

  MouseArea {
    id: mouse
    anchors.fill: parent
    hoverEnabled: true
    enabled: root.enabled
    cursorShape: Qt.PointingHandCursor
    onClicked: root.clicked()
  }

  PanelToolTip {
    visible: mouse.containsMouse && root.tooltipText !== ""
    delay: 400
    text: root.tooltipText
  }
}
