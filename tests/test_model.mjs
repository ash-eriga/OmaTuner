// Headless exercise of ui/ash.omatuner/Model.js. Run: node tests/test_model.mjs
// The .pragma line is QML-only; strip it for node.

import { readFileSync } from "node:fs"
import { inspect, isDeepStrictEqual } from "node:util"

const raw = readFileSync(new URL("../ui/ash.omatuner/Model.js", import.meta.url), "utf8")
const source = raw.replace(/^\.pragma library\s*/m, "")
const names = [...source.matchAll(/^function (\w+)/gm)].map(m => m[1])
const exported = source + `\nexport { ${names.join(", ")} }\n`
const module = await import(`data:text/javascript,${encodeURIComponent(exported)}`)
const M = module

// The model runs in its own module instance, so deep-clone across that realm
// boundary before comparing: isDeepStrictEqual also checks prototypes.
function normalize(value) {
  if (Array.isArray(value)) return value.map(normalize)
  if (value && typeof value === "object") {
    const out = {}
    for (const key of Object.keys(value)) out[key] = normalize(value[key])
    return out
  }
  return value
}

let failures = 0
let count = 0
function check(name, actual, expected) {
  count++
  if (!isDeepStrictEqual(normalize(actual), normalize(expected))) {
    failures++
    console.error(`FAIL ${name}\n  expected ${inspect(expected, { depth: null })}\n  actual   ${inspect(actual, { depth: null })}`)
  }
}

// ---------------------------------------------------------------- fixtures

const cpuCore = { key: "core", label: "CPU core", unit: "mV", min: -250, max: 100, step: 1,
  factory: 0, available: true, cause: null,
  risk: { percent: 10, edge: 120, ref: 1200, refLabel: "stock voltage", direction: "both" } }
const cpuCache = { ...cpuCore, key: "cache", label: "CPU cache" }
const gpuCore = { key: "core", label: "GPU core", unit: "MHz", min: -300, max: 300, step: 1,
  factory: 0, available: true, cause: null,
  risk: { percent: 10, edge: 191, ref: 1911, refLabel: "max boost clock", direction: "up" } }
const gpuMemory = { key: "memory", label: "Memory clock", unit: "MHz", min: -1000, max: 1500, step: 1,
  factory: 0, available: true, cause: null,
  risk: { percent: 10, edge: 400, ref: 4004, refLabel: "memory clock", direction: "up" } }

const factoryPoints = [
  { v: 0.7, f: 1140 }, { v: 0.75, f: 1330 }, { v: 0.8, f: 1510 }, { v: 0.85, f: 1650 },
  { v: 0.9, f: 1770 }, { v: 0.95, f: 1850 }, { v: 1.0, f: 1910 }, { v: 1.05, f: 1970 }, { v: 1.1, f: 2020 },
]
const curve = { supported: true, cause: null, points: factoryPoints.map(p => ({ ...p })),
  factoryPoints: factoryPoints.map(p => ({ ...p })), minF: 1000, maxF: 2250 }

const cpuState = { controls: { cpu: [cpuCore, cpuCache] }, applied: { cpu: { core: 0, cache: 0 } }, curves: {} }
const gpuState = { controls: { gpu: [gpuCore, gpuMemory] }, applied: { gpu: { core: 0, memory: 0 } }, curves: { gpu: curve } }

// ---------------------------------------------------------------- basics

check("clamp", M.clamp(9, 0, 5), 5)
check("signed plus", M.signed(50), "+50")
check("signed minus", M.signed(-50), "-50")
check("signed zero", M.signed(0), "0")

// Offsets carry their sign; an absolute limit does not.
check("display offset up", M.displayValue(gpuCore, 120), "+120")
check("display offset down", M.displayValue(cpuCore, -30), "-30")
check("display absolute", M.displayValue({ ...gpuCore, factory: 83 }, 83), "83")

// ------------------------------------------------------- aggressive zone

check("cpu excess below", M.excess(cpuCore, -50), (50 / 1200) * 100)
check("cpu excess at edge", M.excess(cpuCore, 120), 10)
check("cpu excess beyond", M.excess(cpuCore, -240), 20)
check("gpu excess only up", M.excess(gpuCore, -100), 0)
check("gpu excess up", M.excess(gpuCore, 191), (191 / 1911) * 100)

// ------------------------------------------------------------ slider move
// The aggressive zone is a display note now: the slider always reaches the
// requested value inside the control range.

const cpuDraft = M.newDraft(cpuState, "cpu")
let r = M.sliderChange(cpuDraft, cpuCore, -100, cpuState, "cpu")
check("slider reaches aggressive value", r.value, -100)
r = M.sliderChange(cpuDraft, cpuCore, -999, cpuState, "cpu")
check("slider clamps to range", r.value, -250)
r = M.sliderChange(cpuDraft, cpuCore, 400, cpuState, "cpu")
check("slider clamps max", r.value, 100)

// ------------------------------------------------------------------ curve

const gpuDraft = M.newDraft(gpuState, "gpu")
check("draft has points", gpuDraft.points.length, 9)
check("draft clean not dirty", M.dirty(gpuDraft, gpuState, "gpu"), false)
gpuDraft.values.core = 50
gpuDraft.points = gpuDraft.points.map(p => ({ v: p.v, f: p.f + 50 }))
check("draft dirty after shift", M.dirty(gpuDraft, gpuState, "gpu"), true)

// A half-MHz step is within the driver table's own resolution, so it is the
// same curve. A whole-MHz difference is an edit.
const halfDraft = M.newDraft(gpuState, "gpu")
halfDraft.points[3].f += 0.5
check("a half-MHz draft is not dirty", M.dirty(halfDraft, gpuState, "gpu"), false)
check("a half-MHz curve is not custom",
  M.curveIsCustom({ supported: true, points: halfDraft.points, factoryPoints: curve.factoryPoints }), false)
const oneMhzDraft = M.newDraft(gpuState, "gpu")
oneMhzDraft.points[3].f += 1
check("a one-MHz draft is dirty", M.dirty(oneMhzDraft, gpuState, "gpu"), true)
check("a one-MHz curve is custom",
  M.curveIsCustom({ supported: true, points: oneMhzDraft.points, factoryPoints: curve.factoryPoints }), true)

// Panel.touchDrafts hands QML a cloned draft after every edit. QML cannot
// observe a property change on a plain JS object, so the container must carry
// a different object for the value label, knob and curve bindings to refresh.
const beforeCopy = M.newDraft(gpuState, "gpu")
const copied = M.copyDraft(beforeCopy)
check("copy is a distinct object", copied !== beforeCopy, true)
check("copy keeps values", copied.values, beforeCopy.values)
check("copy keeps points", copied.points, beforeCopy.points)
check("copy clones point objects", copied.points[0] !== beforeCopy.points[0], true)
check("copy keeps the baseline", copied.baseline.values, beforeCopy.baseline.values)
copied.values.core = 99
copied.points[0].f += 10
copied.baseline.values.core = 1
check("mutating the copy leaves values alone", beforeCopy.values.core, 0)
check("mutating the copy leaves points alone", beforeCopy.points[0].f, factoryPoints[0].f)
check("mutating the copy leaves the baseline alone", beforeCopy.baseline.values.core, 0)
check("copying null", M.copyDraft(null), null)

r = M.sliderChange(gpuDraft, gpuCore, 200, gpuState, "gpu")
check("curve slider sets the global offset", r.value, 200)
// The global offset and the per-point curve are separate controls; moving
// the offset slider must not shift the hand-edited points too.
check("curve slider leaves points alone", r.points[0].f, 1190)

// dragging one point into the zone is allowed
r = M.pointChange(gpuDraft, 0, 1300, false, gpuState, "gpu")
check("point drag reaches value", r.points[0].f, 1300)
r = M.pointChange(gpuDraft, 0, 1180, false, gpuState, "gpu")
check("point drag safe", r.points[0].f, 1180)

