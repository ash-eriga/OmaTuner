.pragma library

// Pure tuning logic shared by the panel views. Kept free of QML so the same
// functions can be exercised headlessly by tests/test_model.mjs.
// Draft shape: { values: {controlKey: number}, points: [{v, f}] | null,
//                reset: bool, reverted: bool }
//
// A draft is user-owned state with its own lifecycle. It is never rebuilt
// from the applied values while it differs from them: that was the bug that
// made a trial revert (hardware back to the old values) also reset the
// sliders to 0. `reconcileDraft` is the only place that decides whether a
// state refresh replaces the draft.

function clamp(value, lo, hi) {
  return Math.max(lo, Math.min(hi, value))
}

function signed(value) {
  return (value > 0 ? "+" : "") + value
}

// Display form for a control value. Offsets (factory zero) carry their sign;
// an absolute limit such as the GPU thermal target does not ("+83 °C" would
// read as an offset).
function displayValue(control, value) {
  if (!control || control.factory !== 0) return String(value)
  return signed(value)
}

// Fill a translation placeholder only when the catalog actually provided a
// format string; t() returns the bare key before the catalog loads, and
// calling .arg() on a key logs "Argument missing" for every open.
function targ(text, value) {
  return typeof text === "string" && text.indexOf("%1") >= 0 ? text.arg(value) : text
}

// Marketing noise off a model name: "Intel(R) Core(TM) i7-6700 CPU @
// 3.40GHz" reads as "Intel Core i7-6700", "NVIDIA GeForce GTX 1060 6GB" as
// "GeForce GTX 1060 6GB".
function deviceName(raw) {
  if (!raw) return ""
  return String(raw)
    .replace(/\((R|TM|tm|r)\)/g, "")
    .replace(/\s+CPU\s*@.*$/, "")
    .replace(/\s+@\s*[\d.]+\s*GHz$/i, "")
    .replace(/^NVIDIA\s+GeForce\s+/, "GeForce ")
    .replace(/\s+/g, " ")
    .trim()
}

function controlsBykey(state, component) {
  var map = {}
  var list = (state && state.controls && state.controls[component]) || []
  for (var i = 0; i < list.length; i++) map[list[i].key] = list[i]
  return map
}

function curveInfo(state, component) {
  return (state && state.curves && state.curves[component]) || null
}

// Missing keys mean factory: a partial daemon payload must not turn into a
// phantom edit just because one control was not reported.
function appliedValue(state, component, key, control) {
  var applied = (state && state.applied && state.applied[component]) || {}
  return applied[key] !== undefined ? applied[key] : (control ? control.factory : 0)
}

function copyValues(values) {
  var out = {}
  for (var key in values) out[key] = values[key]
  return out
}

function newDraft(state, component) {
  // `anchors` are the V/F handles the user pinned on top of the automatic
  // spread. They are view state: the daemon always receives every point.
  var draft = { values: {}, points: null, anchors: [], reset: false, reverted: false, baseline: null }
  var controls = controlsBykey(state, component)
  for (var key in controls) {
    draft.values[key] = appliedValue(state, component, key, controls[key])
  }
  var curve = curveInfo(state, component)
  if (curve && curve.supported) {
    draft.points = copyPoints(curve.points)
  }
  // The baseline records what the draft looked like when it last matched the
  // applied state. `edited` compares against it, so a state refresh can tell
  // "the user typed this" from "the applied values moved underneath us".
  draft.baseline = {
    values: copyValues(draft.values),
    points: draft.points ? copyPoints(draft.points) : null
  }
  return draft
}

// A draft is a plain JS object that QML cannot observe in place. Every edit
// therefore ends with a shallow copy: the container reassignment is what
// re-evaluates the bindings in TuneControl, TuneSlider and CurveView.
function copyDraft(draft) {
  if (!draft) return draft
  return {
    values: copyValues(draft.values || {}),
    points: draft.points
      ? copyPoints(draft.points)
      : null,
    anchors: (draft.anchors || []).slice(),
    reset: !!draft.reset,
    reverted: !!draft.reverted,
    baseline: draft.baseline ? {
      values: copyValues(draft.baseline.values || {}),
      points: draft.baseline.points
        ? copyPoints(draft.baseline.points)
        : null
    } : null
  }
}

// One tolerance for "the same frequency": the driver table carries half-MHz
// steps, so any difference at or below them is the same point. Voltages stay
// exact; only the frequency axis has that granularity.
var CURVE_EPSILON_MHZ = 0.5

function sameFrequency(a, b) {
  return Math.abs(a - b) <= CURVE_EPSILON_MHZ
}

function samePoints(a, b) {
  if (!a || !b || a.length !== b.length) return false
  for (var i = 0; i < a.length; i++) {
    if (!sameFrequency(a[i].f, b[i].f) || a[i].v !== b[i].v) return false
  }
  return true
}

// True when the user changed the draft since it last matched the applied
// state. This is what makes user work survive a state refresh, including a
// revert that moves the applied baseline back to the old values.
function edited(draft) {
  if (!draft || !draft.baseline) return false
  for (var key in draft.values) {
    if (draft.values[key] !== draft.baseline.values[key]) return true
  }
  if (draft.points && draft.baseline.points && !samePoints(draft.points, draft.baseline.points)) return true
  return false
}

// True when the draft carries a value (or curve point) the hardware is not
// running. Values are compared against the applied snapshot, the source of
// truth on every open and reconnect.
function dirty(draft, state, component) {
  if (!draft || !state) return false
  var controls = controlsBykey(state, component)
  for (var key in draft.values) {
    var control = controls[key]
    if (!control) continue
    if (draft.values[key] !== appliedValue(state, component, key, control)) return true
  }
  var curve = curveInfo(state, component)
  if (draft.points && curve && curve.supported && curve.writable !== false) {
    if (!samePoints(draft.points, curve.points)) return true
  }
  return false
}

// A draft rebuilt from the applied state, carrying the handles the user
// pinned. Rebuilding must not undo the shape of the editor.
function adoptDraft(draft, state, component) {
  var fresh = newDraft(state, component)
  if (draft) fresh.anchors = sanitizeAnchors(draft.anchors, fresh.points)
  return fresh
}

// Whether `settings` (a trial candidate) reproduces this draft. The candidate
// may be partial, so only its keys are compared.
function matches(draft, settings, state, component) {
  if (!draft || !settings) return false
  var controls = controlsBykey(state, component)
  for (var key in settings) {
    if (key === "points") {
      if (!samePoints(draft.points, settings.points)) return false
      continue
    }
    if (!controls[key]) continue
    if (draft.values[key] !== settings[key]) return false
  }
  return true
}

function lastRevertedFor(state, component) {
  var info = state && state.lastReverted
  return info && info.component === component ? info : null
}

function isReverted(draft, state, component) {
  var info = lastRevertedFor(state, component)
  return !!info && matches(draft, info.candidate, state, component)
}

