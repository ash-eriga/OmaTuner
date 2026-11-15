"""QML scope: root functions cannot see ids declared inside the content Component.

`Panel.qml` builds its popup body inside `content: PanelKeyCatcher { ... }`.
Item ids declared there (the overlays layer, the control repeater, the loaders)
are not visible to properties and functions at the root of the file. Accessing
one from there does not fail at load: it throws a ReferenceError the moment the
function runs, which QML prints to the log and otherwise swallows. The action
the user just took simply does nothing.

That is exactly how "apply" broke: `apply()` set `overlays.enterArmed` as its
first statement, so pressing ✓ (or Enter in the confirm menu) aborted before
the request reached the daemon, and the daemon still reported all zeros. The
same mistake hid in `focusControlField()`, which reached for `controlRepeater`.

These tests parse the real file, find the content Component by matching braces,
and report every root-scope line that mentions an id declared inside it. The
detector is exercised against a snippet that contains the bug, so a passing
run means the check works rather than that it found nothing to look at.

Run from the project root: python3 -m unittest tests.test_qml_scope -v
"""

from __future__ import annotations

import json
import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.join(HERE, "..", "ui", "ash.omatuner")
PANEL = os.path.join(PLUGIN, "Panel.qml")


def _read(name: str) -> str:
    with open(os.path.join(PLUGIN, name), encoding="utf-8") as fh:
        return fh.read()

CONTENT_OPEN = re.compile(r"^\s*content:\s*\w+\s*\{\s*$")
ID_DECL = re.compile(r"^\s*id:\s*([A-Za-z_]\w*)\s*$")


def _strip(text: str) -> str:
    """Blank out comments and string literals, keeping line structure.

    Braces inside strings or comments must not move the brace counter, and
    keeping the newlines lets callers map a cleaned offset back to a line.
    """
    out = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        two = text[i:i + 2]
        if two == "//":
            while i < n and text[i] != "\n":
                out.append(" ")
                i += 1
            continue
        if two == "/*":
            while i < n and text[i:i + 2] != "*/":
                out.append("\n" if text[i] == "\n" else " ")
                i += 1
            out.append("  ")
            i += 2
            continue
        if ch in "\"'`":
            quote = ch
            out.append(" ")
            i += 1
            while i < n:
                if text[i] == "\\":
                    out.append(" ")
                    i += 1
                    if i < n:
                        out.append("\n" if text[i] == "\n" else " ")
                        i += 1
                    continue
                if text[i] == quote:
                    out.append(" ")
                    i += 1
                    break
                out.append("\n" if text[i] == "\n" else " ")
                i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def content_span(text: str) -> tuple[int, int]:
    """Line range [start, end) of the `content: X { ... }` block."""
    lines = text.splitlines(keepends=True)
    start = None
    for index, line in enumerate(lines):
        if CONTENT_OPEN.match(line):
            start = index
            break
    if start is None:
        raise AssertionError("no `content: <Item> {` block found in Panel.qml")

    cleaned = _strip("".join(lines[start:]))
    depth = 0
    opened = False
    end_offset = None
    for offset, ch in enumerate(cleaned):
        if ch == "{":
            depth += 1
            opened = True
        elif ch == "}":
            depth -= 1
            if opened and depth == 0:
                end_offset = offset
                break
    if end_offset is None:
        raise AssertionError("the content block is not brace-balanced")

    end_line = start + cleaned[:end_offset].count("\n")
    return start, end_line + 1


def function_body(source: str, signature: str) -> str:
    """Text from `signature` to its matching closing brace.

    A fixed character window truncates a function that grows past it, so the
    body must end where the block does.
    """
    start = source.index(signature)
    cleaned = _strip(source[start:])
    depth = 0
    opened = False
    for offset, ch in enumerate(cleaned):
        if ch == "{":
            depth += 1
            opened = True
        elif ch == "}":
            depth -= 1
            if opened and depth == 0:
                return source[start:start + offset + 1]
    raise AssertionError(f"unbalanced body for {signature!r}")


def root_scope_text(text: str) -> str:
    """Everything outside the content block: root scope spans both sides."""
    lines = text.splitlines(keepends=True)
    start, end = content_span(text)
    return "".join(lines[:start]) + "".join(lines[end:])


def cross_scope_references(text: str) -> list[tuple[int, str, str]]:
    """Root-scope lines that use an id declared inside the content block.

    Returns (line number, id, line text) for every hit.
    """
    lines = text.splitlines()
    start, end = content_span(text)
    inner_ids = set()
    for line in lines[start:end]:
        match = ID_DECL.match(line)
        if match:
            inner_ids.add(match.group(1))
    if not inner_ids:
        return []

    hits = []
    for index, line in enumerate(lines):
        if start <= index < end:
            continue
        bare = _strip(line)
        for name in inner_ids:
            if re.search(r"(?<![\w.])" + re.escape(name) + r"\b", bare):
                # `foo: overlays.something` and `overlays.x = 1` both count.
                hits.append((index + 1, name, line.strip()))
                break
    return hits


class PanelScopeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        with open(PANEL, encoding="utf-8") as fh:
            cls.source = fh.read()

    def test_detector_catches_the_pattern(self) -> None:
        # The exact shape of the bug that shipped: an id inside the content
        # block, used from a root-scope function.
        broken = """
Item {
  id: root
  function apply() {
    overlays.enterArmed = false
  }
  content: Catcher {
    id: keyCatcher
    Overlays {
      id: overlays
    }
  }
}
"""
        hits = cross_scope_references(broken)
        self.assertEqual(len(hits), 1, hits)
        self.assertEqual(hits[0][1], "overlays")

    def test_no_root_function_reaches_into_the_content_component(self) -> None:
        hits = cross_scope_references(self.source)
        detail = "\n".join(f"  Panel.qml:{line}: `{text}` (id {name})" for line, name, text in hits)
        self.assertEqual(hits, [], "root scope cannot see ids from the content Component:\n" + detail)

    def test_enter_confirmation_lives_at_root(self) -> None:
        # apply() (root scope) clears the guard, so the guard is a root
        # property. Return confirms the trial; Space keeps its "activate"
        # meaning because the catcher emits a separate signal for it.
        self.assertIn("property bool confirmKeyArmed", self.source)
        self.assertIn("function confirmFromKey()", self.source)
        self.assertIn("onReturnRequested: if (root.trialForHere) root.confirmFromKey()", self.source)
        self.assertIn("root.confirmKeyArmed = false", self.source)
        # ...and the guard must be re-armed, or one apply disables Enter for
        # every later trial.
        self.assertIn("onTriggered: root.confirmKeyArmed = true", self.source)

    def test_focus_control_field_delegates(self) -> None:
        # Root delegates; the item lookup lives in the content scope. Root
        # scope spans both sides of the content block, so the split has to
        # come from the brace-matched span, not from a plain string search.
        # Comments are stripped first: the comment explaining the bug names
        # the identifier it is warning about.
        root_code = _strip(root_scope_text(self.source))
        self.assertIn("function focusControlField(key)", root_code)
        self.assertNotIn("controlRepeater", root_code)
        self.assertIn("panel.loadedContent", root_code)
        # The rows itself sit in the tune view's own Component, one level
        # deeper than the key catcher. The catcher must ask that view, not
        # reach for its repeater (a ReferenceError Enter cannot survive).
        lines = self.source.splitlines()
        start, end = content_span(self.source)
        catcher = _strip("\n".join(lines[start:end]))
        self.assertIn("function focusControlField(key)", catcher)
        self.assertIn("bodyLoader.item", catcher)
        # The tune view's own repeater is legitimately inside the content
        # block, so only the catcher's function may be checked for it.
        field_start = catcher.index("function focusControlField(key)")
        field_end = catcher.index("onMoveRequested", field_start)
        self.assertNotIn("controlRepeater", catcher[field_start:field_end])
        self.assertIn("function focusControl(key)", self.source)

    # ------------------------------------------------- draft persistence

    def test_drafts_are_not_discarded_on_close(self) -> None:
        # The reset-to-0 root cause: closing the popup dropped both drafts,
        # and reopening rebuilt them from the reverted (factory) applied
        # values. The close path may persist but must never null the drafts.
        root_code = _strip(root_scope_text(self.source))
        self.assertNotIn("drafts = { cpu: null, gpu: null }", root_code)
        self.assertIn("root.persistUi()", root_code)

    def test_state_refresh_reconciles_instead_of_rebuilding(self) -> None:
        root_code = _strip(root_scope_text(self.source))
        self.assertIn("function reconcileDrafts()", root_code)
        self.assertIn("Model.reconcileDraft", root_code)
        # Every pushed snapshot / reconnect goes through the reconciler.
        self.assertIn("function onStateChanged() { root.reconcileDrafts() }", root_code)
        # A draft's values and curve are persisted, not just the tab.
        # (Checked on the raw file: the shell-quoting string in persistUi
        # confuses the comment/string stripper, not QML.)
        self.assertIn("Model.serializeDraft", self.source)
        self.assertIn("Model.restoreDraft", self.source)
        # ui.json can load after the daemon's first state; the saved drafts
        # must still win over the placeholders created from `applied`.
        self.assertIn("savedDraftsPending", self.source)
        self.assertIn("root.savedDraftsPending = true", self.source)

    def test_reverted_draft_keeps_a_retry_action(self) -> None:
        root_code = _strip(root_scope_text(self.source))
        self.assertIn("function reapplyLast()", root_code)
        self.assertIn("function runStatusAction()", root_code)
        # Checked on the raw file: _strip blanks the key literals too.
        self.assertIn('strings.t("action.reapply")', self.source)

    # ------------------------------------------------------ lighter trial

    def test_risk_prompt_is_gone_and_trial_is_not_modal(self) -> None:
        overlays = os.path.join(HERE, "..", "ui", "ash.omatuner", "Overlays.qml")
        with open(overlays, encoding="utf-8") as fh:
            overlay_source = fh.read()
        # The blocking aggressive-zone dialog is replaced by the value tint.
        self.assertNotIn("riskVisible", overlay_source)
        self.assertNotIn("risk.title", overlay_source)
        self.assertNotIn("riskPrompt", self.source)
        self.assertNotIn("risk.beyond", self.source)
        # The trial is no overlay and no extra row: the footer button becomes
        # the confirmation and the status line carries the countdown.
        self.assertNotIn("trialStrip", overlay_source)
        self.assertNotIn("trialProgress", overlay_source)
        self.assertNotIn("confirmKey", overlay_source)
        self.assertIn("visible: root.noticeVisible", overlay_source)
        self.assertIn("onAnyVisibleChanged: root.modalActive = noticeVisible", self.source)
        # Only the recovery notices dim the panel.
        self.assertIn("trialProgress", self.source)
        self.assertIn("Model.countdownSeconds", self.source)
        self.assertIn('strings.t("action.confirm")', self.source)
        self.assertIn('strings.t("action.cancelTrial")', self.source)

    def test_primary_apply_actions_are_explicit(self) -> None:
        for key in ("action.apply", "action.confirm", "action.keepBoot",
                    "action.restoreFactory", "action.cancelTrial"):
            self.assertIn(f'strings.t("{key}")', self.source)
        # The two full-width apply buttons, the factory button and the preset
        # pills are gone.
        for dead in ("action.applySession", "action.applyBoot", "action.applyDefault",
                     "action.reset", "action.revertNow", "action.keep"):
            self.assertNotIn(f'strings.t("{dead}")', self.source)

    def test_expert_mode_is_surfaced_and_acknowledged(self) -> None:
        self.assertIn("tuningMode", self.source)
        self.assertIn("expertNotice", self.source)
        self.assertIn('daemon.call("ack-expert"', self.source)

    def test_curve_read_only_and_thermal_reason_are_surfaced(self) -> None:
        curve = os.path.join(HERE, "..", "ui", "ash.omatuner", "CurveView.qml")
        with open(curve, encoding="utf-8") as fh:
            curve_source = fh.read()
        # A readable curve without write privileges must be shown as
        # read-only, with its cause, not as an editable control.
        self.assertIn("readOnly", curve_source)
        self.assertIn("curve.cause", curve_source)
        # Voltage is fixed by the driver; the hint travels with the drag
        # action instead of a permanent paragraph.
        self.assertIn("curve.dragHint", curve_source)
        self.assertIn('text: strings.t("curve.dragHint")', curve_source)
        # A thermal revert names its reason in the footer.
        self.assertIn("draft.revertedThermal", self.source)
        self.assertIn('info.reason.indexOf("thermal")', self.source)

    # ------------------------------------------------------ one row design

    def test_removed_controls_are_gone(self) -> None:
        tune = _read("TuneControl.qml")
        slider = _read("TuneSlider.qml")
        curve = _read("CurveView.qml")
        hint = _read("HintBadge.qml")
        everything = self.source + tune + slider + curve
        # Spinboxes, the −/+ pairs, the 0/F/? pills and the per-row range.
        self.assertNotIn("NumberField", everything)
        self.assertNotIn("SpinBox", everything)
        self.assertNotIn("stepRequested", tune)
        for literal in ('text: "0"', 'text: "F"', 'text: "−"', 'text: "+"'):
            self.assertNotIn(literal, tune)
        # The one "?" is the shared help badge: right of each term, its help
        # on hover and on focus.
        self.assertEqual(hint.count('text: "?"'), 1)
        self.assertIn('objectName: "help-badge"', tune)
        # Presets are not offered until they are finished; the old
        # "Per-point curve ›" row is gone in favour of the glyph on the GPU
        # core row.
        self.assertNotIn("component TabButton: Button", self.source)
        self.assertNotIn("presetMenu", self.source)
        self.assertNotIn('strings.t("preset.', self.source)
        self.assertIn('strings.t("control.curve")', tune)
        self.assertNotIn("curveRow", self.source)
        self.assertNotIn("curveArrow", self.source)
        self.assertNotIn("root.view", self.source)
        self.assertNotIn('view === "curve"', self.source)
        self.assertNotIn('strings.t("curve.back")', everything)

    def test_curve_mode_is_inline_and_persisted(self) -> None:
        root_text = root_scope_text(self.source)
        root_code = _strip(root_text)
        self.assertIn("property bool curveMode", root_code)
        self.assertIn("function setCurveMode(on)", root_code)
        # Session memory rides in the same ui.json payload as the last tab.
        self.assertIn("curveMode: root.curveMode", root_text)
        self.assertIn("saved.curveMode", root_text)
        # The mode is a view change only: toggling must not rebuild a draft
        # or touch points/values, or the user's edits would vanish.
        start = root_code.index("function setCurveMode(on)")
        body = root_code[start:root_code.index("\n  }", start)]
        for dead in ("newDraft", "restoreDraft", "discardDraft", ".points", ".values"):
            self.assertNotIn(dead, body)
        self.assertIn("root.curveMode = !!on", body)
        self.assertIn("root.persistUi()", body)
        # The mode only renders while the backend can offer a curve, so a
        # reconnect restores the choice without rendering a dead plot.
        self.assertIn("curveEffective", root_code)
        self.assertIn("curveAvailable", root_code)

    def test_curve_glyph_is_the_toggle(self) -> None:
        tune = _read("TuneControl.qml")
        self.assertIn("showCurveToggle", tune)
        self.assertIn("activeFocusOnTab", tune)
        self.assertIn("Qt.Key_Space", tune)
        self.assertIn("Qt.Key_Return", tune)
        self.assertIn("signal curveToggled()", tune)
        self.assertIn("Style.space(18)", tune)
        self.assertIn('strings.t("control.curve")', tune)
        self.assertIn("curveToggleTooltip", tune)
        # Drawn as a stroke, not an icon font.
        self.assertIn("Canvas {", tune)
        self.assertIn("bezierCurveTo", tune)
        # Three states: togglable, muted with a real cause, hidden when the
        # backend carries no curve at all. Offline is the transient case.
        root_text = root_scope_text(self.source)
        root_code = _strip(root_text)
        self.assertIn("curveGlyphShown", root_code)
        self.assertIn("curveGlyphEnabled", root_code)
        self.assertIn("curveGlyphTooltip", root_code)
        self.assertIn("return root.curveNow !== null", root_code)
        self.assertIn('strings.t("status.offline")', root_text)
        self.assertIn('strings.t("curve.unavailable")', root_text)

    def test_curve_summary_names_the_curve_and_core_offset(self) -> None:
        tune = _read("TuneControl.qml")
        self.assertIn("Model.curveAverageDelta", tune)
        # Number first, like every other row: the signed mean, then its unit.
        self.assertIn("Model.signed(root.curveAverage)", tune)
        self.assertIn('strings.t("curve.averageUnit")', tune)
        # The global core offset and the curve are independent and stack on
        # drivers that stack; the summary says so instead of hiding the
        # offset behind the plot.
        self.assertIn('strings.t("curve.coreOnTop")', tune)
        self.assertIn("Commons.Color.accent", tune)
        # The plot replaces the core slider; memory and temperature keep
        # their sliders.
        self.assertIn("CurveView {", tune)
        self.assertIn("visible: !root.unavailable && !root.curveSummary", tune)
        self.assertIn("visible: root.differs && !root.editing && !root.curveSummary", tune)

    def test_curve_plot_is_quiet_and_keyboard_driven(self) -> None:
        curve = _read("CurveView.qml")
        # Height, no frame, faint grid, and the two curves.
        self.assertIn("Style.space(190)", curve)
        # No unlabelled fill between the curves: the key names every mark.
        self.assertNotIn("Util.alpha(Commons.Color.accent, 0.15)", curve)
        self.assertIn("ctx.setLineDash([3, 3])", curve)
        self.assertIn('ctx.lineJoin = "round"', curve)
        # The limit is a dashed red line with a key; the allowed space under
        # it is not tinted, so red only marks a risky value.
        self.assertIn("Model.curveRiskBounds", curve)
        self.assertNotIn("Util.alpha(Commons.Color.urgent, 0.06)", curve)
        self.assertIn('strings.t("curve.limitKey")', curve)
        # A dense driver table gets a smooth line and a handful of anchors,
        # never one handle per point: 14 px dots with a 36 px target.
        self.assertIn("Model.anchorIndices", curve)
        self.assertIn("Model.anchorCount", curve)
        self.assertIn("Model.nearestAnchor", curve)
        self.assertIn("ctx.arc(ax, ay, 7", curve)
        self.assertIn("ctx.arc(ax, ay, 13", curve)
        self.assertIn("trace(root.draftPoints, true)", curve)
        # Exact entry reuses the slider readout pattern: click to type,
        # Enter commits, Esc cancels, no NumberField.
        self.assertIn("var pendingExact = Math.round(root.selectedPoint.f)", curve)
        self.assertIn("exactVisible", curve)
        self.assertIn("Model.parseFrequency", curve)
        self.assertIn("onAccepted: root.submitExact()", curve)
        self.assertIn("Keys.onEscapePressed", curve)
        # Anchor stepping and selection.
        self.assertIn("Model.stepFrequency", curve)
        self.assertIn("Model.neighbourAnchor", curve)
        self.assertIn("Qt.Key_Left", curve)
        self.assertIn("Qt.Key_Right", curve)
        self.assertIn("Qt.Key_Up", curve)
        self.assertIn("Qt.Key_Down", curve)
        # Wheel only while the plot is focused.
        self.assertIn("if (!plot.activeFocus", curve)
        self.assertIn("wheel.accepted = false", curve)
        self.assertIn('strings.t("curve.keys")', curve)
        self.assertIn('strings.t("curve.shiftHint")', curve)
        self.assertIn('strings.t("curve.flattenAbove")', curve)
        self.assertIn('strings.t("curve.reset")', curve)
        self.assertIn('strings.t("curve.noSelection")', curve)
        # Read-only: same plot, no handles and no actions, cause in a muted
        # line; the panel still lets the user open it to inspect.
        self.assertIn("visible: !root.readOnly", curve)
        self.assertIn("if (!root.readOnly)", curve)

    def test_curve_axes_are_voltage_x_and_frequency_y(self) -> None:
        curve = _read("CurveView.qml")
        # The V/F convention: voltage across, frequency up.
        self.assertIn("function voltToX(", curve)
        self.assertIn("function freqToY(", curve)
        self.assertIn("function xToVolt(", curve)
        self.assertIn("function yToFreq(", curve)
        self.assertNotIn("function freqToX(", curve)
        self.assertNotIn("function voltToY(", curve)
        self.assertIn("Model.curveVoltageToX", curve)
        self.assertIn("Model.curveFrequencyToY", curve)
        self.assertIn("Model.curveXToVoltage", curve)
        self.assertIn("Model.curveYToFrequency", curve)
        # Dragging is vertical now; the pointer's Y sets the frequency.
        self.assertIn("Qt.SizeVerCursor", curve)
        self.assertIn("root.pointEdited(dragIndex, root.yToFreq(mouse.y)", curve)
        # Voltage labels along the bottom, frequency up the left, with a
        # gutter wide enough for the top frequency label.
        self.assertIn("Model.curveAxisLabels", curve)
        self.assertIn("Model.curveAxisAnchors", curve)
        self.assertIn("padLeft: Style.space(64)", curve)
        # One common width for every tab,
        # wide enough for the frequency gutter, so the card never jumps
        # sideways when the curve or the fan curve appears.
        self.assertIn("contentWidth: panel.fittedContentWidth(Style.space(400))", self.source)

    def test_curve_keys_step_vertically_and_select_horizontally(self) -> None:
        curve = _read("CurveView.qml")
        start = curve.index("Keys.onPressed:")
        block = curve[start:curve.index("Keys.onReleased:", start)]
        self.assertIn("event.key === Qt.Key_Up || event.key === Qt.Key_Down", block)
        self.assertIn("root.stepSelected(event.key === Qt.Key_Up ? 1 : -1", block)
        self.assertIn("event.key === Qt.Key_Left || event.key === Qt.Key_Right", block)
        self.assertIn("root.selectNeighbour(event.key === Qt.Key_Left ? -1 : 1)", block)
        self.assertNotIn("root.stepSelected(event.key === Qt.Key_Left", block)
        self.assertNotIn("root.selectNeighbour(event.key === Qt.Key_Down", block)

        catalog_path = os.path.join(HERE, "..", "ui", "ash.omatuner", "i18n", "en.json")
        with open(catalog_path, encoding="utf-8") as fh:
            catalog = json.load(fh)
        self.assertEqual(catalog["curve.keys"],
                         "↑↓ step · ←→ anchor · ⇧ ×10 · a/Insert add · Del remove")
        # Adding and removing handles is a key action, not a hidden one.
        self.assertIn("root.addAnchorNearSelection()", block)
        self.assertIn("root.removeSelectedAnchor()", block)
        # The full key list lives behind the "?" badge; the visible hint is
        # short and reserves the actions' width, so nothing truncates.
        self.assertIn('strings.t("curve.keysShort")', curve)
        self.assertIn("badge.activeFocus", _read("HintBadge.qml"))
        self.assertIn(
            "Math.max(0, parent.width - actions.implicitWidth - keysBadge.width - Style.space(16))",
            curve)

    def test_tabs_are_plain_text_with_an_accent_underline(self) -> None:
        start = self.source.index("component Tab: Item")
        end = self.source.index('Tab { label: strings.t("tab.monitor")')
        block = self.source[start:end]
        self.assertNotIn("Button", block)
        self.assertIn("Style.font.subtitle", block)
        self.assertIn("height: 2", block)
        self.assertIn("Commons.Color.accent", block)
        self.assertIn("visible: parent.current", block)

    def test_one_row_per_control(self) -> None:
        tune = _read("TuneControl.qml")
        # Line 1: 13 px label, large value with the unit once in 11 px.
        self.assertIn("Style.font.subtitle", tune)
        self.assertIn("root.valueSize", tune)
        self.assertIn("Style.font.bodySmall", tune)
        self.assertIn("text: root.control ? root.control.unit : \"\"", tune)
        # Line 2: only while the draft differs, and it restores the applied
        # value of this control.
        self.assertIn('strings.t("control.was")', tune)
        self.assertIn("root.differs && !root.editing", tune)
        self.assertIn("onClicked: root.committed(root.control.key, root.applied)", tune)
        # Line 3: the slider. Range and help are tooltips only.
        self.assertIn("TuneSlider {", tune)
        self.assertIn('root.control.min + " \\u2026 " + root.control.max', tune)
        self.assertIn("root.helpText", tune)
        self.assertNotIn('root.control.min + " … "', tune)
        # The value turns accent while unapplied and takes a warning tint in
        # the aggressive zone; no second row says so.
        self.assertIn("return Commons.Color.accent", tune)
        self.assertIn("Commons.Color.urgent", tune)

    def test_typed_value_and_keyboard_stepping(self) -> None:
        tune = _read("TuneControl.qml")
        slider = _read("TuneSlider.qml")
        self.assertIn("Model.parseExact", tune)
        self.assertIn("IntValidator", tune)
        self.assertIn("onAccepted:", tune)
        self.assertIn("Keys.onEscapePressed", tune)
        self.assertIn("Model.stepValue", slider)
        self.assertIn("Qt.Key_Left", slider)
        self.assertIn("Qt.Key_Right", slider)
        self.assertIn("Qt.ShiftModifier", slider)
        # Wheel is ignored until the slider holds focus.
        self.assertIn("onWheel:", slider)
        self.assertIn("if (!root.focused)", slider)
        self.assertIn("wheel.accepted = false", slider)

    def test_keep_after_reboot_is_the_mode_switch(self) -> None:
        root_code = _strip(root_scope_text(self.source))
        self.assertIn("property bool keepAfterReboot", root_code)
        self.assertIn("Model.applyMode(draft, keepAfterReboot)", root_code)
        self.assertIn("function setKeepAfterReboot(keep)", root_code)
        # Its last state crosses popup opens through ui.json.
        self.assertIn("keepAfterReboot: root.keepAfterReboot", root_code)
        self.assertIn("savedKeepValid", root_code)
        self.assertIn('strings.t("action.keepBoot")', self.source)

    def test_trial_is_the_apply_button_and_the_status_line(self) -> None:
        self.assertIn('root.trialForHere ? strings.t("action.confirm") : strings.t("action.apply")',
                      self.source)
        self.assertIn("width: parent.width * root.trialProgress", self.source)
        self.assertIn("readonly property real trialProgress", self.source)
        self.assertIn("root.trialWindowMs", self.source)
        self.assertIn("function revertTrial(reason)", self.source)
        # The countdown clock must seed when it starts, or the first frame of
        # a trial counts down from the value captured when the popup opened.
        self.assertIn("onRunningChanged: if (running) root.countdownNow = Date.now()",
                      self.source)
        self.assertIn("countdownSeconds(root.trialForHere.deadlineMs, root.countdownNow)",
                      self.source)

    def test_monitor_and_panel_use_the_same_scale(self) -> None:
        monitor = _read("MonitorView.qml")
        # 22 px between blocks, 12 px between a title and its cells, 24 px
        # between the two cells; the panel itself owns the 16 px padding.
        self.assertIn("spacing: Style.space(22)", monitor)
        self.assertIn("spacing: Style.space(12)", monitor)
        self.assertIn("spacing: Style.space(24)", monitor)
        self.assertIn("Style.font.bodySmall", monitor)
        self.assertIn("Style.font.body", monitor)
        self.assertIn("padding: Style.space(16)", self.source)
        # The monitor tab has no footer, so the offline reason lives there.
        self.assertIn('strings.t("status.offline")', monitor)
        self.assertIn("visible: !(root.daemon && root.daemon.online)", monitor)

    def test_monitor_has_two_cells_per_device(self) -> None:
        monitor = _read("MonitorView.qml")
        # Usage on the left, Temperature on the right, one row of two equal
        # cells. RAM has no temperature reading, so only one cell.
        self.assertEqual(monitor.count('strings.t("monitor.usage")'), 3)
        self.assertEqual(monitor.count('strings.t("monitor.temperature")'), 2)
        self.assertIn("(cpuCells.width - Style.space(24)) / 2", monitor)
        self.assertIn("(gpuCells.width - Style.space(24)) / 2", monitor)
        self.assertIn("(ramCells.width - Style.space(24)) / 2", monitor)

        cpu = monitor[monitor.index('strings.t("monitor.cpu")'):monitor.index('strings.t("monitor.gpu")')]
        gpu = monitor[monitor.index('strings.t("monitor.gpu")'):monitor.index('strings.t("monitor.ram")')]
        ram = monitor[monitor.index('strings.t("monitor.ram")'):]
        for block in (cpu, gpu):
            self.assertIn('strings.t("monitor.usage")', block)
            self.assertIn('strings.t("monitor.temperature")', block)
            self.assertLess(block.index('strings.t("monitor.usage")'),
                            block.index('strings.t("monitor.temperature")'))
        self.assertIn('strings.t("monitor.usage")', ram)
        self.assertNotIn('strings.t("monitor.temperature")', ram)

        # The cells stack label, value with one unit, then the 4 px rounded
        # gradient bar. The value is 22 px and the unit is small.
        self.assertIn("Style.space(22)", monitor)
        self.assertIn("height: Style.space(4)", monitor)
        self.assertIn("radius: height / 2", monitor)
        self.assertIn("orientation: Gradient.Horizontal", monitor)
        self.assertIn("Behavior on width", monitor)
        self.assertIn("duration: 250", monitor)
        self.assertIn("easing.type: Easing.OutCubic", monitor)
        # No animation on first show.
        self.assertIn("enabled: cell.animate", monitor)
        self.assertIn("animate: root.animateBars", monitor)

        # Unavailable readings: em dash, empty track, cause in a tooltip.
        self.assertIn("—", monitor)
        self.assertIn('strings.t("monitor.missingCause")', monitor)
        self.assertIn("PanelToolTip {", monitor)
        self.assertIn("visible: cellHover.hovered && !cell.available", monitor)

        # Title line: uppercase kind then an elided device name.
        self.assertIn("font.capitalization: Font.AllUppercase", monitor)
        self.assertIn("elide: Text.ElideRight", monitor)

        # The rows the previous version added are gone from the view.
        for key in ("monitor.clock", "monitor.memoryClock", "monitor.power",
                    "monitor.voltage", "monitor.tempLimit"):
            self.assertNotIn(key, monitor)

    def test_i18n_catalog_matches_usage(self) -> None:
        catalog_path = os.path.join(HERE, "..", "ui", "ash.omatuner", "i18n", "en.json")
        with open(catalog_path, encoding="utf-8") as fh:
            catalog = set(json.load(fh))
        used: set[str] = set()
        for name in sorted(os.listdir(os.path.join(HERE, "..", "ui", "ash.omatuner"))):
            if not name.endswith(".qml"):
                continue
            used.update(re.findall(r'strings\.t\(\s*"([^"]+)"', _read(name)))
        self.assertEqual(sorted(used - catalog), [], "QML asks for keys the catalog does not have")
        self.assertEqual(sorted(catalog - used), [], "catalog carries dead strings")

    def test_translations_go_through_targ(self) -> None:
        # t() returns the bare key before the catalog loads; calling .arg()
        # on it logs "Argument missing" on every open. Model.targ guards it.
        for name in sorted(os.listdir(os.path.join(HERE, "..", "ui", "ash.omatuner"))):
            if name.endswith(".qml"):
                self.assertNotIn(".arg(", _read(name), name)


