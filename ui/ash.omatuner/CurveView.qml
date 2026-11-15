import QtQuick
import qs.Ui
import qs.Commons
import qs.Commons as Commons
import "Model.js" as Model
import "PlotPaint.js" as Paint

// Inline per-point V/F editor, shown under the GPU core row while the curve
// glyph is on. The convention is voltage across and frequency up: X is
// voltage (V), Y is core frequency (MHz). The driver keeps the voltage of
// every point fixed, so editing moves a point vertically.
//
// The driver table is dense (about 80 points on a GTX 1060), so the plot
// draws the curve and only a handful of anchors. Pressing anywhere grabs the
// anchor nearest in X and dragging sets its frequency from the pointer's Y,
// bending the real points around it with a raised-cosine falloff. Factory is
// a dim dashed line, the draft is accent, and a red dashed line marks the
// aggressive limit; a key names both dashed lines. No frame: the grid is the
// only chrome.
Item {
  id: root

  property var daemon: null
  property var draft: null
  property var curve: null // state.curves.gpu
  property var coreControl: null
  property color rowForeground: Commons.Color.foreground
  property int selected: -1
  property int hoverAnchor: -1
  property bool exactVisible: false
  // Table index of a handle this view asked to pin, applied once the draft
  // comes back through the bindings.
  property int pendingAnchor: -1
  property bool shiftHeld: false
  // Incremented by the canvas paint. The interaction tests use it to prove a
  // drag reached the rendered curve, not only the draft array.
  property int paintCount: 0

  readonly property bool readOnly: !!(curve && curve.writable === false)
  readonly property var draftPoints: draft && draft.points ? draft.points : []
  readonly property var factoryPoints: curve && curve.factoryPoints ? curve.factoryPoints : []
  // Handles the user added on top of the automatic spread. They are table
  // indexes, kept with the draft: the plot can be resized without losing
  // them, and the daemon still receives every point.
  readonly property var pins: draft && draft.anchors ? draft.anchors : []
  // The domain actually worth drawing: the factory range plus the largest
  // offset the top anchor may take, not the descriptor's driver-wide window.
  readonly property var domain: Model.curveDomain(root.curve, root.coreControl)
  // Five to seven UI-only anchors over the useful (non-plateau) part of the
  // curve. The selection follows the factory table, so anchors do not jump
  // while a drag moves the draft plateau underneath them. They are never
  // persisted: the daemon always receives the full per-point list.
  readonly property var autoAnchors: Model.anchorIndices(
    root.factoryPoints.length ? root.factoryPoints : root.draftPoints,
    Model.anchorCount(plot.width),
    {
      width: Math.max(1, plot.width - root.padLeft - root.padRight),
      height: Math.max(1, root.plotHeight - root.padTop - root.padBottom),
      xMin: root.domain.voltLo,
      xMax: root.domain.voltHi,
      yMin: root.domain.minF,
      yMax: root.domain.maxF,
      minGap: 36
    })
  readonly property var anchorPoints: Model.curveAnchors(
    root.draftPoints.length ? root.draftPoints : root.factoryPoints, root.autoAnchors, root.pins)
  readonly property var selectedPoint: root.selected >= 0
    && root.selected < root.draftPoints.length ? root.draftPoints[root.selected] : null
  // The plot or the exact-value field owns the keys; the panel's global
  // catcher steps back while either does.
  readonly property bool editing: plot.activeFocus || root.exactVisible

  signal pointEdited(int index, int frequency, bool flatten, var anchorList)
  signal exactSubmitted(int index, int frequency, var anchorList)
  signal anchorAdded(int index)
  signal anchorRemoved(int index)
  signal resetRequested()
  signal editingChangedSignal(bool editing)

  onEditingChanged: root.editingChangedSignal(root.editing)
  onDraftChanged: {
    root.clampSelection()
    root.repaint()
  }
  onCurveChanged: root.repaint()
  onSelectedChanged: root.repaint()
  onHoverAnchorChanged: root.repaint()
  onAnchorPointsChanged: {
    // A handle this view just asked for wins over whatever the press that
    // started the double-click selected: the bindings refresh after the whole
    // event, so without this the new pin would open unselected.
    if (root.pendingAnchor >= 0 && root.anchorPoints.indexOf(root.pendingAnchor) >= 0) {
      root.selected = root.pendingAnchor
      root.pendingAnchor = -1
    }
    // A reflow changes the anchor list (five, six or seven). Keep the
    // selection at the same part of the voltage range instead of dropping it.
    if (root.selected >= 0 && root.anchorPoints.indexOf(root.selected) < 0) {
      root.selected = Model.anchorNear(root.anchorPoints, root.selected)
    }
    root.repaint()
  }

  implicitHeight: column.implicitHeight

  Commons.Translations {
    id: strings
    directory: Qt.resolvedUrl("i18n")
  }

  readonly property real plotHeight: Style.space(190)
  // Enough gutter for the top frequency label ("2198 MHz") plus the 4 px gap
  // to the plot; the labels are right aligned there and never clipped.
  readonly property real padLeft: Style.space(64)
  readonly property real padRight: Style.space(6)
  readonly property real padTop: Style.space(10)
  readonly property real padBottom: Style.space(22)

  readonly property real voltLo: root.domain.voltLo
  readonly property real voltHi: root.domain.voltHi

  // The drawn geometry and the current domain, in one object for the pure
  // screen-mapping helpers in Model.js.
  function plotBox() {
    return {
      width: canvas.width,
      height: canvas.height,
      padLeft: root.padLeft,
      padRight: root.padRight,
      padTop: root.padTop,
      padBottom: root.padBottom,
      voltLo: root.voltLo,
      voltHi: root.voltHi,
      minF: root.domain.minF,
      maxF: root.domain.maxF
    }
  }

  // A draft point above the aggressive limit: its handle and readout turn red.
  function pointRisky(index) {
    var bounds = Model.curveRiskBounds(root.curve, root.coreControl)
    if (!bounds || index < 0 || index >= root.draftPoints.length || index >= bounds.edges.length) return false
    return root.draftPoints[index].f > bounds.edges[index].f + 1e-6
  }

  // The exact-value field follows what is typed: red past the limit.
  readonly property bool exactRisky: {
    if (!root.exactVisible || !exactField.acceptableInput) return root.pointRisky(root.selected)
    var bounds = Model.curveRiskBounds(root.curve, root.coreControl)
    if (!bounds || root.selected < 0 || root.selected >= bounds.edges.length) return false
    return parseInt(exactField.text) > bounds.edges[root.selected].f + 1e-6
  }

  function pointStaged(index) {
    var applied = root.curve && root.curve.points ? root.curve.points : null
    if (!applied || index < 0 || index >= applied.length || index >= root.draftPoints.length) return false
    return Math.abs(root.draftPoints[index].f - applied[index].f) > 0.5
  }

  function repaint() {
    canvas.requestPaint()
  }

  function focusPlot() {
    if (root.readOnly) return
    plot.forceActiveFocus()
  }

  function clampSelection() {
    if (root.selected >= root.draftPoints.length) root.selected = -1
  }

  function voltToX(v) {
    return Model.curveVoltageToX(v, root.plotBox())
  }

  function freqToY(f) {
    return Model.curveFrequencyToY(f, root.plotBox())
  }

  // A press or drag cannot address a voltage or frequency outside the drawn
  // plot; the model then clamps the edit again to the daemon's own limits.
  function xToVolt(x) {
    return Model.curveXToVoltage(x, root.plotBox())
  }

  function yToFreq(y) {
    return Model.curveYToFrequency(y, root.plotBox())
  }

  // Screen positions of the visible handles, from the same draft points the
  // canvas paints. A test can drag one and see whether the curve followed.
  function handlePositions() {
    var out = []
    for (var i = 0; i < root.anchorPoints.length; i++) {
      var point = root.draftPoints[root.anchorPoints[i]]
      if (!point) continue
      out.push({
        index: root.anchorPoints[i],
        x: root.voltToX(point.v),
        y: root.freqToY(point.f)
      })
    }
    return out
  }

  // The whole plot is a hit target: a press grabs the anchor nearest in X
  // (voltage), not the one nearest the pointer in two dimensions.
  function anchorAt(x) {
    return Model.nearestAnchor(root.anchorPoints, root.draftPoints, root.xToVolt(x))
  }

  function selectDefault() {
    if (!root.anchorPoints.length) return
    var live = root.daemon && root.daemon.telemetry ? root.daemon.telemetry.gpuVoltage : undefined
    if (live === undefined || live === null) {
      root.selected = root.anchorPoints[Math.floor(root.anchorPoints.length / 2)]
      return
    }
    root.selected = Model.nearestAnchor(root.anchorPoints, root.draftPoints, live)
  }

  function selectNeighbour(direction) {
    if (!root.anchorPoints.length) return
    if (root.anchorPoints.indexOf(root.selected) < 0) {
      root.selected = direction < 0 ? root.anchorPoints[root.anchorPoints.length - 1] : root.anchorPoints[0]
      return
    }
    root.selected = Model.neighbourAnchor(root.anchorPoints, root.selected, direction)
  }

  function stepSelected(direction, shift) {
    if (root.readOnly || !root.selectedPoint) return
    root.pointEdited(root.selected,
      Model.stepFrequency(root.curve, root.selectedPoint.f, direction, shift, root.domain),
      false, root.anchorPoints)
  }

  function isPinned(index) {
    return root.pins.indexOf(index) >= 0
  }

  // A new handle at the table point nearest that voltage. Pressing on an
  // existing handle selects it instead of adding a second one on top.
  function addAnchorAt(x) {
    if (root.readOnly || root.draftPoints.length < 4) return
    var index = Model.nearestPointIndex(root.draftPoints, root.xToVolt(x))
    if (index < 0) return
    if (root.anchorPoints.indexOf(index) >= 0) {
      root.selected = index
      return
    }
    root.selected = index
    root.pendingAnchor = index
    root.anchorAdded(index)
  }

  // The keyboard path: a handle between the selection and the next one, so it
  // is reachable without a pointer.
  function addAnchorNearSelection() {
    if (root.readOnly || root.draftPoints.length < 4) return
    if (root.anchorPoints.length >= 12) return
    var index = Model.anchorInsertIndex(root.draftPoints, root.anchorPoints, root.selected)
    if (index < 0 || root.anchorPoints.indexOf(index) >= 0) return
    root.selected = index
    root.pendingAnchor = index
    root.anchorAdded(index)
  }

  // Only the handles the user added can be removed; the automatic spread is
  // derived from the plot width.
  function removeSelectedAnchor() {
    if (root.readOnly) return
    if (!root.isPinned(root.selected)) return
    var index = root.selected
    root.selected = -1
    root.anchorRemoved(index)
  }

  function openExact() {
    if (root.readOnly || !root.selectedPoint) return
    var pendingExact = Math.round(root.selectedPoint.f)
    exactField.text = String(pendingExact)
    root.exactVisible = true
    exactField.forceActiveFocus()
    exactField.selectAll()
  }

  function submitExact() {
    if (!root.exactVisible) return
    var index = root.selected
    var entered = Model.parseFrequency(root.curve, exactField.text, root.domain)
    root.exactVisible = false
    if (index >= 0 && entered !== null) root.exactSubmitted(index, entered, root.anchorPoints)
    plot.forceActiveFocus()
  }

  function cancelExact() {
    if (!root.exactVisible) return
    root.exactVisible = false
    plot.forceActiveFocus()
  }

  function setCanvasFont(ctx, pixels) {
    ctx.font = pixels + "px \"" + Style.font.family + "\""
  }

  Column {
    id: column
    width: root.width
    spacing: Style.space(6)

    Item {
      id: plot
      width: parent.width
      height: root.plotHeight
      activeFocusOnTab: !root.readOnly
      Accessible.role: Accessible.Slider
      Accessible.name: strings.t("control.curve")
      Accessible.description: strings.t("curve.keys")

      onActiveFocusChanged: {
        if (!activeFocus) root.shiftHeld = false
        root.repaint()
      }

      Canvas {
        id: canvas
        anchors.fill: parent
        onPaint: {
          root.paintCount++
          var ctx = getContext("2d")
          ctx.reset()
          if (!root.curve || !root.draftPoints.length) return

          var left = root.padLeft
          var right = width - root.padRight
          var top = root.padTop
          var bottom = height - root.padBottom
          var h = bottom - top
          var box = root.plotBox()

          function x(v) { return Model.curveVoltageToX(v, box) }
          function y(f) { return Model.curveFrequencyToY(f, box) }
          // The model de-duplicates the table and walks it with a monotone
          // cubic (Fritsch-Carlson); the canvas only draws the samples. This
          // is what removes the staircase where consecutive points share a
          // frequency or voltage.
          function trace(points, move) {
            var sampled = Model.smoothCurve(points)
            if (!sampled.length) return
            if (move) ctx.moveTo(x(sampled[0].v), y(sampled[0].f))
            for (var i = 1; i < sampled.length; i++) {
              ctx.lineTo(x(sampled[i].v), y(sampled[i].f))
            }
          }

          // Quiet grid: a hairline at the top and the middle, and a slightly
          // stronger baseline, one line per frequency label.
          ctx.lineWidth = 1
          ctx.strokeStyle = Util.alpha(Commons.Color.foreground, 0.16)
          for (var g = 0; g < 2; g++) {
            var gy = top + (h * g) / 2
            ctx.beginPath()
            ctx.moveTo(left, gy)
            ctx.lineTo(right, gy)
            ctx.stroke()
          }
          for (var vg = 0; vg < 3; vg++) {
            var vx = left + ((right - left) * vg) / 2
            ctx.beginPath()
            ctx.moveTo(vx, top)
            ctx.lineTo(vx, bottom)
            ctx.stroke()
          }
          ctx.strokeStyle = Util.alpha(Commons.Color.foreground, 0.18)
          ctx.beginPath()
          ctx.moveTo(left, bottom)
          ctx.lineTo(right, bottom)
          ctx.stroke()

          // The aggressive limit is the dashed red line drawn after the
          // draft; the space below it stays untinted, so red only ever marks
          // a risky value.
          var bounds = Model.curveRiskBounds(root.curve, root.coreControl)

          // Factory: dim dashed reference.
          ctx.strokeStyle = Util.alpha(Commons.Color.foreground, 0.38)
          ctx.lineWidth = 1.2
          ctx.setLineDash([3, 3])
          ctx.beginPath()
          trace(root.factoryPoints, true)
          ctx.stroke()
          ctx.setLineDash([])

          // The aggressive limit, under the draft.
          if (bounds && bounds.edges && bounds.edges.length) {
            ctx.strokeStyle = Util.alpha(Commons.Color.urgent, 0.7)
            ctx.lineWidth = 1
            ctx.setLineDash([3, 3])
            ctx.beginPath()
            trace(bounds.edges, true)
            ctx.stroke()
            ctx.setLineDash([])
          }

          // Draft: the line the user owns, over a background halo so a limit
          // or stock line running beside it never reads as a red fringe.
          ctx.lineJoin = "round"
          ctx.lineCap = "round"
          ctx.beginPath()
          trace(root.draftPoints, true)
          ctx.strokeStyle = Commons.Color.background
          ctx.lineWidth = 5
          ctx.stroke()
          // Muted when the driver refuses writes, so it does not look editable.
          ctx.strokeStyle = root.readOnly ? Util.alpha(Commons.Color.accent, 0.45) : Commons.Color.accent
          ctx.lineWidth = 2
          ctx.stroke()

          // A dashed line marks the limit, and the stretches
          // of the draft beyond it turn red.
          // The same smoothed draft, stroked red and clipped to the area
          // above the limit, so the red covers the blue exactly. The clip
          // sits 1 px above the limit: a draft lying on it stays blue.
          if (bounds && bounds.edges && bounds.edges.length
              && Model.curveExceeds(root.draftPoints, root.curve, root.coreControl)) {
            var firstEdge = bounds.edges[0]
            var lastEdge = bounds.edges[bounds.edges.length - 1]
            ctx.save()
            ctx.beginPath()
            ctx.moveTo(left - 8, y(firstEdge.f) - 1)
            var limitSamples = Model.smoothCurve(bounds.edges)
            for (var ls = 0; ls < limitSamples.length; ls++)
              ctx.lineTo(x(limitSamples[ls].v), y(limitSamples[ls].f) - 1)
            ctx.lineTo(right + 8, y(lastEdge.f) - 1)
            ctx.lineTo(right + 8, top - 20)
            ctx.lineTo(left - 8, top - 20)
            ctx.closePath()
            ctx.clip()
            ctx.beginPath()
            trace(root.draftPoints, true)
            ctx.strokeStyle = Commons.Color.urgent
            ctx.lineWidth = 2
            ctx.stroke()
            ctx.restore()
          }

          // Guides under the selected anchor, then the anchors themselves.
          if (!root.readOnly && root.selectedPoint) {
            var sx = x(root.selectedPoint.v)
            var sy = y(root.selectedPoint.f)
            ctx.strokeStyle = Util.alpha(Commons.Color.accent, 0.5)
            ctx.lineWidth = 1
            ctx.setLineDash([2, 3])
            // Only the vertical guide: the readout under the plot gives the
            // frequency, so a horizontal one would end on an unlabelled line.
            ctx.beginPath()
            ctx.moveTo(sx, sy)
            ctx.lineTo(sx, bottom)
            ctx.stroke()
            ctx.setLineDash([])
          }

          if (!root.readOnly) {
            // One 14 px dot per anchor; a press anywhere in the plot
            // resolves to the nearest handle in X, so the hit target is the
            // full height of the plot. No handle per real point.
            for (var a = 0; a < root.anchorPoints.length; a++) {
              var point = root.draftPoints[root.anchorPoints[a]]
              if (!point) continue
              var ax = x(point.v)
              var ay = y(point.f)
              var active = root.anchorPoints[a] === root.selected
              var hovered = root.anchorPoints[a] === root.hoverAnchor
              var tone = root.pointRisky(root.anchorPoints[a]) ? Commons.Color.urgent : Commons.Color.accent
              if (active) {
                ctx.fillStyle = Util.alpha(tone, 0.2)
                ctx.beginPath()
                ctx.arc(ax, ay, 13, 0, Math.PI * 2)
                ctx.fill()
              }
              ctx.beginPath()
              ctx.arc(ax, ay, 7, 0, Math.PI * 2)
              ctx.fillStyle = Commons.Color.background
              ctx.fill()
              ctx.strokeStyle = active || hovered ? tone : Util.alpha(tone, 0.85)
              ctx.lineWidth = active ? 2 : 1.5
              ctx.stroke()
              // Keys edit this handle: a foreground ring outside the halo.
              if (active && plot.activeFocus) {
                ctx.beginPath()
                ctx.arc(ax, ay, 15, 0, Math.PI * 2)
                ctx.strokeStyle = root.rowForeground
                ctx.lineWidth = 2
                ctx.stroke()
              }
              // A handle the user added is filled in: the automatic ones are
              // the plot's own, the pinned ones are removable.
              if (root.isPinned(root.anchorPoints[a])) {
                ctx.beginPath()
                ctx.arc(ax, ay, 2.5, 0, Math.PI * 2)
                ctx.fillStyle = tone
                ctx.fill()
              }
            }
          }

          // Live operating point: x is voltage, y is frequency. A small
          // crosshair, no label.
          var telemetry = root.daemon ? root.daemon.telemetry : null
          if (telemetry) {
            var lf = telemetry.gpuFrequency
            var lv = telemetry.gpuVoltage
            var hasF = lf !== undefined && lf !== null
            var hasV = lv !== undefined && lv !== null
            if (hasF || hasV) {
              ctx.beginPath()
              if (hasF && hasV) {
                var crossHandles = root.readOnly ? [] : root.handlePositions()
                for (var ch = 0; ch < crossHandles.length; ch++)
                  if (crossHandles[ch].index === root.selected) crossHandles[ch].r = 13
                Paint.operatingCross(ctx, x(lv), y(lf), crossHandles,
                  7, Commons.Color.background, root.rowForeground)
              } else if (hasF) {
                ctx.moveTo(left, y(lf))
                ctx.lineTo(right, y(lf))
              } else {
                ctx.moveTo(x(lv), top)
                ctx.lineTo(x(lv), bottom)
              }
              // Foreground over a background halo, like the fan plot.
              if (!(hasF && hasV)) {
                ctx.lineCap = "round"
                ctx.strokeStyle = Commons.Color.background
                ctx.lineWidth = 4.5
                ctx.stroke()
                ctx.strokeStyle = root.rowForeground
                ctx.lineWidth = 1.5
                ctx.stroke()
                ctx.lineCap = "butt"
              }
            }
          }

          // Axis labels: three per axis. Voltage along the bottom, with the
          // unit on the last label; frequency up the left, with the unit on
          // the top label. The anchors keep every label inside the widget.
          var labels = Model.curveAxisLabels(root.domain)
          var layout = Model.curveAxisAnchors({
            width: width,
            height: height,
            padLeft: left,
            padRight: root.padRight,
            padTop: top,
            padBottom: root.padBottom,
            caption: Style.font.caption,
            labelGap: Style.space(4),
            bottomInset: Style.space(2)
          })
          root.setCanvasFont(ctx, Style.font.caption)
          ctx.fillStyle = Util.alpha(Commons.Color.foreground, 0.85)
          ctx.textBaseline = "alphabetic"
          for (var xLabel = 0; xLabel < 3; xLabel++) {
            ctx.textAlign = layout.x[xLabel].align
            ctx.fillText(labels.x[xLabel], layout.x[xLabel].x, layout.x[xLabel].y)
          }
          for (var yLabel = 0; yLabel < 3; yLabel++) {
            ctx.textAlign = layout.y[yLabel].align
            ctx.fillText(labels.y[yLabel], layout.y[yLabel].x, layout.y[yLabel].y)
          }

          // Key for the two dashed lines, in the corner the rising curve
          // never reaches.
          var keys = [{ text: strings.t("curve.draftKey"), color: Commons.Color.accent, dash: false },
            { text: strings.t("curve.stockKey"), color: Util.alpha(Commons.Color.foreground, 0.55), dash: true }]
          if (bounds) keys.push({ text: strings.t("curve.limitKey"), color: Util.alpha(Commons.Color.urgent, 0.85), dash: true })
          // The pad keeps the same 3 px from the right edge and the baseline.
          Paint.plotKey(ctx, keys, right - Style.space(7), bottom - Style.space(7), Style,
            Util.alpha(Commons.Color.foreground, 0.85), Util.alpha(Commons.Color.background, 0.85))
        }
      }

      MouseArea {
        anchors.fill: parent
        enabled: !root.readOnly
        hoverEnabled: true
        acceptedButtons: Qt.LeftButton
        preventStealing: true
        cursorShape: root.readOnly ? Qt.ArrowCursor : Qt.SizeVerCursor

        property int dragIndex: -1

        onPressed: function(mouse) {
          plot.forceActiveFocus()
          // Select on press, move on drag: a click that never moves must not
          // teleport the anchor, or the first click of a double-click would
          // bend the curve before a handle is even added.
          var grabbed = root.anchorAt(mouse.x)
          root.selected = grabbed
          dragIndex = grabbed
          root.shiftHeld = (mouse.modifiers & Qt.ShiftModifier) !== 0
          root.repaint()
        }
        onPositionChanged: function(mouse) {
          root.hoverAnchor = root.anchorAt(mouse.x)
          root.shiftHeld = (mouse.modifiers & Qt.ShiftModifier) !== 0
          if (!pressed || dragIndex < 0) return
          root.pointEdited(dragIndex, root.yToFreq(mouse.y), root.shiftHeld, root.anchorPoints)
        }
        onReleased: {
          dragIndex = -1
          root.shiftHeld = false
        }
        onDoubleClicked: function(mouse) { root.addAnchorAt(mouse.x) }
        onExited: {
          root.hoverAnchor = -1
          if (!pressed) root.shiftHeld = false
        }
        onWheel: function(wheel) {
          // The panel scrolls and the wheel is easy to hit by accident; only
          // the focused plot takes it, like the sliders.
          if (!plot.activeFocus || root.readOnly || !root.selectedPoint) {
            wheel.accepted = false
            return
          }
          root.stepSelected(wheel.angleDelta.y > 0 ? 1 : -1,
            (wheel.modifiers & Qt.ShiftModifier) !== 0)
          wheel.accepted = true
        }
      }

      // The Shift hint appears while the modifier is held, so a flattened
      // drag is not a surprise.
      Text {
        id: shiftHint
        anchors.top: parent.top
        anchors.right: parent.right
        anchors.margins: Style.space(6)
        visible: root.shiftHeld && !root.readOnly && root.selectedPoint !== null
        textFormat: Text.PlainText
        text: strings.t("curve.shiftHint")
        color: Commons.Color.accent
        font.family: Style.font.family
        font.pixelSize: Style.font.caption
      }

      Keys.onPressed: function(event) {
        if (event.key === Qt.Key_Shift) {
          root.shiftHeld = true
          return
        }
        if (root.readOnly) return
        if (event.key === Qt.Key_Up || event.key === Qt.Key_Down) {
          root.stepSelected(event.key === Qt.Key_Up ? 1 : -1,
            (event.modifiers & Qt.ShiftModifier) !== 0)
          event.accepted = true
          return
        }
        if (event.key === Qt.Key_Left || event.key === Qt.Key_Right) {
          root.selectNeighbour(event.key === Qt.Key_Left ? -1 : 1)
          event.accepted = true
          return
        }
        if (event.key === Qt.Key_A || event.key === Qt.Key_Insert
            || event.key === Qt.Key_Plus) {
          root.addAnchorNearSelection()
          event.accepted = true
          return
        }
        if (event.key === Qt.Key_Delete || event.key === Qt.Key_Backspace
            || event.key === Qt.Key_X) {
          root.removeSelectedAnchor()
          event.accepted = true
          return
        }
        if (event.key === Qt.Key_Escape) {
          if (root.selected >= 0) {
            // First Esc deselects; the next one leaves the plot so the
            // panel's own catcher can close the popup.
            root.selected = -1
            event.accepted = true
          } else {
            plot.focus = false
          }
          return
        }
        if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter) {
          if (root.selectedPoint) {
            root.openExact()
            event.accepted = true
          }
        }
      }

      Keys.onReleased: function(event) {
        if (event.key === Qt.Key_Shift) root.shiftHeld = false
      }

      Connections {
        target: root.daemon
        function onTelemetryChanged() { root.repaint() }
      }
    }

    // Selected anchor as one readout on the right: "Selected 0.581 V →
    // 861 MHz +45 vs stock". The frequency is the exact-value editor.
    Item {
      width: root.width
      visible: !root.readOnly
      implicitHeight: Math.max(voltText.implicitHeight, freqText.implicitHeight,
        exactField.implicitHeight, deltaText.implicitHeight)

      Text {
        id: voltText
        anchors.right: freqArea.visible ? freqArea.left : parent.right
        anchors.rightMargin: freqArea.visible ? Style.space(6) : 0
        y: freqArea.visible
          ? freqArea.y + freqText.y + freqText.baselineOffset - baselineOffset
          : (parent.height - height) / 2
        width: Math.min(implicitWidth, parent.width - (freqArea.visible ? freqArea.width + Style.space(6) : 0))
        horizontalAlignment: Text.AlignRight
        textFormat: Text.PlainText
        elide: Text.ElideLeft
        text: root.selectedPoint
          ? Model.targ(strings.t("curve.selectedVoltage"), Model.formatVoltage(root.selectedPoint.v))
          : strings.t("curve.noSelection")
        color: Util.alpha(root.rowForeground, 0.75)
        font.family: Style.font.family
        font.pixelSize: Style.font.bodySmall
      }

      Item {
        id: freqArea
        anchors.right: parent.right
        anchors.verticalCenter: parent.verticalCenter
        visible: root.selectedPoint !== null
        implicitWidth: (root.exactVisible ? exactField.width : freqText.implicitWidth)
          + Style.space(3) + freqUnit.implicitWidth
          + (deltaText.visible ? Style.space(8) + deltaText.implicitWidth : 0)
          + (deltaDot.visible ? deltaDot.width + Style.space(5) : 0)
        implicitHeight: Math.max(freqText.implicitHeight, exactField.implicitHeight, deltaText.implicitHeight)

        Text {
          id: freqText
          visible: !root.exactVisible
          anchors.left: parent.left
          anchors.bottom: parent.bottom
          textFormat: Text.PlainText
          text: root.selectedPoint
            ? Commons.I18n.formatNumber(Math.round(root.selectedPoint.f))
            : ""
          color: root.pointRisky(root.selected) ? Commons.Color.urgent : root.rowForeground
          font.family: Style.font.family
          font.pixelSize: Style.font.title
          font.weight: Font.Medium

          MouseArea {
            anchors.fill: parent
            cursorShape: Qt.IBeamCursor
            onClicked: root.openExact()
          }
        }

        TextInput {
          id: exactField
          visible: root.exactVisible
          width: Style.space(72)
          anchors.left: parent.left
          anchors.bottom: parent.bottom
          color: root.exactRisky ? Commons.Color.urgent : Commons.Color.accent
          font.family: Style.font.family
          font.pixelSize: Style.font.title
          font.weight: Font.Medium
          horizontalAlignment: Text.AlignRight
          selectionColor: Style.selectionFillFor(root.rowForeground, Commons.Color.accent)
          selectedTextColor: Commons.Color.background
          inputMethodHints: Qt.ImhFormattedNumbersOnly
          validator: IntValidator {
            bottom: Math.ceil(root.domain.minF)
            top: Math.floor(root.domain.maxF)
          }
          onAccepted: root.submitExact()
          onActiveFocusChanged: if (!activeFocus && visible) root.submitExact()
          Keys.onEscapePressed: function(event) {
            root.cancelExact()
            event.accepted = true
          }
        }

        Rectangle {
          visible: root.exactVisible
          anchors.left: exactField.left
          anchors.right: exactField.right
          anchors.bottom: exactField.bottom
          height: 1
          color: Util.alpha(root.exactRisky ? Commons.Color.urgent : Commons.Color.accent, 0.8)
        }

        // The unit stays beside the number while it is being typed.
        Text {
          id: freqUnit
          anchors.left: root.exactVisible ? exactField.right : freqText.right
          anchors.leftMargin: Style.space(3)
          anchors.baseline: root.exactVisible ? exactField.baseline : freqText.baseline
          textFormat: Text.PlainText
          text: "MHz"
          color: Util.alpha(root.rowForeground, 0.85)
          font.family: Style.font.family
          font.pixelSize: Style.font.bodySmall
        }

        // The staged mark of the Curve chip and the fan's "Edited": this
        // point is an unapplied edit.
        Rectangle {
          id: deltaDot
          visible: deltaText.visible && root.pointStaged(root.selected) && !root.pointRisky(root.selected)
          anchors.left: freqUnit.right
          anchors.leftMargin: Style.space(8)
          y: deltaText.y + (deltaText.height - height) / 2
          width: Style.space(6)
          height: width
          radius: width / 2
          color: Commons.Color.accent
        }

        // Change from stock for this point; nothing at stock.
        Text {
          id: deltaText
          readonly property int delta: root.selectedPoint && root.selected < root.factoryPoints.length
            ? Math.round(root.selectedPoint.f - root.factoryPoints[root.selected].f) : 0
          visible: !root.exactVisible && root.selectedPoint !== null && delta !== 0
          anchors.left: deltaDot.visible ? deltaDot.right : freqUnit.right
          anchors.leftMargin: deltaDot.visible ? Style.space(5) : Style.space(8)
          anchors.baseline: freqText.baseline
          textFormat: Text.PlainText
          text: Model.targ(strings.t("curve.pointDelta"), Model.signed(delta).replace("-", "\u2212"))
          // Red past the limit, accent while the point is an unapplied edit,
          // muted when it is what the GPU already runs.
          color: root.pointRisky(root.selected) ? Commons.Color.urgent
            : root.pointStaged(root.selected) ? Commons.Color.accent
            : Util.alpha(root.rowForeground, 0.75)
          font.family: Style.font.family
          font.pixelSize: Style.font.bodySmall
        }
      }
    }

    // A short hint with a "?" that holds every key and the drag rules, and
    // the two actions. Nothing on this line truncates.
    Item {
      width: root.width
      visible: !root.readOnly
      implicitHeight: Math.max(keysBadge.height, keysText.implicitHeight, actions.implicitHeight)

      HintBadge {
        id: keysBadge
        anchors.left: keysText.right
        anchors.leftMargin: Style.space(6)
        anchors.verticalCenter: parent.verticalCenter
        foreground: root.rowForeground
        accessName: strings.t("curve.keys")
        tip: strings.t("curve.keys") + "\n\n" + strings.t("curve.dragHint")
      }

      Text {
        id: keysText
        anchors.left: parent.left
        anchors.verticalCenter: parent.verticalCenter
        // The "?" sits right of its term, as everywhere else.
        width: Math.min(implicitWidth,
          Math.max(0, parent.width - actions.implicitWidth - keysBadge.width - Style.space(16)))
        textFormat: Text.PlainText
        text: strings.t("curve.keysShort")
        elide: Text.ElideRight
        color: Util.alpha(root.rowForeground, 0.75)
        font.family: Style.font.family
        font.pixelSize: Style.font.bodySmall
      }

      Row {
        id: actions
        anchors.right: parent.right
        anchors.verticalCenter: parent.verticalCenter
        spacing: Style.space(10)

        ChipButton {
          text: strings.t("curve.flattenAbove")
          enabled: !!root.selectedPoint
          HoverHandler { id: flattenHover }
          onClicked: {
            if (!root.selectedPoint) return
            root.pointEdited(root.selected, root.selectedPoint.f, true, root.anchorPoints)
          }
        }

        ChipButton {
          iconText: "\u{F099B}"
          text: strings.t("curve.reset")
          // Nothing to reset while the draft is the factory curve.
          enabled: !Model.samePoints(root.draftPoints, root.factoryPoints)
          onClicked: root.resetRequested()
        }
      }
    }

    Text {
      width: root.width
      visible: root.readOnly
      textFormat: Text.PlainText
      wrapMode: Text.WordWrap
      text: root.curve && root.curve.cause ? root.curve.cause : strings.t("curve.readOnly")
      color: Util.alpha(root.rowForeground, 0.85)
      font.family: Style.font.family
      font.pixelSize: Style.font.bodySmall
    }
  }

  PanelToolTip {
    visible: flattenHover.hovered && !root.readOnly
    delay: 500
    text: strings.t("curve.dragHint")
  }

  // The layout settles after construction (the plot width decides the anchor
  // count), so the default selection waits one turn.
  Component.onCompleted: Qt.callLater(root.selectDefault)
}