// Reconcile a draft with a fresh daemon state.
//
// - A dirty draft is the user's work: keep it untouched, and mark it as the
//   rolled-back candidate when the daemon reports that revert.
// - A clean draft simply follows the applied values (confirm, reconnect,
//   external change), so what the popup shows is what the machine runs.
function reconcileDraft(draft, state, component) {
  if (!state) return draft
  if (!draft) return newDraft(state, component)
  // While a trial runs, the daemon reports the candidate as applied. Rebuilding
  // the draft from that would erase the very difference the trial is testing,
  // and a deadline/Esc revert would then silently reset the user's draft to the
  // old values. Keep the draft untouched until the trial ends; confirm and
  // revert are reconciled by the next state push after it does.
  if (state.trial && state.trial.component === component) return draft
  // The draft matches what is applied (a confirm, a reconnect, or someone
  // else changed the machine): adopt the new baseline. The pinned handles are
  // view state and survive it.
  if (!dirty(draft, state, component)) return adoptDraft(draft, state, component)
  // The draft differs from applied. Keep it only when the user is the one
  // who made it differ; a stale unedited draft follows the hardware.
  if (!edited(draft)) return adoptDraft(draft, state, component)
  draft.reverted = isReverted(draft, state, component)
  return draft
}

// Status key for the header and footer: offline | trial | suspended | paused |
// unavailable | reverted | draft | applied.
function draftStatus(draft, state, component) {
  if (!state) return "offline"
  if (state.trial && state.trial.component === component) return "trial"
  if (state.suspended && state.suspended[component]) return "suspended"
  if (state.paused && state.paused[component]) return "paused"
  var controls = controlsBykey(state, component)
  var any = false
  var available = false
  for (var key in controls) {
    any = true
    if (controls[key].available) available = true
  }
  if (any && !available) return "unavailable"
  if (!draft) return "applied"
  if (dirty(draft, state, component)) {
    return isReverted(draft, state, component) ? "reverted" : "draft"
  }
  return "applied"
}

function serializeDraft(draft) {
  if (!draft) return null
  return {
    values: draft.values,
    points: draft.points ? copyPoints(draft.points) : null,
    anchors: (draft.anchors || []).slice(),
    reset: !!draft.reset
  }
}

// Restore a persisted draft, clamped to the ranges the daemon reports today
// (a normal-mode session must not resurrect an expert-only value).
function restoreDraft(raw, state, component) {
  var draft = newDraft(state, component)
  if (!raw || typeof raw !== "object") return draft
  var controls = controlsBykey(state, component)
  if (raw.values && typeof raw.values === "object") {
    for (var key in controls) {
      var value = raw.values[key]
      if (typeof value === "number" && isFinite(value)) {
        draft.values[key] = clamp(Math.round(value), controls[key].min, controls[key].max)
      }
    }
  }
  var curve = curveInfo(state, component)
  if (curve && curve.supported && raw.points && raw.points.length === curve.points.length) {
    var points = []
    var stale = false
    for (var i = 0; i < raw.points.length; i++) {
      var point = raw.points[i]
      if (!point || typeof point.f !== "number" || !isFinite(point.f)) { points = null; break }
      // A draft written before the driver's frequency scale was corrected
      // carries raw (doubled) values. Clamping those would produce a flat
      // line at maxF; the live curve is the honest fallback instead.
      if (point.f > curve.maxF) { stale = true; break }
      points.push({ v: curve.points[i].v, f: clamp(Number(point.f), curve.minF, curve.maxF) })
    }
    if (points && !stale) draft.points = enforceCurve(points, curve)
  }
  // Pinned anchors address table indexes; a driver change can shorten the
  // table, so anything out of range is dropped instead of trusted.
  draft.anchors = sanitizeAnchors(raw.anchors, draft.points)
  draft.reset = !!raw.reset
  return draft
}

// Pins are table indexes: whole numbers inside the editable part of the curve.
function sanitizeAnchors(raw, points) {
  if (!raw || !raw.length || !points || !points.length) return []
  var last = plateauStart(points)
  var out = []
  for (var i = 0; i < raw.length; i++) {
    var index = Number(raw[i])
    if (!isFinite(index)) continue
    index = Math.round(index)
    if (index < 0 || index >= points.length) continue
    if (last >= 0 && index > last) continue
    if (out.indexOf(index) >= 0) continue
    out.push(index)
  }
  return out.sort(function (a, b) { return a - b })
}

// The handles the plot draws: the automatic spread plus the user's pins, with
// the plateau and the table bounds still respected.
function curveAnchors(points, auto, pins) {
  return sanitizeAnchors((auto || []).concat(pins || []), points)
}

// The table point nearest to a voltage, which is what a double-click in the
// V/F plot adds.
function nearestPointIndex(points, voltage) {
  if (!points || !points.length) return -1
  var best = 0
  var bestDistance = Infinity
  for (var i = 0; i < points.length; i++) {
    var distance = Math.abs(points[i].v - Number(voltage))
    if (distance < bestDistance) { bestDistance = distance; best = i }
  }
  var last = plateauStart(points)
  if (last >= 0 && best > last) best = last
  return best
}

// The table point halfway (by voltage) between one anchor and the next, used
// by the keyboard "add a point" so it lands between two handles.
function anchorInsertIndex(points, anchors, selected) {
  if (!points || !points.length) return -1
  var last = plateauStart(points)
  var end = last >= 0 ? last : points.length - 1
  var list = sanitizeAnchors(anchors, points)
  var position = list.indexOf(selected)
  var left = position >= 0 ? selected : 0
  var right = position >= 0 && position < list.length - 1 ? list[position + 1] : end
  if (right <= left) {
    // The selected handle is the last one: step toward the plateau instead.
    right = Math.min(end, left + Math.max(2, Math.round((end - left) / 2)))
  }
  if (right <= left) return -1
  var middle = (points[left].v + points[right].v) / 2
  var index = nearestPointIndex(points, middle)
  if (index <= left || index >= right) index = Math.round((left + right) / 2)
  if (index <= left || index >= right) return -1
  return index
}

// Removing a pin only removes the handle; the points keep their values.
function withoutAnchor(pins, index) {
  return (pins || []).filter(function (pin) { return Math.round(Number(pin)) !== index })
}

// ---------------------------------------------------------- aggressive zone

// Percent of the factory reference by which `value` goes beyond stock in the
// risk direction. Direction "both" (CPU voltage) counts either way. This is
// display-only now: the slider is never blocked, and the note is inline.
// Text for a StyledText label: the daemon's labels are data, not markup.
function escapeMarkup(text) {
  return String(text).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
}

function excess(control, value) {
  var factory = control.factory || 0
  var delta = value - factory
  if (control.risk.direction === "both") delta = Math.abs(delta)
  else if (delta < 0) delta = 0
  if (!control.risk.ref) return 0
  return (delta / control.risk.ref) * 100
}

// Whether one control in the draft differs from what the machine runs. The
// row shows "was <applied>" only for these controls, and the line restores
// exactly this value.
function controlDiffers(draft, state, component, key, control) {
  if (!draft || !state || draft.values[key] === undefined) return false
  return draft.values[key] !== appliedValue(state, component, key, control)
}

// True when the machine runs factory values everywhere. The footer keeps the
// "restore factory" action reachable while anything else is applied.
function appliedIsFactory(state, component) {
  var controls = controlsBykey(state, component)
  for (var key in controls) {
    if (!controls[key].available) continue
    if (appliedValue(state, component, key, controls[key]) !== controls[key].factory) return false
  }
  return !curveIsCustom(curveInfo(state, component))
}

// True when any draft point sits above the curve's aggressive limit.
function curveExceeds(points, curve, coreControl) {
  var bounds = curveRiskBounds(curve, coreControl)
  if (!bounds || !points || points.length !== bounds.edges.length) return false
  for (var i = 0; i < points.length; i++) {
    if (points[i].f > bounds.edges[i].f + 1e-6) return true
  }
  return false
}

