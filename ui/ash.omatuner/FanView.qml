import QtQuick
import qs.Ui
import qs.Commons
import qs.Commons as Commons
import "Model.js" as Model

// The fan section, the last one of the GPU tab. Mode lives in one row of
// plain words (Auto / Fixed / Curve), then only the control that mode needs:
// a hint for Auto, a duty slider for Fixed, the temperature -> speed editor
// for Curve. The panel keeps the footer (Apply / Keep after restart), so this
// view only edits the draft and the Apply sends it with the clocks.
Column {
  id: root

  property var fan: null
  property var draft: null
  property var config: null
  property int liveSpeed: -1
  property int liveTemp: -1
  property color rowForeground: Commons.Color.foreground
  property bool cursorHere: false
  property bool trialRunning: false
  // The fan draft differs from the applied policy (the panel decides).
  property bool staged: false
  property int previewSpeed: -1
  // The section folds to its header in any mode, the curve included; the
  // panel keeps the flag, so the choice survives a reopen.
  property bool collapsed: false

  signal modeRequested(string mode)
  signal speedChanged(int speed)
  signal pointChanged(int index, int temp, int speed)
  signal curveResetRequested()
  signal sliderFocusChanged(bool focused)
  signal inputFocusChanged(bool focused)
  signal curveEditingChanged(bool editing)
  signal pointAdded(int temp, int speed)
  signal pointRemoved(int index)
  signal enterRequested()
  signal escapeRequested()
  signal tabRequested(int direction)
  signal collapseToggled()

  readonly property bool available: !!(root.fan && root.fan.supported)
  readonly property bool writable: !!(root.fan && root.fan.writable)
  // A saved Fixed/Curve config whose daemon loop is gone: the driver owns
  // the fan again, so the mode shown is a remembered setting, not a running
  // policy. The decision lives in Model.js, with the state it reads.
  readonly property bool policyStopped: Model.fanPolicyStopped(root.fan)
  // The stop could not hand the fan back: the driver may not own the duty.
  readonly property bool handBackFailed: Model.fanHandBackFailed(root.fan)
  readonly property int minP: root.fan && root.fan.curve ? root.fan.curve.minP : 0
  readonly property int maxP: root.fan && root.fan.curve ? root.fan.curve.maxP : 100
  readonly property int fixedMinP: root.fan && root.fan.curve ? root.fan.curve.fixedMinP : 0
  // The curve window and the factory reference come from the daemon, so the
  // editor never invents a temperature the daemon would refuse.
  readonly property int minT: root.fan && root.fan.curve && typeof root.fan.curve.minT === "number"
    ? root.fan.curve.minT : 30
  readonly property int maxT: root.fan && root.fan.curve && typeof root.fan.curve.maxT === "number"
    ? root.fan.curve.maxT : 90
  readonly property int maxPoints: root.fan && root.fan.curve && typeof root.fan.curve.maxPoints === "number"
    ? root.fan.curve.maxPoints : 12
  readonly property var factoryPoints: root.fan && root.fan.curve && root.fan.curve.factory
    ? root.fan.curve.factory : []
  readonly property var points: root.draft && root.draft.points ? root.draft.points : []
  readonly property int draftSpeed: root.draft ? root.draft.speed : 0
  readonly property int appliedSpeed: root.config && root.config.mode === "fixed" ? root.config.speed : -1
  readonly property int shownSpeed: root.liveSpeed >= 0
    ? root.liveSpeed : (root.fan && root.fan.speed !== undefined ? root.fan.speed : -1)
  readonly property int shownTemp: root.liveTemp >= 0
    ? root.liveTemp : (root.fan && root.fan.temperatureC !== undefined ? root.fan.temperatureC : -1)
  readonly property int curveNow: root.points.length ? Model.fanDutyAt(root.points, root.shownTemp) : 0
  readonly property var fixedControl: ({
    key: "fan-speed",
    label: strings.t("fan.speed"),
    unit: "%",
    min: root.fixedMinP,
    max: root.maxP,
    step: 1,
    factory: root.fixedMinP,
    available: root.writable,
    cause: root.fan ? root.fan.cause : null,
    risk: { percent: 0, ref: 0, direction: "up" }
  })

  spacing: Style.space(16)

  Commons.Translations {
    id: strings
    directory: Qt.resolvedUrl("i18n")
  }

  function focusPrimary() {
    if (!root.available) return
    // Enter on a folded section opens it first, then lands on its control.
    if (root.collapsed) {
      root.collapseToggled()
      Qt.callLater(root.focusPrimary)
      return
    }
    if (root.draft && root.draft.mode === "curve") {
      curve.focusPlot()
    } else if (root.draft && root.draft.mode === "fixed") {
      speedSlider.forceActiveFocus()
    } else {
      focusMode(indexOfMode(root.draft ? root.draft.mode : "auto"))
    }
  }

  function indexOfMode(mode) {
    return ["auto", "fixed", "curve"].indexOf(mode)
  }

  function focusMode(index) {
    var item = modeRepeater.itemAt(Math.max(0, Math.min(2, index)))
    if (item) item.forceActiveFocus()
  }

  // Literal keys, so the catalog check can see every string the panel asks
  // for; a computed "fan.mode." + mode is invisible to it.
  function modeLabel(mode) {
    if (mode === "auto") return strings.t("fan.mode.auto")
    if (mode === "fixed") return strings.t("fan.mode.fixed")
    return strings.t("fan.mode.curve")
  }

  // ------------------------------------------------------------ section line

  // The fan is its own section of the GPU tab: a rule and extra air above
  // it, an icon and a plain sentence, like the clocks section.
  // The rule sits a little lower than the row gap, so the fan reads as
  // its own section and not as one more row of the clocks.
  Item {
    width: parent.width
    implicitHeight: Style.space(9)

    Rectangle {
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.bottom: parent.bottom
      height: 1
      color: Util.alpha(root.rowForeground, 0.12)
    }
  }

  Column {
    width: parent.width
    spacing: Style.space(6)

    Item {
      width: parent.width
      implicitHeight: Math.max(sectionRow.implicitHeight, reading.implicitHeight)

      Row {
        id: sectionRow
        anchors.left: parent.left
        anchors.verticalCenter: parent.verticalCenter
        spacing: Style.space(8)

        OpticalGlyph {
          anchors.verticalCenter: parent.verticalCenter
          width: Style.space(16)
          height: Style.space(16)
          text: "\u{F0210}"
          fontSize: Style.font.title
          paintedSize: Style.space(15)
          color: Commons.Color.accent
        }

        Text {
          id: sectionTitle
          anchors.verticalCenter: parent.verticalCenter
          textFormat: Text.PlainText
          text: strings.t("section.fan")
          color: root.rowForeground
          font.family: Style.font.family
          font.pixelSize: Style.font.subtitle
          font.bold: true
          font.capitalization: Font.AllUppercase
          font.letterSpacing: 0.5
        }

        // Down while open, right while folded, like a disclosure row.
        OpticalGlyph {
          anchors.verticalCenter: parent.verticalCenter
          width: Style.space(14)
          height: Style.space(14)
          text: root.collapsed ? "\u{F0142}" : "\u{F0140}"
          fontSize: Style.font.body
          paintedSize: Style.space(13)
          color: Util.alpha(root.rowForeground, collapseMouse.containsMouse || collapseFocus.activeFocus ? 1 : 0.7)
        }

        // The folded section still names its mode, so a glance tells
        // whether the fan runs a curve.
        Text {
          visible: root.collapsed && !!root.draft
          anchors.baseline: sectionTitle.baseline
          textFormat: Text.PlainText
          text: root.draft ? root.modeLabel(root.draft.mode) : ""
          color: Util.alpha(root.rowForeground, 0.75)
          font.family: Style.font.family
          font.pixelSize: Style.font.bodySmall
        }

        Row {
          visible: root.staged
          anchors.verticalCenter: parent.verticalCenter
          spacing: Style.space(5)

          Rectangle {
            anchors.verticalCenter: parent.verticalCenter
            width: Style.space(6)
            height: width
            radius: width / 2
            color: Commons.Color.accent
          }

          Text {
            anchors.verticalCenter: parent.verticalCenter
            textFormat: Text.PlainText
            text: strings.t("fan.edited")
            color: Commons.Color.accent
            font.family: Style.font.family
            font.pixelSize: Style.font.bodySmall
          }
        }
      }

      // The whole title row folds and opens the section.
      MouseArea {
        id: collapseMouse
        x: sectionRow.x
        y: sectionRow.y
        width: sectionRow.width
        height: sectionRow.height
        hoverEnabled: true
        cursorShape: Qt.PointingHandCursor
        onClicked: root.collapseToggled()
      }

      Item {
        id: collapseFocus
        x: sectionRow.x - Style.space(4)
        y: sectionRow.y - Style.space(2)
        width: sectionRow.width + Style.space(8)
        height: sectionRow.height + Style.space(4)
        activeFocusOnTab: true
        Accessible.role: Accessible.Button
        Accessible.name: root.collapsed ? strings.t("fan.expand") : strings.t("fan.collapse")
        Keys.onPressed: function(event) {
          if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter || event.key === Qt.Key_Space) {
            if (!event.isAutoRepeat) root.collapseToggled()
            event.accepted = true
          } else if (event.key === Qt.Key_Escape) {
            root.escapeRequested()
            event.accepted = true
          }
        }

        // Keyboard focus, and the panel cursor while the section is folded
        // (the mode track that normally carries it is hidden).
        Rectangle {
          anchors.fill: parent
          visible: collapseFocus.activeFocus || (root.collapsed && root.cursorHere && !root.trialRunning)
          radius: Style.space(4)
          color: "transparent"
          border.width: collapseFocus.activeFocus ? 1 : 2
          border.color: root.rowForeground
        }
      }

      PanelToolTip {
        visible: collapseMouse.containsMouse
        delay: 300
        text: root.collapsed ? strings.t("fan.expand") : strings.t("fan.collapse")
      }

      // What the fan does right now, whatever the draft says.
      Text {
        id: reading
        objectName: "fan-reading"
        anchors.right: parent.right
        y: sectionRow.y + sectionTitle.y + sectionTitle.baselineOffset - baselineOffset
        width: Math.min(implicitWidth, parent.width - sectionRow.width - Style.space(10))
        horizontalAlignment: Text.AlignRight
        elide: Text.ElideLeft
        // A measurement, in the body colour; the device names stay muted.
        textFormat: Text.PlainText
        text: {
          if (!root.available) return strings.t("fan.unavailable")
          var parts = []
          if (root.shownSpeed >= 0) parts.push(root.shownSpeed + "%")
          if (root.fan && root.fan.rpm) parts.push(root.fan.rpm + " RPM")
          if (root.shownTemp >= 0) parts.push(root.shownTemp + " °C")
          return parts.join(" · ")
        }
        color: root.fan && root.fan.override
          ? Commons.Color.urgent
          : root.rowForeground
        font.family: Style.font.family
        font.pixelSize: Style.font.bodySmall

        HoverHandler { id: readingHover }
        PanelToolTip {
          visible: readingHover.hovered && root.available
          delay: 300
          text: strings.t("fan.readingHint")
        }
      }
    }

    Text {
      width: parent.width
      visible: !root.collapsed
      textFormat: Text.PlainText
      text: strings.t("section.fanHint")
      color: Util.alpha(root.rowForeground, 0.8)
      font.family: Style.font.family
      font.pixelSize: Style.font.bodySmall
      wrapMode: Text.WordWrap
    }
  }

  // ------------------------------------------------------------------- modes

  // A segmented control, not a second row of tabs: it is a setting.
  Rectangle {
    visible: !root.collapsed
    width: parent.width
    implicitHeight: Style.space(30)
    radius: Style.cornerRadius > 0 ? Math.min(Style.cornerRadius, height / 2) : Style.space(5)
    color: Util.alpha(root.rowForeground, 0.05)
    border.width: 1
    // The panel's keyboard cursor lands on the fan as one row; this is
    // where it shows.
    border.color: Util.alpha(root.rowForeground, 0.18)

    // The panel's keyboard cursor: a 2 px ring on the track's own edge (the
    // body clips anything outside it), apart from the tinted segment.
    Rectangle {
      visible: root.cursorHere && !root.trialRunning
      anchors.fill: parent
      radius: parent.radius
      color: "transparent"
      border.width: 2
      // Foreground, so focus never reads as a second selected segment.
      border.color: root.rowForeground
    }

    Row {
      id: modeRow
      anchors.fill: parent
      anchors.margins: Style.space(3)
      spacing: Style.space(3)

      Repeater {
        id: modeRepeater
        model: ["auto", "fixed", "curve"]

        Rectangle {
          id: modeItem
          objectName: "mode-" + modelData
          readonly property bool current: root.draft && root.draft.mode === modelData
          readonly property bool modeEnabled: modelData === "auto" || root.writable

          width: (modeRow.width - modeRow.spacing * 2) / 3
          height: modeRow.height
          radius: Math.max(0, parent.parent.radius - Style.space(2))
          color: modeItem.current
            ? Util.alpha(Commons.Color.accent, 0.22)
            : (modeMouse.containsMouse && modeItem.modeEnabled ? Util.alpha(root.rowForeground, 0.08) : "transparent")
          border.width: modeItem.activeFocus || modeItem.current ? 1 : 0
          // Focus in the foreground colour; accent marks only the selected mode.
          border.color: modeItem.activeFocus ? root.rowForeground : Util.alpha(Commons.Color.accent, 0.5)
          opacity: modeItem.modeEnabled ? 1 : 0.45
          activeFocusOnTab: modeItem.modeEnabled
          Accessible.role: Accessible.Button
          Accessible.name: modeLabel.text

          Text {
            id: modeLabel
            anchors.centerIn: parent
            textFormat: Text.PlainText
            text: root.modeLabel(modelData)
            color: modeItem.current
              ? root.rowForeground
              : Util.alpha(root.rowForeground, 0.8)
            font.family: Style.font.family
            font.pixelSize: Style.font.body
            font.bold: modeItem.current
          }

          // Hover stays on for a disabled mode so its cause can show; only
          // the click is guarded.
          MouseArea {
            id: modeMouse
            anchors.fill: parent
            hoverEnabled: true
            cursorShape: modeItem.modeEnabled ? Qt.PointingHandCursor : Qt.ForbiddenCursor
            onClicked: if (modeItem.modeEnabled) root.modeRequested(modelData)
          }

          PanelToolTip {
            visible: modeMouse.containsMouse && !modeItem.modeEnabled && root.fan && !!root.fan.cause
            delay: 300
            text: root.fan && root.fan.cause ? root.fan.cause : ""
          }

          onActiveFocusChanged: root.inputFocusChanged(activeFocus)

          Keys.onPressed: function(event) {
            if (event.key === Qt.Key_Left || event.key === Qt.Key_Up) {
              root.focusMode(index - 1)
              event.accepted = true
            } else if (event.key === Qt.Key_Right || event.key === Qt.Key_Down) {
              root.focusMode(index + 1)
              event.accepted = true
            } else if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter
                       || event.key === Qt.Key_Space) {
              if (!event.isAutoRepeat && modeItem.modeEnabled) root.modeRequested(modelData)
              event.accepted = true
            } else if (event.key === Qt.Key_Escape) {
              root.escapeRequested()
              event.accepted = true
            } else if (event.key === Qt.Key_Tab || event.key === Qt.Key_Backtab) {
              root.tabRequested((event.modifiers & Qt.ShiftModifier) !== 0
                || event.key === Qt.Key_Backtab ? -1 : 1)
              event.accepted = true
            }
          }
        }
      }
    }
  }

  // Why Fixed and Curve stay out of reach: always on, not only on hover.
  Text {
    objectName: "fan-read-only-cause"
    width: parent.width
    visible: root.fan && root.fan.supported && !root.writable && !!root.fan.cause
    textFormat: Text.PlainText
    text: root.fan && root.fan.cause ? root.fan.cause : ""
    color: Util.alpha(root.rowForeground, 0.75)
    font.family: Style.font.family
    font.pixelSize: Style.font.caption
    wrapMode: Text.WordWrap
  }

  // ---------------------------------------------------------------- stopped

  // A red line carries the warning glyph, like every other red line here.
  Item {
    visible: root.policyStopped
    width: parent.width
    implicitHeight: Math.max(stoppedGlyph.height, stoppedText.implicitHeight)

    OpticalGlyph {
      id: stoppedGlyph
      anchors.left: parent.left
      y: Math.max(0, (stoppedText.font.pixelSize * 1.3 - height) / 2)
      width: Style.space(14)
      height: Style.space(14)
      text: "\u{F0026}"
      fontSize: Style.font.title
      paintedSize: Style.space(13)
      color: stoppedText.color
    }

    Text {
      id: stoppedText
      objectName: "fan-policy-stopped"
      anchors.left: stoppedGlyph.right
      anchors.leftMargin: Style.space(5)
      anchors.right: parent.right
      textFormat: Text.PlainText
      text: root.handBackFailed
        ? strings.t("fan.policyStoppedHandBack")
        : strings.t("fan.policyStopped")
      color: Util.alpha(Commons.Color.urgent, 0.9)
      font.family: Style.font.family
      font.pixelSize: Style.font.bodySmall
      wrapMode: Text.WordWrap
    }
  }

  // ----------------------------------------------------------------- auto

  Text {
    width: parent.width
    visible: !root.collapsed && root.draft && root.draft.mode === "auto"
    textFormat: Text.PlainText
    text: strings.t("fan.autoHint")
    color: Util.alpha(root.rowForeground, 0.8)
    font.family: Style.font.family
    font.pixelSize: Style.font.bodySmall
    wrapMode: Text.WordWrap
  }

  // ---------------------------------------------------------------- fixed

  Item {
    width: parent.width
    visible: !root.collapsed && root.draft && root.draft.mode === "fixed"
    implicitHeight: fixedColumn.implicitHeight

    Column {
      id: fixedColumn
      width: parent.width
      spacing: Style.space(6)

      Item {
        width: parent.width
        implicitHeight: Math.max(speedLabel.implicitHeight, speedValue.implicitHeight)

        Text {
          id: speedLabel
          anchors.left: parent.left
          anchors.verticalCenter: parent.verticalCenter
          textFormat: Text.PlainText
          text: strings.t("fan.speed")
          color: root.rowForeground
          font.family: Style.font.family
          font.pixelSize: Style.font.subtitle
        }

        Text {
          id: speedValue
          objectName: "fan-speed-value"
          anchors.right: parent.right
          anchors.verticalCenter: parent.verticalCenter
          textFormat: Text.PlainText
          text: root.draftSpeed + " %"
          color: root.draftSpeed !== root.appliedSpeed ? Commons.Color.accent : root.rowForeground
          font.family: Style.font.family
          font.pixelSize: Math.max(Style.font.heading, Math.round(Style.font.heading * 1.25))
          font.weight: Font.Medium
        }
      }

      TuneSlider {
        id: speedSlider
        objectName: "fan-speed-slider"
        width: parent.width
        control: root.fixedControl
        value: root.previewSpeed >= 0 ? root.previewSpeed : root.draftSpeed
        applied: root.appliedSpeed >= 0 ? root.appliedSpeed : root.draftSpeed
        unavailable: !root.writable
        cursorHere: root.cursorHere && !root.trialRunning
        foreground: root.rowForeground
        onPreview: function(value) { root.previewSpeed = value }
        onCommitted: function(value) {
          root.previewSpeed = -1
          root.speedChanged(value)
        }
        onKeyboardFocusChanged: function(focused) {
          if (!focused) root.previewSpeed = -1
          root.sliderFocusChanged(focused)
        }
        onEnterPressed: root.enterRequested()
        onEscapePressed: root.escapeRequested()
        onTabRequested: function(direction) { root.tabRequested(direction) }
      }

      Text {
        width: parent.width
        textFormat: Text.PlainText
        text: strings.t("fan.fixedHint")
        color: Util.alpha(root.rowForeground, 0.75)
        font.family: Style.font.family
        font.pixelSize: Style.font.caption
        wrapMode: Text.WordWrap
      }
    }
  }

  // ---------------------------------------------------------------- curve

  Column {
    width: parent.width
    visible: !root.collapsed && root.draft && root.draft.mode === "curve"
    spacing: Style.space(6)

    FanCurve {
      id: curve
      objectName: "fan-curve"
      width: parent.width
      points: root.points
      factory: root.factoryPoints
      minT: root.minT
      maxT: root.maxT
      minP: root.minP
      maxP: root.maxP
      maxPoints: root.maxPoints
      currentTemp: root.shownTemp
      writable: root.writable
      foreground: root.rowForeground
      onPointEdited: function(index, temp, speed) { root.pointChanged(index, temp, speed) }
      onPointAdded: function(temp, speed) { root.pointAdded(temp, speed) }
      onPointRemoved: function(index) { root.pointRemoved(index) }
      onEditorFocusChanged: function(focused) {
        root.inputFocusChanged(focused)
        root.curveEditingChanged(focused)
      }
      onEnterRequested: root.enterRequested()
      onEscapeRequested: root.escapeRequested()
      onTabRequested: function(direction) { root.tabRequested(direction) }
    }

    // The handle being edited, on the right under the plot like the V/F
    // readout: "Selected 40 °C →" muted, the duty as the number.
    Item {
      width: parent.width
      visible: curve.pointCount > 0 && curve.selected >= 0
      implicitHeight: Math.max(selectedLabel.implicitHeight, selectedValue.implicitHeight)

      Text {
        id: selectedLabel
        anchors.right: selectedValue.left
        anchors.rightMargin: Style.space(6)
        anchors.baseline: selectedValue.baseline
        textFormat: Text.PlainText
        text: parent.visible
          ? Model.targ(strings.t("fan.selectedPoint"), curve.pointAt(curve.selected).t + " °C →") : ""
        color: Util.alpha(root.rowForeground, 0.75)
        font.family: Style.font.family
        font.pixelSize: Style.font.bodySmall
      }

      Text {
        id: selectedValue
        anchors.right: selectedUnit.left
        anchors.rightMargin: Style.space(1)
        anchors.bottom: parent.bottom
        textFormat: Text.PlainText
        text: parent.visible ? String(curve.pointAt(curve.selected).p) : ""
        // Accent while the point is an unapplied edit, as on the sliders.
        color: {
          var point = curve.pointAt(curve.selected)
          var applied = root.config && root.config.mode === "curve" && root.config.points
            ? root.config.points[curve.selected] : null
          return applied && applied.t === point.t && applied.p === point.p
            ? root.rowForeground : Commons.Color.accent
        }
        font.family: Style.font.family
        font.pixelSize: Style.font.title
        font.weight: Font.Medium
      }

      Text {
        id: selectedUnit
        anchors.right: parent.right
        anchors.baseline: selectedValue.baseline
        textFormat: Text.PlainText
        text: "%"
        color: Util.alpha(root.rowForeground, 0.85)
        font.family: Style.font.family
        font.pixelSize: Style.font.bodySmall
      }
    }

    // What the draft curve asks for at the current temperature (the header
    // has the measured speed), its "?" with the how-to, and the reset button.
    Item {
      width: parent.width
      implicitHeight: Math.max(readoutColumn.implicitHeight, resetLabel.implicitHeight)

      Column {
        id: readoutColumn
        anchors.left: parent.left
        anchors.right: resetLabel.left
        anchors.rightMargin: hintBadge.width + Style.space(16)
        anchors.verticalCenter: parent.verticalCenter

        Text {
          id: curveReadout
          width: Math.min(implicitWidth, parent.width)
          textFormat: Text.PlainText
          text: {
            var text = strings.t("fan.curveTarget")
            text = text.replace("%1", root.curveNow)
            text = text.replace("%2", root.shownTemp >= 0 ? root.shownTemp : "—")
            return text
          }
          elide: Text.ElideRight
          color: root.rowForeground
          font.family: Style.font.family
          font.pixelSize: Style.font.bodySmall
          font.weight: Font.Medium
        }
      }

      ChipButton {
        id: resetLabel
        objectName: "fan-curve-reset"
        anchors.right: parent.right
        anchors.verticalCenter: parent.verticalCenter
        iconText: "\u{F099B}"
        text: strings.t("curve.reset")
        // Nothing to reset while the draft is the stock curve.
        enabled: !Model.sameFanPoints(root.points, root.factoryPoints)
        onClicked: root.curveResetRequested()
      }

      // The "?" sits right of the readout it explains, apart from Reset.
      HintBadge {
        id: hintBadge
        x: readoutColumn.x + curveReadout.width + Style.space(6)
        y: readoutColumn.y + curveReadout.y + (curveReadout.height - height) / 2
        foreground: root.rowForeground
        accessName: strings.t("fan.curveHint")
        tip: strings.t("fan.curveTargetHint") + "\n\n" + strings.t("fan.curveHint")
      }
    }

    Item {
      width: parent.width
      visible: !!(root.fan && root.fan.override)
      implicitHeight: Math.max(overrideGlyph.height, overrideText.implicitHeight)

      OpticalGlyph {
        id: overrideGlyph
        anchors.left: parent.left
        y: Math.max(0, (overrideText.font.pixelSize * 1.3 - height) / 2)
        width: Style.space(14)
        height: Style.space(14)
        text: "\u{F0026}"
        fontSize: Style.font.title
        paintedSize: Style.space(13)
        color: overrideText.color
      }

      Text {
        id: overrideText
        anchors.left: overrideGlyph.right
        anchors.leftMargin: Style.space(5)
        anchors.right: parent.right
        textFormat: Text.PlainText
        text: strings.t("fan.override")
        color: Commons.Color.urgent
        font.family: Style.font.family
        font.pixelSize: Style.font.bodySmall
        wrapMode: Text.WordWrap
      }
    }
  }
}
