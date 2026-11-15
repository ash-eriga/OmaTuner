import QtQuick
import qs.Ui
import qs.Commons
import qs.Commons as Commons

// The round "?" that explains the term beside it. Hover or keyboard focus
// tints the ring and opens the tooltip; the owning row places and sizes it.
Rectangle {
  id: badge

  property string tip: ""
  property string accessName: ""
  property color foreground: Commons.Color.foreground
  property int ringWidth: 1
  property bool ringAccent: false

  width: Style.space(16)
  height: Style.space(16)
  radius: height / 2
  color: badgeMouse.containsMouse || activeFocus
    ? Util.alpha(Commons.Color.accent, 0.18) : "transparent"
  border.width: badge.ringWidth
  border.color: badgeMouse.containsMouse || activeFocus || badge.ringAccent
    ? Commons.Color.accent : Util.alpha(badge.foreground, 0.6)
  activeFocusOnTab: true
  Accessible.role: Accessible.Button
  Accessible.name: badge.accessName

  Text {
    anchors.centerIn: parent
    textFormat: Text.PlainText
    text: "?"
    color: badgeMouse.containsMouse || badge.activeFocus
      ? Commons.Color.accent : Util.alpha(badge.foreground, 0.9)
    font.family: Style.font.family
    font.pixelSize: Style.font.bodySmall
    font.bold: true
  }

  MouseArea {
    id: badgeMouse
    anchors.fill: parent
    hoverEnabled: true
    cursorShape: Qt.WhatsThisCursor
  }

  PanelToolTip {
    visible: badge.tip !== "" && (badgeMouse.containsMouse || badge.activeFocus)
    delay: 0
    text: badge.tip
  }
}