// True when the machine runs a V/F curve other than the factory one. A
// curve applied with the sliders at stock still counts as tuned.
function curveIsCustom(curve) {
  if (!curve || !curve.supported || !curve.points || !curve.factoryPoints) return false
  if (curve.points.length !== curve.factoryPoints.length) return false
  for (var i = 0; i < curve.points.length; i++) {
    if (!sameFrequency(curve.points[i].f, curve.factoryPoints[i].f)) return true
  }
  return false
}

// ---------------------------------------------------------------- curve math

// The editable range of a real V/F table stops where the driver's maximum
// frequency becomes a plateau: the last points repeat the same frequency at
// rising voltage, and moving them apart would address nothing. Returns the
// index of the first point that reaches the final frequency (the last useful
// anchor, which caps the plateau); -1 without points.
function plateauStart(points) {
  if (!points || !points.length) return -1
  var last = points[points.length - 1].f
  for (var i = 0; i < points.length; i++) {
    if (points[i].f >= last - 1e-9) return i
  }
  return points.length - 1
}

// Mean frequency change of the draft against the factory curve, in real MHz,
// across the editable range only. The plateau points repeat the maximum
// frequency and are not separately editable, so counting them would dilute
// the figure the user sees beside "curve".
function curveAverageDelta(points, factoryPoints) {
  if (!points || !factoryPoints || !points.length) return 0
  if (points.length !== factoryPoints.length) return 0
  var end = plateauStart(factoryPoints)
  if (end < 0) end = points.length - 1
  var count = end + 1
  if (count < 1) return 0
  var sum = 0
  for (var i = 0; i < count; i++) sum += points[i].f - factoryPoints[i].f
  return Math.round(sum / count)
}

// The aggressive zone of a curve: the band above the factory line on the
// frequency axis, from the factory frequency to factory * (1 + RISK_PERCENT),
// capped by the plot top. Each edge shares its voltage with the factory point,
// so the band hugs the curve in the swapped frame.
function curveRiskBounds(curve, coreControl) {
  if (!curve || !curve.factoryPoints || !curve.factoryPoints.length) return null
  var percent = coreControl && coreControl.risk ? Number(coreControl.risk.percent) : 0
  if (!isFinite(percent) || percent <= 0) return null
  var factor = 1 + percent / 100
  var domain = curveDomain(curve, coreControl)
  var edges = []
  for (var i = 0; i < curve.factoryPoints.length; i++) {
    var point = curve.factoryPoints[i]
    edges.push({ v: point.v, f: Math.min(domain.maxF, point.f * factor) })
  }
  // A band whose boundary never leaves the factory line has nothing to tint.
  if (!edges.some(function (edge, index) {
      return edge.f > curve.factoryPoints[index].f + 1e-6
  })) return null
  return { edges: edges, factor: factor, topF: domain.maxF }
}

// The plot domain actually worth showing. The descriptor's minF/maxF span
// the driver's writable window, which can be far wider than the data (78 MHz
// to 4096 MHz on the GTX 1060), so the axis was mostly empty. The horizontal
// axis is voltage, just beyond the point range by 4% each side. The vertical
// axis is frequency, from just below the lowest factory frequency to the
// factory maximum plus the largest offset the top anchor may take. Three
// rounded ticks label each axis without implying more precision than the
// data has.
function curveDomain(curve, coreControl) {
  var factory = curve && curve.factoryPoints && curve.factoryPoints.length
    ? curve.factoryPoints
    : (curve && curve.points ? curve.points : [])
  if (!factory.length) {
    return {
      minF: 0,
      maxF: 1,
      voltLo: 0,
      voltHi: 1,
      xTicks: [0, 0.5, 1],
      yTicks: [1, 0.5, 0]
    }
  }
  var fMin = Infinity
  var fMax = -Infinity
  var vMin = Infinity
  var vMax = -Infinity
  for (var i = 0; i < factory.length; i++) {
    fMin = Math.min(fMin, factory[i].f)
    fMax = Math.max(fMax, factory[i].f)
    vMin = Math.min(vMin, factory[i].v)
    vMax = Math.max(vMax, factory[i].v)
  }
  // Controls carry their range as `min`/`max` in the daemon descriptor.
  var top = coreControl ? coreControl.max : undefined
  var headroom = typeof top === "number" ? Math.max(0, top) : Math.round(fMax * 0.15)
  var yLo = Math.max(0, Math.round(fMin * 0.98))
  var yHi = fMax + headroom
  if (!(yHi > yLo)) yHi = yLo + 1
  var vSpan = vMax - vMin
  if (!(vSpan > 0)) vSpan = 0.01
  var xLo = vMin - vSpan * 0.04
  var xHi = vMax + vSpan * 0.04
  return {
    minF: yLo,
    maxF: yHi,
    voltLo: xLo,
    voltHi: xHi,
    xTicks: [xLo, (xLo + xHi) / 2, xHi],
    yTicks: [niceValue(yHi, yHi - yLo), niceValue((yHi + yLo) / 2, yHi - yLo),
      niceValue(yLo, yHi - yLo)]
  }
}

// The three label strings on each axis. Voltage runs along the bottom and
// only the last label carries the unit; frequency runs up the left and only
// the top label carries the unit. Pure so the view and the tests format the
// same way.
function curveAxisLabels(domain) {
  if (!domain) return { x: ["", "", ""], y: ["", "", ""] }
  var volts = domain.xTicks || [domain.voltLo, (domain.voltLo + domain.voltHi) / 2, domain.voltHi]
  var hertz = domain.yTicks || [domain.maxF, (domain.maxF + domain.minF) / 2, domain.minF]
  return {
    x: [
      Number(volts[0]).toFixed(2),
      Number(volts[1]).toFixed(2),
      Number(volts[2]).toFixed(2) + " V"
    ],
    y: [
      String(Math.round(hertz[0])) + " MHz",
      String(Math.round(hertz[1])),
      String(Math.round(hertz[2]))
    ]
  }
}

// Where those labels sit on the canvas. The bottom row is left, centre and
// right aligned inside the plot; the left column is right aligned in the
// gutter, clear of the plot. Keeping the anchors pure lets the tests check
// that no label anchor leaves the widget.
function curveAxisAnchors(plot) {
  var width = plot && plot.width > 0 ? Number(plot.width) : 0
  var height = plot && plot.height > 0 ? Number(plot.height) : 0
  var left = plot && plot.padLeft !== undefined ? Number(plot.padLeft) : 0
  var right = width - (plot && plot.padRight !== undefined ? Number(plot.padRight) : 0)
  var top = plot && plot.padTop !== undefined ? Number(plot.padTop) : 0
  var bottom = height - (plot && plot.padBottom !== undefined ? Number(plot.padBottom) : 0)
  var caption = plot && plot.caption !== undefined ? Number(plot.caption) : 10
  var gap = plot && plot.labelGap !== undefined ? Number(plot.labelGap) : 4
  var inset = plot && plot.bottomInset !== undefined ? Number(plot.bottomInset) : 2
  return {
    x: [
      { x: left, y: height - inset, align: "left" },
      { x: (left + right) / 2, y: height - inset, align: "center" },
      { x: right, y: height - inset, align: "right" }
    ],
    y: [
      // Each frequency label is centred on its grid line.
      { x: left - gap, y: top + caption * 0.35, align: "right" },
      { x: left - gap, y: (top + bottom) / 2 + caption * 0.35, align: "right" },
      { x: left - gap, y: bottom + caption * 0.35, align: "right" }
    ]
  }
}