// shift+drag flattens everything above, preserving below
r = M.pointChange(gpuDraft, 3, 1600, true, gpuState, "gpu")
check("flatten keeps below", r.points[2].f, 1560)
check("flatten levels above", r.points[4].f, 1600)
check("flatten levels top", r.points[8].f, 1600)
// A flatten target below the point before the anchor would invert the curve;
// the backend refuses that, so it is raised to the previous point instead.
r = M.pointChange(gpuDraft, 3, 1500, true, gpuState, "gpu")
check("flatten never inverts the curve", r.points[3].f, 1560)
check("flatten clamped keeps below", r.points[2].f, 1560)

// The inline summary states the mean change of the draft curve.
check("curve average zero", M.curveAverageDelta(factoryPoints, factoryPoints), 0)
check("curve average shift",
  M.curveAverageDelta(factoryPoints.map(p => ({ v: p.v, f: p.f + 50 })), factoryPoints), 50)
check("curve average negative",
  M.curveAverageDelta(factoryPoints.map(p => ({ v: p.v, f: p.f - 25 })), factoryPoints), -25)
check("curve average mismatched is zero", M.curveAverageDelta([{ v: 1, f: 2000 }], factoryPoints), 0)
check("curve average missing is zero", M.curveAverageDelta(null, factoryPoints), 0)

// The aggressive band sits above the factory line on the frequency axis,
// factor 1 + RISK_PERCENT, and is capped by the plot top.
const bounds = M.curveRiskBounds(curve, gpuCore)
const boundsDomain = M.curveDomain(curve, gpuCore)
check("risk band exists", !!bounds, true)
check("risk band factor", bounds.factor, 1.1)
check("risk band top is the plot top", bounds.topF, boundsDomain.maxF)
check("risk band edges", bounds.edges.length, factoryPoints.length)
check("risk band edge value", bounds.edges[0].f, 1140 * 1.1)
check("risk band keeps the factory voltage",
  bounds.edges.every((edge, i) => edge.v === factoryPoints[i].v), true)
check("risk band stays above factory",
  bounds.edges.every((edge, i) => edge.f >= factoryPoints[i].f), true)
check("risk band never leaves the plot",
  bounds.edges.every(edge => edge.f <= bounds.topF), true)
check("risk band absent without a risk percentage",
  M.curveRiskBounds(curve, { ...gpuCore, risk: { ...gpuCore.risk, percent: 0 } }), null)
// A risk ceiling above the plot top is cut there instead of drawn outside.
const cappedBounds = M.curveRiskBounds(curve, { ...gpuCore, risk: { ...gpuCore.risk, percent: 50 } })
check("risk band caps at the plot top",
  cappedBounds.edges.some(edge => edge.f === boundsDomain.maxF), true)
check("capped risk band stays inside the plot",
  cappedBounds.edges.every(edge => edge.f <= boundsDomain.maxF), true)

// Keyboard stepping and typed frequency on a selected point.
check("point step one", M.stepFrequency(curve, 1650, 1, false), 1651)
check("point step back", M.stepFrequency(curve, 1650, -1, false), 1649)
check("point step shift", M.stepFrequency(curve, 1650, 1, true), 1660)
check("point step shift back", M.stepFrequency(curve, 1650, -1, true), 1640)
check("point step clamps low", M.stepFrequency(curve, 1000, -1, true), 1000)
check("point step clamps high", M.stepFrequency(curve, 2250, 1, false), 2250)
check("typed frequency", M.parseFrequency(curve, "1620"), 1620)
check("typed frequency whitespace", M.parseFrequency(curve, " 1500 "), 1500)
check("typed frequency clamps high", M.parseFrequency(curve, "99999"), 2250)
check("typed frequency clamps low", M.parseFrequency(curve, "-5"), 1000)
check("typed frequency garbage", M.parseFrequency(curve, "1.5"), null)
check("typed frequency empty", M.parseFrequency(curve, ""), null)

check("voltage trimmed", M.formatVoltage(0.45), "0.45")
check("voltage rounded", M.formatVoltage(0.54375), "0.544")
check("voltage integer", M.formatVoltage(1), "1")

// ------------------------------------------------------ dense curve anchors

// A GTX 1060 V/F table as the driver reports it: about 80 voltage slots in
// 6.25 mV steps, frequencies following the fake backend's factory curve.
const denseFactory = (() => {
  const base = [
    [0.51875, 405], [0.55, 620], [0.60, 810], [0.65, 980],
    [0.70, 1140], [0.75, 1330], [0.80, 1510], [0.85, 1650],
    [0.90, 1770], [0.95, 1850], [1.00, 1910], [1.05, 1970], [1.10, 2020],
  ]
  const points = []
  for (let i = 0; i < 80; i++) {
    const v = 0.51875 + i * 0.00625
    let f = base[base.length - 1][1]
    for (let k = 0; k < base.length - 1; k++) {
      if (v >= base[k][0] && v <= base[k + 1][0]) {
        const t = (v - base[k][0]) / (base[k + 1][0] - base[k][0])
        f = base[k][1] + t * (base[k + 1][1] - base[k][1])
        break
      }
    }
    points.push({ v: Math.round(v * 100000) / 100000, f: Math.round(f) })
  }
  return points
})()
const denseCurve = {
  supported: true, writable: true, cause: null,
  points: denseFactory.map(p => ({ ...p })),
  factoryPoints: denseFactory.map(p => ({ ...p })),
  minF: 105, maxF: 2320,
}
const denseState = {
  controls: { gpu: [gpuCore, gpuMemory] },
  applied: { gpu: { core: 0, memory: 0 } },
  curves: { gpu: denseCurve },
}
const denseDraft = M.newDraft(denseState, "gpu")
const monotonic = points => points.every((p, i) => i === 0 || p.f >= points[i - 1].f)
const integers = points => points.every(p => Number.isInteger(p.f))
const edit = (index, frequency, flatten = false, anchors = denseAnchors) =>
  M.pointChange(denseDraft, index, frequency, flatten, denseState, "gpu", anchors).points

const denseAnchors = M.anchorIndices(denseFactory, 6)
check("anchor count narrow", M.anchorCount(180), 5)
check("anchor count panel row", M.anchorCount(336), 6)
check("anchor count wide", M.anchorCount(520), 7)
check("anchor count before layout", M.anchorCount(0), 5)
check("six anchors", denseAnchors.length, 6)
check("anchors start at the first point", denseAnchors[0], 0)
check("anchors end at the last point", denseAnchors[denseAnchors.length - 1], 79)
check("anchors spread evenly", denseAnchors[3], 47)
check("anchors rise with voltage", denseAnchors.every((v, i) => i === 0 || v > denseAnchors[i - 1]), true)
check("no anchors without points", M.anchorIndices([], 6), [])
check("anchors cannot exceed the points",
  M.anchorIndices([{ v: 0.7, f: 1000 }, { v: 0.8, f: 1100 }, { v: 0.9, f: 1200 }], 6).length, 3)

check("nearest anchor at the first voltage",
  M.nearestAnchor(denseAnchors, denseFactory, denseFactory[0].v), 0)
check("nearest anchor at the last voltage",
  M.nearestAnchor(denseAnchors, denseFactory, denseFactory[79].v), 79)
check("nearest anchor by voltage between handles",
  M.nearestAnchor(denseAnchors, denseFactory, denseFactory[45].v), 47)
check("nearest anchor without points", M.nearestAnchor(denseAnchors, [], 1000), -1)
// X is voltage, not frequency: a query just below the high-voltage handle
// must pick it, even though its frequency is numerically far away.
check("nearest anchor compares voltage",
  M.nearestAnchor([0, 1], [{ v: 0.5, f: 1000 }, { v: 0.9, f: 2000 }], 0.88), 1)