class FanInGpuTabWiringTest(unittest.TestCase):
    """The fan is the last section of the GPU tab, not a tab of its own.

    The rendered harness in tests/qml/tst_fan.qml proves FanView itself; these
    checks pin the Panel wiring it would not catch, because the harness mirrors
    Panel's controller functions instead of importing them.
    """

    @classmethod
    def setUpClass(cls) -> None:
        with open(PANEL, encoding="utf-8") as fh:
            cls.source = fh.read()

    def test_there_is_no_fan_tab(self) -> None:
        self.assertNotIn('"tab.fan"', self.source)
        self.assertNotIn('value: "fan"', self.source)
        self.assertNotIn('["monitor", "cpu", "gpu", "fan"]', self.source)
        self.assertIn('const order = ["monitor", "cpu", "gpu"]', self.source)
        # The fan draft is still saved with the other drafts.
        self.assertIn("fanDraft: Model.serializeFanDraft(root.fanDraft)", self.source)

    def test_the_fan_section_lives_in_the_gpu_component(self) -> None:
        tune_start = self.source.index("id: tuneComponent")
        fan_start = self.source.index("id: fanView")
        self.assertLess(tune_start, fan_start)
        # No component boundary between the two: the fan is a child of the
        # GPU view, not a component of its own loaded in its place.
        self.assertNotIn("Component {", self.source[tune_start:fan_start])
        body = function_body(self.source, "FanView {")
        self.assertIn("visible: root.fanShown", body)
        self.assertIn("draft: root.fanDraft", body)
        self.assertNotIn("id: fanComponent", self.source)

    def test_fan_is_one_extra_focus_row_in_the_gpu_tab(self) -> None:
        self.assertIn("readonly property int focusCount: root.controlsNow.length", self.source)
        self.assertIn("root.focusIndex === root.controlsNow.length", self.source)
        self.assertIn("function focusPrimary()", self.source)

    def test_switching_back_to_curve_keeps_the_draft_points(self) -> None:
        # Fixed -> Curve used to overwrite next.points with the factory curve,
        # throwing away the points the user edited in the draft. The Fixed
        # branch already preserves the speed; the Curve branch must mirror it.
        body = function_body(self.source, "function setFanMode(mode)")
        self.assertIn("next.points = root.fanDraft.points || fresh.points", body)
        self.assertNotIn("next.points = fresh.points", body)

    def test_fan_edits_clone_before_reassigning(self) -> None:
        # QML does not notify when a property of a JS object changes, and
        # reassigning the same object changes nothing: an edit made in place
        # would leave the rendered speed, mode underline and curve stale.
        for name in ("setFanMode", "setFanSpeed", "changeFanPoint", "addFanPoint",
                     "removeFanPoint", "resetFanCurve"):
            body = function_body(self.source, f"function {name}(")
            self.assertIn("Model.copyFanDraft(root.fanDraft", body, name)
            self.assertIn("root.fanDraft = next", body, name)

    def test_gpu_apply_carries_the_fan_in_the_same_request(self) -> None:
        body = function_body(self.source, "function apply(keepAfterReboot)")
        # One request: clocks plus the staged fan policy, one trial, one
        # revert. A separate applyFan() would split the GPU trial in two.
        self.assertIn("settings.fan = Model.fanPayload(root.fanDraft)", body)
        # An unchanged fan draft must not ride the request: the daemon would
        # refuse the whole apply when it cannot write the fan.
        self.assertIn("root.fanShown && root.fanDraft && root.fanDirtyNow", body)
        self.assertNotIn("function applyFan(", self.source)

    def test_factory_also_stages_the_fan_back_to_auto(self) -> None:
        body = function_body(self.source, "function stageFactory()")
        self.assertIn('root.setFanMode("auto")', body)

    def test_the_body_scrolls_instead_of_running_under_the_footer(self) -> None:
        # The GPU tab grew past a fixed card height once the fan section
        # landed: the panel clipped the content and the footer painted over
        # the fan hint. The card now follows its content up to the screen and
        # the body scrolls inside a clamped viewport.
        self.assertIn("contentHeight: panel.fittedContentHeight(panel.loadedContentHeight)",
                      self.source)
        self.assertNotIn("Style.space(820)", self.source)
        self.assertNotIn("Style.space(560)", self.source)
        body = function_body(self.source, "Flickable {")
        self.assertIn("id: bodyScroll", body)
        self.assertIn("height: Math.min(contentHeight, keyCatcher.bodyViewport)", body)
        self.assertIn("clip: true", body)
        self.assertIn("interactive: false", body)
        # A scrolled body must follow the keyboard cursor.
        self.assertIn("function revealFocusRow()", self.source)
        self.assertIn("function rowSpan(index)", self.source)
        self.assertIn("revealFocusRow()", self.source)

    def test_fan_view_signals_are_connected(self) -> None:
        body = function_body(self.source, "FanView {")
        for signal in ("onModeRequested", "onSpeedChanged", "onPointChanged",
                       "onPointAdded", "onPointRemoved", "onCurveResetRequested",
                       "onEnterRequested", "onEscapeRequested", "onTabRequested"):
            self.assertIn(signal, body, signal)
        self.assertIn("draft: root.fanDraft", body)


