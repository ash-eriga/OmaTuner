pragma Singleton
import QtQuick
QtObject {
  function alpha(c, opacity) {
    if (!c) return Qt.rgba(0, 0, 0, opacity)
    if (typeof c === "string") c = Qt.color(c)
    return Qt.rgba(c.r, c.g, c.b, opacity)
  }
}