check("anchor near keeps the same voltage", M.anchorNear(denseAnchors, 40), 47)
check("anchor near clamps at the start", M.anchorNear(denseAnchors, 1), 0)
check("anchor near clamps at the end", M.anchorNear(denseAnchors, 1000), 79)
check("anchor near without anchors", M.anchorNear([], 10), -1)
check("neighbour anchor down", M.neighbourAnchor(denseAnchors, 32, -1), 16)
check("neighbour anchor up", M.neighbourAnchor(denseAnchors, 32, 1), 47)
check("neighbour anchor clamps low", M.neighbourAnchor(denseAnchors, 0, -1), 0)
check("neighbour anchor clamps high", M.neighbourAnchor(denseAnchors, 79, 1), 79)

// Dragging a middle anchor: the anchor lands exactly, its neighbouring
// anchors stay put, and the points in between follow the falloff.
const upMove = edit(32, 1400)
check("anchor lands on the frequency", upMove[32].f, 1400)
check("anchor left neighbour stays", upMove[16].f, denseFactory[16].f)
check("anchor right neighbour stays", upMove[47].f, denseFactory[47].f)
check("falloff lifts the points nearer the anchor",
  upMove[28].f - denseFactory[28].f > upMove[20].f - denseFactory[20].f, true)
check("falloff leaves points outside the window", upMove[15].f, denseFactory[15].f)
check("falloff stays smooth and monotonic", monotonic(upMove), true)
check("falloff rounds to whole MHz", integers(upMove), true)

const downMove = edit(32, 1000)
check("anchor moves down", downMove[32].f, 1000)
check("down drag stays monotonic", monotonic(downMove), true)
check("down drag lifts the left neighbour", downMove[24].f < denseFactory[24].f, true)

// The end anchors have one neighbour only.
const firstMove = edit(0, 500)
check("first anchor moves", firstMove[0].f, 500)
check("first anchor keeps the next anchor", firstMove[16].f, denseFactory[16].f)
check("first anchor bends its neighbour", firstMove[8].f > denseFactory[8].f, true)
check("first anchor drag stays monotonic", monotonic(firstMove), true)

const lastMove = edit(79, 2200)
check("last anchor moves", lastMove[79].f, 2200)
check("last anchor bends its neighbour", lastMove[72].f > denseFactory[72].f, true)
check("last anchor drag stays monotonic", monotonic(lastMove), true)

// The ends cannot cross their only neighbour in either direction.
const firstUp = edit(0, 99999)
check("first anchor cannot cross its neighbour", firstUp[0].f <= denseFactory[16].f, true)
check("first anchor up stays monotonic", monotonic(firstUp), true)
const lastDown = edit(79, 0)
check("last anchor cannot cross its neighbour", lastDown[79].f >= denseFactory[63].f, true)
check("last anchor down stays monotonic", monotonic(lastDown), true)

// Dragging past a neighbour stops at it instead of inverting the curve.
const pastRight = edit(32, 99999)
check("drag past the right neighbour stops at it", pastRight[32].f, denseFactory[47].f)
check("drag past the right neighbour stays monotonic", monotonic(pastRight), true)
const pastLeft = edit(32, -50)
check("drag past the left neighbour never passes it",
  pastLeft[32].f >= denseFactory[16].f, true)
check("drag past the left neighbour stays below its start",
  pastLeft[32].f <= denseFactory[32].f, true)
check("drag past the left neighbour stays monotonic", monotonic(pastLeft), true)

// The descriptor range wins at the ends of the curve.
check("drag beyond maxF clamps to maxF", edit(79, 99999)[79].f, denseCurve.maxF)
// The plot starts just below the factory floor, so a drag cannot address the
// descriptor's much lower minimum (105 MHz is off-screen).
check("drag below the plot clamps to its floor",
  edit(0, -50)[0].f, Math.round(denseFactory[0].f * 0.98))

// The same step rounding the daemon payload uses.
check("fractional drag rounds", edit(32, 1400.6)[32].f, 1401)
check("typed exact frequency lands exactly",
  edit(47, M.parseFrequency(denseCurve, "1620"))[47].f, 1620)
check("typed exact frequency stays monotonic",
  monotonic(edit(32, M.parseFrequency(denseCurve, "99999"))), true)

// Shift flattens everything above the grabbed anchor at its new frequency.
const flattened = edit(32, 1400, true)
check("flatten sets the anchor", flattened[32].f, 1400)
check("flatten levels above", flattened[60].f, 1400)
check("flatten levels the top", flattened[79].f, 1400)
check("flatten keeps below", flattened[20].f, denseFactory[20].f)
check("flatten stays monotonic", monotonic(flattened), true)

// Reset restores the factory curve without aliasing it.
const reset = M.factoryCurve(denseCurve)
check("reset returns the factory curve", reset.length, 80)
check("reset matches factory", reset[40].f, denseFactory[40].f)
reset[40].f = 1
check("reset does not alias the factory", denseCurve.factoryPoints[40].f, denseFactory[40].f)

// A draft restored from an older build can arrive with an inverted point;
// every edit hands the daemon a valid, non-decreasing curve.
const brokenDraft = M.newDraft(denseState, "gpu")
brokenDraft.points[40].f = 100
const repaired = M.pointChange(brokenDraft, 32, 1400, false, denseState, "gpu", denseAnchors).points
check("edit repairs an inverted draft", monotonic(repaired), true)
check("edit keeps untouched points within range",
  repaired.every(p => p.f >= denseCurve.minF && p.f <= denseCurve.maxF), true)

// A draft persisted by an older build is repaired on restore too.
const rescued = M.restoreDraft({
  points: denseFactory.map((p, i) => ({ f: i === 40 ? 100 : p.f })),
}, denseState, "gpu")
check("restore repairs an inverted draft", monotonic(rescued.points), true)


// ------------------------------------------------- real GTX 1060 curve
// tests/fixtures/gtx1060-curve.json is the driver table as the daemon
// reported it before the scale fix: 80 points at twice the real SM clock.
// Halving the frequencies is what the backend now does at runtime.

const fixture = JSON.parse(readFileSync(new URL("./fixtures/gtx1060-curve.json", import.meta.url), "utf8"))
const realPoints = fixture.factoryPoints.map(p => ({ v: p.v, f: p.f / 2 }))
const realCurve = {
  supported: true, writable: true, cause: null, scale: 1,
  points: realPoints.map(p => ({ ...p })),
  factoryPoints: realPoints.map(p => ({ ...p })),
  minF: Math.max(1, Math.round(Math.min(...realPoints.map(p => p.f))) + gpuCore.min),
  maxF: Math.round(Math.max(...realPoints.map(p => p.f))) + gpuCore.max,
}
const realState = {
  controls: { gpu: [gpuCore, gpuMemory] },
  applied: { gpu: { core: 0, memory: 0 } },
  curves: { gpu: realCurve },
}

check("fixture keeps the driver's 80 points", fixture.points.length, 80)
check("fixture is raw and doubled", fixture.factoryPoints[0].f, 278)
check("fixture scales to a real 139 MHz floor", realPoints[0].f, 139)
check("fixture scales to a real 1898 MHz ceiling", realPoints[79].f, 1898)
check("fixture plateau starts at the last useful point", M.plateauStart(realPoints), 66)
check("plateau-aware anchors keep six handles", M.anchorIndices(realPoints, 6).length, 6)
check("last anchor is the plateau's first point", M.anchorIndices(realPoints, 6)[5], 66)

const realDomain = M.curveDomain(realCurve, gpuCore)
check("domain starts just below the factory floor", realDomain.minF, Math.round(139 * 0.98))
check("domain ends at factory max plus the top offset", realDomain.maxF, 1898 + 300)
check("voltage axis has three ticks", realDomain.xTicks.length, 3)
check("voltage ticks rise left to right",
  realDomain.xTicks[0] < realDomain.xTicks[1]
  && realDomain.xTicks[1] < realDomain.xTicks[2], true)