// Screen mapping for the swapped frame: voltage across, frequency up.
// `plot` carries the canvas size, paddings and the current domain. The
// inverse functions clamp to the drawn plot, so a drag outside it cannot
// address a value the axis does not show.
function curveVoltageToX(voltage, plot) {
  var span = Math.max(1, plot.width - plot.padLeft - plot.padRight)
  var range = Math.max(1e-6, plot.voltHi - plot.voltLo)
  return plot.padLeft + ((voltage - plot.voltLo) / range) * span
}

function curveFrequencyToY(frequency, plot) {
  var span = Math.max(1, plot.height - plot.padTop - plot.padBottom)
  var range = Math.max(1, plot.maxF - plot.minF)
  return plot.padTop + (1 - (frequency - plot.minF) / range) * span
}

function curveXToVoltage(x, plot) {
  var span = Math.max(1, plot.width - plot.padLeft - plot.padRight)
  var fraction = clamp((x - plot.padLeft) / span, 0, 1)
  return plot.voltLo + fraction * (plot.voltHi - plot.voltLo)
}

function curveYToFrequency(y, plot) {
  var span = Math.max(1, plot.height - plot.padTop - plot.padBottom)
  var bottom = plot.height - plot.padBottom
  var fraction = clamp((bottom - y) / span, 0, 1)
  return Math.round(plot.minF + fraction * (plot.maxF - plot.minF))
}

// Round axis labels to a step an order of magnitude below the span, so the
// three numbers read as 150 / 1200 / 2200 rather than 136.22 / 1167.4 / 2198.
function niceValue(value, span) {
  if (!isFinite(value) || !isFinite(span) || span <= 0) return Math.round(value)
  var step = Math.pow(10, Math.floor(Math.log10(span)) - 2)
  if (step < 1) step = 1
  return Math.round(value / step) * step
}

// One keyboard step on a selected curve point: 1 MHz normally, 10 with Shift.
// `bounds` is the plot domain when it is narrower than the daemon range, so a
// key press can never walk the readout off the drawn plot.
function stepFrequency(curve, value, direction, shift, bounds) {
  if (!curve) return value
  var lo = Math.round(bounds && bounds.minF !== undefined ? bounds.minF : curve.minF)
  var hi = Math.round(bounds && bounds.maxF !== undefined ? bounds.maxF : curve.maxF)
  var step = shift ? 10 : 1
  return clamp(Math.round(value + direction * step), lo, hi)
}

// A frequency typed for one point. Same whole-number rule as the sliders'
// exact entry, clamped to the range the daemon accepts for a point (and to
// the plot domain when one is supplied).
function parseFrequency(curve, text, bounds) {
  if (!curve) return null
  var raw = String(text === undefined || text === null ? "" : text)
    .replace(/[\u2212\u2013\u2014]/g, "-")
    .replace(/\s+/g, "")
  if (!/^[+-]?\d+$/.test(raw)) return null
  var lo = bounds && bounds.minF !== undefined ? bounds.minF : curve.minF
  var hi = bounds && bounds.maxF !== undefined ? bounds.maxF : curve.maxF
  return Math.round(clamp(parseInt(raw, 10), lo, hi))
}

// Point voltages carry five decimals in the driver table; three are enough
// to identify a point without a wall of digits.
function formatVoltage(value) {
  var rounded = Math.round(Number(value) * 1000) / 1000
  return String(rounded)
}

// Apply a slider move to a draft. Always clamps to the control range; never
// blocks on the aggressive zone (that is an inline note, not a modal).
function sliderChange(draft, control, wanted, state, component) {
  // The global offset and the per-point V/F curve are independent controls,
  // exactly as the driver keeps them. The slider must not also shift the
  // curve: doing both would apply the offset twice on drivers that stack.
  var value = clamp(Math.round(wanted), control.min, control.max)
  return { value: value, points: draft.points }
}

// Exact value typed by the user. Anything that is not a whole number is not
// a value (null), and a number outside the daemon-reported range is clamped
// to it rather than silently dropped.
function parseExact(control, text) {
  if (!control) return null
  var raw = String(text === undefined || text === null ? "" : text)
    .replace(/[\u2212\u2013\u2014]/g, "-")
    .replace(/\s+/g, "")
  if (!/^[+-]?\d+$/.test(raw)) return null
  return clamp(parseInt(raw, 10), control.min, control.max)
}

// One arrow-key step on a focused slider: one step normally, ten with Shift.
function stepValue(control, value, direction, shift) {
  if (!control) return value
  var step = Math.max(1, control.step || 1) * (shift ? 10 : 1)
  return clamp(Math.round(value + direction * step), control.min, control.max)
}

// The daemon mode a footer apply uses. The "keep after restart" checkbox is
// the only switch: unchecked runs this session, checked also saves the values
// for the next boot. A factory draft is a reset, which the daemon applies
// without a trial.
function applyMode(draft, keepAfterReboot) {
  if (draft && draft.reset) return { mode: "reset", saveDefault: !!keepAfterReboot }
  return { mode: keepAfterReboot ? "default" : "session", saveDefault: false }
}

// ------------------------------------------------------------------ anchors
//
// A driver V/F table is dense (the GTX 1060 exposes about 80 points). Drawing
// and grabbing one handle per point makes the editor unusable, so the plot
// shows a handful of UI-only anchors spread evenly by point index, always
// including the first and last point. Dragging an anchor bends the real
// points around it with a raised-cosine falloff.

// Five to seven anchors, chosen from the plot width. 320 px is the common
// panel row and lands on six.
function anchorCount(width) {
  var w = Number(width)
  if (!isFinite(w) || w < 200) return 5
  if (w >= 420) return 7
  return 6
}

// Pixel position of every point inside the plot, used to keep anchor handles
// from stacking. Voltage is X and frequency is Y, as drawn. `plot` carries the
// drawn geometry; without it the points' own ranges are normalised to a
// square, which is enough for a pure ordering.
function anchorPositions(points, plot) {
  var width = plot && plot.width > 0 ? Number(plot.width) : 100
  var height = plot && plot.height > 0 ? Number(plot.height) : 100
  var xMin
  var xMax
  var yMin
  var yMax
  if (plot && plot.xMin !== undefined && plot.xMax > plot.xMin
      && plot.yMin !== undefined && plot.yMax > plot.yMin) {
    xMin = Number(plot.xMin)
    xMax = Number(plot.xMax)
    yMin = Number(plot.yMin)
    yMax = Number(plot.yMax)
  } else {
    xMin = Infinity
    xMax = -Infinity
    yMin = Infinity
    yMax = -Infinity
    for (var i = 0; i < points.length; i++) {
      xMin = Math.min(xMin, points[i].v)
      xMax = Math.max(xMax, points[i].v)
      yMin = Math.min(yMin, points[i].f)
      yMax = Math.max(yMax, points[i].f)
    }
    if (!(xMax > xMin)) xMax = xMin + 1
    if (!(yMax > yMin)) yMax = yMin + 1
  }
  var x = []
  var y = []
  for (var j = 0; j < points.length; j++) {
    x.push(((points[j].v - xMin) / (xMax - xMin)) * width)
    y.push(height - ((points[j].f - yMin) / (yMax - yMin)) * height)
  }
  return {
    x: x,
    y: y,
    distance: function (a, b) {
      var dx = x[a] - x[b]
      var dy = y[a] - y[b]
      return Math.sqrt(dx * dx + dy * dy)
    }
  }
}

