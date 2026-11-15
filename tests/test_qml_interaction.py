"""Drive the shipped GPU tab with real Qt mouse and keyboard events.

The three bugs the user hit on the real GTX 1060 were rendered behaviour, not
model arithmetic: the memory row stayed at 0 after a typed value or a restore,
and the curve did not move while an anchor was dragged. `tests/qml/
tst_interaction.qml` loads the actual TuneControl/TuneSlider/CurveView with the
GPU state captured read-only from `omatuner status --json`, sends QtTest mouse
and keyboard events, and asserts on the rendered Text, knob positions, "was"
line and plot handles.

The test skips, with a reason, when Qt Quick Test is not installed. Run from
the project root:

    python -m unittest tests.test_qml_interaction -v
    python -m unittest discover -s tests

Set QMLTESTRUNNER to an explicit runner path if it is not on PATH.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
QML_DIR = os.path.join(HERE, "qml")
STUBS = os.path.join(QML_DIR, "stubs")
HARNESS = os.path.join(QML_DIR, "tst_interaction.qml")
FAN_HARNESS = os.path.join(QML_DIR, "tst_fan.qml")
STATE = os.path.join(QML_DIR, "RealGpuState.js")
PANEL = os.path.join(ROOT, "ui", "ash.omatuner", "Panel.qml")
TRANSLATIONS = os.path.join(STUBS, "qs", "Commons", "Translations.qml")
EN_CATALOG = os.path.join(ROOT, "ui", "ash.omatuner", "i18n", "en.json")

MISSING_MODULE = (
    'module "QtTest" is not installed',
    'module "QtQuick" is not installed',
    "Cannot find module 'QtTest'",
)


def find_runner() -> str | None:
    override = os.environ.get("QMLTESTRUNNER")
    if override:
        return override if os.path.exists(override) else None
    found = shutil.which("qmltestrunner")
    if found:
        return found
    # Arch's qt6-declarative installs the runner outside PATH.
    bundled = "/usr/lib/qt6/bin/qmltestrunner"
    return bundled if os.path.exists(bundled) else None


class RenderedInteractionTests(unittest.TestCase):
    def run_harness(self, harness: str, expected_passed: int) -> None:
        runner = find_runner()
        if not runner:
            self.skipTest(
                "Qt Quick Test runner (qmltestrunner) is not installed; "
                "install qt6-declarative-dev-tools or set QMLTESTRUNNER"
            )
        for path, reason in (
            (harness, f"the interaction harness {harness} is missing"),
            (STATE, f"the captured GPU state {STATE} is missing"),
            (os.path.join(STUBS, "qs", "Commons", "qmldir"), "the qs.Commons stub is missing"),
            (os.path.join(STUBS, "qs", "Ui", "qmldir"), "the qs.Ui stub is missing"),
        ):
            if not os.path.exists(path):
                self.skipTest(reason)

        env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
        try:
            proc = subprocess.run(
                [runner, "-import", STUBS, "-input", harness],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=25,
            )
        except subprocess.TimeoutExpired:
            self.fail("the rendered interaction harness did not finish within 25 s")
        output = proc.stdout + proc.stderr
        if any(marker in output for marker in MISSING_MODULE):
            self.skipTest(
                "the Qt Quick Test QML modules are not installed; "
                "install qt6-declarative-dev-tools:\n" + output
            )
        totals = re.search(r"Totals: (\d+) passed, (\d+) failed, (\d+) skipped", output)
        if totals is None:
            self.fail("qmltestrunner produced no result line:\n" + output)
        passed, failed, skipped = (int(group) for group in totals.groups())
        self.assertEqual(failed, 0, output)
        self.assertEqual(skipped, 0, output)
        self.assertEqual(passed, expected_passed, output)

    def test_gpu_tab_interactions(self):
        self.run_harness(HARNESS, 10)

    def test_fan_tab_interactions(self):
        self.run_harness(FAN_HARNESS, 13)


class StubCatalogTests(unittest.TestCase):
    """The stub catalog must not drift from the shipped one."""

    def test_stub_keys_exist_in_the_shipped_catalog(self):
        with open(TRANSLATIONS, encoding="utf-8") as fh:
            stub = fh.read()
        with open(EN_CATALOG, encoding="utf-8") as fh:
            catalog = json.load(fh)
        keys = re.findall(r'^\s*"([^"]+)":', stub, re.M)
        missing = [key for key in keys if key not in catalog]
        self.assertEqual(missing, [], "the stub catalog names keys en.json lacks")


class PanelDraftWiringTests(unittest.TestCase):
    """The harness mirrors Panel.touchDrafts; keep the two in step.

    If Panel went back to reassigning the same draft objects, the rendered
    harness would still pass because its own copy is correct. This pins the
    production wiring that makes the value, knob and curve bindings refresh.
    """

    def test_touch_drafts_copies_both_components(self):
        with open(PANEL, encoding="utf-8") as fh:
            source = fh.read()
        start = source.index("function touchDrafts()")
        body = source[start:start + 400]
        self.assertIn("Model.copyDraft(root.drafts.cpu)", body)
        self.assertIn("Model.copyDraft(root.drafts.gpu)", body)
        self.assertNotIn("cpu: root.drafts.cpu,\n      gpu: root.drafts.gpu", body)


if __name__ == "__main__":
    unittest.main()