class PointEditorsTest(unittest.TestCase):
    """Both plots let the user place points; neither depends on fixed anchors.

    The fan curve is a free list of (temperature, speed) points and the V/F
    plot adds handles on top of the automatic spread, so the checks here are
    about what the editors expose and where the values come from.
    """

    def test_fan_curve_edits_points_not_fixed_anchors(self) -> None:
        curve = _read("FanCurve.qml")
        # The temperature axis is the daemon's window, not the factory temps.
        self.assertIn("property int minT: 30", curve)
        self.assertIn("property int maxT: 90", curve)
        self.assertNotIn("root.temps.length", curve)
        self.assertIn("function xForTemp(temp)", curve)
        # Both axes move, points can be added and removed, and each gesture
        # is a signal the panel turns into a draft edit.
        self.assertIn("signal pointEdited(int index, int temp, int speed)", curve)
        self.assertIn("signal pointAdded(int temp, int speed)", curve)
        self.assertIn("signal pointRemoved(int index)", curve)
        self.assertIn("onDoubleClicked", curve)
        self.assertIn("root.pointAdded(", curve)
        self.assertIn("root.pointRemoved(", curve)
        self.assertIn("Qt.RightButton", curve)
        self.assertIn("Qt.Key_Delete", curve)
        self.assertIn("Qt.Key_A", curve)
        # A press selects; only a drag moves, so the first click of a
        # double-click cannot bend the curve.
        start = curve.index("onPressed: function(m)")
        press = curve[start:curve.index("onPositionChanged:", start)]
        self.assertNotIn("root.movePoint(", press)

    def test_curve_view_pins_sit_on_top_of_the_automatic_spread(self) -> None:
        curve = _read("CurveView.qml")
        self.assertIn("readonly property var pins: draft && draft.anchors", curve)
        self.assertIn("Model.curveAnchors(", curve)
        self.assertIn("signal anchorAdded(int index)", curve)
        self.assertIn("signal anchorRemoved(int index)", curve)
        self.assertIn("onDoubleClicked", curve)
        self.assertIn("root.anchorAdded(index)", curve)
        self.assertIn("root.anchorRemoved(index)", curve)
        body = function_body(curve, "function removeSelectedAnchor()")
        # The automatic spread is derived from the plot width: only pinned
        # handles can be removed.
        self.assertIn("root.isPinned(root.selected)", body)
        start = curve.index("onPressed: function(mouse)")
        press = curve[start:curve.index("onPositionChanged:", start)]
        self.assertNotIn("root.pointEdited(", press)

    def test_the_panel_persists_the_pinned_handles(self) -> None:
        source = open(PANEL, encoding="utf-8").read()
        body = (function_body(source, "function addCurveAnchor(index)")
                + function_body(source, "function removeCurveAnchor(index)"))
        self.assertIn("Model.sanitizeAnchors(", body)
        self.assertIn("next.anchors = pins", body)
        self.assertIn("Model.withoutAnchor(", body)
        self.assertIn("persistTimer.restart()", body)
        # Replacing handles is a view change: the payload stays the full table.
        self.assertNotIn("applyPayload", body)


if __name__ == "__main__":
    unittest.main()
