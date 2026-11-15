import QtQuick
import qs.Ui
import qs.Commons
import qs.Commons as Commons
import "Model.js" as Model

// One tunable control. Line 1: label on the left, the current value as a
// large number with its unit once, in small muted text. Line 2 appears only
// while the draft differs from the applied value ("was -30 mV ↺") and
// restores that one control. Line 3: the thin slider. The number is the exact
// value editor (Enter commits, Esc cancels); help and the range are
// tooltips, never permanent rows.
Item {
  id: root

  property var control: null
  property var draft: null
  property int applied: 0
  property string helpText: ""
  property bool cursorHere: false
  property bool trialRunning: false
  // The last row gives its empty note line back to the section's padding.
  property bool lastRow: false
  property color rowForeground: Commons.Color.foreground
  // The GPU core row carries the per-point curve glyph. The glyph only
  // makes sense there, so the panel drives these; every other row leaves
  // them false and renders the plain offset control.
  property bool showCurveToggle: false
  property bool curveToggleOn: false
  property bool curveToggleEnabled: false
  property string curveToggleTooltip: ""
  property bool curveMode: false
  property var curveData: null
  property var curveDaemon: null

  signal committed(string key, int value)
  signal sliderFocusChanged(string key, bool focused)
  signal inputFocusChanged(bool focused)
  signal enterRequested()
  signal escapeRequested()
  signal tabRequested(int direction)
  signal curveToggled()
  signal curveToggleFocusChanged(bool focused)
  signal curvePointEdited(int index, int frequency, bool flatten, var anchorList)
  signal curveExactSubmitted(int index, int frequency, var anchorList)
  signal curveAnchorAdded(int index)
  signal curveAnchorRemoved(int index)
  signal curveResetRequested()
  signal curveEditingChanged(bool editing)

  readonly property bool unavailable: !root.control || !root.control.available
  readonly property bool editing: exactInput.visible
  // Read-only probes for the rendered state. They are what the offscreen
  // interaction tests assert on, instead of inspecting the draft alone.
  readonly property alias valueText: valueLabel.text
  readonly property alias valueItem: valueLabel
  readonly property alias exactShown: exactInput.visible
  readonly property alias wasShown: wasRow.visible
  readonly property alias wasText: wasValue.text
  readonly property alias wasItem: wasRow
  readonly property alias knobX: slider.knobPos
  readonly property alias factoryKnobX: slider.factoryPos
  readonly property alias sliderItem: slider
  readonly property alias causeText: unavailableText.text
  // The plot loads only while the curve is open, so the two closed rows keep
  // no hidden editor.
  readonly property var curveViewItem: curveLoader.item
  property int previewValue: 0
  readonly property var liveValue: {
    if (!root.draft || !root.control) return 0
    return slider.dragging ? root.previewValue : root.draft.values[root.control.key]
  }
  readonly property bool differs: !root.unavailable && root.control !== null
    && root.liveValue !== root.applied
  readonly property real liveExcess: root.control ? Model.excess(root.control, root.liveValue) : 0
  readonly property bool risky: root.control ? root.liveExcess > root.control.risk.percent : false
  // The draft curve differs from what the GPU runs: the Curve chip carries
  // the staged mark even while the plot is closed.
  readonly property bool curveStaged: !!(root.showCurveToggle && root.curveData
    && root.curveData.supported && root.draft && root.draft.points
    && !Model.samePoints(root.draft.points, root.curveData.points))
  // The GPU runs a curve of its own: the chip stays lit with the plot closed.
  readonly property bool curveCustom: !!(root.showCurveToggle && Model.curveIsCustom(root.curveData))
  readonly property bool curveRisky: root.curveSummary
    && Model.curveExceeds(root.curvePoints, root.curveData, root.control)
  readonly property real valueSize: Math.max(Style.font.heading, Math.round(Style.font.heading * 1.25))
  // Curve mode is the core row only, and only while the panel says the mode
  // is effective; a transient backend loss falls back to the offset slider.
  readonly property bool curveSummary: root.curveMode && root.control !== null && root.control.key === "core"
  readonly property var curvePoints: root.draft && root.draft.points ? root.draft.points : []
  readonly property int curveAverage: Model.curveAverageDelta(
    root.curvePoints, root.curveData ? root.curveData.factoryPoints : null)
  readonly property real rightReserve: root.curveSummary
    ? curveSummaryArea.implicitWidth : valueArea.implicitWidth

  // The value is the only place the draft state shows: accent while it is
  // unapplied, a muted warning tint inside the aggressive zone.
  readonly property color valueColor: {
    if (root.risky) return Commons.Color.urgent
    if (root.differs) return Commons.Color.accent
    return root.rowForeground
  }

  // While the value is typed, the colour follows what is typed, so a risky
  // number stays red in the editor too.
  readonly property bool editRisky: {
    if (!root.editing || !root.control || !root.control.risk || !exactInput.acceptableInput) return root.risky
    return Model.excess(root.control, parseInt(exactInput.text)) > root.control.risk.percent
  }

  function formatValue(value) {
    // Typographic minus for the read-out; the model keeps ASCII.
    return Model.displayValue(root.control, value).replace("-", "\u2212")
  }

  function focusField() {
    if (root.curveSummary) {
      if (root.curveViewItem) root.curveViewItem.focusPlot()
      return
    }
    slider.forceActiveFocus()
  }

  function beginEdit() {
    if (root.unavailable || root.editing) return
    root.previewValue = root.liveValue
    exactInput.text = String(root.liveValue)
    exactInput.visible = true
    exactInput.forceActiveFocus()
    exactInput.selectAll()
  }

  function commitEdit() {
    if (!root.editing) return
    const entered = Model.parseExact(root.control, exactInput.text)
    exactInput.visible = false
    if (entered !== null) root.committed(root.control.key, entered)
  }

  function cancelEdit() {
    if (!root.editing) return
    exactInput.visible = false
    slider.forceActiveFocus()
  }

  implicitHeight: column.implicitHeight
  opacity: root.unavailable ? 0.85 : 1

  Commons.Translations {
    id: strings
    directory: Qt.resolvedUrl("i18n")
  }

  Column {
    id: column
    width: parent.width
    spacing: Style.space(6)

    Item {
      width: parent.width
      // Only the row dims when the control is unavailable; the cause below
      // stays at full strength so it can be read.
      opacity: root.unavailable ? 0.6 : 1
      implicitHeight: Math.max(labelText.implicitHeight, valueArea.implicitHeight,
        curveSummaryArea.implicitHeight, Style.space(22))

      Text {
        id: labelText
        textFormat: Text.PlainText
        text: root.control ? root.control.label : ""
        color: root.rowForeground
        font.family: Style.font.family
        font.pixelSize: Style.font.subtitle
        elide: Text.ElideRight
        // The glyph follows the painted label, not the reserved width the
        // value on the right leaves over.
        width: Math.min(implicitWidth, Math.max(0, parent.width - root.rightReserve
          - Style.space(12) - helpBadge.width - Style.space(6)
          - (root.showCurveToggle ? curveGlyph.width + Style.space(8) : 0)))
        anchors.verticalCenter: parent.verticalCenter

        HoverHandler { id: labelHover }
      }

      // Help is a visible "?" right after the term, on hover and on focus,
      // so nobody has to discover that the label itself carries a tooltip.
      HintBadge {
        id: helpBadge
        objectName: "help-badge"
        visible: root.helpText !== "" || (root.unavailable && root.control && root.control.cause)
        anchors.left: labelText.right
        anchors.leftMargin: Style.space(6)
        anchors.verticalCenter: parent.verticalCenter
        width: visible ? Style.space(16) : 0
        foreground: root.rowForeground
        ringWidth: root.cursorHere && root.unavailable ? 2 : 1
        ringAccent: root.cursorHere && root.unavailable
        tip: root.unavailable && root.control && root.control.cause
          ? root.control.cause
          : root.helpText
        accessName: root.helpText
      }

      // The curve toggle: the 18 px stroke and the word Curve in one chip.
      // Muted while off, accent while on; muted and inert while the backend
      // cannot offer a curve, with the real cause in its tooltip. A labelled chip around the stroke, so it reads as a switch to the
      // per-point editor and not as a stray mark beside the label.
      Rectangle {
        id: curveGlyph
        visible: root.showCurveToggle
        anchors.left: helpBadge.visible ? helpBadge.right : labelText.right
        anchors.leftMargin: Style.space(8)
        anchors.verticalCenter: parent.verticalCenter
        width: Style.space(18) + curveChipText.implicitWidth + Style.space(14)
          + (stagedDot.visible ? stagedDot.width + Style.space(9) : 0)
        height: Style.space(22)
        radius: Style.cornerRadius > 0 ? Math.min(Style.cornerRadius, height / 2) : Style.space(4)
        color: root.curveToggleOn
          ? Util.alpha(Commons.Color.accent, 0.24)
          : (curveGlyphMouse.containsMouse ? Util.alpha(root.rowForeground, 0.08) : "transparent")
        border.width: 1
        border.color: (root.curveToggleOn || curveGlyph.activeFocus || root.curveStaged || root.curveCustom)
          ? Commons.Color.accent
          : Util.alpha(root.rowForeground, 0.35)
        activeFocusOnTab: root.curveToggleEnabled
        opacity: root.curveToggleEnabled ? 1 : 0.55
        Accessible.role: Accessible.Button
        Accessible.name: strings.t("control.curve")
        Accessible.description: root.curveToggleTooltip

        // Open, focused or edited: accent. A custom curve that already runs
        // keeps only the accent outline; the dot means an unapplied edit.
        property color glyphColor: (root.curveToggleOn || curveGlyph.activeFocus || root.curveStaged)
          ? Commons.Color.accent
          : Util.alpha(root.rowForeground, 0.75)
        onGlyphColorChanged: glyphCanvas.requestPaint()
        onActiveFocusChanged: root.curveToggleFocusChanged(activeFocus)

        Canvas {
          id: glyphCanvas
          anchors.left: parent.left
          anchors.leftMargin: Style.space(5)
          anchors.verticalCenter: parent.verticalCenter
          width: Style.space(18)
          height: Style.space(18)
          scale: 0.8
          onPaint: {
            var ctx = getContext("2d")
            ctx.reset()
            ctx.strokeStyle = curveGlyph.glyphColor
            ctx.lineWidth = 1.5
            ctx.lineCap = "round"
            ctx.lineJoin = "round"
            ctx.beginPath()
            ctx.moveTo(2, 15)
            ctx.bezierCurveTo(7, 15, 9, 12, 11, 8)
            ctx.bezierCurveTo(12.5, 5, 14, 3.5, 16, 3)
            ctx.stroke()
          }
        }

        Text {
          id: curveChipText
          anchors.left: glyphCanvas.right
          anchors.leftMargin: Style.space(4)
          anchors.verticalCenter: parent.verticalCenter
          textFormat: Text.PlainText
          // A staged curve carries the same accent dot as "Not applied".
          text: strings.t("control.curveButton")
          color: curveGlyph.glyphColor
          font.family: Style.font.family
          font.pixelSize: Style.font.bodySmall
          font.bold: root.curveToggleOn
        }

        // The panel's keyboard cursor on the core row while the plot is
        // open: the slider that would carry the ring is hidden.
        Rectangle {
          visible: root.cursorHere && root.curveSummary && !root.trialRunning
          anchors.fill: parent
          anchors.margins: -Style.space(3)
          radius: parent.radius + Style.space(3)
          color: "transparent"
          border.width: 2
          border.color: Commons.Color.accent
        }

        // An unapplied curve edit: the same dot as "Not applied".
        Rectangle {
          id: stagedDot
          visible: root.curveStaged
          anchors.left: curveChipText.right
          anchors.leftMargin: Style.space(6)
          anchors.verticalCenter: parent.verticalCenter
          width: Style.space(6)
          height: width
          radius: width / 2
          color: Commons.Color.accent
        }

        MouseArea {
          id: curveGlyphMouse
          anchors.fill: parent
          hoverEnabled: true
          cursorShape: root.curveToggleEnabled ? Qt.PointingHandCursor : Qt.ArrowCursor
          onClicked: {
            if (!root.curveToggleEnabled) return
            curveGlyph.forceActiveFocus()
            root.curveToggled()
          }
        }

        PanelToolTip {
          visible: curveGlyphMouse.containsMouse && root.curveToggleTooltip !== ""
          delay: 300
          text: root.curveToggleTooltip
        }

        Keys.onPressed: function(event) {
          if (event.isAutoRepeat) {
            event.accepted = true
            return
          }
          if (event.key === Qt.Key_Space || event.key === Qt.Key_Return || event.key === Qt.Key_Enter) {
            if (root.curveToggleEnabled) root.curveToggled()
            event.accepted = true
          } else if (event.key === Qt.Key_Escape) {
            root.escapeRequested()
            event.accepted = true
          }
        }
      }

      // Curve mode readout: the draft curve's mean change, in accent. The
      // global core offset still applies on top, so it is named when it is
      // not zero instead of silently losing the slider that owned it.
      Item {
        id: curveSummaryArea
        visible: root.curveSummary
        anchors.right: parent.right
        anchors.verticalCenter: parent.verticalCenter
        implicitWidth: Math.max(curveTitleRow.implicitWidth, curveCoreLine.implicitWidth)
        implicitHeight: curveTitleRow.implicitHeight
          + (curveCoreLine.visible ? curveCoreLine.implicitHeight + Style.space(1) : 0)

        Item {
          id: curveTitleRow
          anchors.right: parent.right
          implicitWidth: (curveRiskGlyph.visible ? curveRiskGlyph.width + Style.space(5) : 0)
            + curveTitle.implicitWidth + Style.space(4) + curveAverageText.implicitWidth
          implicitHeight: curveTitle.implicitHeight

          // A curve past the limit is named by the warning icon as well as
          // the red, like the aggressive note on the sliders.
          OpticalGlyph {
            id: curveRiskGlyph
            visible: root.curveRisky
            anchors.left: parent.left
            anchors.verticalCenter: curveTitle.verticalCenter
            width: Style.space(14)
            height: Style.space(14)
            text: "\u{F0026}"
            fontSize: Style.font.title
            paintedSize: Style.space(13)
            color: Commons.Color.urgent
          }

          Text {
            id: curveTitle
            anchors.left: curveRiskGlyph.visible ? curveRiskGlyph.right : parent.left
            anchors.leftMargin: curveRiskGlyph.visible ? Style.space(5) : 0
            textFormat: Text.PlainText
            text: Model.signed(root.curveAverage).replace("-", "\u2212")
            color: root.curveRisky ? Commons.Color.urgent
              : root.curveStaged ? Commons.Color.accent
              : root.rowForeground
            font.family: Style.font.family
            font.pixelSize: root.valueSize
            font.weight: Font.Medium
          }

          Text {
            id: curveAverageText
            anchors.left: curveTitle.right
            anchors.leftMargin: Style.space(4)
            anchors.baseline: curveTitle.baseline
            textFormat: Text.PlainText
            text: strings.t("curve.averageUnit")
            color: Util.alpha(root.rowForeground, 0.85)
            font.family: Style.font.family
            font.pixelSize: Style.font.bodySmall
          }
        }

        Text {
          id: curveCoreLine
          visible: root.liveValue !== 0
          anchors.right: parent.right
          anchors.top: curveTitleRow.bottom
          textFormat: Text.PlainText
          text: Model.targ(strings.t("curve.coreOnTop"), root.formatValue(root.liveValue))
          color: Util.alpha(root.rowForeground, 0.7)
          font.family: Style.font.family
          font.pixelSize: Style.font.caption
        }
      }

      Item {
        id: valueArea
        visible: !root.curveSummary
        anchors.right: parent.right
        anchors.verticalCenter: parent.verticalCenter
        implicitWidth: (root.editing ? exactInput.implicitWidth : valueLabel.implicitWidth)
          + unitText.implicitWidth + Style.space(3)
        implicitHeight: Math.max(valueLabel.implicitHeight, exactInput.implicitHeight, unitText.implicitHeight)

        Text {
          id: valueLabel
          visible: !root.editing
          anchors.right: unitText.left
          anchors.rightMargin: Style.space(3)
          anchors.bottom: parent.bottom
          textFormat: Text.PlainText
          text: root.unavailable ? "\u2014" : root.formatValue(root.liveValue)
          color: root.unavailable ? Util.alpha(root.rowForeground, 0.6) : root.valueColor
          font.family: Style.font.family
          font.pixelSize: root.valueSize
          font.weight: Font.Medium

          MouseArea {
            anchors.fill: parent
            enabled: !root.unavailable
            cursorShape: Qt.IBeamCursor
            onClicked: root.beginEdit()
          }
        }

        TextInput {
          id: exactInput
          visible: false
          width: Math.max(Style.space(72), implicitWidth)
          anchors.right: unitText.left
          anchors.rightMargin: Style.space(3)
          anchors.bottom: parent.bottom
          color: root.editRisky ? Commons.Color.urgent : Commons.Color.accent
          font.family: Style.font.family
          font.pixelSize: root.valueSize
          font.weight: Font.Medium
          horizontalAlignment: Text.AlignRight
          selectionColor: Style.selectionFillFor(root.rowForeground, Commons.Color.accent)
          selectedTextColor: Commons.Color.background
          inputMethodHints: Qt.ImhFormattedNumbersOnly
          validator: IntValidator {
            bottom: root.control ? root.control.min : 0
            top: root.control ? root.control.max : 0
          }
          onAccepted: {
            root.commitEdit()
            slider.forceActiveFocus()
          }
          Keys.onEscapePressed: function(event) {
            root.cancelEdit()
            event.accepted = true
          }
          onActiveFocusChanged: {
            root.inputFocusChanged(activeFocus)
            if (!activeFocus && visible) root.commitEdit()
          }
        }

        Rectangle {
          visible: root.editing
          anchors.left: exactInput.left
          anchors.right: exactInput.right
          anchors.bottom: exactInput.bottom
          height: 1
          color: Util.alpha(root.editRisky ? Commons.Color.urgent : Commons.Color.accent, 0.8)
        }

        Text {
          id: unitText
          visible: !root.unavailable
          anchors.right: parent.right
          anchors.baseline: root.editing ? exactInput.baseline : valueLabel.baseline
          textFormat: Text.PlainText
          text: root.control ? root.control.unit : ""
          color: Util.alpha(root.rowForeground, 0.85)
          font.family: Style.font.family
          font.pixelSize: Style.font.bodySmall
        }
      }
    }

    TuneSlider {
      id: slider
      width: parent.width
      visible: !root.unavailable && !root.curveSummary
      control: root.control
      value: root.liveValue
      applied: root.applied
      risky: root.risky
      cursorHere: root.cursorHere && !root.trialRunning
      foreground: root.rowForeground
      onPreview: function(live) { root.previewValue = live }
      onCommitted: function(v) { root.committed(root.control.key, v) }
      onKeyboardFocusChanged: function(focused) { root.sliderFocusChanged(root.control.key, focused) }
      onEnterPressed: {
        if (root.trialRunning) root.enterRequested()
        else root.beginEdit()
      }
      onEscapePressed: root.escapeRequested()
      onTabRequested: function(direction) { root.tabRequested(direction) }
    }

    // One reserved line under the slider: the aggressive note on the left,
    // "was <applied> ↺" on the right. Every slider row keeps the line even
    // when it is empty, so crossing the threshold never shifts the rows and
    // the label stays next to its slider.
    Item {
      width: parent.width
      visible: !root.curveSummary && !root.unavailable
        && (!root.lastRow || root.risky || wasRow.visible)
      implicitHeight: Math.max(riskNote.implicitHeight, wasRow.implicitHeight)

      // Inside the aggressive zone the value, the knob and this note turn
      // red, with how far past stock it goes; red alone reads as an error.
      OpticalGlyph {
        id: riskGlyph
        visible: root.risky
        anchors.left: parent.left
        anchors.verticalCenter: parent.verticalCenter
        width: Style.space(14)
        height: Style.space(14)
        implicitWidth: width
        implicitHeight: height
        text: "\u{F0026}"
        fontSize: Style.font.title
        paintedSize: Style.space(13)
        color: root.valueColor
      }

      Text {
        id: riskNote
        visible: root.risky
        anchors.left: riskGlyph.right
        anchors.leftMargin: Style.space(5)
        anchors.verticalCenter: parent.verticalCenter
        width: Math.min(implicitWidth, parent.width - x - (wasRow.visible ? wasRow.implicitWidth + Style.space(10) : 0))
        elide: Text.ElideRight
        textFormat: Text.PlainText
        text: Model.targ(root.liveValue < (root.control ? root.control.factory : 0)
          ? strings.t("control.aggressiveBelow") : strings.t("control.aggressiveAbove"),
          root.liveExcess.toFixed(1))
        color: root.valueColor
        font.family: Style.font.family
        font.pixelSize: Style.font.body
      }

      Item {
        id: wasRow
        visible: root.differs && !root.editing && !root.curveSummary
        anchors.right: parent.right
        anchors.verticalCenter: parent.verticalCenter
        implicitWidth: wasLabel.implicitWidth + wasValue.implicitWidth + wasArrow.implicitWidth + Style.space(8)
        implicitHeight: Math.max(wasLabel.implicitHeight, wasValue.implicitHeight, wasArrow.implicitHeight)

        Text {
          id: wasLabel
          anchors.left: parent.left
          anchors.verticalCenter: parent.verticalCenter
          textFormat: Text.PlainText
          text: strings.t("control.was")
          color: Util.alpha(root.rowForeground, 0.7)
          font.family: Style.font.family
          font.pixelSize: Style.font.bodySmall
        }
        Text {
          id: wasValue
          anchors.left: wasLabel.right
          anchors.leftMargin: Style.space(4)
          anchors.verticalCenter: parent.verticalCenter
          textFormat: Text.PlainText
          text: root.formatValue(root.applied) + " " + (root.control ? root.control.unit : "")
          color: Commons.Color.accent
          font.family: Style.font.family
          font.pixelSize: Style.font.bodySmall
        }
        OpticalGlyph {
          id: wasArrow
          anchors.left: wasValue.right
          anchors.leftMargin: Style.space(4)
          anchors.verticalCenter: parent.verticalCenter
          width: Style.space(12)
          height: Style.space(12)
          implicitWidth: width
          implicitHeight: height
          text: "\u{F099B}"
          fontSize: Style.font.body
          paintedSize: Style.space(11)
          color: Util.alpha(root.rowForeground, 0.7)
        }

        HoverHandler { id: wasHover }

        MouseArea {
          anchors.fill: parent
          cursorShape: Qt.PointingHandCursor
          onClicked: root.committed(root.control.key, root.applied)
        }
      }
    }

    // The inline plot replaces the core offset slider while the glyph is on.
    Loader {
      id: curveLoader
      width: parent.width
      active: root.curveSummary
      sourceComponent: CurveView {
        width: parent.width
        daemon: root.curveDaemon
        draft: root.draft
        curve: root.curveData
        coreControl: root.control
        rowForeground: root.rowForeground
        onPointEdited: function(index, frequency, flatten, anchorList) {
          root.curvePointEdited(index, frequency, flatten, anchorList)
        }
        onExactSubmitted: function(index, frequency, anchorList) {
          root.curveExactSubmitted(index, frequency, anchorList)
        }
        onAnchorAdded: function(index) { root.curveAnchorAdded(index) }
        onAnchorRemoved: function(index) { root.curveAnchorRemoved(index) }
        onResetRequested: root.curveResetRequested()
        onEditingChangedSignal: function(editing) { root.curveEditingChanged(editing) }
      }
    }

    Text {
      id: unavailableText
      visible: root.unavailable && root.control && root.control.cause
      width: parent.width
      textFormat: Text.PlainText
      text: root.control ? root.control.cause : ""
      color: Util.alpha(root.rowForeground, 0.9)
      font.family: Style.font.family
      font.pixelSize: Style.font.bodySmall
      wrapMode: Text.WordWrap
    }
  }

  PanelToolTip {
    visible: labelHover.hovered && text !== ""
    delay: 500
    text: root.unavailable && root.control && root.control.cause
      ? root.control.cause
      : root.helpText
  }

  PanelToolTip {
    visible: (slider.hovered || slider.dragging) && root.control !== null && !root.unavailable
    delay: 0
    text: root.control
      ? root.control.min + " \u2026 " + root.control.max + " " + root.control.unit
      : ""
  }

  PanelToolTip {
    visible: wasHover.hovered && wasRow.visible
    delay: 500
    text: strings.t("control.restoreHint")
  }
}