check("voltage ticks start at the padded floor", realDomain.xTicks[0], realDomain.voltLo)
check("voltage ticks end at the padded ceiling", realDomain.xTicks[2], realDomain.voltHi)
check("frequency axis has three ticks", realDomain.yTicks.length, 3)
check("frequency ticks fall from the top",
  realDomain.yTicks[0] > realDomain.yTicks[1]
  && realDomain.yTicks[1] > realDomain.yTicks[2], true)
check("frequency ticks stay near the domain",
  realDomain.yTicks[0] >= realDomain.maxF - 10
  && realDomain.yTicks[2] <= realDomain.minF + 10, true)
check("domain pads the voltage floor by 4%", realDomain.voltLo, 0.45 - 0.04 * (1.24375 - 0.45))
check("domain pads the voltage ceiling by 4%", realDomain.voltHi, 1.24375 + 0.04 * (1.24375 - 0.45))

// Labels: voltage along the bottom, frequency up the left, one unit each.
const axisLabels = M.curveAxisLabels(realDomain)
check("voltage axis has three labels", axisLabels.x.length, 3)
check("frequency axis has three labels", axisLabels.y.length, 3)
check("the last voltage label carries the unit", axisLabels.x[2].slice(-2), " V")
check("the other voltage labels carry no unit", axisLabels.x[0].includes("V"), false)
check("the top frequency label carries the unit", axisLabels.y[0].slice(-4), " MHz")
check("the other frequency labels carry no unit", axisLabels.y[1].includes("MHz"), false)

const axisLayout = M.curveAxisAnchors({
  width: 320, height: 190, padLeft: 64, padRight: 6,
  padTop: 10, padBottom: 18, caption: 10, labelGap: 4, bottomInset: 2
})
check("axis labels stay inside the widget",
  axisLayout.x.concat(axisLayout.y).every(point => point.x >= 0 && point.x <= 320
    && point.y >= 0 && point.y <= 190), true)
check("left labels sit in the gutter",
  axisLayout.y.every(point => point.x === 60), true)
const widestLabel = Math.max(...axisLabels.y.map(text => text.length)) * 6
check("left labels fit before the plot", axisLayout.y[0].x - widestLabel >= 0, true)
check("bottom labels span the plot width",
  axisLayout.x[0].x === 64 && axisLayout.x[2].x === 314, true)

// The swapped screen mapping: vertical drags set frequency and clamp at the
// plot top (highest frequency) and floor (lowest frequency).
const screenBox = {
  width: 320, height: 190, padLeft: 64, padRight: 6, padTop: 10, padBottom: 18,
  voltLo: 0.42, voltHi: 1.28, minF: 136, maxF: 2198
}
const near = (a, b) => Math.abs(a - b) < 1e-9
check("voltage maps to the left edge", M.curveVoltageToX(0.42, screenBox), 64)
check("voltage maps to the right edge", M.curveVoltageToX(1.28, screenBox), 314)
check("x maps to the middle voltage", near(M.curveXToVoltage(189, screenBox), 0.85), true)
check("x left of the plot clamps to the lowest voltage",
  M.curveXToVoltage(0, screenBox), 0.42)
check("x right of the plot clamps to the highest voltage",
  M.curveXToVoltage(999, screenBox), 1.28)
check("top frequency maps to the plot top", M.curveFrequencyToY(2198, screenBox), 10)
check("bottom frequency maps to the plot floor", M.curveFrequencyToY(136, screenBox), 172)
check("y above the plot clamps to the top", M.curveYToFrequency(-50, screenBox), 2198)
check("y below the plot clamps to the floor", M.curveYToFrequency(999, screenBox), 136)
check("vertical drag maps the middle to mid frequency",
  M.curveYToFrequency(91, screenBox), Math.round((136 + 2198) / 2))

const realPlot = {
  width: 250, height: 162,
  xMin: realDomain.voltLo, xMax: realDomain.voltHi,
  yMin: realDomain.minF, yMax: realDomain.maxF,
}
const realAnchors = M.anchorIndices(realPoints, 6, realPlot)
const realPos = M.anchorPositions(realPoints, realPlot)
const realGaps = realAnchors.slice(1).map((index, i) =>
  realPos.x[index] - realPos.x[realAnchors[i]])
check("real curve keeps six anchors", realAnchors.length, 6)
check("real anchors start at the first point", realAnchors[0], 0)
check("real anchors end at the plateau", realAnchors[5], 66)
check("real anchors rise with voltage",
  realAnchors.every((index, i) => i === 0
    || realPoints[index].v > realPoints[realAnchors[i - 1]].v), true)
check("real anchors are at least 36 px apart on the voltage axis",
  realGaps.every(gap => gap >= 36), true)

// A narrower plot cannot fit six handles 36 px apart on the voltage axis;
// it must use fewer instead of stacking them.
const narrowPlot = { ...realPlot, width: 200 }
const narrowAnchors = M.anchorIndices(realPoints, 6, narrowPlot)
const narrowPos = M.anchorPositions(realPoints, narrowPlot)
const narrowGaps = narrowAnchors.slice(1).map((index, i) =>
  narrowPos.x[index] - narrowPos.x[narrowAnchors[i]])
check("narrow plot uses fewer anchors", narrowAnchors.length < 6, true)
check("narrow anchors still cap the plateau",
  narrowAnchors[narrowAnchors.length - 1], 66)
check("narrow anchors keep 36 px on the voltage axis",
  narrowGaps.every(gap => gap >= 36), true)

const realDraft = M.newDraft(realState, "gpu")
const topDrag = M.pointChange(realDraft, 66, 5000, false, realState, "gpu", realAnchors).points
check("top anchor clamps to the daemon maximum", topDrag[66].f, realCurve.maxF)
check("the plateau follows its capping anchor", topDrag[79].f, realCurve.maxF)
const bottomDrag = M.pointChange(realDraft, 0, -5000, false, realState, "gpu", realAnchors).points
check("bottom anchor clamps to the plot floor", bottomDrag[0].f, realDomain.minF)
check("bottom drag stays inside the daemon range", bottomDrag[0].f >= realCurve.minF, true)

// Every anchor against a spread of drag targets, flattening or not: the
// result stays a monotone curve inside the descriptor range, leaves the
// voltages alone, and writes whole MHz wherever it changes a frequency. The
// factory table itself carries half-MHz steps, so an untouched point keeps
// its fraction.
const editViolations = []
const editTargets = Array.from({ length: 12 }, (_, i) =>
  Math.round(-200 + (realCurve.maxF + 400) * i / 11))
for (const anchor of realAnchors) {
  for (const flatten of [false, true]) {
    for (const target of editTargets) {
      const out = M.pointChange(realDraft, anchor, target, flatten, realState, "gpu", realAnchors).points
      const label = `anchor ${anchor} target ${target} flatten ${flatten}`
      if (!out.every((p, i) => i === 0 || p.f >= out[i - 1].f)) {
        editViolations.push(`${label}: not monotone`)
      }
      if (!out.every(p => p.f >= realCurve.minF && p.f <= realCurve.maxF)) {
        editViolations.push(`${label}: outside the curve range`)
      }
      if (!out.every((p, i) => p.v === realPoints[i].v)) {
        editViolations.push(`${label}: changed a voltage`)
      }
      out.forEach((p, i) => {
        if (p.f !== realPoints[i].f && !Number.isInteger(p.f)) {
          editViolations.push(`${label}: point ${i} is ${p.f} MHz`)
        }
      })
    }
  }
}
check("anchored edits stay monotone, in range, whole MHz and keep voltages",
  editViolations.slice(0, 5), [])

// The smooth path is built from the de-duplicated table, so the plateau and
// the repeated frequencies near the top can no longer draw staircase steps.
const smooth = M.smoothCurve(realPoints)
check("smooth path is denser than the table", smooth.length > realPoints.length, true)
check("smooth path is monotone in frequency",
  smooth.every((p, i) => i === 0 || p.f >= smooth[i - 1].f), true)
