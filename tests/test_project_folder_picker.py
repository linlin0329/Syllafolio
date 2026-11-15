import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import time
import unittest

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialogButtonBox

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from syllafolio.annotation_desktop_project_picker import ProjectFolderDialog


class ProjectFolderPickerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def wait(self, predicate):
        deadline = time.monotonic() + 10
        while not predicate() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.005)
        self.assertTrue(predicate())

    def test_navigate_then_choose_project_or_cancel(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            project = root / "nest" / "sample project"
            project.mkdir(parents=True)
            (project / "project.json").write_text("{}", encoding="utf-8")
            dlg = ProjectFolderDialog(None, root)
            dlg.open()
            self.wait(lambda: dlg.folders.count() == 1)
            dlg.folders.setCurrentRow(0)
            choose = dlg.buttons.button(QDialogButtonBox.StandardButton.Open)
            choose.click()
            self.wait(lambda: dlg.current_folder == project.parent)
            self.assertTrue(dlg.isVisible())
            dlg.folders.setCurrentRow(0)
            choose.click()
            self.wait(lambda: not dlg.isVisible())
            self.assertEqual(dlg.selected_project, project)

            dlg = ProjectFolderDialog(None, root)
            dlg.open()
            QTest.keyClick(dlg, Qt.Key.Key_Escape)
            self.assertIsNone(dlg.selected_project)
            self.assertFalse(dlg.isVisible())

    def test_typing_replaces_selection_and_invalid_path_can_be_corrected(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "distractor").mkdir()
            project = root / "bird-试用"
            project.mkdir()
            (project / "project.json").touch()
            dlg = ProjectFolderDialog(None, root)
            dlg.open()
            self.wait(lambda: dlg.folders.count() == 2)
            dlg.folders.setCurrentRow(0)
            dlg.path_edit.selectAll()
            QTest.keyClicks(dlg.path_edit, "missing")
            choose = dlg.buttons.button(QDialogButtonBox.StandardButton.Open)
            choose.click()
            self.wait(lambda: "Cannot read" in dlg.message.text())
            self.assertIsNone(dlg.selected_project)
            dlg.path_edit.setText(os.fspath(project))
            QTest.keyClick(dlg.path_edit, Qt.Key.Key_Return)
            self.wait(lambda: dlg.selected_project == project)