// Point indexes of the anchors. The plateau at the driver's maximum
// frequency is not editable (its points share one frequency), so anchors are
// spread along the horizontal voltage axis over the useful range only: the
// first point, and the first point that reaches the final frequency, which
// caps the plateau. Handles are kept at least 36 px apart in X. The voltage
// range can be too narrow for the wanted count; then fewer anchors are used
// rather than stacking handles.
function anchorIndices(points, anchors, plot) {
  if (!points || !points.length) return []
  var count = points.length
  var wanted = Math.round(Number(anchors))
  var n = clamp(isFinite(wanted) && wanted > 0 ? wanted : 6, 1, count)
  if (n === 1) return [0]
  var last = plateauStart(points)
  // Never spend anchors on the flat plateau: if the useful range has fewer
  // points than the wanted count, use fewer handles.
  if (last + 1 < n) n = last + 1
  if (n < 2) return [0]
  // A bare model call asks for an even index spread; the pixel rule only
  // means something with the real plot geometry.
  if (!plot) {
    var even = []
    for (var e = 0; e < n; e++) even.push(Math.round((e * last) / (n - 1)))
    return even
  }
  var positions = anchorPositions(points, plot)
  var xs = positions.x
  var minGap = plot.minGap ? Number(plot.minGap) : 36
  if (!(minGap > 0)) minGap = 0
  var usefulSpan = xs[last] - xs[0]
  if (minGap > 0) {
    var room = Math.floor(usefulSpan / minGap) + 1
    if (room < n) n = room
  }
  if (n < 2) return [0]
  for (var tryCount = n; tryCount >= 2; tryCount--) {
    var picked = spreadByVoltage(xs, last, tryCount, minGap)
    if (picked) return picked
  }
  return [0]
}

// Pick `n` indexes from 0..last whose X (voltage) positions are as close as
// possible to an even spread and never closer than `minGap`. Returns null
// when the point spacing cannot satisfy the gap.
function spreadByVoltage(xs, last, n, minGap) {
  var first = xs[0]
  var end = xs[last]
  var out = [0]
  for (var k = 1; k < n - 1; k++) {
    var remaining = n - 1 - k
    var target = first + ((end - first) * k) / (n - 1)
    var lo = out[out.length - 1] + 1
    var hi = last - remaining
    var previousX = xs[out[out.length - 1]]
    var best = -1
    var bestDistance = Infinity
    for (var i = lo; i <= hi; i++) {
      if (xs[i] - previousX < minGap) continue
      // Leave room for every gap still to come, including the one to the
      // fixed last anchor.
      if (end - xs[i] < remaining * minGap) continue
      var distance = Math.abs(xs[i] - target)
      if (distance < bestDistance) {
        best = i
        bestDistance = distance
      }
    }
    if (best < 0) return null
    out.push(best)
  }
  if (end - xs[out[out.length - 1]] < minGap) return null
  out.push(last)
  return out
}

// The anchor whose handle is nearest in X to `voltage`, i.e. what a press
// anywhere in the plot grabs. Returns the point index, or -1.
function nearestAnchor(anchors, points, voltage) {
  if (!anchors || !anchors.length || !points || !points.length) return -1
  var best = -1
  var bestDistance = Infinity
  for (var i = 0; i < anchors.length; i++) {
    var point = points[anchors[i]]
    if (!point) continue
    var distance = Math.abs(point.v - voltage)
    if (distance < bestDistance) {
      best = anchors[i]
      bestDistance = distance
    }
  }
  return best
}

// The anchor closest in point index. A reflow changes the anchor count, and
// this keeps the selection near the same voltage instead of jumping.
function anchorNear(anchors, index) {
  if (!anchors || !anchors.length) return -1
  var best = anchors[0]
  for (var i = 1; i < anchors.length; i++) {
    if (Math.abs(anchors[i] - index) < Math.abs(best - index)) best = anchors[i]
  }
  return best
}

// The next anchor by index, clamped at the ends. direction -1 is toward the
// lower-voltage neighbour.
function neighbourAnchor(anchors, index, direction) {
  if (!anchors || !anchors.length) return -1
  var pos = anchors.indexOf(index)
  if (pos < 0) return direction < 0 ? anchors[anchors.length - 1] : anchors[0]
  pos = clamp(pos + (direction < 0 ? -1 : 1), 0, anchors.length - 1)
  return anchors[pos]
}

// Raised-cosine weight of point `i` inside the anchor's window: 1 at the
// anchor itself, 0 at each neighbouring anchor, smooth in between. left and
// right are point indexes, or -1 at the ends of the curve.
function anchorWeight(index, left, right, i) {
  if (i === index) return 1
  if (i < index) {
    if (left < 0) return 0
    var leftSpan = index - left
    if (leftSpan <= 0) return 0
    return 0.5 * (1 - Math.cos(Math.PI * (i - left) / leftSpan))
  }
  if (right < 0) return 0
  var rightSpan = right - index
  if (rightSpan <= 0) return 0
  return 0.5 * (1 - Math.cos(Math.PI * (right - i) / rightSpan))
}

function copyPoints(points) {
  return points.map(function (p) { return { v: p.v, f: p.f } })
}

// Smooth path through the edge-lying result. The plot used midpoint
// quadratics over the raw table, and wherever consecutive points share a
// frequency or voltage (the plateau, and the quarter-MHz duplicates near the
// top) the control points collapsed into visible staircase steps. This walks
// the de-duplicated points with a Fritsch-Carlson monotone cubic instead: the
// curve is monotone in both axes, the slope is continuous, and flat runs stay
// flat. Returns sampled {v, f} points for the canvas.
function smoothCurve(points, samples) {
  if (!points || points.length < 3) return copyPoints(points || [])
  var pts = []
  for (var p = 0; p < points.length; p++) {
    var last = pts[pts.length - 1]
    if (last && last.v === points[p].v && last.f === points[p].f) continue
    pts.push({ v: Number(points[p].v), f: Number(points[p].f) })
  }
  if (pts.length < 3) return pts
  var n = pts.length
  var h = []
  var delta = []
  for (var i = 0; i < n - 1; i++) {
    h.push(pts[i + 1].v - pts[i].v)
    if (!(h[i] > 0)) return pts
    delta.push((pts[i + 1].f - pts[i].f) / h[i])
  }
  function edgeSlope(h0, h1, d0, d1) {
    var slope = ((2 * h0 + h1) * d0 - h0 * d1) / (h0 + h1)
    if (slope * d0 <= 0) return 0
    if (d0 * d1 < 0 && Math.abs(slope) > Math.abs(3 * d0)) return 3 * d0
    return slope
  }
  var m = [edgeSlope(h[0], h[1], delta[0], delta[1])]
  for (var j = 1; j < n - 1; j++) {
    if (delta[j - 1] === 0 || delta[j] === 0 || delta[j - 1] * delta[j] <= 0) {
      m.push(0)
      continue
    }
    var w1 = 2 * h[j] + h[j - 1]
    var w2 = h[j] + 2 * h[j - 1]
    m.push((w1 + w2) / (w1 / delta[j - 1] + w2 / delta[j]))
  }
  m.push(edgeSlope(h[n - 2], h[n - 3], delta[n - 2], delta[n - 3]))
  var per = samples && samples > 0 ? Math.round(samples) : 4
  var out = [{ v: pts[0].v, f: pts[0].f }]
  for (var k = 0; k < n - 1; k++) {
    for (var s = 1; s <= per; s++) {
      var t = s / per
      var t2 = t * t
      var t3 = t2 * t
      var h00 = 2 * t3 - 3 * t2 + 1
      var h10 = t3 - 2 * t2 + t
      var h01 = -2 * t3 + 3 * t2
      var h11 = t3 - t2
      out.push({
        v: pts[k].v + t * h[k],
        f: h00 * pts[k].f + h10 * h[k] * m[k]
          + h01 * pts[k + 1].f + h11 * h[k] * m[k + 1]
      })
    }
  }
  return out
}

