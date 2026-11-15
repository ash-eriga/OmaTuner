.pragma library

// Canvas drawing shared by the V/F and fan plots, so both draw their key and
// operating point the same way.

// The key row's geometry, identical in both plots: the gaps between keys and
// the sample stroke, the lift of the stroke above the baseline, and the pad.
function keyMetrics(style) {
  return { gap: style.space(12), sample: style.space(14),
    sampleGap: style.space(4), lift: style.space(3),
    pad: style.space(4), height: style.font.caption + style.space(2) }
}

// A small cross at the operating point. Next to a handle it would sit on top
// of the ring, so there it becomes four short ticks just outside it.
function operatingCross(ctx, cx, cy, handles, handleRadius, halo, ink) {
  // A handle may carry its own radius (a selected one has a halo).
  var nearRadius = -1
  for (var i = 0; i < (handles ? handles.length : 0); i++) {
    var dx = handles[i].x - cx
    var dy = handles[i].y - cy
    var r = handles[i].r || handleRadius
    if (Math.sqrt(dx * dx + dy * dy) < r + 6) nearRadius = Math.max(nearRadius, r)
  }
  var near = nearRadius >= 0
  var inner = near ? nearRadius + 3 : 0
  var outer = near ? nearRadius + 8 : 5
  ctx.beginPath()
  ctx.moveTo(cx - outer, cy)
  ctx.lineTo(cx - inner, cy)
  ctx.moveTo(cx + inner, cy)
  ctx.lineTo(cx + outer, cy)
  ctx.moveTo(cx, cy - outer)
  ctx.lineTo(cx, cy - inner)
  ctx.moveTo(cx, cy + inner)
  ctx.lineTo(cx, cy + outer)
  ctx.lineCap = "round"
  ctx.strokeStyle = halo
  ctx.lineWidth = 4.5
  ctx.stroke()
  ctx.strokeStyle = ink
  ctx.lineWidth = 1.5
  ctx.stroke()
  ctx.lineCap = "butt"
}

// A right-aligned key, last entry rightmost, on a pad of the background so
// grid lines never run through its words. Each key: { text, color, dash }.
// The canvas font must already be set.
function plotKey(ctx, keys, right, baseline, style, textColor, padColor) {
  var m = keyMetrics(style)
  var widths = []
  var total = 0
  for (var i = 0; i < keys.length; i++) {
    widths.push(ctx.measureText(keys[i].text).width)
    total += widths[i] + m.sample + m.sampleGap
  }
  total += m.gap * Math.max(0, keys.length - 1)
  ctx.fillStyle = padColor
  ctx.fillRect(right - total - m.pad, baseline - m.height + m.pad / 2, total + m.pad * 2, m.height + m.pad / 2)

  ctx.textAlign = "right"
  ctx.textBaseline = "alphabetic"
  var x = right
  for (var k = keys.length - 1; k >= 0; k--) {
    ctx.fillStyle = textColor
    ctx.fillText(keys[k].text, x, baseline)
    ctx.strokeStyle = keys[k].color
    ctx.lineWidth = keys[k].dash ? 1.2 : 2
    ctx.setLineDash(keys[k].dash ? [3, 3] : [])
    ctx.beginPath()
    ctx.moveTo(x - widths[k] - m.sampleGap - m.sample, baseline - m.lift)
    ctx.lineTo(x - widths[k] - m.sampleGap, baseline - m.lift)
    ctx.stroke()
    ctx.setLineDash([])
    x -= widths[k] + m.sample + m.sampleGap + m.gap
  }
}
