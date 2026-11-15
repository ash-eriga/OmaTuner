import QtQuick
import qs.Commons
import qs.Commons as Commons

// The keyboard focus ring around a control: a transparent rectangle with a
// 2 px accent border drawn just outside it. The owning control sets `shown`
// and, where the shape differs, `radius`.
Rectangle {
  property bool shown: false

  visible: shown
  anchors.fill: parent
  anchors.margins: -Style.space(3)
  radius: Style.cornerRadius > 0 ? Math.min(Style.cornerRadius, height / 2) : Style.space(5)
  color: "transparent"
  border.width: 2
  border.color: Commons.Color.accent
}
