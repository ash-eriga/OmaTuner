import QtQuick
// Test stand-in for the shell's OpticalGlyph: same properties, plain text.
Item {
  property string text: ""
  property string fontFamily: ""
  property real fontSize: 12
  property real paintedSize: 0
  property color color: "white"
  implicitWidth: glyph.implicitWidth
  implicitHeight: glyph.implicitHeight
  Text { id: glyph; text: parent.text; color: parent.color; font.pixelSize: parent.fontSize }
}