check("smooth path is monotone in voltage",
  smooth.every((p, i) => i === 0 || p.v >= smooth[i - 1].v), true)
check("smooth path has no duplicate consecutive points",
  smooth.every((p, i) => i === 0 || p.f !== smooth[i - 1].f || p.v !== smooth[i - 1].v), true)

const avgDraft = realPoints.map(p => ({ ...p }))
for (let i = 0; i < 10; i++) avgDraft[i].f += 100
check("curve average covers the editable range only",
  M.curveAverageDelta(avgDraft, realPoints), Math.round(1000 / 67))

check("typed frequency clamps to the plotted maximum",
  M.parseFrequency(realCurve, "99999", realDomain), Math.round(realDomain.maxF))
check("stepping clamps to the plotted maximum",
  M.stepFrequency(realCurve, Math.round(realDomain.maxF), 1, false, realDomain),
  Math.round(realDomain.maxF))
check("stepping clamps to the plotted minimum",
  M.stepFrequency(realCurve, Math.round(realDomain.minF), -1, true, realDomain),
  Math.round(realDomain.minF))

const realBand = M.curveRiskBounds(realCurve, gpuCore)
check("real risk band stays above the factory line",
  realBand.edges.every((edge, i) => edge.f >= realPoints[i].f), true)
check("real risk band shares the factory voltages",
  realBand.edges.every((edge, i) => edge.v === realPoints[i].v), true)
check("real risk band ends at the plot top", realBand.topF, realDomain.maxF)
check("real risk band never leaves the plot",
  realBand.edges.every(edge => edge.f <= realBand.topF), true)

// A draft persisted before the scale fix carries doubled frequencies; its
// top points are above what the live curve can address, so the saved points
// are dropped and the live curve shown instead of a clamped flat line.
const staleDraft = M.restoreDraft({ points: fixture.points }, realState, "gpu")
check("doubled persisted draft is ignored", staleDraft.points[0].f, 139)
check("doubled persisted draft keeps the live curve", staleDraft.points[66].f, 1898)

// ------------------------------------------------------ draft persistence

// A state refresh that reports the old applied values (what a deadline/Esc
// revert does) must keep a dirty draft instead of resetting it to 0.
const edited = M.newDraft(cpuState, "cpu")
edited.values.core = -100
edited.values.cache = -80

// During the trial the daemon reports the candidate as applied. Rebuilding
// the draft from that would erase the difference being tested, and the revert
// below would then reset the user's values. The draft must survive both.
const trialState = {
  controls: cpuState.controls,
  applied: { cpu: { core: -100, cache: -80 } },
  curves: {},
  trial: { component: "cpu", mode: "session", deadlineMs: 1000 }
}
const duringTrial = M.reconcileDraft(edited, trialState, "cpu")
check("trial keeps draft core", duringTrial.values.core, -100)
check("trial keeps draft cache", duringTrial.values.cache, -80)
check("trial status", M.draftStatus(duringTrial, trialState, "cpu"), "trial")

const revertedState = {
  controls: cpuState.controls,
  applied: { cpu: { core: 0, cache: 0 } },
  curves: {},
  lastReverted: { component: "cpu", reason: "deadline", mode: "session",
    candidate: { core: -100, cache: -80 } }
}
const kept = M.reconcileDraft(duringTrial, revertedState, "cpu")
check("revert keeps draft value", kept.values.core, -100)
check("revert keeps draft cache", kept.values.cache, -80)
check("revert marks draft reverted", kept.reverted, true)
check("revert status", M.draftStatus(kept, revertedState, "cpu"), "reverted")

// After a confirm the applied values equal the draft, so it adopts them.
const confirmedTrialState = { controls: cpuState.controls, applied: { cpu: { core: -100, cache: -80 } }, curves: {} }
const confirmedDraft = M.reconcileDraft(duringTrial, confirmedTrialState, "cpu")
check("confirm adopts applied", confirmedDraft.values.core, -100)
check("confirm status", M.draftStatus(confirmedDraft, confirmedTrialState, "cpu"), "applied")

// A confirm reports the applied candidate; a clean draft follows it.
const clean = M.newDraft(cpuState, "cpu")
const confirmedState = { controls: cpuState.controls, applied: { cpu: { core: -60, cache: -40 } }, curves: {} }
const followed = M.reconcileDraft(clean, confirmedState, "cpu")
check("clean draft follows applied core", followed.values.core, -60)
check("clean draft follows applied cache", followed.values.cache, -40)
check("clean draft is applied", M.draftStatus(followed, confirmedState, "cpu"), "applied")

// A draft that no longer matches the reverted candidate is a plain edit.
const editedAgain = M.reconcileDraft(kept, revertedState, "cpu")
editedAgain.values.core = -20
check("edit after revert clears reverted",
  M.draftStatus(editedAgain, revertedState, "cpu"), "draft")

// Partial applied snapshots must not invent a phantom edit.
const partial = { controls: cpuState.controls, applied: { cpu: { core: -30 } }, curves: {} }
check("partial applied not dirty", M.dirty(M.newDraft(partial, "cpu"), partial, "cpu"), false)

// Persistence roundtrip, clamped to today's ranges. Use a fresh draft: the
// objects above were mutated in place by the revert/edit checks.
const toPersist = M.newDraft(cpuState, "cpu")
toPersist.values.core = -100
toPersist.values.cache = -80
const persisted = M.serializeDraft(toPersist)
check("serialize keeps values", persisted.values.core, -100)
check("serialize keeps points", M.serializeDraft(gpuDraft).points.length, 9)
const restored = M.restoreDraft(persisted, cpuState, "cpu")
check("restore keeps value", restored.values.core, -100)
check("restore marks the persisted edit", M.edited(restored), true)
check("restore value clamps to normal range",
  M.restoreDraft({ values: { core: -500, cache: 0 } }, cpuState, "cpu").values.core, -250)
check("restore tolerates garbage", M.restoreDraft({ values: { core: "x" } }, cpuState, "cpu").values.core, 0)

// --------------------------------------------------------------- monitor

// Telemetry may omit a reading or report null; the Monitor tab shows an em
// dash for those and never invents a value.
check("monitor reads a number", M.monitorValue({ cpuTemp: 67 }, "cpuTemp"), 67)
check("monitor keeps zero", M.monitorValue({ gpuUsage: 0 }, "gpuUsage"), 0)
check("monitor missing is null", M.monitorValue({}, "cpuTemp"), null)
check("monitor null is null", M.monitorValue({ gpuTemp: null }, "gpuTemp"), null)
check("monitor non-number is null", M.monitorValue({ x: "12" }, "x"), null)
check("monitor without telemetry is null", M.monitorValue(null, "cpuTemp"), null)

check("usage fraction", M.monitorPercentFraction(59), 0.59)
check("usage fraction clamps high", M.monitorPercentFraction(140), 1)
check("usage fraction clamps low", M.monitorPercentFraction(-5), 0)
check("missing usage fraction", M.monitorPercentFraction(null), 0)

// The bar length is temperature over the device's thermal ceiling, not 100.
check("cpu ceiling", M.monitorCeiling({ cpuC: 95, gpuC: 88 }, "cpu"), 95)
check("gpu ceiling", M.monitorCeiling({ cpuC: 95, gpuC: 88 }, "gpu"), 88)
check("ceiling falls back to 100", M.monitorCeiling(null, "gpu"), 100)
check("zero ceiling falls back", M.monitorCeiling({ gpuC: 0 }, "gpu"), 100)
check("temperature fraction uses the ceiling", M.monitorTemperatureFraction(70, 88), 70 / 88)
check("temperature fraction clamps", M.monitorTemperatureFraction(100, 88), 1)
check("missing temperature fraction", M.monitorTemperatureFraction(null, 88), 0)