// Clamp to a whole MHz inside [lower, upper] when that interval holds one;
// a fractional bound leaves the clamp as it is.
function clampFrequency(value, lower, upper) {
  var wholeLo = Math.ceil(lower)
  var wholeHi = Math.floor(upper)
  if (wholeLo <= wholeHi) return clamp(Math.round(value), wholeLo, wholeHi)
  return clamp(value, lower, upper)
}

// Move one anchor to `frequency` and bend the real points around it with the
// raised-cosine falloff. The anchor never crosses either neighbouring anchor,
// and every point is rounded, kept inside the descriptor range and kept
// non-decreasing with voltage, which is exactly what the daemon validates.
function anchorMove(points, anchors, index, frequency, curve) {
  var next = copyPoints(points || [])
  if (!points || !points.length || !anchors || anchors.indexOf(index) < 0) return next
  if (!isFinite(Number(frequency))) return next
  var minF = curve && curve.minF !== undefined ? curve.minF : -Infinity
  var maxF = curve && curve.maxF !== undefined ? curve.maxF : Infinity
  // The plot's left edge is just below the factory floor; a drag must stay
  // inside it even when the daemon would accept a lower frequency.
  if (curve && curve.factoryPoints && curve.factoryPoints.length) {
    minF = Math.max(minF, Math.round(curve.factoryPoints[0].f * 0.98))
  }
  var pos = anchors.indexOf(index)
  var left = pos > 0 ? anchors[pos - 1] : -1
  var right = pos < anchors.length - 1 ? anchors[pos + 1] : -1
  var lo = left >= 0 ? left + 1 : 0
  var hi = right >= 0 ? right - 1 : points.length - 1
  var lower = left >= 0 ? Math.max(minF, points[left].f) : minF
  var upper = right >= 0 ? Math.min(maxF, points[right].f) : maxF
  if (lower > upper) lower = upper
  var target = clampFrequency(Number(frequency), lower, upper)
  var delta = target - points[index].f
  var prev = lo > 0 ? points[lo - 1].f : -Infinity
  // The last useful anchor owns the plateau: every point above it repeats
  // the same frequency, so it moves them with it and caps the plateau at the
  // anchor's value instead of leaving a step behind.
  var capsPlateau = right < 0 && index === plateauStart(points)
  for (var i = lo; i <= hi; i++) {
    var weight = capsPlateau && i > index
      ? 1
      : anchorWeight(index, left, right, i)
    var moved = Math.round(points[i].f + weight * delta)
    // Forward pass: never below the point before it, never past the right
    // neighbour or the descriptor range. A whole MHz is written whenever the
    // interval between those bounds holds one.
    var floor = Math.max(minF, prev)
    var f = clampFrequency(Math.max(moved, floor), floor, upper)
    next[i].f = f
    prev = f
  }
  return next
}

// Shift semantics: cap every point above the grabbed anchor at its frequency.
// A target below the point before the anchor would invert the curve, so it is
// raised to that point instead.
function flattenPoints(points, index, frequency, curve) {
  var next = copyPoints(points || [])
  if (!points || !points.length || !isFinite(Number(frequency))) return next
  var minF = curve && curve.minF !== undefined ? curve.minF : -Infinity
  var maxF = curve && curve.maxF !== undefined ? curve.maxF : Infinity
  if (curve && curve.factoryPoints && curve.factoryPoints.length) {
    minF = Math.max(minF, Math.round(curve.factoryPoints[0].f * 0.98))
  }
  var lower = index > 0 ? Math.max(minF, points[index - 1].f) : minF
  var f = clampFrequency(Number(frequency), Math.min(lower, maxF), maxF)
  for (var i = index; i < next.length; i++) next[i].f = f
  return next
}

// A copy of the factory curve, for "reset curve" and the factory stage.
function factoryCurve(curve) {
  if (!curve || !curve.factoryPoints) return []
  return copyPoints(curve.factoryPoints)
}

// One final pass over the whole curve. Points the user did not touch keep
// their exact value (factory tables carry quarter-MHz steps), but the range
// and the non-decreasing rule hold everywhere, including a draft restored
// from an older build.
function enforceCurve(points, curve) {
  var minF = curve && curve.minF !== undefined ? curve.minF : -Infinity
  var maxF = curve && curve.maxF !== undefined ? curve.maxF : Infinity
  var out = []
  var prev = -Infinity
  for (var i = 0; i < points.length; i++) {
    var f = Number(points[i].f)
    if (!isFinite(f)) f = isFinite(prev) ? prev : minF
    f = clamp(f, minF, maxF)
    if (f < prev) f = prev
    out.push({ v: points[i].v, f: f })
    prev = f
  }
  return out
}

// Apply one curve edit to a draft. `anchors` is the UI list the plot shows;
// when absent the default six are used, so every edit stays anchor-based.
// flatten=true levels every point above the grabbed anchor at the same
// frequency, preserving lower points.
function pointChange(draft, index, frequency, flatten, state, component, anchors) {
  var curve = curveInfo(state, component)
  if (!curve || !draft.points || curve.writable === false) return { points: draft.points }
  if (flatten) {
    return { points: enforceCurve(flattenPoints(draft.points, index, frequency, curve), curve) }
  }
  var list = anchors && anchors.length ? anchors : anchorIndices(draft.points, 6)
  return { points: enforceCurve(anchorMove(draft.points, list, index, frequency, curve), curve) }
}

// Draft settings payload sent to the daemon on apply. Controls the backend
// reports unavailable are omitted: the daemon rejects an unavailable key,
// and the GPU thermal limit is exactly that on driver 580 (getter, no
// setter). The curve is only sent when the driver supports it.
function applyPayload(draft, component, state) {
  var controls = controlsBykey(state, component)
  var payload = {}
  for (var key in draft.values) {
    if (state && controls[key] && !controls[key].available) continue
    payload[key] = draft.values[key]
  }
  var curve = curveInfo(state, component)
  if (draft.points && (!state || (curve && curve.supported && curve.writable !== false))) {
    payload.points = copyPoints(draft.points)
  }
  return payload
}

// --------------------------------------------------------------- factory

// Stage the factory values and curve; nothing is applied until Apply.
function stageFactory(draft, state, component) {
  if (!draft || !state) return draft
  var controls = controlsBykey(state, component)
  var curve = curveInfo(state, component)
  for (var key in controls) draft.values[key] = controls[key].factory
  if (curve && curve.supported) draft.points = factoryCurve(curve)
  draft.reset = true
  draft.reverted = false
  return draft
}

function countdownSeconds(deadlineMs, nowMs) {
  return Math.max(1, Math.ceil((deadlineMs - nowMs) / 1000))
}

// --------------------------------------------------------------- monitor

// A telemetry reading, or null when the backend has no sensor (missing key,
// null, or a non-number). The Monitor tab shows an em dash for null instead
// of inventing a value.
function monitorValue(telemetry, key) {
  if (!telemetry) return null
  var value = telemetry[key]
  return typeof value === "number" && isFinite(value) ? value : null
}

