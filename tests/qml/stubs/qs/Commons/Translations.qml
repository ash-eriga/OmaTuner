import QtQuick

// Minimal catalog for the interaction tests: every key TuneControl,
// TuneSlider and CurveView ask for. The real catalog is ui/ash.omatuner/i18n.
Item {
  property string directory: ""
  visible: false
  property var catalog: ({
    "control.was": "was",
    "control.restoreHint": "Restore the value the machine runs now.",
    "control.curve": "Per-point curve",
    "control.curveButton": "Curve",
    "control.aggressiveAbove": "Aggressive · %1% above stock",
    "control.aggressiveBelow": "Aggressive · %1% below stock",
    "curve.averageUnit": "MHz avg",
    "curve.coreOnTop": "%1 core on top",
    "curve.keys": "↑↓ step · ←→ anchor · ⇧ ×10",
    "curve.keysShort": "Drag · keys",
    "curve.draftKey": "curve",
    "curve.stockKey": "stock",
    "curve.limitKey": "limit",
    "curve.pointDelta": "%1 vs stock",
    "curve.selectedVoltage": "Selected %1 V →",
    "curve.noSelection": "no anchor selected",
    "curve.shiftHint": "Shift: flatten above",
    "curve.flattenAbove": "flatten above",
    "curve.reset": "reset curve",
    "curve.dragHint": "Voltage is fixed at each anchor. Press anywhere and the nearest anchor follows; drag vertically to set its frequency. Hold Shift to flatten every point above it.",
    "curve.unavailable": "The driver did not expose an editable V/F curve.",
    "curve.readOnly": "Read-only: this process may read the curve but the driver refused per-point writes.",
    "section.fan": "Fan control",
    "section.fanHint": "Driver default, a fixed speed, or a temperature curve.",
    "fan.mode.auto": "Auto",
    "fan.mode.fixed": "Fixed",
    "fan.mode.curve": "Curve",
    "fan.speed": "Fan speed",
    "fan.autoHint": "The driver owns the fan.",
    "fan.fixedHint": "Holds one duty.",
    "fan.curveHint": "Temperature anchors are fixed.",
    "fan.edited": "Edited",
    "fan.factoryCurve": "stock",
    "fan.collapse": "Hide the fan controls",
    "fan.expand": "Show the fan controls",
    "fan.selectedPoint": "Selected %1",
    "fan.curveTarget": "Curve asks %1% at %2 °C",
    "fan.curveTargetHint": "Curve asks: the speed the curve sets at the GPU's temperature now. The header shows the measured speed.",
    "fan.readingHint": "Measured now: fan speed and GPU temperature. The curve's target follows the temperature, so the two can differ by a few percent.",
    "fan.override": "Thermal override: the fan is forced to 100%.",
    "fan.policyStopped": "The fan policy is not running: the driver controls the fan.",
    "fan.policyStoppedHandBack": "The fan policy stopped and the fan may still be at its last setting; choose Auto to hand it back to the driver.",
    "fan.unavailable": "This GPU does not report a controllable fan.",
    "status.offline": "Service offline"
  })
  function t(key) {
    return catalog && catalog[key] !== undefined ? catalog[key] : key
  }
}