check("temperature level below 75%", M.monitorTemperatureLevel(65, 88), 0)
check("temperature level at 75%", M.monitorTemperatureLevel(66, 88), 1)
check("temperature level below 92%", M.monitorTemperatureLevel(80, 88), 1)
check("temperature level above 92%", M.monitorTemperatureLevel(81, 88), 2)
check("temperature level at the ceiling", M.monitorTemperatureLevel(88, 88), 2)
check("temperature level uses the fallback ceiling",
  M.monitorTemperatureLevel(75, M.monitorCeiling(null, "gpu")), 1)

// --------------------------------------------------------------- factory

const factoryDraft = M.newDraft(cpuState, "cpu")
factoryDraft.values.core = -100
factoryDraft.values.cache = -80
M.stageFactory(factoryDraft, cpuState, "cpu")
check("factory stage resets", factoryDraft.reset, true)
check("factory stage values", factoryDraft.values.core, 0)
check("factory stage clears the revert mark", factoryDraft.reverted, false)

const shiftedDraft = M.newDraft(gpuState, "gpu")
shiftedDraft.points[0].f = 1300
shiftedDraft.values.core = 200
const staged = M.stageFactory(shiftedDraft, gpuState, "gpu")
check("factory stage resets the curve", staged.points[0].f, 1140)
check("factory stage resets the controls", staged.values.core, 0)
check("factory stage returns the same draft", staged, shiftedDraft)
check("factory stage without state is a no-op", M.stageFactory(null, cpuState, "cpu"), null)

// --------------------------------------------------------------- status

const pausedState = { ...cpuState, paused: { cpu: true } }
check("paused status", M.draftStatus(null, pausedState, "cpu"), "paused")
const noControls = { controls: { cpu: [{ ...cpuCore, available: false }] }, applied: { cpu: {} }, curves: {} }
check("unavailable status", M.draftStatus(null, noControls, "cpu"), "unavailable")
check("offline status", M.draftStatus(null, null, "cpu"), "offline")
check("trial status", M.draftStatus(null, { ...cpuState, trial: { component: "cpu" } }, "cpu"), "trial")

// ---------------------------------------------------- one row per control

// Typed exact value: whole numbers only, clamped to the daemon range.
check("typed plain", M.parseExact(cpuCore, "-40"), -40)
check("typed whitespace", M.parseExact(cpuCore, "  -40  "), -40)
check("typed unicode minus", M.parseExact(cpuCore, "\u221240"), -40)
check("typed plus", M.parseExact(cpuCore, "+30"), 30)
check("typed below range clamps", M.parseExact(cpuCore, "-999"), -250)
check("typed above range clamps", M.parseExact(cpuCore, "999"), 100)
check("typed garbage is not a value", M.parseExact(cpuCore, "abc"), null)
check("typed empty is not a value", M.parseExact(cpuCore, ""), null)
check("typed decimal rejected", M.parseExact(cpuCore, "1.5"), null)

// Arrow stepping on a focused slider: 1 normally, 10 with Shift.
check("step up one", M.stepValue(cpuCore, -40, 1, false), -39)
check("step down one", M.stepValue(cpuCore, -40, -1, false), -41)
check("step shift up", M.stepValue(cpuCore, -40, 1, true), -30)
check("step shift down", M.stepValue(cpuCore, -40, -1, true), -50)
check("step clamps", M.stepValue(cpuCore, -250, -1, true), -250)

// The "was" line appears only for a control whose draft differs from the
// machine, and restores exactly the applied value.
const rowDraft = M.newDraft(cpuState, "cpu")
check("was hidden when clean", M.controlDiffers(rowDraft, cpuState, "cpu", "core", cpuCore), false)
rowDraft.values.core = -40
check("was shown when edited", M.controlDiffers(rowDraft, cpuState, "cpu", "core", cpuCore), true)
check("was target is the applied value",
  M.appliedValue(cpuState, "cpu", "core", cpuCore), 0)

// The footer keeps "restore factory" reachable while anything non-factory
// is applied.
check("applied is factory", M.appliedIsFactory(cpuState, "cpu"), true)
const tunedState = { ...cpuState, applied: { cpu: { core: -40, cache: 0 } } }
check("applied is not factory", M.appliedIsFactory(tunedState, "cpu"), false)
const curveFactory = [{ v: 800, f: 1500 }, { v: 900, f: 1700 }]
const curvedState = { controls: { gpu: [] }, applied: { gpu: {} },
  curves: { gpu: { supported: true, points: [{ v: 800, f: 1550 }, { v: 900, f: 1700 }], factoryPoints: curveFactory } } }
check("custom curve is not factory", M.appliedIsFactory(curvedState, "gpu"), false)
curvedState.curves.gpu.points = curveFactory.map(p => ({ ...p }))
check("factory curve is factory", M.appliedIsFactory(curvedState, "gpu"), true)
check("markup in labels is escaped", M.escapeMarkup("a<b>&c"), "a&lt;b&gt;&amp;c")
const riskCurve = { factoryPoints: curveFactory, points: curveFactory, minF: 100, maxF: 4000 }
const riskCore = { ...gpuCore, risk: { percent: 5, ref: 1700 } }
check("curve within the limit is not aggressive",
  M.curveExceeds(curveFactory.map(p => ({ ...p })), riskCurve, riskCore), false)
check("curve point past the limit is aggressive",
  M.curveExceeds([{ v: 800, f: 1600 }, { v: 900, f: 1700 }], riskCurve, riskCore), true)

// The "keep after restart" checkbox is the only thing that picks the mode.
check("mode unchecked", M.applyMode(M.newDraft(cpuState, "cpu"), false), { mode: "session", saveDefault: false })
check("mode checked", M.applyMode(M.newDraft(cpuState, "cpu"), true), { mode: "default", saveDefault: false })
const resetDraft = M.newDraft(cpuState, "cpu")
resetDraft.reset = true
check("mode reset unchecked", M.applyMode(resetDraft, false), { mode: "reset", saveDefault: false })
check("mode reset checked", M.applyMode(resetDraft, true), { mode: "reset", saveDefault: true })

// The risk zone is a tint on the value, not a modal and not a footer state.
check("risk tint beyond", M.excess(gpuCore, 250) > gpuCore.risk.percent, true)

check("countdown clamps at 1", M.countdownSeconds(-500, 0), 1)
check("countdown rounds up", M.countdownSeconds(4100, 0), 5)

check("apply payload includes points", M.applyPayload(gpuDraft, "gpu", gpuState).points.length, 9)

// The daemon rejects a request that names an unavailable control; the UI
// must omit it (driver 580: temperature getter without setter).
const unwritableState = {
  controls: { gpu: [{ ...gpuCore, available: false }, gpuMemory] },
  applied: { gpu: { core: 0, memory: 0 } },
  curves: { gpu: { ...curve, supported: false } }
}
const unwritableDraft = M.newDraft(unwritableState, "gpu")
const filtered = M.applyPayload(unwritableDraft, "gpu", unwritableState)
check("payload omits unavailable control", filtered.core, undefined)
check("payload keeps available control", filtered.memory, 0)
check("payload omits unsupported curve", filtered.points, undefined)

// A readable but unwritable curve (no service privileges) is displayed, but
// never dragged or sent to the daemon.
const readOnlyState = {
  controls: { gpu: [gpuCore, gpuMemory] },
  applied: { gpu: { core: 0, memory: 0 } },
  curves: { gpu: { ...curve, writable: false, cause: "service required" } }
}
const readOnlyDraft = M.newDraft(readOnlyState, "gpu")
check("read-only curve is displayed", readOnlyDraft.points.length, 9)
check("read-only curve omitted from payload", M.applyPayload(readOnlyDraft, "gpu", readOnlyState).points, undefined)
r = M.pointChange(readOnlyDraft, 0, 1500, false, readOnlyState, "gpu")
check("read-only point cannot move", r.points[0].f, 1140)
r = M.sliderChange(readOnlyDraft, gpuCore, 100, readOnlyState, "gpu")
check("read-only slider keeps points", r.points[0].f, 1140)
const readOnlyFactory = M.stageFactory(M.newDraft(readOnlyState, "gpu"), readOnlyState, "gpu")
check("read-only factory stage keeps points", readOnlyFactory.points[0].f, 1140)
check("read-only factory stage sends no points", M.applyPayload(readOnlyFactory, "gpu", readOnlyState).points, undefined)

