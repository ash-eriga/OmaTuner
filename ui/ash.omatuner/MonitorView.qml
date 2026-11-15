import QtQuick
import qs.Ui
import qs.Commons
import qs.Commons as Commons
import "Model.js" as Model

// Read-only telemetry: CPU, GPU and RAM, each with its icon and device name
// over one row of equal cells (usage, and temperature where the daemon
// reports it), drawn as a label, a value with its unit and a gradient bar.
Column {
  id: root

  property var daemon: null
  property color rowForeground: Commons.Color.foreground
  // The first paint snaps to the current values; later telemetry changes
  // animate the bars.
  property bool animateBars: false
  spacing: Style.space(22)

  readonly property var telemetry: root.daemon ? root.daemon.telemetry : null
  readonly property var thermalCeilings: root.daemon && root.daemon.state
    ? root.daemon.state.thermalCeilings : null
  // The theme has no warning token; urgent is its only warm attention
  // colour. A lighter urgent reads as "approaching" against the full urgent
  // of the 92 % state.
  readonly property color warningColor: Qt.tint(Commons.Color.accent, Util.alpha(Commons.Color.urgent, 0.6))

  Commons.Translations {
    id: strings
    directory: Qt.resolvedUrl("i18n")
  }

  Timer {
    interval: 350
    running: true
    onTriggered: root.animateBars = true
  }

  function reading(key) {
    return Model.monitorValue(root.telemetry, key)
  }

  // -1 is the cell's unavailable state.
  function display(key) {
    var value = root.reading(key)
    return value === null ? -1 : value
  }

  function ceiling(component) {
    return Model.monitorCeiling(root.thermalCeilings, component)
  }

  function temperatureFraction(key, component) {
    var value = root.reading(key)
    return value === null ? 0 : Model.monitorTemperatureFraction(value, root.ceiling(component))
  }

  function temperatureLevel(key, component) {
    var value = root.reading(key)
    return value === null ? 0 : Model.monitorTemperatureLevel(value, root.ceiling(component))
  }

  function temperatureFill(key, component) {
    var value = root.reading(key)
    if (value === null) return Commons.Color.accent
    var level = Model.monitorTemperatureLevel(value, root.ceiling(component))
    if (level >= 2) return Commons.Color.urgent
    if (level === 1) return root.warningColor
    return Commons.Color.accent
  }

  function causeText() {
    if (!(root.daemon && root.daemon.online)) return strings.t("status.offline")
    return strings.t("monitor.missingCause")
  }

  // Offline leaves the last telemetry visible but unexplained otherwise; the
  // tuning tabs put the reason in their footer, which Monitor does not have.
  Item {
    visible: !(root.daemon && root.daemon.online)
    width: root.width
    implicitHeight: offlineText.implicitHeight

    OpticalGlyph {
      id: offlineGlyph
      anchors.left: parent.left
      anchors.top: parent.top
      anchors.topMargin: Math.max(0, (offlineText.font.pixelSize * 1.3 - height) / 2)
      width: Style.space(12)
      height: Style.space(12)
      text: "\u{F0026}"
      fontSize: Style.font.body
      paintedSize: Style.space(11)
      color: Commons.Color.urgent
    }

    // Says what is wrong and how to start the service.
    Text {
      id: offlineText
      anchors.left: offlineGlyph.right
      anchors.leftMargin: Style.space(6)
      anchors.right: parent.right
      textFormat: Text.PlainText
      text: strings.t("status.offline") + ". " + strings.t("panel.offlineHint")
        + (root.daemon && root.daemon.lastError ? "\n" + root.daemon.lastError : "")
      color: Commons.Color.urgent
      font.family: Style.font.family
      font.pixelSize: Style.font.bodySmall
      wrapMode: Text.WordWrap
    }
  }

  function deviceName(raw) {
    return Model.deviceName(raw)
  }

  component BlockTitle: Item {
    id: title

    property string kind: ""
    property string device: ""
    property string icon: ""

    width: root.width
    implicitHeight: Math.max(iconText.implicitHeight, kindText.implicitHeight, deviceText.implicitHeight)

    // Icon, kind, then the model name in a dimmer weight on the same line.
    OpticalGlyph {
      id: iconText
      anchors.left: parent.left
      anchors.verticalCenter: parent.verticalCenter
      width: Style.space(16)
      height: Style.space(16)
      implicitHeight: height
      text: title.icon
      fontSize: Style.font.title
      paintedSize: Style.space(15)
      color: Commons.Color.accent
    }

    Text {
      id: kindText
      anchors.left: iconText.right
      anchors.leftMargin: Style.space(8)
      anchors.verticalCenter: parent.verticalCenter
      textFormat: Text.PlainText
      text: title.kind
      color: root.rowForeground
      font.family: Style.font.family
      font.pixelSize: Style.font.subtitle
      font.bold: true
      font.capitalization: Font.AllUppercase
      font.letterSpacing: 0.5
    }

    Text {
      id: deviceText
      visible: title.device !== ""
      anchors.left: kindText.right
      anchors.leftMargin: Style.space(10)
      anchors.right: parent.right
      anchors.baseline: kindText.baseline
      textFormat: Text.PlainText
      text: root.deviceName(title.device)
      elide: Text.ElideRight
      color: Util.alpha(root.rowForeground, 0.7)
      font.family: Style.font.family
      font.pixelSize: Style.font.bodySmall
    }
  }

  // One metric: label, value with the unit once, then the bar. The bar is a
  // 4 px rounded track of the faint line colour with a horizontal gradient
  // fill; no ticks, outline or glow.
  component MeterCell: Item {
    id: cell

    property string label: ""
    property real value: -1
    property string unit: ""
    property real fraction: 0
    property color fillFrom: Commons.Color.accent
    property color fillTo: Qt.lighter(cell.fillFrom, 1.22)
    property string causeText: ""
    property bool animate: false
    // A hot reading tints its number like its bar, so the warning reads
    // without looking at the bar.
    property bool warm: false

    readonly property bool available: value >= 0

    implicitHeight: column.implicitHeight

    Column {
      id: column
      width: cell.width
      spacing: Style.space(2)

      Text {
        width: parent.width
        textFormat: Text.PlainText
        text: cell.label
        color: Util.alpha(root.rowForeground, 0.7)
        font.family: Style.font.family
        font.pixelSize: Style.font.caption
      }

      Item {
        width: parent.width
        height: numberText.implicitHeight

        // Number and unit form one block, left-aligned with the header.
        Item {
          anchors.left: parent.left
          width: numberText.implicitWidth + (cell.available ? Style.space(3) + unitText.implicitWidth : 0)
          height: parent.height

          Text {
            id: numberText
            anchors.left: parent.left
            anchors.bottom: parent.bottom
            textFormat: Text.PlainText
            text: cell.available
              ? Commons.I18n.formatNumber(Math.round(cell.value))
              : "—"
            color: cell.available && cell.warm ? cell.fillFrom : root.rowForeground
            font.family: Style.font.family
            font.pixelSize: Style.space(22)
            font.weight: Font.Medium
          }

          Text {
            id: unitText
            visible: cell.available
            anchors.left: numberText.right
            anchors.leftMargin: Style.space(3)
            anchors.baseline: numberText.baseline
            textFormat: Text.PlainText
            text: cell.unit
            color: Util.alpha(root.rowForeground, 0.85)
            font.family: Style.font.family
            font.pixelSize: Style.font.bodySmall
          }
        }
      }

      Item {
        width: parent.width
        height: track.height + Style.space(5)

        Rectangle {
          id: track
          anchors.left: parent.left
          anchors.right: parent.right
          anchors.bottom: parent.bottom
          height: Style.space(4)
          radius: height / 2
          color: Util.alpha(root.rowForeground, 0.2)

          Rectangle {
            id: fill
            width: cell.available ? Math.max(0, Math.min(1, cell.fraction)) * track.width : 0
            height: parent.height
            radius: parent.radius
            gradient: Gradient {
              orientation: Gradient.Horizontal
              GradientStop { position: 0; color: cell.fillFrom }
              GradientStop { position: 1; color: cell.fillTo }
            }

            Behavior on width {
              enabled: cell.animate
              NumberAnimation { duration: 250; easing.type: Easing.OutCubic }
            }
          }
        }
      }
    }

    HoverHandler { id: cellHover }

    PanelToolTip {
      visible: cellHover.hovered && !cell.available
      delay: 300
      text: cell.causeText
    }
  }

  // One device block: title, then usage on the left and temperature on the
  // right. RAM reuses only the title and its one cell below.
  component DeviceBlock: Column {
    id: block

    property string kind: ""
    property string icon: ""
    property string deviceKey: ""
    property string usageLabel: ""
    property string temperatureLabel: ""
    property real cellWidth: 0

    width: root.width
    spacing: Style.space(12)

    BlockTitle {
      kind: block.kind
      icon: block.icon
      device: (daemon && daemon.state && daemon.state.devices
        && daemon.state.devices[block.deviceKey]) || ""
    }

    Row {
      width: parent.width
      spacing: Style.space(24)

      MeterCell {
        width: block.cellWidth
        label: block.usageLabel
        value: root.display(block.deviceKey + "Usage")
        unit: "%"
        fraction: Model.monitorPercentFraction(root.reading(block.deviceKey + "Usage"))
        causeText: root.causeText()
        animate: root.animateBars
      }

      MeterCell {
        width: block.cellWidth
        label: block.temperatureLabel
        value: root.display(block.deviceKey + "Temp")
        unit: "°C"
        fraction: root.temperatureFraction(block.deviceKey + "Temp", block.deviceKey)
        fillFrom: root.temperatureFill(block.deviceKey + "Temp", block.deviceKey)
        warm: root.temperatureLevel(block.deviceKey + "Temp", block.deviceKey) >= 1
        causeText: root.causeText()
        animate: root.animateBars
      }
    }
  }

  // ------------------------------------------------------------------ CPU
  DeviceBlock {
    id: cpuCells
    kind: strings.t("monitor.cpu")
    icon: "\u{F4BC}"
    deviceKey: "cpu"
    usageLabel: strings.t("monitor.usage")
    temperatureLabel: strings.t("monitor.temperature")
    cellWidth: (cpuCells.width - Style.space(24)) / 2
  }

  // ------------------------------------------------------------------ GPU
  DeviceBlock {
    id: gpuCells
    kind: strings.t("monitor.gpu")
    icon: "\u{F08AE}"
    deviceKey: "gpu"
    usageLabel: strings.t("monitor.usage")
    temperatureLabel: strings.t("monitor.temperature")
    cellWidth: (gpuCells.width - Style.space(24)) / 2
  }

  // ------------------------------------------------------------------ RAM
  Column {
    width: root.width
    spacing: Style.space(12)

    BlockTitle {
      kind: strings.t("monitor.ram")
      icon: "\u{EFC5}"
    }

    Row {
      id: ramCells
      width: parent.width
      spacing: Style.space(24)

      // RAM has no temperature reading; the right half stays empty.
      MeterCell {
        width: (ramCells.width - Style.space(24)) / 2
        label: strings.t("monitor.usage")
        value: root.display("ramUsage")
        unit: "%"
        fraction: Model.monitorPercentFraction(root.reading("ramUsage"))
        causeText: root.causeText()
        animate: root.animateBars
      }
    }
  }
}
