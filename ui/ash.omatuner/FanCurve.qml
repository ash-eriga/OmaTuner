import QtQuick
import qs.Commons
import qs.Commons as Commons
import "Model.js" as Model
import "PlotPaint.js" as Paint

// Temperature -> fan speed editor. The points are the user's: double-click in
// the plot adds one, dragging moves it on both axes (temperature across, duty
// up), Delete or a right-click removes it. The curve stays rising and inside
// the daemon's window because every edit goes through Model.fanMovePoint,
// fanAddPoint and fanRemovePoint.
Item {
  id: root

  property var points: []
  property var factory: []
  property int minT: 30
  property int maxT: 90
  property int minP: 0
  property int maxP: 100
  property int maxPoints: 12
  property real currentTemp: -1
  property bool writable: true
  property color foreground: Commons.Color.foreground
  property color accent: Commons.Color.accent
  property int selected: 0
  // Temperature of a point this view just asked to add: the draft comes back
  // through a property binding, and the new point has to end up selected so
  // Delete removes what was just created.
  property int pendingTemp: -1

  signal pointEdited(int index, int temp, int speed)
  signal pointAdded(int temp, int speed)
  signal pointRemoved(int index)
  signal editorFocusChanged(bool focused)
  signal enterRequested()
  signal escapeRequested()
  signal tabRequested(int direction)

  readonly property bool focused: activeFocus && root.writable
  // The V/F plot's gutter, so both plots on the GPU tab share a left edge.
  readonly property real leftPad: Style.space(64)
  readonly property real rightPad: Style.space(6)
  readonly property real topPad: Style.space(10)
  readonly property real bottomPad: Style.space(20)
  readonly property real plotWidth: Math.max(1, width - leftPad - rightPad)
  readonly property real plotHeight: Math.max(1, height - topPad - bottomPad)
  // Read-only probe for the interaction tests.
  readonly property int pointCount: root.points ? root.points.length : 0

  Commons.Translations {
    id: strings
    directory: Qt.resolvedUrl("i18n")
  }

  implicitHeight: Style.space(170)
  focus: true
  activeFocusOnTab: false
  onActiveFocusChanged: {
    root.editorFocusChanged(activeFocus)
    canvas.requestPaint()
  }
  Accessible.role: Accessible.Slider
  Accessible.name: strings.t("fan.curveName")

  function xForTemp(temp) {
    var span = root.maxT - root.minT
    if (!(span > 0)) return root.leftPad
    var fraction = Math.max(0, Math.min(1, (temp - root.minT) / span))
    return root.leftPad + fraction * root.plotWidth
  }

  function tempForX(x) {
    var span = root.maxT - root.minT
    if (!(span > 0)) return root.minT
    var fraction = Math.max(0, Math.min(1, (x - root.leftPad) / root.plotWidth))
    return Math.round(root.minT + fraction * span)
  }

  function yForSpeed(speed) {
    var span = Math.max(1, root.maxP - root.minP)
    return root.topPad + root.plotHeight - ((speed - root.minP) / span) * root.plotHeight
  }

  function speedForY(y) {
    var span = Math.max(1, root.maxP - root.minP)
    var fraction = 1 - Math.max(0, Math.min(1, (y - root.topPad) / root.plotHeight))
    return Math.round(root.minP + fraction * span)
  }

  function pointAt(index) {
    return root.points && root.points[index] ? root.points[index] : { t: root.minT, p: root.minP }
  }

  // The point under the pointer, or -1. Both axes count: a handle is a
  // position on the plane now, not a column on a fixed temperature grid.
  function pointAtPixel(x, y) {
    if (!root.points || !root.points.length) return -1
    var best = -1
    var bestDistance = 16
    for (var i = 0; i < root.points.length; i++) {
      var dx = root.xForTemp(root.points[i].t) - x
      var dy = root.yForSpeed(root.points[i].p) - y
      var distance = Math.sqrt(dx * dx + dy * dy)
      if (distance < bestDistance) { bestDistance = distance; best = i }
    }
    return best
  }

  function movePoint(index, temp, speed) {
    if (!root.points || index < 0 || index >= root.points.length) return
    var point = root.points[index]
    var nextTemp = Math.round(temp)
    var nextSpeed = Math.round(speed)
    if (nextTemp !== point.t || nextSpeed !== point.p) root.pointEdited(index, nextTemp, nextSpeed)
  }

  // Where a new point lands from the keyboard: halfway to the next one, so it
  // never lands on top of the selection.
  function addPointNearSelection() {
    if (!root.writable) return
    if (root.pointCount >= root.maxPoints) return
    var list = root.points || []
    var next = root.selected + 1
    var temp
    if (next < list.length) {
      temp = Math.round((list[root.selected].t + list[next].t) / 2)
      if (temp <= list[root.selected].t || temp >= list[next].t) return
    } else {
      temp = list.length ? Math.min(root.maxT, list[list.length - 1].t + 5) : root.minT
      if (list.length && temp <= list[list.length - 1].t) return
    }
    root.pendingTemp = temp
    root.pointAdded(temp, -1)
  }

  function removeSelected() {
    if (!root.writable) return
    if (root.pointCount <= 2) return
    root.pointRemoved(root.selected)
  }

  function focusPlot() {
    root.forceActiveFocus()
  }

  Canvas {
    id: canvas
    anchors.fill: parent
    onPaint: {
      var ctx = getContext("2d")
      ctx.reset()
      var l = root.leftPad
      var t = root.topPad
      var w = root.plotWidth
      var h = root.plotHeight
      var textColor = Util.alpha(root.foreground, 0.85)
      var gridColor = Util.alpha(root.foreground, 0.16)

      ctx.font = Style.font.caption + "px \"" + Style.font.family + "\""
      ctx.textBaseline = "middle"
      ctx.lineWidth = 1

      // Horizontal grid: 0/25/50/75/100 within the driver window.
      var steps = 4
      for (var i = 0; i <= steps; i++) {
        var speed = root.minP + (root.maxP - root.minP) * (i / steps)
        var y = root.yForSpeed(speed)
        ctx.strokeStyle = gridColor
        ctx.beginPath()
        ctx.moveTo(l, y)
        ctx.lineTo(l + w, y)
        ctx.stroke()
        ctx.fillStyle = textColor
        ctx.textAlign = "right"
        ctx.fillText(Math.round(speed) + "%", l - Style.space(5), y)
      }

      // Vertical grid every 10 °C over the whole window: points can sit
      // anywhere inside it, so the grid cannot follow them.
      var step = 10
      for (var temp = Math.ceil(root.minT / step) * step; temp <= root.maxT; temp += step) {
        var gx = root.xForTemp(temp)
        ctx.strokeStyle = gridColor
        ctx.beginPath()
        ctx.moveTo(gx, t)
        ctx.lineTo(gx, t + h)
        ctx.stroke()
        ctx.fillStyle = textColor
        // The end labels align to the plot's edges instead of hanging past
        // them into the gutter and the panel padding.
        ctx.textAlign = temp === Math.ceil(root.minT / step) * step && gx - l < Style.space(8)
          ? "left" : (temp + step > root.maxT && l + w - gx < Style.space(8) ? "right" : "center")
        ctx.fillText(temp + "°", gx, t + h + Style.space(10))
      }


      // Live temperature marker.
      if (root.currentTemp >= 0) {
        var markerX = root.xForTemp(Math.max(root.minT, Math.min(root.maxT, root.currentTemp)))
        // Muted foreground, like the operating cross on it; the accent is
        // the selection's colour in both plots.
        ctx.strokeStyle = Util.alpha(root.foreground, 0.45)
        ctx.setLineDash([2, 3])
        ctx.beginPath()
        ctx.moveTo(markerX, t)
        ctx.lineTo(markerX, t + h)
        ctx.stroke()
        ctx.setLineDash([])
        // Name the line: it is the GPU temperature now.
        ctx.fillStyle = textColor
        ctx.font = Style.font.caption + "px \"" + Style.font.family + "\""
        ctx.textAlign = markerX > l + w - Style.space(30) ? "right" : "left"
        ctx.textBaseline = "top"
        ctx.fillText(Math.round(root.currentTemp) + "°", markerX + (ctx.textAlign === "right" ? -Style.space(3) : Style.space(3)), t + Style.space(2))
      }

      if (root.points && root.points.length > 0) {
        ctx.lineJoin = "round"
        var curvePixels = []
        ctx.beginPath()
        for (var k = 0; k < root.points.length; k++) {
          var px = root.xForTemp(root.points[k].t)
          var py = root.yForSpeed(root.points[k].p)
          curvePixels.push({ x: px, y: py })
          if (k === 0) ctx.moveTo(px, py)
          else ctx.lineTo(px, py)
        }
        // A background halo, then stock over it, then the curve: stock stays
        // visible where it runs right beside the curve.
        ctx.strokeStyle = Commons.Color.background
        ctx.lineWidth = 5
        ctx.stroke()
        root.paintFactory(ctx)
        ctx.beginPath()
        for (var k2 = 0; k2 < curvePixels.length; k2++) {
          if (k2 === 0) ctx.moveTo(curvePixels[k2].x, curvePixels[k2].y)
          else ctx.lineTo(curvePixels[k2].x, curvePixels[k2].y)
        }
        ctx.strokeStyle = root.writable ? root.accent : Util.alpha(root.accent, 0.45)
        ctx.lineWidth = 2
        ctx.stroke()
      } else {
        root.paintFactory(ctx)
      }

      // Handles drawn like the V/F plot's: a ring with the background inside,
      // and a soft halo around the selected one.
      var tone = root.writable ? root.accent : Util.alpha(root.foreground, 0.4)
      for (var n = 0; n < root.points.length; n++) {
        var ax = root.xForTemp(root.points[n].t)
        var ay = root.yForSpeed(root.points[n].p)
        var active = n === root.selected && root.writable
        if (active) {
          ctx.fillStyle = Util.alpha(tone, 0.2)
          ctx.beginPath()
          ctx.arc(ax, ay, 11, 0, Math.PI * 2)
          ctx.fill()
        }
        ctx.beginPath()
        ctx.arc(ax, ay, 5.5, 0, Math.PI * 2)
        ctx.fillStyle = Commons.Color.background
        ctx.fill()
        ctx.strokeStyle = active ? tone : Util.alpha(tone, 0.85)
        ctx.lineWidth = active ? 2 : 1.5
        ctx.stroke()
        // Keys edit this handle: a foreground ring outside the halo.
        if (active && root.focused) {
          ctx.beginPath()
          ctx.arc(ax, ay, 13, 0, Math.PI * 2)
          ctx.strokeStyle = root.foreground
          ctx.lineWidth = 2
          ctx.stroke()
        }
      }

      // The operating point, over the handles so it is never hidden: a
      // small cross on the temperature line.
      if (root.currentTemp >= 0 && root.points && root.points.length > 0) {
        var currentSpeed = root.points.length ? root.speedAt(root.currentTemp) : 0
        var cx = root.xForTemp(Math.max(root.minT, Math.min(root.maxT, root.currentTemp)))
        var cy = root.yForSpeed(currentSpeed)
        var crossHandles = root.handlePixels()
        if (root.writable && crossHandles[root.selected]) crossHandles[root.selected].r = 11
        Paint.operatingCross(ctx, cx, cy, crossHandles, 5.5, Commons.Color.background, root.foreground)
      }

      // Key in the lower right corner, where the rising curve never passes.
      var keys = [{ text: strings.t("curve.draftKey"), color: root.accent, dash: false }]
      if (root.factory && root.factory.length > 1 && !Model.sameFanPoints(root.points, root.factory))
        keys.push({ text: strings.t("fan.factoryCurve"), color: Util.alpha(root.foreground, 0.45), dash: true })
      // The pad keeps the same 3 px from the right edge and the baseline.
      Paint.plotKey(ctx, keys, l + w - Style.space(7), t + h - Style.space(7), Style,
        textColor, Util.alpha(Commons.Color.background, 0.85))
    }
  }

  // Factory curve: a dashed reference, drawn only when the curve departs
  // from it (otherwise the two lines coincide).
  function paintFactory(ctx) {
    if (!(root.factory && root.factory.length > 1) || Model.sameFanPoints(root.points, root.factory)) return
    ctx.strokeStyle = Util.alpha(root.foreground, 0.55)
    ctx.lineWidth = 1.2
    ctx.setLineDash([3, 3])
    ctx.beginPath()
    for (var f = 0; f < root.factory.length; f++) {
      var fx = root.xForTemp(root.factory[f].t)
      var fy = root.yForSpeed(root.factory[f].p)
      if (f === 0) ctx.moveTo(fx, fy)
      else ctx.lineTo(fx, fy)
    }
    ctx.stroke()
    ctx.setLineDash([])
  }

  function handlePixels() {
    var out = []
    for (var i = 0; i < root.pointCount; i++)
      out.push({ x: root.xForTemp(root.points[i].t), y: root.yForSpeed(root.points[i].p) })
    return out
  }

  function speedAt(temp) {
    return Model.fanDutyAt(root.points, temp)
  }

  MouseArea {
    id: mouse
    anchors.fill: parent
    enabled: root.writable
    cursorShape: root.writable ? Qt.PointingHandCursor : Qt.ArrowCursor
    hoverEnabled: true
    acceptedButtons: Qt.LeftButton | Qt.RightButton

    property int draggedIndex: -1

    onPressed: function(m) {
      root.forceActiveFocus()
      if (m.button === Qt.RightButton) {
        var victim = root.pointAtPixel(m.x, m.y)
        if (victim >= 0) {
          root.selected = victim
          root.removeSelected()
        }
        return
      }
      var grabbed = root.pointAtPixel(m.x, m.y)
      // A press away from every handle starts nothing: points are created by
      // a double-click, never by a stray click.
      if (grabbed < 0) {
        draggedIndex = -1
        return
      }
      root.selected = grabbed
      draggedIndex = grabbed
    }
    onPositionChanged: function(m) {
      if (!pressed || draggedIndex < 0) return
      root.movePoint(draggedIndex, root.tempForX(m.x), root.speedForY(m.y))
    }
    onReleased: draggedIndex = -1
    onDoubleClicked: function(m) {
      if (m.button !== Qt.LeftButton) return
      if (root.pointAtPixel(m.x, m.y) >= 0) return
      var temp = root.tempForX(m.x)
      root.pendingTemp = temp
      root.pointAdded(temp, root.speedForY(m.y))
    }
  }

  Keys.onPressed: function(event) {
    if (!root.writable) return
    var point = root.pointAt(root.selected)
    var step = (event.modifiers & Qt.ShiftModifier) ? 10 : 1
    if (event.key === Qt.Key_Up) {
      root.movePoint(root.selected, point.t, point.p + step)
      event.accepted = true
    } else if (event.key === Qt.Key_Down) {
      root.movePoint(root.selected, point.t, point.p - step)
      event.accepted = true
    } else if (event.key === Qt.Key_Left) {
      root.selected = Math.max(0, root.selected - 1)
      event.accepted = true
    } else if (event.key === Qt.Key_Right) {
      root.selected = Math.min(root.pointCount - 1, root.selected + 1)
      event.accepted = true
    } else if (event.key === Qt.Key_A || event.key === Qt.Key_Insert
               || event.key === Qt.Key_Plus) {
      root.addPointNearSelection()
      event.accepted = true
    } else if (event.key === Qt.Key_Delete || event.key === Qt.Key_Backspace
               || event.key === Qt.Key_X) {
      root.removeSelected()
      event.accepted = true
    } else if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter) {
      if (!event.isAutoRepeat) root.enterRequested()
      event.accepted = true
    } else if (event.key === Qt.Key_Escape) {
      root.escapeRequested()
      event.accepted = true
    } else if (event.key === Qt.Key_Tab || event.key === Qt.Key_Backtab) {
      root.tabRequested((event.modifiers & Qt.ShiftModifier) !== 0 || event.key === Qt.Key_Backtab ? -1 : 1)
      event.accepted = true
    }
  }

  onPointsChanged: {
    if (root.pendingTemp >= 0) {
      for (var i = 0; i < root.pointCount; i++) {
        if (root.points[i].t === root.pendingTemp) root.selected = i
      }
      root.pendingTemp = -1
    }
    if (root.selected >= root.pointCount) root.selected = Math.max(0, root.pointCount - 1)
    canvas.requestPaint()
  }
  onSelectedChanged: canvas.requestPaint()
  onCurrentTempChanged: canvas.requestPaint()
  onFactoryChanged: canvas.requestPaint()
  onMinPChanged: canvas.requestPaint()
  onMaxPChanged: canvas.requestPaint()
  onMinTChanged: canvas.requestPaint()
  onMaxTChanged: canvas.requestPaint()
  onWidthChanged: canvas.requestPaint()
  onHeightChanged: canvas.requestPaint()
  onWritableChanged: canvas.requestPaint()
  Component.onCompleted: canvas.requestPaint()
}