// ------------------------------------------------------------------- fan

const fanCurve = {
  minP: 0, maxP: 100, fixedMinP: 25, lastMinP: 90,
  minT: 30, maxT: 90, minPoints: 2, maxPoints: 12,
  factory: [
    { t: 40, p: 30 }, { t: 50, p: 35 }, { t: 60, p: 50 },
    { t: 70, p: 75 }, { t: 80, p: 100 },
  ],
}
const fanState = {
  ...gpuState,
  fans: { gpu: {
    supported: true, writable: true, cause: null, count: 1,
    speed: 32, temperatureC: 70, targetC: 88, slowdownC: 97,
    config: { mode: "auto" }, curve: fanCurve,
  } },
}
const unwritableFan = {
  ...fanState,
  fans: { gpu: { ...fanState.fans.gpu, writable: false, cause: "service required" } },
}
const noFan = { ...gpuState, fans: { gpu: { supported: false, writable: false, cause: "no fan" } } }

check("fan info", M.fanInfo(fanState).speed, 32)
check("fan config defaults to auto", M.fanConfig({}), { mode: "auto" })
const autoDraft = M.newFanDraft(fanState)
check("auto draft mode", autoDraft.mode, "auto")
check("auto draft seeds speed from the live fan", autoDraft.speed, 32)
check("auto draft carries the factory curve", autoDraft.points.length, 5)
check("auto draft is clean", M.fanEdited(autoDraft), false)
check("auto draft is not dirty", M.fanDirty(autoDraft, fanState), false)
check("auto draft applied", M.fanDraftStatus(autoDraft, fanState), "applied")
check("auto payload", M.fanPayload(autoDraft), { mode: "auto" })
check("auto applies without write access", M.fanCanApply(autoDraft, unwritableFan), true)

const fixedDraft = M.copyFanDraft(autoDraft)
fixedDraft.mode = "fixed"
fixedDraft.speed = 60
check("fixed draft edited", M.fanEdited(fixedDraft), true)
check("fixed draft dirty", M.fanDirty(fixedDraft, fanState), true)
check("fixed draft status", M.fanDraftStatus(fixedDraft, fanState), "draft")
check("fixed payload", M.fanPayload(fixedDraft), { mode: "fixed", speed: 60 })
check("fixed needs write access", M.fanCanApply(fixedDraft, unwritableFan), false)
check("speed clamps to the driver floor", M.clampFanSpeed(fanState.fans.gpu, 5), 25)
check("speed clamps to the driver ceiling", M.clampFanSpeed(fanState.fans.gpu, 140), 100)

const curveFanState = {
  ...fanState,
  fans: { gpu: { ...fanState.fans.gpu, config: { mode: "fixed", speed: 60 } } },
}
check("fixed config loads into the draft", M.newFanDraft(curveFanState).speed, 60)
const appliedCurveState = {
  ...fanState,
  fans: { gpu: { ...fanState.fans.gpu,
    config: { mode: "curve", points: fanCurve.factory.map(p => ({ ...p })) } } },
}
const curveDraft = M.newFanDraft(appliedCurveState)
check("curve config loads into the draft", curveDraft.points[4].p, 100)
check("window speed at 65 C", M.fanDutyAt(curveDraft.points, 65), 63)
check("window speed below the first anchor", M.fanDutyAt(curveDraft.points, 20), 30)
check("window speed above the last anchor", M.fanDutyAt(curveDraft.points, 95), 100)
const editedCurve = M.copyFanDraft(curveDraft)
editedCurve.points[2].p = 55
check("edited curve is dirty", M.fanDirty(editedCurve, appliedCurveState), true)
check("curve payload carries points", M.fanPayload(editedCurve).points[2].p, 55)

const fanRevertedState = {
  ...fanState,
  lastReverted: { component: "gpu", candidate: { fan: { mode: "fixed", speed: 60 } } },
}
check("reverted candidate is recognised", M.fanDraftStatus(fixedDraft, fanRevertedState), "reverted")

const stoppedCurveState = {
  ...fanState,
  fans: { gpu: { ...fanState.fans.gpu, loopActive: false,
    config: { mode: "curve", points: fanCurve.factory.map(p => ({ ...p })) } } },
}
check("a dead curve loop is stopped", M.fanLoopStopped(stoppedCurveState), true)
check("a stopped policy is not applied",
  M.fanDraftStatus(M.newFanDraft(stoppedCurveState), stoppedCurveState), "stopped")
check("a dirty draft outranks the stopped verdict",
  M.fanDraftStatus(fixedDraft, stoppedCurveState), "draft")
const runningCurveState = {
  ...stoppedCurveState,
  fans: { gpu: { ...stoppedCurveState.fans.gpu, loopActive: true } },
}
check("a running loop is applied",
  M.fanDraftStatus(M.newFanDraft(runningCurveState), runningCurveState), "applied")
check("auto needs no loop", M.fanLoopStopped({
  ...fanState,
  fans: { gpu: { ...fanState.fans.gpu, loopActive: false, config: { mode: "auto" } } },
}), false)

// A stopped loop whose hand-back to the driver failed may have left NVML on
// the last manual duty: the banner must not promise driver control.
check("a failed hand-back is reported", M.fanHandBackFailed({
  ...stoppedCurveState.fans.gpu, handBackFailed: true,
}), true)
check("a stopped loop that handed back has no failed hand-back",
  M.fanHandBackFailed(stoppedCurveState.fans.gpu), false)
check("a running loop has no failed hand-back", M.fanHandBackFailed({
  ...stoppedCurveState.fans.gpu, loopActive: true, handBackFailed: true,
}), false)
check("auto has no failed hand-back", M.fanHandBackFailed({
  ...stoppedCurveState.fans.gpu, handBackFailed: true, config: { mode: "auto" },
}), false)

check("no fan is unavailable", M.fanDraftStatus(autoDraft, noFan), "unavailable")
check("no fan cannot apply", M.fanCanApply(autoDraft, noFan), false)
check("no fan still reports the safe config", M.fanConfig(noFan), { mode: "auto" })
check("offline fan status", M.fanDraftStatus(autoDraft, null), "offline")

const savedFan = M.serializeFanDraft(fixedDraft)
check("saved fan draft keeps the mode", savedFan.mode, "fixed")
const restoredFan = M.restoreFanDraft(savedFan, fanState)
check("restored fan draft keeps the speed", restoredFan.speed, 60)
check("restored fan draft is edited against the machine", M.fanEdited(restoredFan), true)
const restoredSpeed = M.restoreFanDraft({ mode: "fixed", speed: 999 }, fanState)
check("restored speed clamps to the driver", restoredSpeed.speed, 100)
const decreasing = { mode: "curve", points: fanCurve.factory.map(p => ({ ...p })) }
decreasing.points[1].p = 20
const restoredDecreasing = M.restoreFanDraft(decreasing, appliedCurveState)
check("decreasing saved curve is rejected",
  M.sameFanPoints(restoredDecreasing.points, fanCurve.factory), true)
check("reconcile keeps a dirty draft",
  M.fanDirty(M.reconcileFanDraft(fixedDraft, fanState), fanState), true)
check("reconcile follows a clean draft", M.fanDirty(M.reconcileFanDraft(autoDraft, fanState), fanState), false)

// ------------------------------------------------ fan curve point placement

