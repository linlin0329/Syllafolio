"""Exercise both project-wide entry points and the scope declaration boundary."""
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import time
import unittest

import numpy as np
import soundfile as sf
from PySide6.QtCore import Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QApplication

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from syllafolio.annotation_desktop import MainWindow
from syllafolio.annotation_desktop_project import create_project


class FinalChecksNavigation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def wait_for(self, predicate):
        deadline = time.monotonic() + 20
        while not predicate():
            self.app.processEvents()
            if time.monotonic() > deadline:
                self.fail("Timed out waiting for the desktop operation")
            time.sleep(.01)

    def test_scope_is_explicit_and_history_is_separate(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            clips = root / "clips"
            clips.mkdir()
            t = np.arange(4800) / 48000
            for i, hz in enumerate((3100, 4200)):
                sf.write(clips / f"sample-{i}.wav", .05 * np.sin(2*np.pi*hz*t), 48000)
            window = MainWindow(diagnostics_dir=root / "logs")
            try:
                window.show()
                self.assertFalse(window.final_checks_button.isEnabled())
                project = create_project(clips, root / "project")
                window._install_project(project)
                revision = project.snapshot()["revision"]
                window.final_checks_button.click()
                dialog = window._final_review_dialog
                self.assertIs(dialog.pages.currentWidget(), dialog.overview_page)
                self.assertFalse(dialog.begin_button.isEnabled())
                self.assertTrue(all(not c.isVisible() for c in dialog.checks.values()))
                dialog.edit_scope_button.click()
                self.assertIs(dialog.pages.currentWidget(), dialog.scope_page)
                self.assertEqual(dialog.scope_mode.currentData(), "all")
                self.assertFalse(dialog.scope_items.isVisible())
                dialog.scope_mode.setCurrentIndex(dialog.scope_mode.findData("local"))
                self.assertTrue(dialog.scope_items.isVisible())
                self.assertIsNone(project.snapshot().get("catalogue_scope"))
                self.assertEqual(project.snapshot()["revision"], revision)
                window.findChild(QAction, "finalWorkspace").trigger()
                self.assertIs(window._final_review_dialog, dialog)
                self.assertIs(dialog.pages.currentWidget(), dialog.overview_page)
                window.findChild(QAction, "checkpointWorkspace").trigger()
                history = window._checkpoint_dialog
                self.assertIsNot(history, dialog)
                self.assertTrue(history.history_only)
                self.assertFalse(dialog.history_only)
                self.assertIs(history.pages.currentWidget(), history.history_page)
                self.assertEqual(project.snapshot()["revision"], revision)
                history.close()
                dialog.edit_scope_button.click()
                dialog.scope_mode.setCurrentIndex(dialog.scope_mode.findData("local"))
                for index in range(dialog.scope_items.rowCount()):
                    dialog.scope_items.item(index, 0).setCheckState(
                        Qt.CheckState.Checked if index == 0 else Qt.CheckState.Unchecked)
                selected = dialog.scope_items.item(0, 1).text()
                self.assertTrue(dialog.scope_button.isEnabled())
                dialog.scope_button.click()
                self.wait_for(lambda: not window._busy and dialog.pages.currentWidget() is dialog.overview_page)
                saved = project.snapshot()["catalogue_scope"]
                self.assertEqual(saved["scope_ids"], [selected])
                self.assertEqual(dialog._view["outcome_counts"]["unfinished"], 1)
                self.assertFalse(dialog.begin_button.isEnabled())
            finally:
                window.close()
                self.wait_for(lambda: window.project is None)
