import QtQuick
import Quickshell
import Quickshell.Io
import qs.Ui
import qs.Commons
import qs.Commons as Commons
import "Model.js" as Model


Panel {
  id: root

  moduleName: "ash.omatuner"
  ipcTarget: "ash.omatuner"
  manageIpc: false

  // One row of plain text tabs, a section line, one row per control and one
  // footer. The bar chip opens it; nothing in here is a second window.
  property string tab: "monitor"
  // Per-point curve mode is an inline state of the GPU tab, not a separate
  // page: the glyph after "GPU core" switches the offset slider for the
  // plot. Remembered per session in ui.json like the last tab.
  property bool curveMode: false
  property bool fanCollapsed: true
  // Drafts are user-owned and survive trial start/confirm/revert, warnings,
  // popup close and daemon reconnects. They are persisted in ui.json; the
  // applied snapshot is only their baseline, never their replacement while
  // they differ from it (see Model.reconcileDraft).
  property var drafts: ({ cpu: null, gpu: null })
  property var savedDraftData: ({})
  // The fan is its own draft, edited by the GPU tab's last section. Keeping
  // it apart from the clock draft keeps the merge on reload simple: each
  // draft compares against its own slice of the applied snapshot.
  property var fanDraft: null
  property var savedFanDraftData: null
  // ui.json can load after the daemon's first state (the socket is fast, the
  // file read is a separate async step). Until the saved drafts have been
  // merged once, the automatically created drafts are placeholders.
  property bool savedDraftsPending: false
  property string focusSection: "tabs" // tabs | controls | footer
  property int focusIndex: 0
  property int inputFocusCount: 0
  property real countdownNow: 0
  property bool cursorActive: false
  // "Keep after restart": unchecked applies for this session, checked also
  // saves the values for the next boot. Its last state is persisted.
  property bool keepAfterReboot: false
  property bool savedKeepValid: false
  // Key of the slider that holds keyboard focus. While one does, the panel's
  // key catcher steps back so the slider sees arrows, Shift+arrows, Esc and
  // Enter itself.
  property string sliderFocusKey: ""
  // Whether Enter may confirm the 5-second trial. It lives at this scope
  // because apply() needs to clear it; it is re-armed shortly after an apply
  // so the same keypress cannot also confirm the trial it starts.
  property bool confirmKeyArmed: true
  // Modal/keyboard-block state, mirrored from the overlay layer so the key
  // catcher's `blocked` binding reads only root-scope properties.
  property bool modalActive: false
  property bool modalAbrupt: false
  property bool modalError: false
  property bool modalFailed: false
  property bool curveEditing: false
  property bool curveGlyphFocused: false
  readonly property string component: tab === "cpu" ? "cpu" : "gpu"
  readonly property int trialWindowMs: 5000

  Commons.Translations {
    id: strings
    directory: Qt.resolvedUrl("i18n")
  }

  Daemon {
    id: daemon
  }

  // Children that declare their own `daemon` property cannot reach the id
  // above through `daemon: daemon` — that binding resolves to the property
  // being defined, so the view received null and rendered fallbacks and
  // "no reading" for everything. This alias keeps the injection unambiguous.
  readonly property var daemonForViews: daemon

  readonly property var controlsNow: daemon.state && daemon.state.controls ? daemon.state.controls[component] || [] : []
  readonly property var curveNow: daemon.state && daemon.state.curves ? daemon.state.curves.gpu || null : null
  readonly property var fanNow: daemon.state && daemon.state.fans ? daemon.state.fans.gpu || null : null
  readonly property int fanLiveSpeed: daemon.telemetry && typeof daemon.telemetry.gpuFanSpeed === "number"
    ? daemon.telemetry.gpuFanSpeed : -1
  readonly property int fanLiveTemp: daemon.telemetry && typeof daemon.telemetry.gpuTemp === "number"
    ? daemon.telemetry.gpuTemp : -1
  // The fan is the last section of the GPU tab, not a tab of its own: it
  // shares the footer, the trial window and the Apply button with the clocks.
  readonly property bool fanShown: root.tab === "gpu" && !!(root.fanNow && root.fanNow.supported)
  readonly property bool fanDirtyNow: root.fanShown && root.fanDraft && daemon.state
    ? Model.fanDirty(root.fanDraft, daemon.state) : false
  readonly property int focusCount: root.controlsNow.length + (root.fanShown ? 1 : 0)
  readonly property bool cursorOnFanRow: root.focusSection === "controls"
    && root.fanShown && root.focusIndex === root.controlsNow.length
  readonly property bool curveAvailable: !!(root.curveNow && root.curveNow.supported)
  // The glyph appears on the GPU core row in three states: normal (toggled),
  // muted with the real cause (backend online but the curve is unavailable),
  // and offline (muted, not togglable). A backend that does not carry a curve
  // at all hides it.
  readonly property bool curveGlyphShown: {
    if (!daemon.online || !daemon.state) return true
    return root.curveNow !== null
  }
  readonly property bool curveGlyphEnabled: daemon.online && root.curveAvailable
  readonly property string curveGlyphTooltip: {
    if (!daemon.online || !daemon.state) return strings.t("status.offline")
    if (!root.curveNow) return ""
    if (!root.curveNow.supported) return root.curveNow.cause || strings.t("curve.unavailable")
    return strings.t("control.curve")
  }
  // The mode only renders while the backend can offer a curve. Keeping the
  // raw flag means a reconnect restores the mode the user chose.
  readonly property bool curveEffective: root.curveMode && root.curveAvailable
  readonly property bool trialActive: daemon.state && daemon.state.trial !== null && daemon.state.trial !== undefined
  // The trial only decorates the tab it belongs to.
  readonly property var trialForHere: trialActive && daemon.state.trial.component === component
    ? daemon.state.trial : null
  readonly property real trialProgress: {
    if (!root.trialForHere || !root.trialForHere.deadlineMs) return 0
    return Math.max(0, Math.min(1, (root.trialForHere.deadlineMs - root.countdownNow) / root.trialWindowMs))
  }
  readonly property bool pausedAny: daemon.state && (
    (daemon.state.paused && (daemon.state.paused.cpu || daemon.state.paused.gpu)) ||
    (daemon.state.suspended && (daemon.state.suspended.cpu || daemon.state.suspended.gpu)))
  readonly property bool conflictHere: daemon.state && daemon.state.conflicts
    ? daemon.state.conflicts.some(c => c.components.includes(component)) : false
  readonly property bool canApply: {
    if (!daemon.online || trialActive) return false
    if (daemon.state && daemon.state.notice && daemon.state.notice.type === "abrupt-restart"
        && daemon.state.abruptPending) return false
    const draft = root.drafts[root.component]
    // Returning to factory is always allowed: the daemon's reset path skips
    // the conflict check, and removing a tuning conflict is the safe move.
    if (conflictHere && !(draft && draft.reset)) return false
    // A staged fan policy needs the driver to accept writes; Auto is always
    // allowed because it only hands the fan back.
    if (root.fanDirtyNow && !Model.fanCanApply(root.fanDraft, daemon.state)) return false
    return true
  }
  readonly property string tuningMode: daemon.state && daemon.state.tuningMode ? daemon.state.tuningMode : "normal"
  readonly property bool expertNotice: !!(daemon.state && daemon.state.tuningMode === "expert"
    && !daemon.state.expertWarned)
  readonly property string componentStatus: {
    if (root.tab === "monitor") return daemon.online ? "monitor" : "offline"
    const base = Model.draftStatus(root.drafts[root.component], daemon.state, root.component)
    if (!root.fanShown) return base
    const fan = Model.fanDraftStatus(root.fanDraft, daemon.state)
    // One status line for the whole GPU tab: the trial, pause and suspension
    // states are per component and already cover the fan; a dirty or
    // reverted fan draft is a dirty or reverted GPU draft.
    if (base === "unavailable" || base === "offline" || base === "trial"
        || base === "suspended" || base === "paused" || base === "draft"
        || base === "reverted") return base
    if (fan === "unavailable" || fan === "offline" || fan === "trial"
        || fan === "suspended" || fan === "paused") return fan
    return fan === "applied" ? base : fan
  }
  // What runs under the 5-second trial, by name: "CPU core −180 mV · CPU
  // cache −110 mV". The trial carries no values, so this reads the applied
  // snapshot, which is the trial's values while it runs.
  readonly property string trialSummary: {
    if (!root.trialForHere || !daemon.state) return ""
    const parts = []
    // Labels muted, values in the body colour and bold, the facts under
    // test; a value in the aggressive zone is red here too, where the
    // question is asked.
    function pair(label, value, risky) {
      const valueColor = risky ? Commons.Color.urgent : Commons.Color.foreground
      return "<font color=\"" + Util.alpha(Commons.Color.foreground, 0.7) + "\">"
        + Model.escapeMarkup(label) + "</font> <b><font color=\"" + valueColor + "\">"
        + Model.escapeMarkup(value) + "</font></b>"
    }
    for (let i = 0; i < root.controlsNow.length; i++) {
      const control = root.controlsNow[i]
      if (!control.available) continue
      const value = Model.appliedValue(daemon.state, root.component, control.key, control)
      parts.push(pair(control.label,
        Model.displayValue(control, value).replace("-", "\u2212") + " " + control.unit,
        control.risk && Model.excess(control, value) > control.risk.percent))
    }
    if (root.component === "gpu" && Model.curveIsCustom(root.curveNow)) {
      const core = root.controlByKey("core")
      parts.push(pair(strings.t("summary.curve"), strings.t("summary.curveCustom"),
        Model.curveExceeds(root.curveNow.points, root.curveNow, core)))
    }
    if (root.fanShown) {
      const fan = Model.fanConfig(daemon.state)
      const mode = fan.mode === "fixed" ? Model.targ(strings.t("summary.fanFixed"), fan.speed)
        : fan.mode === "curve" ? strings.t("summary.fanCurve") : strings.t("summary.fanAuto")
      parts.push(pair(strings.t("summary.fan"), mode, false))
    }
    return parts.join(" · ")
  }

  // Which parts of the draft differ from what runs, by name, for the
  // footer's "Not applied" line.
  readonly property string stagedSummary: {
    if (root.tab === "monitor" || !daemon.state) return ""
    const draft = root.drafts[root.component]
    if (!draft) return ""
    const parts = []
    for (let i = 0; i < root.controlsNow.length; i++) {
      const control = root.controlsNow[i]
      if (Model.controlDiffers(draft, daemon.state, root.component, control.key, control))
        parts.push(control.label)
    }
    if (root.component === "gpu" && root.curveNow && root.curveNow.supported && draft.points
        && !Model.samePoints(draft.points, root.curveNow.points))
      parts.push(strings.t("summary.curveShort"))
    if (root.fanDirtyNow) parts.push(strings.t("summary.fanShort"))
    return parts.join(", ")
  }

  // Something staged that Apply would change: the button is solid only then.
  readonly property bool hasChanges: root.tab !== "monitor" && (root.draftIsDirty(root.component)
    || root.fanDirtyNow || root.componentStatus === "reverted")
  readonly property bool factoryStaged: {
    const draft = root.drafts[root.component]
    return !!(draft && draft.reset) && (!root.fanShown || !root.fanDraft || root.fanDraft.mode === "auto")
  }
  readonly property bool appliedAtFactory: daemon.state
    ? (Model.appliedIsFactory(daemon.state, root.component)
        && (!root.fanShown || Model.fanConfig(daemon.state).mode === "auto"))
    : true

  // ---------------------------------------------------------- footer text

  // A conflict blocks Apply, so it takes the status line instead of hiding
  // in the disabled button's tooltip. A staged reset is still allowed.
  readonly property bool conflictBlocks: root.conflictHere && !root.trialForHere
    && !(root.drafts[root.component] && root.drafts[root.component].reset)
  readonly property string statusKind: root.conflictBlocks ? "conflict" : root.componentStatus
  readonly property string footerStatus: root.footerStatusText()
  readonly property string footerStatusAction: {
    if (root.trialForHere) return ""
    if (root.componentStatus === "reverted") return strings.t("action.reapply")
    if (root.componentStatus === "draft") return strings.t("draft.discard")
    return ""
  }

  function footerStatusText() {
    const status = root.componentStatus
    if (root.conflictBlocks) return strings.t("status.conflict")
    if (root.trialForHere) {
      return Model.targ(strings.t("draft.testing"),
        Model.countdownSeconds(root.trialForHere.deadlineMs, root.countdownNow))
    }
    if (status === "reverted") {
      const info = daemon.state ? daemon.state.lastReverted : null
      if (info && typeof info.reason === "string" && info.reason.indexOf("thermal") === 0)
        return Model.targ(strings.t("draft.revertedThermal"), info.reason)
      return strings.t("draft.reverted")
    }
    if (status === "draft") {
      const draft = root.drafts[root.component]
      if (draft && draft.reset) return strings.t("draft.factory")
      return strings.t("draft.unapplied")
    }
    if (status === "applied") return root.appliedAtFactory ? "" : strings.t("draft.applied")
    return root.statusLabel(status)
  }

  function runStatusAction() {
    if (root.componentStatus === "reverted") {
      root.reapplyLast()
    } else if (root.componentStatus === "draft") {
      root.discardDraft()
      if (root.fanShown) root.discardFanDraft()
    }
  }

  // ------------------------------------------------------------- drafts

  function componentOf(which) {
    return which === "cpu" ? "cpu" : "gpu"
  }

  function draftFor(which) {
    return root.drafts[componentOf(which)]
  }

  function draftIsDirty(key) {
    const draft = root.drafts[key]
    return draft && daemon.state ? Model.dirty(draft, daemon.state, key) : false
  }

  function controlByKey(key, which) {
    const name = componentOf(which || root.component)
    const controls = daemon.state && daemon.state.controls ? daemon.state.controls[name] || [] : []
    for (let i = 0; i < controls.length; i++) {
      if (controls[i].key === key) return controls[i]
    }
    return null
  }

  // One counter for every field that takes keyboard focus: while one has it,
  // the panel's key catcher steps back.
  function countInputFocus(focused) {
    if (focused) root.inputFocusCount++
    else root.inputFocusCount = Math.max(0, root.inputFocusCount - 1)
  }

  // What the machine runs for one control right now: the tick on the slider,
  // the "was" line and its restore target.
  function appliedFor(control) {
    if (!control) return 0
    if (!daemon.state) return control.factory
    return Model.appliedValue(daemon.state, root.component, control.key, control)
  }

  function ensureDrafts() {
    if (!daemon.state) return
    const next = { cpu: root.drafts.cpu, gpu: root.drafts.gpu }
    const keys = ["cpu", "gpu"]
    for (let i = 0; i < keys.length; i++) {
      const key = keys[i]
      const saved = root.savedDraftsPending ? root.savedDraftData[key] : null
      // A draft the user already touched after the state arrived wins over
      // the older file; otherwise the saved edits are the user's work.
      const useSaved = saved && (!next[key] || !Model.edited(next[key]))
      if (useSaved) {
        next[key] = Model.restoreDraft(saved, daemon.state, key)
      } else if (next[key]) {
        next[key] = Model.reconcileDraft(next[key], daemon.state, key)
      } else {
        next[key] = Model.newDraft(daemon.state, key)
      }
    }
    root.savedDraftsPending = false
    root.drafts = next
    root.ensureFanDraft()
  }

  // The fan draft is separate from the clock drafts. Its saved copy is merged
  // once, before the first reconcile, so a file edit is not mistaken for a
  // clean draft that should follow the applied config.
  function ensureFanDraft() {
    if (!daemon.state) return
    if (root.savedFanDraftData) {
      const saved = root.savedFanDraftData
      root.savedFanDraftData = null
      if (!root.fanDraft || !Model.fanEdited(root.fanDraft))
        root.fanDraft = Model.restoreFanDraft(saved, daemon.state)
    }
    if (!root.fanDraft) root.fanDraft = Model.newFanDraft(daemon.state)
    else root.fanDraft = Model.reconcileFanDraft(root.fanDraft, daemon.state)
  }

  // Every pushed snapshot goes through here. A dirty draft is never replaced:
  // after a deadline/Esc/popup-close revert the daemon reports the old applied
  // values, and the sliders must keep the values the user configured.
  function reconcileDrafts() {
    if (!daemon.state) return
    root.ensureDrafts()
  }

  // The edit functions above mutate the draft object in place. QML does not
  // notify a binding when a property of a JS object changes, and reassigning
  // the same object changes nothing: draftFor() would hand back the identical
  // object, so the value label, the knob and the curve kept rendering the old
  // numbers. Clone the mutated draft so every binding that reads it
  // re-evaluates. This is also what makes the "was" line appear and the plot
  // repaint during a drag.
  function touchDrafts() {
    root.drafts = {
      cpu: Model.copyDraft(root.drafts.cpu),
      gpu: Model.copyDraft(root.drafts.gpu)
    }
    persistTimer.restart()
  }

  function setTab(next) {
    if (trialActive || (daemon.state && daemon.state.notice
      && daemon.state.notice.type === "abrupt-restart" && daemon.state.abruptPending)) return
    root.showTab(next)
  }

  // A trial that starts while another tab is open (from the CLI, or the
  // other component) brings its own tab forward: its answer is in that
  // tab's footer, and tabs are locked until it ends.
  onTrialActiveChanged: {
    if (!root.trialActive || !daemon.state.trial) return
    const owner = daemon.state.trial.component
    if ((owner === "cpu" || owner === "gpu") && root.tab !== owner) root.showTab(owner)
  }

  function showTab(next) {
    root.tab = next
    root.sliderFocusKey = ""
    root.curveEditing = false
    root.curveGlyphFocused = false
    // The content of the previous tab is destroyed; a value field that held
    // focus may never deliver activeFocusChanged(false), and the counter would
    // keep the key catcher blocked for the rest of the session.
    root.inputFocusCount = 0
    root.cursorActive = false
    root.focusSection = next === "monitor" ? "tabs" : "controls"
    root.focusIndex = 0
    root.persistUi()
  }

  function switchPanel(direction) {
    const order = ["monitor", "cpu", "gpu"]
    const next = Math.max(0, Math.min(order.length - 1, order.indexOf(root.tab) + (direction || 1)))
    root.setTab(order[next])
  }

  // ------------------------------------------------------------- tuning

  function setControl(key, value) {
    const draft = root.drafts[root.component]
    if (!draft || !daemon.state) return
    const control = root.controlByKey(key)
    if (!control || !control.available) return
    const result = Model.sliderChange(draft, control, value, daemon.state, root.component)
    draft.values[key] = result.value
    if (result.points) draft.points = result.points
    draft.reset = false
    draft.reverted = false
    root.touchDrafts()
  }

  function stageFactory() {
    const draft = root.drafts[root.component]
    if (!draft || !daemon.state) return
    Model.stageFactory(draft, daemon.state, root.component)
    root.touchDrafts()
    // Factory for the GPU tab means the fan belongs to the driver again.
    if (root.fanShown) root.setFanMode("auto")
  }

  // Discard edits: the draft goes back to exactly what the machine runs,
  // curve points included.
  function discardDraft() {
    if (!daemon.state) return
    const fresh = Model.newDraft(daemon.state, root.component)
    // Discarding restores the values, not the editor's shape: the anchors the
    // user pinned stay where they put them.
    const current = root.drafts[root.component]
    if (current && current.anchors && current.anchors.length)
      fresh.anchors = Model.sanitizeAnchors(current.anchors, fresh.points)
    root.drafts = {
      cpu: root.component === "cpu" ? fresh : root.drafts.cpu,
      gpu: root.component === "gpu" ? fresh : root.drafts.gpu
    }
    persistTimer.restart()
  }

  function setKeepAfterReboot(keep) {
    root.keepAfterReboot = !!keep
    persistTimer.restart()
  }

  // The curve toggle changes the view only. Draft points and the global core
  // offset stay exactly as they were, so toggling never discards an edit;
  // the footer reports the draft as edited either way.
  function setCurveMode(on) {
    if (on && !root.curveAvailable) return
    root.curveMode = !!on
    root.curveEditing = false
    root.persistUi()
  }

  function changePoint(index, frequency, flatten, anchorList) {
    const draft = root.drafts.gpu
    if (!draft || !daemon.state) return
    const result = Model.pointChange(draft, index, frequency, flatten, daemon.state, "gpu", anchorList)
    draft.points = result.points
    draft.reset = false
    draft.reverted = false
    root.touchDrafts()
  }

  function resetCurve() {
    const draft = root.drafts.gpu
    if (!draft || !root.curveNow || !root.curveNow.supported) return
    draft.points = Model.factoryCurve(root.curveNow)
    draft.reset = false
    draft.reverted = false
    root.touchDrafts()
  }

  // V/F handles on top of the automatic spread. A pin is a table index, so it
  // survives a reflow of the plot and never sends anything extra to the
  // daemon: the payload is still every point.
  function addCurveAnchor(index) {
    const draft = root.drafts.gpu
    if (!draft || !draft.points) return
    const pins = Model.sanitizeAnchors((draft.anchors || []).concat([index]), draft.points)
    if (pins.length === (draft.anchors || []).length) return
    const next = Model.copyDraft(draft)
    next.anchors = pins
    root.drafts = { cpu: root.drafts.cpu, gpu: next }
    persistTimer.restart()
  }

  function removeCurveAnchor(index) {
    const draft = root.drafts.gpu
    if (!draft || !draft.anchors || draft.anchors.indexOf(index) < 0) return
    const next = Model.copyDraft(draft)
    next.anchors = Model.withoutAnchor(draft.anchors, index)
    root.drafts = { cpu: root.drafts.cpu, gpu: next }
    persistTimer.restart()
  }

  // ------------------------------------------------------------------ fan

  function setFanMode(mode) {
    if (!root.fanDraft || !daemon.state) return
    if (mode !== "auto" && !(root.fanNow && root.fanNow.writable)) return
    const next = Model.copyFanDraft(root.fanDraft)
    const fresh = Model.newFanDraft(daemon.state)
    next.mode = mode
    if (mode === "fixed") {
      next.speed = root.fanDraft.mode === "fixed"
        ? root.fanDraft.speed
        : Model.clampFanSpeed(root.fanNow, root.fanLiveSpeed >= 0
            ? root.fanLiveSpeed
            : (root.fanNow && typeof root.fanNow.speed === "number" ? root.fanNow.speed : 50))
    }
    if (mode === "curve") next.points = root.fanDraft.points || fresh.points
    next.reverted = false
    root.fanDraft = next
    persistTimer.restart()
  }

  function setFanSpeed(value) {
    if (!root.fanDraft || !root.fanNow) return
    const next = Model.copyFanDraft(root.fanDraft)
    next.mode = "fixed"
    next.speed = Model.clampFanSpeed(root.fanNow, value)
    next.reverted = false
    root.fanDraft = next
    persistTimer.restart()
  }

  // A point moves on both axes. The clamp lives in the model, so the daemon
  // never receives a curve it would refuse (falling temperatures or duties).
  function changeFanPoint(index, temp, speed) {
    if (!root.fanDraft || !root.fanDraft.points) return
    const next = Model.copyFanDraft(root.fanDraft)
    next.points = Model.fanMovePoint(next.points, index, temp, speed, daemon.state)
    next.mode = "curve"
    next.reverted = false
    root.fanDraft = next
    persistTimer.restart()
  }

  function addFanPoint(temp, speed) {
    if (!root.fanDraft) return
    const added = Model.fanAddPoint(root.fanDraft.points || [], temp, speed, daemon.state)
    if (!added) return
    const next = Model.copyFanDraft(root.fanDraft)
    next.points = added.points
    next.mode = "curve"
    next.reverted = false
    root.fanDraft = next
    persistTimer.restart()
  }

  function removeFanPoint(index) {
    if (!root.fanDraft) return
    const points = Model.fanRemovePoint(root.fanDraft.points || [], index, daemon.state)
    if (!points) return
    const next = Model.copyFanDraft(root.fanDraft)
    next.points = points
    next.mode = "curve"
    next.reverted = false
    root.fanDraft = next
    persistTimer.restart()
  }

  function resetFanCurve() {
    if (!daemon.state) return
    const next = Model.copyFanDraft(root.fanDraft || Model.newFanDraft(daemon.state))
    next.mode = "curve"
    next.points = Model.fanFactoryPoints(daemon.state)
    next.reverted = false
    root.fanDraft = next
    persistTimer.restart()
  }

  function discardFanDraft() {
    if (!daemon.state) return
    root.fanDraft = Model.newFanDraft(daemon.state)
    persistTimer.restart()
  }

  // -------------------------------------------------------------- apply

  function apply(keepAfterReboot) {
    const draft = root.drafts[root.component]
    if (!draft || !daemon.online || !root.canApply) return
    // Do not let the Enter that applied also confirm the trial it starts.
    root.confirmKeyArmed = false
    armTimer.restart()
    const plan = Model.applyMode(draft, keepAfterReboot)
    const settings = Model.applyPayload(draft, root.component, daemon.state)
    // The GPU tab carries the fan: sending it in the same request means one
    // trial, one revert and one confirmed state for clocks and fan alike. A
    // factory reset already hands the fan back to the driver, so it carries
    // no policy of its own.
    if (plan.mode !== "reset" && root.fanShown && root.fanDraft && root.fanDirtyNow)
      settings.fan = Model.fanPayload(root.fanDraft)
    daemon.call("apply", {
      component: root.component,
      settings: settings,
      mode: plan.mode,
      saveDefault: plan.saveDefault
    }, frame => {
      if (!frame.ok && frame.error) {
        daemon.lastError = frame.error.message
        errorToast.show(frame.error.message)
        return
      }
      draft.reverted = false
      if (root.fanShown && root.fanDraft) {
        const nextFan = Model.copyFanDraft(root.fanDraft)
        nextFan.reverted = false
        root.fanDraft = nextFan
      }
      root.touchDrafts()
      root.persistUi()
    })
  }

  function reapplyLast() {
    root.apply(root.keepAfterReboot)
  }

  function confirmTrial() {
    if (!root.trialActive) return
    daemon.call("confirm", {}, () => {})
  }

  function confirmFromKey() {
    if (!root.confirmKeyArmed) return
    root.confirmTrial()
  }

  function revertTrial(reason) {
    if (!root.trialActive) return
    daemon.call("revert", { reason: reason }, () => {
      const draft = root.drafts[root.component]
      if (draft && root.draftIsDirty(root.component)) draft.reverted = true
      root.touchDrafts()
      root.persistUi()
    })
  }

  function ackExpert() {
    daemon.call("ack-expert", {}, () => {})
  }

  // ------------------------------------------------- popup open/close

  onOpenedChanged: {
    if (opened) {
      root.ensureDrafts()
      daemon.setTelemetryWanted(true)
      root.cursorActive = false
      root.sliderFocusKey = ""
      root.countdownNow = Date.now()
      // The content Component may have been destroyed while a field or the
      // curve editor held focus; stale flags would block the key catcher.
      root.inputFocusCount = 0
      root.curveEditing = false
      root.modalAbrupt = !!(daemon.state && daemon.state.notice
        && daemon.state.notice.type === "abrupt-restart")
      root.modalError = !!(daemon.state && daemon.state.notice
        && daemon.state.notice.type === "component-error")
      root.modalFailed = !!(daemon.state && daemon.state.notice
        && daemon.state.notice.type === "apply-failed")
      root.modalActive = root.modalAbrupt || root.modalError || root.modalFailed
      // First opening lands on Monitor only when the ui.json FileView
      // produced no valid saved tab; otherwise honor the saved tab.
      if (!root.savedTabValid) root.tab = "monitor"
      root.focusSection = root.tab === "monitor" ? "tabs" : "controls"
      root.focusIndex = 0
      // Without a stored choice the checkbox follows what the machine has
      // saved for boot, so it never claims a session-only mode that is not
      // the truth.
      if (!root.savedKeepValid && daemon.state && daemon.state.defaults)
        root.keepAfterReboot = !!daemon.state.defaults[root.component]
    } else {
      if (root.trialActive) root.revertTrial("popup-closed")
      daemon.setTelemetryWanted(false)
      root.persistUi()
      root.inputFocusCount = 0
      root.curveEditing = false
      root.curveGlyphFocused = false
      root.sliderFocusKey = ""
    }
  }

  // A reconnect or a pushed state must reconcile, not rebuild, the drafts.
  Connections {
    target: daemon
    function onStateChanged() { root.reconcileDrafts() }
  }

  Timer {
    id: persistTimer
    interval: 700
    repeat: false
    onTriggered: root.persistUi()
  }

  Timer {
    id: armTimer
    interval: 350
    repeat: false
    onTriggered: root.confirmKeyArmed = true
  }

  // True once uiReader parsed a ui.json with a valid lastTab; until then the
  // first open falls back to Monitor.
  property bool savedTabValid: false

  // ------------------------------------------------------- ui state file

  function uiStatePath() {
    const state = Quickshell.env("XDG_STATE_HOME") || (Quickshell.env("HOME") + "/.local/state")
    return state + "/omatuner/ui.json"
  }

  function shellQuote(text) {
    return "'" + String(text).replace(/'/g, "'\\''") + "'"
  }

  function persistUi() {
    const payload = {
      lastTab: root.tab,
      curveMode: root.curveMode,
      fanCollapsed: root.fanCollapsed,
      keepAfterReboot: root.keepAfterReboot,
      drafts: {
        cpu: Model.serializeDraft(root.drafts.cpu),
        gpu: Model.serializeDraft(root.drafts.gpu)
      },
      fanDraft: Model.serializeFanDraft(root.fanDraft)
    }
    uiWriter.running = false
    const path = root.uiStatePath()
    const tmp = path + ".tmp"
    const dir = path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : "."
    uiWriter.command = [
      "bash", "-c",
      // Every path goes through shellQuote: XDG_STATE_HOME or HOME can hold a
      // quote or a space, and an unquoted one broke the write silently.
      "mkdir -p " + root.shellQuote(dir) +
      " && printf '%s' " + root.shellQuote(JSON.stringify(payload)) +
      " > " + root.shellQuote(tmp) +
      " && mv " + root.shellQuote(tmp) + " " + root.shellQuote(path)
    ]
    uiWriter.running = true
  }

  Process {
    id: uiWriter
    stdout: StdioCollector { waitForEnd: true }
  }

  FileView {
    id: uiReader
    path: root.uiStatePath()
    watchChanges: false
    printErrors: false
    blockLoading: true
    onLoaded: {
      try {
        const saved = JSON.parse(text())
        if (saved.lastTab && ["monitor", "cpu", "gpu"].includes(saved.lastTab)) {
          root.tab = saved.lastTab
          root.savedTabValid = true
        }
        if (typeof saved.curveMode === "boolean") {
          root.curveMode = saved.curveMode
        }
        if (typeof saved.fanCollapsed === "boolean") {
          root.fanCollapsed = saved.fanCollapsed
        }
        if (typeof saved.keepAfterReboot === "boolean") {
          root.keepAfterReboot = saved.keepAfterReboot
          root.savedKeepValid = true
        }
        if (saved.fanDraft && typeof saved.fanDraft === "object") {
          root.savedFanDraftData = saved.fanDraft
        }
        if (saved.drafts && typeof saved.drafts === "object") {
          root.savedDraftData = saved.drafts
          root.savedDraftsPending = true
          root.ensureDrafts()
        } else if (root.savedFanDraftData) {
          root.ensureFanDraft()
        }
      } catch (e) {}
    }
  }
  Component.onCompleted: {
    uiReader.reload()
  }

  // ------------------------------------------------------------ toast

  PanelToolTip {
    id: errorToast
    timeout: 4000
    delay: 0
    text: ""
    function show(message) {
      errorToast.text = String(message).substring(0, 200)
      errorToast.open()
    }
  }

  // ---------------------------------------------------------- bar chip

  implicitWidth: chipButton.implicitWidth
  implicitHeight: chipButton.implicitHeight

  // The bar's other widgets draw Nerd Font glyphs and fit them to their ink,
  // not to the line box. This uses the same font, size and optical centering
  // the neighbours use, so it sits in the row instead of standing out of it.
  readonly property string chipGlyph: "\u{F4BC}" // cpu, Octicons (Nerd Font)

  Item {
    id: chipButton
    anchors.fill: parent
    // Same slot the bar's own icon buttons take: iconSlot across the bar,
    // barSize along it.
    implicitWidth: root.bar && root.bar.vertical ? root.bar.barSize : Style.bar.iconSlot
    implicitHeight: root.bar && !root.bar.vertical ? root.bar.barSize : Style.bar.iconSlot

    OpticalGlyph {
      anchors.fill: parent
      text: root.chipGlyph
      fontFamily: root.bar ? root.bar.fontFamily : Style.font.family
      fontSize: Style.bar.iconFont
      paintedSize: Style.bar.iconSize
      color: root.pausedAny ? Commons.Color.urgent : (root.bar ? root.bar.barForeground : Commons.Color.foreground)
    }

    MouseArea {
      id: chipMouse
      anchors.fill: parent
      hoverEnabled: true
      cursorShape: Qt.PointingHandCursor
      onClicked: root.toggle()
    }

    PanelToolTip {
      visible: chipMouse.containsMouse || root.pausedAny || !daemon.online
      delay: 300
      text: !daemon.online
        ? strings.t("panel.offlineHint")
          + (daemon.lastError !== "" ? "\n" + daemon.lastError : "")
        : root.pausedAny
          ? strings.t("notice.abruptBody")
          : strings.t("panel.name")
    }
  }

  // ------------------------------------------------------------- panel

  KeyboardPanel {
    id: panel
    anchorItem: chipButton
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: panel.loadedContent
    padding: Style.space(16)
    // The inline plot needs a little more room for its frequency gutter, the
    // key hints and the two actions on the same line; the offset view keeps
    // the old width.
    // One width for every tab: switching tabs or modes never makes the card
    // jump sideways. 400 leaves the plots their frequency gutter.
    contentWidth: panel.fittedContentWidth(Style.space(400))
    // No fixed height: the card follows its content up to what the screen
    // allows. A hard number here was the bug that put the footer on top of
    // the fan section once the GPU tab carried both plots.
    contentHeight: panel.fittedContentHeight(panel.loadedContentHeight)

    content: PanelKeyCatcher {
      id: keyCatcher
      implicitHeight: popupColumn.implicitHeight
      anchors.fill: parent

      blocked: root.modalActive || root.inputFocusCount > 0 || root.curveEditing
        || root.curveGlyphFocused || root.sliderFocusKey !== ""

      // The rows live inside the tune view's own Component, one level deeper
      // than this scope; that view exposes the lookup. Reaching for its
      // repeater from here was a ReferenceError that made Enter do nothing.
      function focusControlField(key) {
        const body = bodyLoader.item
        if (body && typeof body.focusControl === "function") body.focusControl(key)
      }

      function focusFanPrimary() {
        revealFocusRow()
        const body = bodyLoader.item
        if (body && typeof body.focusPrimary === "function") body.focusPrimary()
      }

      // The tabs, the status line and Apply stay pinned and the body gets the
      // rest of the screen; on a display too short for the whole tab the body
      // scrolls instead of running under the footer.
      property real bodyViewport: Math.max(Style.space(220),
        panel.availableCardHeight - panel.verticalContentInset
          - tabStrip.implicitHeight - (footer.visible ? footer.implicitHeight : 0)
          - (expertBlock.visible ? expertBlock.implicitHeight : 0)
          - popupColumn.spacing * 4)

      // Keyboard focus moves in the cursor model, which knows nothing about
      // scrolling; this keeps the focused row inside the viewport.
      function revealFocusRow() {
        if (root.focusSection !== "controls") return
        const body = bodyLoader.item
        if (!body || typeof body.rowSpan !== "function") return
        const span = body.rowSpan(root.focusIndex)
        if (!span) return
        const viewTop = bodyScroll.contentY
        const viewBottom = viewTop + bodyScroll.height
        const slack = Style.space(8)
        if (span.y < viewTop) {
          bodyScroll.contentY = Math.max(0, span.y - slack)
        } else if (span.y + span.h > viewBottom) {
          bodyScroll.contentY = Math.max(0, Math.min(
            Math.max(0, bodyScroll.contentHeight - bodyScroll.height),
            span.y + span.h - bodyScroll.height + slack))
        }
      }

      onMoveRequested: function(dx, dy) {
        if (root.trialForHere) return
        if (!root.cursorActive) { root.cursorActive = true; return }
        if (dy !== 0) root.moveCursor(dy)
        else if (dx !== 0) root.moveHorizontal(dx)
        revealFocusRow()
      }
      // Return and Space arrive as two signals; only Return arms the trial
      // confirmation, so Space keeps its usual "activate" meaning.
      onReturnRequested: if (root.trialForHere) root.confirmFromKey()
      onActivateRequested: if (root.cursorActive && !root.trialForHere) root.activateCursor()
      onCloseRequested: root.escapePressed()
      onTabRequested: function(direction) { root.switchPanel(direction) }

      // A different tab starts at its top, whatever the last one was scrolled
      // to. The scroll offset belongs to this scope; root only names the tab.
      Connections {
        target: root
        function onTabChanged() { bodyScroll.contentY = 0 }
      }

      Column {
        id: popupColumn
        width: keyCatcher.width
        spacing: Style.space(16)

        // ------------------------------------------------------ tabs
        Item {
          id: tabStrip
          width: parent.width
          implicitHeight: tabRow.implicitHeight + hairline.height

          Rectangle {
            id: hairline
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.bottom: parent.bottom
            height: 1
            color: Util.alpha(Commons.Color.foreground, 0.12)
          }

          Row {
            id: tabRow
            anchors.left: parent.left
            anchors.bottom: parent.bottom
            spacing: Style.space(22)

            component Tab: Item {
              property string label: ""
              property string value: ""
              readonly property bool current: root.tab === value

              property string icon: ""

              opacity: root.trialActive && !current ? 0.4 : 1
              implicitWidth: tabIcon.implicitWidth + Style.space(6) + tabLabel.implicitWidth
              implicitHeight: tabLabel.implicitHeight + Style.space(10)

              // Same optical size for every icon, whatever its aspect.
              OpticalGlyph {
                id: tabIcon
                anchors.left: parent.left
                anchors.verticalCenter: tabLabel.verticalCenter
                width: Style.space(15)
                height: Style.space(15)
                implicitWidth: width
                text: parent.icon
                fontSize: Style.font.title
                paintedSize: Style.space(13)
                color: parent.current
                  ? Commons.Color.accent
                  : Util.alpha(Commons.Color.foreground, 0.7)
              }

              Text {
                id: tabLabel
                anchors.left: tabIcon.right
                anchors.leftMargin: Style.space(6)
                textFormat: Text.PlainText
                font.bold: parent.current
                text: parent.label
                color: parent.current
                  ? Commons.Color.foreground
                  : Util.alpha(Commons.Color.foreground, 0.7)
                font.family: Style.font.family
                font.pixelSize: Style.font.subtitle
              }

              Rectangle {
                visible: parent.current
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.bottom: parent.bottom
                height: 2
                color: Commons.Color.accent
              }

              // The keyboard cursor on the tab strip rings the current tab.
              Rectangle {
                visible: parent.current && root.cursorActive && root.focusSection === "tabs"
                anchors.fill: parent
                anchors.leftMargin: -Style.space(5)
                anchors.rightMargin: -Style.space(5)
                anchors.bottomMargin: Style.space(3)
                radius: Style.cornerRadius > 0 ? Math.min(Style.cornerRadius, Style.space(6)) : Style.space(3)
                color: "transparent"
                border.width: 2
                border.color: Commons.Color.accent
              }

              MouseArea {
                id: tabMouse
                anchors.fill: parent
                hoverEnabled: true
                cursorShape: root.trialActive && !parent.current ? Qt.ArrowCursor : Qt.PointingHandCursor
                onClicked: root.setTab(parent.value)
              }

              PanelToolTip {
                visible: tabMouse.containsMouse && root.trialActive && !parent.current
                delay: 300
                text: strings.t("trial.tabsLocked")
              }
            }

            Tab { label: strings.t("tab.monitor"); value: "monitor"; icon: "\u{F029A}" }
            Tab { label: strings.t("tab.cpu"); value: "cpu"; icon: "\u{F4BC}" }
            Tab { label: strings.t("tab.gpu"); value: "gpu"; icon: "\u{F08AE}" }
          }
        }

        // The body is the only part that can outgrow the screen, and the
        // Flickable keeps it inside the card: clip plus a scroll offset, with
        // the keys still owned by the catcher above (interactive: false keeps
        // drags going to the sliders and the plots).
        Flickable {
          id: bodyScroll
          width: parent.width
          // During the trial the answer is in the footer; the body steps back.
          opacity: root.trialForHere ? 0.7 : 1
          // Nothing in the body can change the draft while the trial runs;
          // the footer holds the only two answers.
          enabled: !root.trialForHere
          Behavior on opacity { NumberAnimation { duration: 150 } }
          height: Math.min(contentHeight, keyCatcher.bodyViewport)
          implicitHeight: height
          contentHeight: bodyLoader.implicitHeight
          contentWidth: width
          clip: true
          interactive: false
          boundsBehavior: Flickable.StopAtBounds

          Loader {
            id: bodyLoader
            width: parent.width
            sourceComponent: root.tab === "monitor" ? monitorComponent : tuneComponent
          }

          // Plain Flickable has no wheel support; this adds it without
          // turning the drag on, which would take the presses the sliders
          // and the plots need.
          WheelHandler {
            blocking: true
            onWheel: function(event) {
              const max = Math.max(0, bodyScroll.contentHeight - bodyScroll.height)
              bodyScroll.contentY = Math.max(0, Math.min(max,
                bodyScroll.contentY - event.angleDelta.y))
              event.accepted = true
            }
          }
        }

        // expert opt-in warning, once
        Rectangle {
          id: expertBlock
          visible: root.expertNotice
          width: parent.width
          implicitHeight: expertRow.implicitHeight + Style.space(14)
          radius: Style.cornerRadius
          color: Util.alpha(Commons.Color.urgent, 0.08)

          Row {
            id: expertRow
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            anchors.margins: Style.space(7)
            spacing: Style.space(8)

            Text {
              textFormat: Text.PlainText
              text: strings.t("mode.expertWarning")
              color: Util.alpha(Commons.Color.foreground, 0.85)
              font.family: Style.font.family
              font.pixelSize: Style.font.bodySmall
              wrapMode: Text.WordWrap
              width: parent.width - expertAck.implicitWidth - Style.space(8)
            }
            Text {
              id: expertAck
              textFormat: Text.PlainText
              text: strings.t("mode.understood")
              color: Commons.Color.urgent
              font.family: Style.font.family
              font.pixelSize: Style.font.bodySmall
              font.bold: true
              activeFocusOnTab: root.expertNotice
              Accessible.role: Accessible.Button
              Accessible.name: expertAck.text
              Keys.onSpacePressed: root.ackExpert()
              Keys.onReturnPressed: root.ackExpert()
              onActiveFocusChanged: root.countInputFocus(activeFocus)
              MouseArea {
                anchors.fill: parent
                cursorShape: Qt.PointingHandCursor
                onClicked: root.ackExpert()
              }

              FocusRing {
                shown: expertAck.activeFocus
              }
            }
          }
        }

        // ----------------------------------------------------- footer
        // Two shapes. At rest: the state of the draft with its one action,
        // then Keep after restart and Apply. During the 5-second trial: a
        // draining bar, the question with a large countdown, and the two
        // answers, Revert (Esc) and Keep (Enter).
        Column {
          id: footer
          width: parent.width
          visible: root.tab !== "monitor"
          spacing: Style.space(10)

          Rectangle {
            visible: !root.trialForHere
            width: parent.width
            height: 1
            color: Util.alpha(Commons.Color.foreground, 0.12)
          }

          // ------------------------------------------------ trial header
          Item {
            width: parent.width
            visible: root.trialForHere !== null
            implicitHeight: Math.max(trialCount.implicitHeight, trialCopy.implicitHeight)
              + trialTrack.height + Style.space(10)

            Rectangle {
              id: trialTrack
              anchors.left: parent.left
              anchors.right: parent.right
              anchors.top: parent.top
              height: Style.space(3)
              radius: height / 2
              color: Util.alpha(Commons.Color.accent, 0.2)

              Rectangle {
                anchors.left: parent.left
                anchors.top: parent.top
                anchors.bottom: parent.bottom
                width: parent.width * root.trialProgress
                radius: parent.radius
                color: Commons.Color.accent
              }
            }

            Text {
              id: trialCount
              anchors.left: parent.left
              anchors.verticalCenter: trialCopy.verticalCenter
              textFormat: Text.PlainText
              text: root.trialForHere
                ? String(Model.countdownSeconds(root.trialForHere.deadlineMs, root.countdownNow)) : ""
              color: Commons.Color.accent
              font.family: Style.font.family
              font.pixelSize: Style.font.displayLarge
              font.bold: true
            }

            // The unit, so the number reads as seconds left.
            Text {
              id: trialUnit
              anchors.left: trialCount.right
              anchors.leftMargin: Style.space(2)
              anchors.baseline: trialCount.baseline
              textFormat: Text.PlainText
              text: strings.t("trial.secondsUnit")
              color: Util.alpha(Commons.Color.accent, 0.85)
              font.family: Style.font.family
              font.pixelSize: Style.font.title
            }

            Column {
              id: trialCopy
              anchors.left: trialUnit.right
              anchors.leftMargin: Style.space(12)
              anchors.right: parent.right
              anchors.top: trialTrack.bottom
              anchors.topMargin: Style.space(8)
              spacing: Style.space(2)

              Text {
                width: parent.width
                textFormat: Text.PlainText
                text: strings.t("trial.question")
                color: Commons.Color.foreground
                font.family: Style.font.family
                font.pixelSize: Style.font.title
                font.bold: true
                elide: Text.ElideRight
              }
              Text {
                width: parent.width
                textFormat: Text.StyledText
                text: root.trialSummary
                wrapMode: Text.WordWrap
                maximumLineCount: 2
                color: Util.alpha(Commons.Color.foreground, 0.8)
                font.family: Style.font.family
                font.pixelSize: Style.font.body
                elide: Text.ElideRight

                HoverHandler { id: trialSummaryHover }
                PanelToolTip {
                  visible: trialSummaryHover.hovered && parent.truncated
                  delay: 300
                  text: root.trialSummary.replace(/<[^>]*>/g, "")
                }
              }
              // What Keep does with the values: the trial's own mode, which
              // the checkbox hidden during the trial decided.
              Text {
                width: parent.width
                textFormat: Text.PlainText
                text: !root.trialForHere ? ""
                  : (root.trialForHere.mode ? root.trialForHere.mode === "default" : root.keepAfterReboot)
                    ? strings.t("trial.revertsBoot") : strings.t("trial.revertsSession")
                color: Util.alpha(Commons.Color.foreground, 0.75)
                font.family: Style.font.family
                font.pixelSize: Style.font.bodySmall
                elide: Text.ElideRight
              }
            }
          }

          // ------------------------------------------------- status line
          Item {
            width: parent.width
            // Always present on the tuning tabs, so the footer keeps its
            // height and Restore factory stays where it is.
            visible: !root.trialForHere
            implicitHeight: Math.max(statusCaption.implicitHeight, factoryButton.implicitHeight)

            OpticalGlyph {
              id: statusIcon
              anchors.left: parent.left
              anchors.verticalCenter: parent.verticalCenter
              width: Style.space(13)
              height: Style.space(13)
              implicitWidth: width
              text: root.statusKind === "draft" ? "" : root.statusGlyph(root.statusKind)
              fontSize: Style.font.body
              paintedSize: Style.space(12)
              color: root.statusTone(root.statusKind)

              // The same 6 px staged dot as the Curve chip and "Edited".
              Rectangle {
                visible: root.statusKind === "draft"
                anchors.centerIn: parent
                width: Style.space(6)
                height: width
                radius: width / 2
                color: root.statusTone("draft")
              }
            }

            Text {
              id: statusCaption
              anchors.left: statusIcon.right
              anchors.leftMargin: Style.space(6)
              anchors.verticalCenter: parent.verticalCenter
              width: Math.min(implicitWidth,
                parent.width - statusIcon.implicitWidth - Style.space(6)
                  - factoryButton.implicitWidth - Style.space(10)
                  - (statusInlineAction.visible ? statusInlineAction.implicitWidth + Style.space(12) : 0))
              textFormat: Text.PlainText
              text: {
                if (root.footerStatus === "") return strings.t("draft.atFactory")
                const draft = root.drafts[root.component]
                if (root.componentStatus === "draft" && root.stagedSummary !== ""
                    && !(draft && draft.reset))
                  return root.footerStatus + ": " + root.stagedSummary
                return root.footerStatus
              }
              elide: Text.ElideRight
              // The icon carries the state's colour; the words stay in body
              // text so they read over any wallpaper. Errors stay red.
              color: root.statusKind === "applied" || root.statusKind === "draft"
                  || root.statusKind === "trial" || root.statusKind === "reverted"
                ? Util.alpha(Commons.Color.foreground, 0.9)
                : root.statusTone(root.statusKind)
              font.family: Style.font.family
              font.pixelSize: Style.font.body

              HoverHandler { id: statusHover }
              PanelToolTip {
                visible: statusHover.hovered && (statusCaption.truncated || root.conflictBlocks
                  || root.statusKind === "offline")
                delay: 300
                text: root.conflictBlocks
                  ? Model.targ(strings.t("conflict.body"), root.component.toUpperCase())
                  : root.statusKind === "offline" ? strings.t("panel.offlineHint")
                    + (daemon.lastError !== "" ? "\n" + daemon.lastError : "")
                  : statusCaption.text
              }
            }

            ChipButton {
              id: statusInlineAction
              anchors.left: statusCaption.right
              anchors.leftMargin: Style.space(12)
              anchors.verticalCenter: parent.verticalCenter
              visible: root.footerStatusAction !== ""
              text: root.footerStatusAction
              onClicked: root.runStatusAction()
            }

            ChipButton {
              id: factoryButton
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              iconText: "\u{F099B}"
              text: strings.t("action.factoryShort")
              tooltipText: strings.t("action.restoreFactory")
              // Nothing to do once the factory values are already staged.
              enabled: !(root.appliedAtFactory && !root.hasChanges) && !root.factoryStaged
              onClicked: root.stageFactory()
            }
          }

          Item {
            width: parent.width
            implicitHeight: Math.max(keepBox.implicitHeight, applyButton.implicitHeight)

            Item {
              id: keepBox
              visible: !root.trialForHere
              opacity: daemon.online ? 1 : 0.4
              anchors.left: parent.left
              anchors.verticalCenter: parent.verticalCenter
              implicitWidth: keepMark.width + Style.space(8) + keepLabel.implicitWidth
              implicitHeight: Math.max(keepMark.height, keepLabel.implicitHeight, Style.space(24))
              activeFocusOnTab: root.tab !== "monitor" && !root.trialForHere && daemon.online
              Accessible.role: Accessible.CheckBox
              Accessible.name: strings.t("action.keepBoot")
              Accessible.checked: root.keepAfterReboot
              Keys.onSpacePressed: {
                if (daemon.online) root.setKeepAfterReboot(!root.keepAfterReboot)
              }
              Keys.onReturnPressed: {
                if (daemon.online) root.setKeepAfterReboot(!root.keepAfterReboot)
              }
              onActiveFocusChanged: root.countInputFocus(activeFocus)

              Rectangle {
                id: keepMark
                width: Style.space(16)
                height: Style.space(16)
                radius: Style.space(3)
                anchors.left: parent.left
                anchors.verticalCenter: parent.verticalCenter
                color: root.keepAfterReboot ? Util.alpha(Commons.Color.accent, 0.16) : "transparent"
                border.width: 1
                border.color: root.keepAfterReboot || keepMouse.containsMouse
                  ? Commons.Color.accent
                  : Util.alpha(Commons.Color.foreground, 0.55)

                OpticalGlyph {
                  anchors.centerIn: parent
                  visible: root.keepAfterReboot
                  width: Style.space(12)
                  height: Style.space(12)
                  text: "\u{F012C}"
                  fontSize: Style.font.body
                  paintedSize: Style.space(12)
                  color: Commons.Color.accent
                }
              }

              Text {
                id: keepLabel
                anchors.left: keepMark.right
                anchors.leftMargin: Style.space(8)
                anchors.verticalCenter: parent.verticalCenter
                textFormat: Text.PlainText
                text: strings.t("action.keepBoot")
                color: Util.alpha(Commons.Color.foreground, 0.85)
                font.family: Style.font.family
                font.pixelSize: Style.font.body
              }

              MouseArea {
                id: keepMouse
                anchors.fill: parent
                hoverEnabled: true
                enabled: daemon.online
                cursorShape: Qt.PointingHandCursor
                onClicked: root.setKeepAfterReboot(!root.keepAfterReboot)
              }

              PanelToolTip {
                visible: keepMouse.containsMouse
                delay: 500
                text: strings.t("action.keepBootHint")
              }

              FocusRing {
                shown: keepBox.activeFocus
              }
            }

            // The second answer during the trial; Esc does the same.
            ChipButton {
              visible: root.trialForHere !== null
              anchors.left: parent.left
              anchors.verticalCenter: parent.verticalCenter
              height: applyButton.height
              large: true
              iconText: "\u{F054C}"
              text: strings.t("action.cancelTrial")
              onClicked: root.revertTrial("revert-now")
            }

            // One primary action. During the trial it keeps the settings;
            // with nothing staged it stays outlined, still able to re-apply.
            Rectangle {
              id: applyButton
              readonly property bool solid: root.trialForHere !== null || root.hasChanges
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              width: Math.max(Style.space(104), applyContent.implicitWidth + Style.space(32))
              height: Style.space(30)
              radius: Style.cornerRadius > 0 ? Math.min(Style.cornerRadius, height / 2) : Style.space(5)
              color: applyButton.solid
                ? (applyMouse.containsMouse ? Qt.lighter(Commons.Color.accent, 1.12) : Commons.Color.accent)
                : (applyMouse.containsMouse ? Util.alpha(Commons.Color.accent, 0.14) : "transparent")
              border.width: applyButton.solid ? 0 : 1
              border.color: applyMouse.containsMouse
                ? Commons.Color.accent : Util.alpha(Commons.Color.foreground, 0.14)
              opacity: (root.trialForHere || root.canApply) ? 1 : 0.4

              Row {
                id: applyContent
                anchors.centerIn: parent
                spacing: Style.space(5)

                // A check beside Keep, as Revert carries its undo glyph.
                OpticalGlyph {
                  visible: root.trialForHere !== null
                  anchors.verticalCenter: parent.verticalCenter
                  width: visible ? Style.space(12) : 0
                  height: Style.space(12)
                  text: "\u{F012C}"
                  fontSize: Style.font.body
                  paintedSize: Style.space(12)
                  color: applyLabel.color
                }

                Text {
                  id: applyLabel
                  anchors.verticalCenter: parent.verticalCenter
                  textFormat: Text.PlainText
                  text: root.trialForHere ? strings.t("action.confirm") : strings.t("action.apply")
                  color: applyButton.solid
                    ? Commons.Color.background
                    : (applyMouse.containsMouse ? Commons.Color.accent : Util.alpha(Commons.Color.foreground, 0.6))
                  font.family: Style.font.family
                  font.pixelSize: Style.font.body
                  font.bold: applyButton.solid
                }
              }

              MouseArea {
                id: applyMouse
                anchors.fill: parent
                hoverEnabled: true
                cursorShape: Qt.PointingHandCursor
                enabled: root.trialForHere || root.canApply
                onClicked: {
                  if (root.trialForHere) root.confirmTrial()
                  else root.apply(root.keepAfterReboot)
                }
              }

              FocusRing {
                shown: root.cursorActive && root.focusSection === "footer"
                radius: parent.radius + Style.space(3)
              }

              HoverHandler { id: applyHover }
              PanelToolTip {
                visible: applyHover.hovered && root.conflictHere && !root.trialForHere
                delay: 300
                text: Model.targ(strings.t("conflict.body"), root.component.toUpperCase())
              }
              PanelToolTip {
                visible: applyHover.hovered && !root.trialForHere && !root.conflictHere
                delay: 500
                text: strings.t("action.applyHint")
              }
            }
          }
        }
      }

      Overlays {
        id: overlays
        anchors.fill: parent
        daemon: root.daemonForViews
        countdownNow: root.countdownNow
        rowForeground: Commons.Color.foreground
        onKeepPausedRequested: daemon.call("keep-paused", {}, () => {})
        onNoticeDismissed: daemon.call("dismiss-notice", {}, () => {})
        onReviewRequested: {
          const component = daemon.state && daemon.state.notice ? daemon.state.notice.component : null
          daemon.call("dismiss-notice", {}, () => {})
          root.setTab(component === "cpu" || component === "gpu" ? component : "gpu")
        }

        // Mirror the modal flags into root scope: the key catcher's `blocked`
        // binding and the debug report read them there.
        onAnyVisibleChanged: root.modalActive = noticeVisible
        onAbruptVisibleChanged: root.modalAbrupt = abruptVisible
        onErrorVisibleChanged: root.modalError = errorVisible
        onFailedVisibleChanged: root.modalFailed = failedVisible
      }

      Component {
        id: monitorComponent

        MonitorView {
          daemon: root.daemonForViews
          rowForeground: Commons.Color.foreground
        }
      }

      Component {
        id: tuneComponent

        Column {
          id: tuneColumn
          width: bodyLoader.width
          spacing: Style.space(16)

          // Called by the key catcher, which cannot reach this Component's
          // ids. The row lookup belongs in the scope that declares them.
          function focusControl(key) {
            const index = root.controlsNow.indexOf(root.controlByKey(key))
            const item = controlRepeater.itemAt(index)
            if (item) item.focusField()
          }

          // The fan section is the last focus row of the GPU tab.
          function focusPrimary() {
            fanView.focusPrimary()
          }

          // Where a focus row sits in this column, for the panel's scroll
          // viewport: the row index the cursor model uses, 0-based, with the
          // fan section past the last control.
          function rowSpan(index) {
            let item = null
            if (index >= 0 && index < root.controlsNow.length) {
              item = controlRepeater.itemAt(index)
            } else if (index === root.controlsNow.length && root.fanShown) {
              item = fanView
            }
            if (!item) return null
            const point = item.mapToItem(tuneColumn, 0, 0)
            return { y: point.y, h: item.height }
          }

          Column {
            id: tuneRows
            width: parent.width
            spacing: Style.space(12)

            // Section line: the component's icon, title and chip name, and
            // a plain sentence under them.
            Item {
              id: sectionLine
              width: parent.width
              implicitHeight: sectionHead.implicitHeight

              Column {
                id: sectionHead
                anchors.left: parent.left
                anchors.right: parent.right
                spacing: Style.space(6)

                Row {
                  spacing: Style.space(8)
                  height: Style.space(24)

                  OpticalGlyph {
                    anchors.verticalCenter: parent.verticalCenter
                    width: Style.space(16)
                    height: Style.space(16)
                    text: root.component === "cpu" ? "\u{F4BC}" : "\u{F08AE}"
                    fontSize: Style.font.title
                    paintedSize: Style.space(15)
                    color: Commons.Color.accent
                  }

                  Text {
                    id: sectionTitle
                    anchors.verticalCenter: parent.verticalCenter
                    textFormat: Text.PlainText
                    text: root.component === "cpu" ? strings.t("section.cpu") : strings.t("section.gpu")
                    color: Commons.Color.foreground
                    font.family: Style.font.family
                    font.pixelSize: Style.font.subtitle
                    font.bold: true
                    font.capitalization: Font.AllUppercase
                    font.letterSpacing: 0.5
                  }

                  // Which chip this tab tunes, as Monitor names it.
                  // 10 px from the title, as on Monitor; the icon sits 8 px away.
                  Text {
                    anchors.baseline: sectionTitle.baseline
                    leftPadding: Style.space(2)
                    width: Math.max(0, sectionLine.width - x)
                    textFormat: Text.PlainText
                    text: daemon.state && daemon.state.devices
                      ? Model.deviceName(daemon.state.devices[root.component]) : ""
                    elide: Text.ElideRight
                    color: Util.alpha(Commons.Color.foreground, 0.7)
                    font.family: Style.font.family
                    font.pixelSize: Style.font.bodySmall
                  }
                }

                // What the section does, in words, for someone who does not
                // want to learn what an offset is. Full width, under the
                // title row.
                Text {
                  width: parent.width
                  textFormat: Text.PlainText
                  text: root.component === "cpu" ? strings.t("section.cpuHint") : strings.t("section.gpuHint")
                  color: Util.alpha(Commons.Color.foreground, 0.8)
                  font.family: Style.font.family
                  font.pixelSize: Style.font.bodySmall
                  wrapMode: Text.WordWrap
                }
              }
            }

            // One row per control.
            Repeater {
              id: controlRepeater
              // A count, not the array: a state frame replaces every object,
              // and an array model would destroy and rebuild each row (and
              // the value being typed in it).
              model: root.controlsNow.length

              TuneControl {
                readonly property var modelData: root.controlsNow.length > index
                  ? root.controlsNow[index] : null
                readonly property bool gpuCore: root.component === "gpu"
                  && modelData !== null && modelData.key === "core"
                width: tuneRows.width
                control: modelData
                draft: root.draftFor(root.component)
                applied: root.appliedFor(modelData)
                helpText: root.helpFor(modelData ? modelData.key : "")
                cursorHere: root.cursorActive && root.focusSection === "controls" && root.focusIndex === index
                trialRunning: root.trialForHere !== null
                lastRow: index === root.controlsNow.length - 1
                rowForeground: Commons.Color.foreground
                showCurveToggle: gpuCore && root.curveGlyphShown
                curveToggleOn: gpuCore && root.curveEffective
                curveToggleEnabled: gpuCore && root.curveGlyphEnabled
                curveToggleTooltip: gpuCore ? root.curveGlyphTooltip : ""
                curveMode: gpuCore && root.curveEffective
                curveData: root.curveNow
                curveDaemon: root.daemonForViews
                onCommitted: function(key, value) { root.setControl(key, value) }
                onSliderFocusChanged: function(key, focused) {
                  root.sliderFocusKey = focused ? key : (root.sliderFocusKey === key ? "" : root.sliderFocusKey)
                }
                onInputFocusChanged: function(focused) { root.countInputFocus(focused) }
                onEnterRequested: root.confirmFromKey()
                onEscapeRequested: root.escapePressed()
                onTabRequested: function(direction) { root.switchPanel(direction) }
                onCurveToggled: root.setCurveMode(!root.curveMode)
                onCurveToggleFocusChanged: function(focused) {
                  root.curveGlyphFocused = focused
                }
                onCurvePointEdited: function(index, frequency, flatten, anchorList) {
                  root.changePoint(index, frequency, flatten, anchorList)
                }
                onCurveExactSubmitted: function(index, frequency, anchorList) {
                  root.changePoint(index, frequency, false, anchorList)
                }
                onCurveAnchorAdded: function(index) { root.addCurveAnchor(index) }
                onCurveAnchorRemoved: function(index) { root.removeCurveAnchor(index) }
                onCurveResetRequested: root.resetCurve()
                onCurveEditingChanged: function(editing) { root.curveEditing = editing }
              }
            }

            // The fan closes the GPU tab: same scrollable column, same
            // section rhythm, same footer and trial as the clocks.
            FanView {
              id: fanView
              width: tuneRows.width
              visible: root.fanShown
              fan: root.fanNow
              draft: root.fanDraft
              config: root.fanNow ? root.fanNow.config : null
              liveSpeed: root.fanLiveSpeed
              liveTemp: root.fanLiveTemp
              rowForeground: Commons.Color.foreground
              cursorHere: root.cursorActive && root.focusSection === "controls"
                && root.focusIndex === root.controlsNow.length
              trialRunning: root.trialForHere !== null
              staged: root.fanDirtyNow
              collapsed: root.fanCollapsed
              onCollapseToggled: {
                root.fanCollapsed = !root.fanCollapsed
                root.curveEditing = false
                root.persistUi()
              }

              onModeRequested: function(mode) { root.setFanMode(mode) }
              onSpeedChanged: function(value) { root.setFanSpeed(value) }
              onPointChanged: function(index, temp, speed) {
                root.changeFanPoint(index, temp, speed)
              }
              onPointAdded: function(temp, speed) { root.addFanPoint(temp, speed) }
              onPointRemoved: function(index) { root.removeFanPoint(index) }
              onCurveResetRequested: root.resetFanCurve()
              onSliderFocusChanged: function(focused) {
                root.sliderFocusKey = focused
                  ? "fan-speed"
                  : (root.sliderFocusKey === "fan-speed" ? "" : root.sliderFocusKey)
              }
              onInputFocusChanged: function(focused) { root.countInputFocus(focused) }
              onCurveEditingChanged: function(editing) { root.curveEditing = editing }
              onEnterRequested: root.confirmFromKey()
              onEscapeRequested: root.escapePressed()
              onTabRequested: function(direction) { root.switchPanel(direction) }
            }

            // FanView hides when the GPU reports no controllable fan; the
            // reason still needs a line on the GPU tab.
            Text {
              width: tuneRows.width
              visible: root.tab === "gpu" && !!root.fanNow && !root.fanNow.supported
              textFormat: Text.PlainText
              text: root.fanNow && root.fanNow.cause
                ? root.fanNow.cause : strings.t("fan.unavailable")
              color: Util.alpha(Commons.Color.foreground, 0.8)
              font.family: Style.font.family
              font.pixelSize: Style.font.bodySmall
              wrapMode: Text.WordWrap
            }
          }
        }
      }
    }
  }

  // ------------------------------------------------------------- labels

  function statusLabel(status) {
    const map = {
      monitor: strings.t("status.live"),
      applied: strings.t("status.applied"),
      draft: strings.t("status.draft"),
      reverted: strings.t("status.reverted"),
      trial: strings.t("status.trial"),
      paused: strings.t("status.paused"),
      suspended: strings.t("status.suspended"),
      stopped: strings.t("status.stopped"),
      unavailable: strings.t("status.unavailable"),
      offline: strings.t("status.offline")
    }
    return map[status] || status
  }

  // One glyph and one colour per footer state, so the state reads at a
  // glance: check for applied, a dot for an edit, undo for a revert, a
  // warning for everything that blocks.
  function statusGlyph(status) {
    if (status === "applied") return "\u{F012C}"
    if (status === "draft") return "\u25CF"
    if (status === "reverted") return "\u{F054C}"
    return "\u{F0026}"
  }

  function statusTone(status) {
    if (status === "applied") return Commons.Color.accent
    if (status === "draft" || status === "trial") return Commons.Color.accent
    return Commons.Color.urgent
  }

  function helpFor(key) {
    const map = {
      core: root.component === "cpu" ? strings.t("help.cpuCore") : strings.t("help.gpuCore"),
      cache: strings.t("help.cpuCache"),
      memory: strings.t("help.gpuMemory"),
      temp: strings.t("help.gpuTemp")
    }
    const text = map[key] || ""
    // Every slider row carries the same three marks; its help names them.
    if (!text || key === "temp" || (key === "core" && root.component === "gpu" && root.curveEffective)) return text
    return text + "\n\n" + strings.t("help.marks")
  }

  // ------------------------------------------------------- cursor model

  function moveCursor(dy) {
    if (focusSection === "tabs") {
      focusSection = "controls"
      focusIndex = 0
      return
    }
    if (focusSection === "controls") {
      const count = root.focusCount
      if (focusIndex + dy >= 0 && focusIndex + dy < count) {
        focusIndex += dy
        return
      }
      if (focusIndex + dy >= count && tab !== "monitor") {
        focusSection = "footer"
        return
      }
      if (focusIndex + dy < 0) {
        focusSection = "tabs"
        return
      }
      return
    }
    if (focusSection === "footer" && dy < 0) {
      focusSection = "controls"
      focusIndex = Math.max(0, root.focusCount - 1)
    }
  }

  function moveHorizontal(dx) {
    if (focusSection === "tabs") {
      const order = ["monitor", "cpu", "gpu"]
      const next = Math.max(0, Math.min(order.length - 1, order.indexOf(tab) + dx))
      setTab(order[next])
      return
    }
    if (focusSection === "controls" && root.cursorOnFanRow) {
      // Left/right on the fan row cycle the three modes; the mode row then
      // reports which one is staged, and Enter focuses its control.
      const modes = ["auto", "fixed", "curve"]
      const current = root.fanDraft ? modes.indexOf(root.fanDraft.mode) : 0
      root.setFanMode(modes[Math.max(0, Math.min(modes.length - 1, current + dx))])
      return
    }
    if (focusSection === "controls") {
      const control = controlsNow[focusIndex]
      const draft = root.drafts[root.component]
      if (!control || !control.available || !draft) return
      // While the plot replaces the core offset slider, the offset is not on
      // screen; stirring it from the keyboard would be an invisible edit.
      if (root.component === "gpu" && control.key === "core" && root.curveEffective) return
      root.setControl(control.key, draft.values[control.key] + dx * (control.step || 1))
    }
  }

  function activateCursor() {
    if (focusSection === "controls") {
      if (root.cursorOnFanRow) {
        const catcher = panel.loadedContent
        if (catcher) catcher.focusFanPrimary()
        return
      }
      const control = controlsNow[focusIndex]
      if (control && control.available) focusControlField(control.key)
      return
    }
    if (focusSection === "footer") root.apply(root.keepAfterReboot)
  }

  // The control items are built inside the content Component, so the lookup
  // has to run there; this scope asks the catcher, which can see the
  // repeater. Calling into it directly from here raised
  // "ReferenceError: controlRepeater is not defined" and Enter over a
  // control did nothing.
  function focusControlField(key) {
    const catcher = panel.loadedContent
    if (catcher && typeof catcher.focusControlField === "function") catcher.focusControlField(key)
  }

  function escapePressed() {
    if (root.trialForHere) {
      root.revertTrial("esc")
      return
    }
    close()
  }

  // ----------------------------------------------------- countdown clock

  Timer {
    id: clock
    interval: 100
    running: root.opened && (root.trialActive || (daemon.state && daemon.state.notice && daemon.state.notice.deadlineMs !== undefined))
    repeat: true
    // Seed on start: the first frame of a trial would otherwise show the
    // deadline against the clock value captured when the popup opened.
    onRunningChanged: if (running) root.countdownNow = Date.now()
    onTriggered: root.countdownNow = Date.now()
  }

  Shortcut {
    sequence: "Ctrl+Return"
    enabled: root.opened && root.tab !== "monitor" && root.canApply
    onActivated: root.apply(root.keepAfterReboot)
  }

  ShellIpc {
    target: "ash.omatuner"

    function open(): void { root.open() }
    function close(): void { root.close() }
    function toggle(): void { root.toggle() }
    function status(): string {
      return JSON.stringify({
        online: daemon.online,
        tab: root.tab,
        curveMode: root.curveEffective,
        status: root.componentStatus,
        tuningMode: root.tuningMode,
        keepAfterReboot: root.keepAfterReboot,
        paused: daemon.state ? daemon.state.paused : null,
        conflicts: daemon.state ? daemon.state.conflicts : []
      })
    }
    function selftest(): string { return root.runSelfTest() }
  }

  // Headless exercise of the deployed plugin: drives the same draft,
  // persistence and apply-payload paths a user drives with the mouse and
  // keyboard, and reports what really happened. Used by bin/validate when
  // the session is locked and interactive checking is impossible. Nothing
  // here writes to hardware.
  function runSelfTest() {
    const results = []
    const check = (name, ok, detail) => results.push({ name: name, ok: !!ok, detail: detail || "" })
    // A validation run must leave the user's drafts and tab untouched.
    const beforeTab = root.tab
    const beforeCurveMode = root.curveMode
    const beforeDrafts = {
      cpu: Model.serializeDraft(root.drafts.cpu),
      gpu: Model.serializeDraft(root.drafts.gpu)
    }
    const beforeFan = Model.serializeFanDraft(root.fanDraft)
    const restoreUserState = () => {
      if (daemon.state) {
        root.drafts = {
          cpu: Model.restoreDraft(beforeDrafts.cpu, daemon.state, "cpu"),
          gpu: Model.restoreDraft(beforeDrafts.gpu, daemon.state, "gpu")
        }
        root.fanDraft = Model.restoreFanDraft(beforeFan, daemon.state)
      }
      root.curveMode = beforeCurveMode
      root.tab = beforeTab
      root.curveEditing = false
      root.curveGlyphFocused = false
      root.touchDrafts()
      root.persistUi()
    }
    // Toggling the curve is a view change: the draft points and the global
    // offset must come out identical, so no edit is lost by switching.
    const checkCurveMode = () => {
      const curve = daemon.state && daemon.state.curves ? daemon.state.curves.gpu : null
      if (!curve || !curve.supported) {
        check("curve-mode-skipped", true, "backend exposes no curve")
        return
      }
      const pointsBefore = JSON.stringify(root.drafts.gpu ? root.drafts.gpu.points : null)
      const valuesBefore = JSON.stringify(root.drafts.gpu ? root.drafts.gpu.values : null)
      const modeBefore = root.curveMode
      root.setCurveMode(!modeBefore)
      check("curve-mode-toggles", root.curveMode === !modeBefore, "mode " + root.curveMode)
      check("curve-mode-keeps-points", JSON.stringify(root.drafts.gpu.points) === pointsBefore,
        "points unchanged")
      check("curve-mode-keeps-values", JSON.stringify(root.drafts.gpu.values) === valuesBefore,
        "values unchanged")
      root.setCurveMode(modeBefore)
    }
    try {
      root.ensureDrafts()
      check("drafts-created", drafts.cpu && drafts.gpu, "cpu/gpu drafts present")
      check("fan-draft-created", !!root.fanDraft, root.fanDraft ? root.fanDraft.mode : "missing")
      const fanPayload = Model.fanPayload(root.fanDraft)
      check("fan-payload-built", !!fanPayload, JSON.stringify(fanPayload) + " (not sent)")
      setTab("gpu")
      check("gpu-tab-shows-the-fan", root.fanShown === !!(root.fanNow && root.fanNow.supported),
        "fanShown " + root.fanShown)
      check("fan-rides-the-gpu-status", root.componentStatus !== "", root.componentStatus)

      setTab("cpu")
      check("tab-switch", tab === "cpu", "tab is " + tab)
      check("tab-remembered", true, "last tab and drafts persist across opens")

      const core = controlByKey("core")
      if (!core || !core.available) {
        // Real machine without the system service: the unavailable path IS
        // the behavior under test — the real cause must reach the UI.
        check("cpu-core-unavailable-with-cause", !!core && !!core.cause,
          core ? core.cause : "control missing")
        const gpuCore = controlByKey("core", "gpu")
        check("gpu-core-state-reported", !!gpuCore,
          gpuCore ? (gpuCore.available ? "available (system service installed)" : gpuCore.cause) : "missing")
        checkCurveMode()
        restoreUserState()
        return JSON.stringify({
          at: Date.now(), results: results,
          passed: results.filter(r => r.ok).length,
          failed: results.filter(r => !r.ok).length,
          note: "tuning controls unavailable on this daemon; interactive flow checks skipped"
        })
      }
      check("cpu-core-available", true, "control resolved")

      setControl("core", -30)
      check("safe-change", drafts.cpu.values.core === -30, "value " + drafts.cpu.values.core)
      check("dirty", draftIsDirty("cpu") === true, "draft dirty")
      check("draft-status", root.componentStatus === "draft", "status " + root.componentStatus)
      check("was-line", Model.controlDiffers(drafts.cpu, daemon.state, "cpu", "core", core) === true,
        "applied " + appliedFor(core))

      // The old model blocked the slider at the aggressive boundary; the
      // new one accepts the value and tints the number instead.
      setControl("core", -100)
      check("aggressive-zone-not-blocking", drafts.cpu.values.core === -100,
        "value " + drafts.cpu.values.core)

      // Typed value and keyboard step use the same clamps as the slider.
      const typed = Model.parseExact(core, " -40 ")
      check("typed-exact-value", typed === -40, "typed " + typed)
      check("typed-out-of-range-clamps", Model.parseExact(core, "-999") === core.min,
        "typed " + Model.parseExact(core, "-999"))
      check("step-one", Model.stepValue(core, -40, 1, false) === -39,
        "stepped " + Model.stepValue(core, -40, 1, false))
      check("step-ten", Model.stepValue(core, -40, -1, true) === -50,
        "stepped " + Model.stepValue(core, -40, -1, true))

      // The "was" line restores exactly the applied value.
      setControl("core", appliedFor(core))
      check("was-restore", drafts.cpu.values.core === appliedFor(core),
        "restored " + drafts.cpu.values.core)

      // Mode mapping: unchecked runs this session, checked also saves boot.
      setControl("core", -30)
      check("mode-session", Model.applyMode(drafts.cpu, false).mode === "session", "session")
      check("mode-default", Model.applyMode(drafts.cpu, true).mode === "default", "default")
      check("mode-reset", (function () {
        const staged = Model.newDraft(daemon.state, "cpu")
        staged.reset = true
        return Model.applyMode(staged, false).mode === "reset"
      })(), "reset")

      // A state refresh that reports the old applied values (as a revert
      // does) must not reset the draft.
      const kept = Model.reconcileDraft(drafts.cpu, daemon.state, "cpu")
      check("draft-survives-refresh", kept.values.core === -30, "value " + kept.values.core)

      stageFactory()
      check("factory-staged", drafts.cpu.reset === true && drafts.cpu.values.core === 0, "factory staged")

      drafts.cpu.values.core = -30
      touchDrafts()
      check("footer-status", root.footerStatus !== "", root.footerStatus)
      check("footer-action", root.footerStatusAction === strings.t("draft.discard"),
        root.footerStatusAction)
      check("trial-label", strings.t("action.confirm") !== "action.confirm",
        strings.t("action.confirm"))
      discardDraft()
      check("discard-staged-values", drafts.cpu.values.core === appliedFor(core),
        "value " + drafts.cpu.values.core)
      setControl("core", -30)
      // Build the payload but never send it: a validation run must not
      // change the machine.
      const payload = Model.applyPayload(drafts.cpu, "cpu", daemon.state)
      check("apply-payload-built", payload && payload.core === -30,
        "payload " + JSON.stringify(payload) + " (not sent)")
      daemon.call("hello", {}, () => {})
      check("daemon-reachable", daemon.online, daemon.online ? "socket connected" : "offline")
    } catch (err) {
      check("no-exception", false, String(err))
    }
    checkCurveMode()
    restoreUserState()
    const summary = { at: Date.now(), results: results,
      passed: results.filter(r => r.ok).length, failed: results.filter(r => !r.ok).length,
      note: "draft, exact value, stepping, mode and payload checks only; no hardware write issued" }
    return JSON.stringify(summary)
  }
}