const curvePoints = fanCurve.factory.map(p => ({ ...p }))
const window = M.fanWindow(fanState)
check("fan window comes from the descriptor", [window.minT, window.maxT], [30, 90])
check("fan window point count", [window.minPoints, window.maxPoints], [2, 12])
check("fan window falls back without a descriptor",
  M.fanWindow({}).maxPoints, 12)

const inserted = M.fanAddPoint(curvePoints, 65, 62, fanState)
check("a new point lands at its temperature", inserted.points.length, 6)
check("the new point keeps the requested duty", inserted.points[inserted.index].p, 62)
check("the new point is ordered", inserted.points.map(p => p.t).join(","), "40,50,60,65,70,80")
const midInsert = M.fanAddPoint(curvePoints, 65, -1, fanState)
check("a keyboard point takes the curve's own duty",
  midInsert.points[midInsert.index].p, M.fanDutyAt(curvePoints, 65))
check("a taken temperature is refused", M.fanAddPoint(curvePoints, 70, 80, fanState), null)
check("outside the window clamps instead of growing the axis",
  M.fanAddPoint(curvePoints, 20, 40, fanState).points[0].t, 30)
const full = Array.from({ length: 12 }, (_, i) => ({ t: 30 + i, p: 40 + i }))
check("a full curve refuses another point", M.fanAddPoint(full, 60, 50, fanState), null)

const widened = M.fanMovePoint(curvePoints, 2, 65, 50, fanState)
check("moving a point changes its temperature", widened[2].t, 65)
check("moving a point cannot cross its left neighbour",
  M.fanMovePoint(curvePoints, 2, 20, 50, fanState)[2].t, 51)
check("moving a point cannot cross its right neighbour",
  M.fanMovePoint(curvePoints, 2, 95, 50, fanState)[2].t, 69)
check("moving a point stays above the lower duty",
  M.fanMovePoint(curvePoints, 2, 60, 5, fanState)[2].p, 35)
check("moving a point stays below the higher duty",
  M.fanMovePoint(curvePoints, 2, 60, 99, fanState)[2].p, 75)
check("the hot end keeps the normal-mode floor",
  M.fanMovePoint(curvePoints, 4, 80, 50, fanState)[4].p, 90)
check("expert mode unlocks a cold hot end",
  M.fanMovePoint(curvePoints, 4, 80, 50, { ...fanState, tuningMode: "expert" })[4].p, 75)
check("a no-op move returns the same values",
  M.fanMovePoint(curvePoints, 2, 60, 50, fanState)[2].p, 50)

const thinned = M.fanRemovePoint(curvePoints, 2, fanState)
check("removing a point shortens the curve", thinned.length, 4)
check("removing a point keeps the others", thinned.map(p => p.t).join(","), "40,50,70,80")
check("the hot end keeps its floor after a removal",
  M.fanRemovePoint(curvePoints.slice(0, 4), 3, fanState).pop().p, 90)
const twoPoints = [{ t: 40, p: 30 }, { t: 80, p: 100 }]
check("a two-point curve refuses removal", M.fanRemovePoint(twoPoints, 0, fanState), null)
const coldEnd = [{ t: 40, p: 30 }, { t: 60, p: 50 }, { t: 80, p: 100 }]
check("removing the hottest point raises the new hot end",
  M.fanRemovePoint(coldEnd, 2, fanState).pop().p, 90)
check("removing out of range does nothing", M.fanRemovePoint(curvePoints, 9, fanState), null)

const messy = [{ t: 70, p: 80 }, { t: 40, p: 30 }, { t: 55, p: 20 }]
const tidy = M.fanClampPoints(messy, fanState)
check("clamping sorts the points", tidy.map(p => p.t).join(","), "40,55,70")
check("clamping raises falling duties", tidy.map(p => p.p).join(","), "30,30,90")
check("clamping keeps a legal curve untouched",
  M.fanClampPoints(curvePoints, fanState).map(p => p.p).join(","), "30,35,50,75,100")

const placedSaved = M.restoreFanDraft({
  mode: "curve",
  points: [{ t: 35, p: 40 }, { t: 62, p: 60 }, { t: 88, p: 100 }],
}, appliedCurveState)
check("a saved curve keeps its own temperatures",
  placedSaved.points.map(p => p.t).join(","), "35,62,88")
check("a saved curve with one point falls back",
  M.restoreFanDraft({ mode: "curve", points: [{ t: 40, p: 30 }] }, appliedCurveState).points.length, 5)

// The daemon rejects a curve with more points than its cap; a stored draft
// over the cap is ignored like any other invalid input.
const savedCurvePoints = count =>
  Array.from({ length: count }, (_, i) => ({ t: 30 + i * 4, p: 30 + i * 4 }))
const restoredOverCap = M.restoreFanDraft(
  { mode: "curve", points: savedCurvePoints(14) }, appliedCurveState)
check("a saved curve over the daemon point cap is ignored",
  M.fanPayload(restoredOverCap).points.length, fanCurve.factory.length)
check("a saved curve at the daemon point cap is kept",
  M.fanPayload(M.restoreFanDraft(
    { mode: "curve", points: savedCurvePoints(12) }, appliedCurveState)).points.length, 12)

// ------------------------------------------------ V/F handles pinned by hand

const anchorPointsVf = [{ v: 0.7, f: 1000 }, { v: 0.8, f: 1200 }, { v: 0.9, f: 1500 }, { v: 1.0, f: 1500 }]
const risingVf = [{ v: 0.7, f: 1000 }, { v: 0.8, f: 1200 }, { v: 0.9, f: 1400 }, { v: 1.0, f: 1500 }]
check("pins merge with the automatic spread",
  M.curveAnchors(risingVf, [0, 1], [3]), [0, 1, 3])
check("a pin past the plateau is dropped", M.curveAnchors(anchorPointsVf, [0], [3]), [0])
check("a pin out of the table is dropped", M.curveAnchors(anchorPointsVf, [0], [9]), [0])
check("a duplicate pin collapses", M.curveAnchors(anchorPointsVf, [0, 1], [1]), [0, 1])
check("pins without points are ignored", M.curveAnchors([], [0], [1]), [])
check("nearest table point by voltage", M.nearestPointIndex(anchorPointsVf, 0.81), 1)
check("the nearest point stays off the plateau", M.nearestPointIndex(anchorPointsVf, 1.2), 2)
check("insert lands between two handles", M.anchorInsertIndex(anchorPointsVf, [0, 2], 0), 1)
check("insert without points", M.anchorInsertIndex([], [0], 0), -1)
check("removing a pin keeps the others", M.withoutAnchor([1, 3, 5], 3), [1, 5])

const pinnedDraft = M.newDraft(denseState, "gpu")
pinnedDraft.anchors = [3, 9]
check("anchors serialize with the draft", M.serializeDraft(pinnedDraft).anchors, [3, 9])
check("anchors restore with the draft",
  M.restoreDraft(M.serializeDraft(pinnedDraft), denseState, "gpu").anchors, [3, 9])
check("a stale pin is dropped on restore",
  M.restoreDraft({ values: {}, anchors: [3, 999], reset: false }, denseState, "gpu").anchors, [3])
check("pins do not make the draft dirty", M.dirty(pinnedDraft, denseState, "gpu"), false)
check("a rebuilt draft keeps the pins",
  M.reconcileDraft(pinnedDraft, denseState, "gpu").anchors, [3, 9])

check("device name drops the CPUID noise",
  M.deviceName("Intel(R) Core(TM) i7-6700 CPU @ 3.40GHz"), "Intel Core i7-6700")
check("device name shortens the GPU vendor",
  M.deviceName("NVIDIA GeForce GTX 1060 6GB"), "GeForce GTX 1060 6GB")
check("device name passes a plain name through", M.deviceName("AMD Ryzen 7 5800X"), "AMD Ryzen 7 5800X")
check("device name of nothing is empty", M.deviceName(null), "")

console.log(`${count - failures}/${count} model checks passed`)
process.exit(failures ? 1 : 0)
