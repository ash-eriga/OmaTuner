pragma Singleton
import QtQuick
QtObject {
  id: root
  property string fontFamily: "monospace"
  property int fontBaseSize: 12
  readonly property QtObject font: QtObject {
    readonly property string family: root.fontFamily
    readonly property int caption: 10
    readonly property int bodySmall: 11
    readonly property int body: 12
    readonly property int subtitle: 13
    readonly property int title: 14
    readonly property int heading: 16
  }
  function space(px) { return Math.max(1, Math.round(px)) }
  function selectionFillFor(foreground, accent) { return Qt.rgba(accent.r, accent.g, accent.b, 0.35) }
}