// Usage bars run 0..100 %; anything outside that range is clamped.
function monitorPercentFraction(value) {
  if (typeof value !== "number" || !isFinite(value)) return 0
  return clamp(value, 0, 100) / 100
}

// The temperature scale is the daemon's thermal ceiling for the component
// (not 100 °C): the same limit after which the tuning side starts reverting.
// A snapshot without ceilings falls back to 100 °C.
function monitorCeiling(thermalCeilings, component) {
  var value = thermalCeilings ? thermalCeilings[component + "C"] : undefined
  return typeof value === "number" && isFinite(value) && value > 0 ? value : 100
}

function monitorTemperatureFraction(tempC, ceilingC) {
  if (typeof tempC !== "number" || !isFinite(tempC)) return 0
  if (typeof ceilingC !== "number" || !isFinite(ceilingC) || ceilingC <= 0) return 0
  return clamp(tempC / ceilingC, 0, 1)
}

// 0 = accent range, 1 = warning (from 75 % of the ceiling), 2 = urgent
// (from 92 %). The Monitor tab maps these to theme colours.
function monitorTemperatureLevel(tempC, ceilingC) {
  var fraction = monitorTemperatureFraction(tempC, ceilingC)
  if (fraction >= 0.92) return 2
  if (fraction >= 0.75) return 1
  return 0
}

// ------------------------------------------------------------------- fan

// Fan is a policy, not a Control: its state lives in state.fans.gpu and its
// draft is separate from the clock draft, so applying one never writes the
// other.
function fanInfo(state) {
  return (state && state.fans && state.fans.gpu) || null
}

function fanConfig(state) {
  var info = fanInfo(state)
  return (info && info.config) || { mode: "auto" }
}

// The daemon enforces a fixed duty or a curve itself; without a loop the
// driver owns the fan. A saved fixed/curve config with loopActive false
// therefore means the policy stopped (the helper went away, or writes were
// refused until the loop gave up): the tab must not show it as applied.
function fanPolicyStopped(info) {
  if (!info || info.loopActive !== false) return false
  var mode = (info.config && info.config.mode) || "auto"
  return mode === "fixed" || mode === "curve"
}

function fanLoopStopped(state) {
  return fanPolicyStopped(fanInfo(state))
}

// A stopped policy whose hand-back to the driver failed (the helper was
// gone): the driver may not own the fan, and NVML can keep the last manual
// duty until something writes it. The banner must not promise auto control
// there; choosing Auto is what asks the driver again.
function fanHandBackFailed(info) {
  if (!fanPolicyStopped(info)) return false
  return !!(info && info.handBackFailed)
}

function copyFanPoints(points) {
  if (!points) return null
  return points.map(function (point) { return { t: point.t, p: point.p } })
}

function fanFactoryPoints(state) {
  var info = fanInfo(state)
  var factory = info && info.curve ? info.curve.factory : null
  return copyFanPoints(factory) || []
}

function clampFanSpeed(info, value) {
  var lo = info && info.curve ? info.curve.fixedMinP : 0
  var hi = info && info.curve ? info.curve.maxP : 100
  return clamp(Math.round(value), lo, hi)
}

function newFanDraft(state) {
  var info = fanInfo(state)
  var config = fanConfig(state)
  var mode = config.mode || "auto"
  var speed = config.mode === "fixed"
    ? config.speed
    : clampFanSpeed(info, info && typeof info.speed === "number" ? info.speed : 50)
  var points = config.mode === "curve" && config.points
    ? copyFanPoints(config.points)
    : fanFactoryPoints(state)
  var draft = { mode: mode, speed: speed, points: points, baseline: null }
  draft.baseline = { mode: mode, speed: speed, points: copyFanPoints(points) }
  return draft
}

function copyFanDraft(draft) {
  if (!draft) return draft
  return {
    mode: draft.mode,
    speed: draft.speed,
    points: copyFanPoints(draft.points),
    baseline: draft.baseline ? {
      mode: draft.baseline.mode,
      speed: draft.baseline.speed,
      points: copyFanPoints(draft.baseline.points)
    } : null
  }
}

function sameFanPoints(a, b) {
  if (!a || !b || a.length !== b.length) return false
  for (var i = 0; i < a.length; i++) {
    if (a[i].t !== b[i].t || a[i].p !== b[i].p) return false
  }
  return true
}

// The mode, and the setting that mode uses: speed for fixed, points for
// curve. Auto compares on the mode alone.
function sameFanConfig(a, b) {
  if (!a || !b) return false
  var aMode = a.mode || "auto"
  var bMode = b.mode || "auto"
  if (aMode !== bMode) return false
  if (aMode === "fixed" && a.speed !== b.speed) return false
  if (aMode === "curve" && !sameFanPoints(a.points, b.points)) return false
  return true
}

function fanEdited(draft) {
  if (!draft || !draft.baseline) return false
  return !sameFanConfig(draft, draft.baseline)
}

function fanDirty(draft, state) {
  if (!draft || !state) return false
  return !sameFanConfig(draft, fanConfig(state))
}

function fanPayload(draft) {
  if (!draft) return null
  if (draft.mode === "fixed") return { mode: "fixed", speed: draft.speed }
  if (draft.mode === "curve") {
    return {
      mode: "curve",
      points: copyFanPoints(draft.points) || []
    }
  }
  return { mode: "auto" }
}

// The daemon's lastReverted record carries the fan candidate under `fan`.
function fanReverted(draft, state) {
  var info = state && state.lastReverted
  if (!info || info.component !== "gpu" || !draft) return false
  var candidate = info.candidate && info.candidate.fan
  if (!candidate) return false
  return sameFanConfig(draft, candidate)
}

function reconcileFanDraft(draft, state) {
  if (!state) return draft
  if (!draft) return newFanDraft(state)
  if (state.trial && state.trial.component === "gpu") return draft
  if (!fanDirty(draft, state)) return newFanDraft(state)
  if (!fanEdited(draft)) return newFanDraft(state)
  draft.reverted = fanReverted(draft, state)
  return draft
}

// offline | trial | suspended | paused | unavailable | reverted | draft |
// stopped | applied — the same vocabulary the clock tabs use, plus `stopped`
// for a fixed/curve config whose daemon loop is no longer running.
function fanDraftStatus(draft, state) {
  var info = fanInfo(state)
  if (!state) return "offline"
  if (!info) return "unavailable"
  if (state.trial && state.trial.component === "gpu") return "trial"
  if (state.suspended && state.suspended.gpu) return "suspended"
  if (state.paused && state.paused.gpu) return "paused"
  if (!info.supported) return "unavailable"
  if (draft && fanDirty(draft, state)) return fanReverted(draft, state) ? "reverted" : "draft"
  return fanLoopStopped(state) ? "stopped" : "applied"
}

// Apply is allowed for Auto even when the driver refuses writes, because
// returning to the driver is always the safe direction; fixed and curve need
// the driver to accept a write.
function fanCanApply(draft, state) {
  if (!draft || !state) return false
  var info = fanInfo(state)
  if (!info || !info.supported) return false
  if (draft.mode === "auto") return true
  return !!info.writable
}

function fanDutyAt(points, temperatureC) {
  if (!points || points.length === 0) return 0
  var ordered = points.slice().sort(function (a, b) { return a.t - b.t })
  if (temperatureC <= ordered[0].t) return ordered[0].p
  for (var i = 0; i < ordered.length - 1; i++) {
    var lower = ordered[i]
    var higher = ordered[i + 1]
    if (temperatureC <= higher.t) {
      var span = higher.t - lower.t
      if (span <= 0) return higher.p
      var ratio = (temperatureC - lower.t) / span
      return Math.round(lower.p + (higher.p - lower.p) * ratio)
    }
  }
  return ordered[ordered.length - 1].p
}

// ------------------------------------------------------- fan curve points
//
// The curve is not a fixed set of anchors any more: the user places points
// anywhere in the window the daemon validates (rising temperatures, a duty
// that never falls). These helpers are the only place that clamps them, so the
// plot and the draft can never disagree with what the daemon accepts.

function fanWindow(state) {
  var info = fanInfo(state)
  var curve = info && info.curve ? info.curve : null
  function number(value, fallback) {
    return typeof value === "number" && isFinite(value) ? value : fallback
  }
  return {
    minT: number(curve && curve.minT, 30),
    maxT: number(curve && curve.maxT, 90),
    minPoints: number(curve && curve.minPoints, 2),
    maxPoints: number(curve && curve.maxPoints, 12),
    minP: number(curve && curve.minP, 0),
    maxP: number(curve && curve.maxP, 100),
    // Normal mode refuses a hot end that cannot cool the card.
    lastFloor: state && state.tuningMode === "expert" ? 0 : number(curve && curve.lastMinP, 0)
  }
}

function fanSorted(points) {
  return (points || []).slice().sort(function (a, b) { return a.t - b.t })
}

// The canonical list: sorted, inside the window, rising temperatures, a duty
// that never falls, and a hot end at least as warm as the mode demands.
function fanClampPoints(points, state) {
  var window = fanWindow(state)
  var ordered = fanSorted(points)
  var out = []
  var previousSpeed = window.minP
  for (var i = 0; i < ordered.length; i++) {
    var temp = Math.round(Number(ordered[i].t))
    var speed = Math.round(Number(ordered[i].p))
    if (!isFinite(temp) || !isFinite(speed)) continue
    temp = clamp(temp, window.minT, window.maxT)
    if (out.length && temp <= out[out.length - 1].t) temp = out[out.length - 1].t + 1
    if (temp > window.maxT) continue
    speed = clamp(speed, previousSpeed, window.maxP)
    out.push({ t: temp, p: speed })
    previousSpeed = speed
  }
  if (out.length) {
    var last = out[out.length - 1]
    last.p = Math.max(last.p, Math.min(window.maxP, window.lastFloor))
  }
  return out
}

// Move one point on both axes. It stays strictly between its neighbours in
// temperature and between their duties, which is what keeps the curve rising.
function fanMovePoint(points, index, temp, speed, state) {
  var ordered = fanSorted(points)
  if (index < 0 || index >= ordered.length) return ordered
  var window = fanWindow(state)
  var lower = index > 0 ? ordered[index - 1] : null
  var upper = index < ordered.length - 1 ? ordered[index + 1] : null
  var tempLo = lower ? lower.t + 1 : window.minT
  var tempHi = upper ? upper.t - 1 : window.maxT
  var speedLo = lower ? lower.p : window.minP
  var speedHi = upper ? upper.p : window.maxP
  if (index === ordered.length - 1) speedLo = Math.max(speedLo, window.lastFloor)
  var next = Math.round(Number(temp))
  var duty = Math.round(Number(speed))
  if (!isFinite(next) || !isFinite(duty)) return ordered
  next = clamp(next, Math.min(tempLo, tempHi), Math.max(tempLo, tempHi))
  duty = clamp(duty, Math.min(speedLo, speedHi), Math.max(speedLo, speedHi))
  if (next === ordered[index].t && duty === ordered[index].p) return ordered
  var out = ordered.slice()
  out[index] = { t: next, p: duty }
  return out
}

// Insert a point at `tempC`, with the duty the current curve already asks for
// there. Returns null when the list is full or the temperature is taken.
function fanAddPoint(points, tempC, speed, state) {
  var window = fanWindow(state)
  var ordered = fanSorted(points)
  if (ordered.length >= window.maxPoints) return null
  var temp = Math.round(Number(tempC))
  if (!isFinite(temp)) return null
  temp = clamp(temp, window.minT, window.maxT)
  for (var i = 0; i < ordered.length; i++) {
    if (ordered[i].t === temp) return null
  }
  // A negative or missing duty means "take the one the curve already asks
  // for here", which is what the keyboard and the double-click pass.
  var wanted = Number(speed)
  var duty = !isFinite(wanted) || wanted < 0 ? fanDutyAt(ordered, temp) : Math.round(wanted)
  var next = fanClampPoints(ordered.concat([{ t: temp, p: duty }]), state)
  var index = -1
  for (var j = 0; j < next.length; j++) {
    if (next[j].t === temp) index = j
  }
  return index < 0 ? null : { points: next, index: index }
}

// Drop one point. The daemon needs at least `minPoints`, and the curve needs
// two ends to mean anything, so the last removable pair stays.
function fanRemovePoint(points, index, state) {
  var window = fanWindow(state)
  var ordered = fanSorted(points)
  if (index < 0 || index >= ordered.length) return null
  if (ordered.length <= Math.max(2, window.minPoints)) return null
  var out = []
  for (var i = 0; i < ordered.length; i++) {
    if (i !== index) out.push(ordered[i])
  }
  // Dropping the hottest point makes the one below it the hot end, which
  // normal mode still requires warm: fanClampPoints raises it instead of
  // leaving a draft the daemon would refuse.
  return fanClampPoints(out, state)
}

function serializeFanDraft(draft) {
  if (!draft) return null
  return {
    mode: draft.mode,
    speed: draft.speed,
    points: copyFanPoints(draft.points)
  }
}

function restoreFanDraft(raw, state) {
  var draft = newFanDraft(state)
  if (!raw || typeof raw !== "object") return draft
  var info = fanInfo(state)
  var maxP = info && info.curve ? info.curve.maxP : 100
  var minP = info && info.curve ? info.curve.minP : 0
  if (["auto", "fixed", "curve"].indexOf(raw.mode) >= 0) draft.mode = raw.mode
  if (typeof raw.speed === "number" && isFinite(raw.speed)) {
    draft.speed = clampFanSpeed(info, raw.speed)
  }
  // A saved curve may carry up to the daemon's point cap: the file is
  // clamped to the window and the rising rules, never reshaped back onto the
  // factory temperatures.
  var window = fanWindow(state)
  if (raw.points && raw.points.length >= 2 && raw.points.length <= window.maxPoints) {
    var points = []
    var previous = minP
    var valid = true
    for (var i = 0; i < raw.points.length; i++) {
      var point = raw.points[i]
      if (!point || typeof point.t !== "number" || !isFinite(point.t)
          || typeof point.p !== "number" || !isFinite(point.p)) { valid = false; break }
      var temp = clamp(Math.round(point.t), window.minT, window.maxT)
      var speed = clamp(Math.round(point.p), minP, maxP)
      if (speed < previous) { valid = false; break }
      if (points.length && temp <= points[points.length - 1].t) { valid = false; break }
      previous = speed
      points.push({ t: temp, p: speed })
    }
    if (valid && points.length >= window.minPoints) draft.points = fanClampPoints(points, state)
  }
  // The baseline stays what the machine runs, not what the file asked for:
  // that difference is exactly what makes fanEdited() true and preserves the
  // saved draft across a reconnect.
  var applied = newFanDraft(state)
  draft.baseline = {
    mode: applied.mode,
    speed: applied.speed,
    points: copyFanPoints(applied.points)
  }
  return draft
}
