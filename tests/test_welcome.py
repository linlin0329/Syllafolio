"""Public project entry points, operating mode and offline help."""
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from PySide6.QtCore import QSettings
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox, QFileDialog, QMessageBox, QTextBrowser

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from syllafolio import __version__
from syllafolio.annotation_desktop import MainWindow, ProjectContractDialog


class WelcomeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_blank_actions_open_their_respective_routes_and_help(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            settings = QSettings(str(root / "test.ini"), QSettings.Format.IniFormat)
            with patch("syllafolio.annotation_desktop.QSettings", return_value=settings):
                window = MainWindow(diagnostics_dir=root / "logs")
            try:
                window.show()
                start = window.welcome_page
                self.assertIs(window.workspace_stack.currentWidget(), start)
                self.assertFalse(window.main_splitter.isVisible())
                with patch.object(QFileDialog, "getExistingDirectory", return_value="") as picker:
                    start.syllables_button.click()
                    picker.assert_called_once()
                start.recording_button.click()
                self.assertTrue(window._interval_import_dialog.isVisible())
                window._interval_import_dialog.close()
                start.open_button.click()
                self.assertTrue(window._project_picker.isVisible())
                window._project_picker.reject()
                self.assertTrue(start.isVisible())
                window.findChild(QAction, "userGuide").trigger()
                self.assertIn("Start and use your own project",
                              window._guide_dialog.findChild(QTextBrowser).toPlainText())
                window._guide_dialog.close()
                with patch.object(QMessageBox, "about") as about:
                    window.findChild(QAction, "aboutSyllafolio").trigger()
                    self.assertIn(__version__, about.call_args.args[-1])
            finally:
                window.close()
                self.app.processEvents()

    def test_daily_identity_requires_operator_and_engineering_is_explicit(self):
        dialog = ProjectContractDialog()
        dialog.show()
        try:
            self.assertEqual(dialog.options(), {"purpose":"daily_annotation", "operator_id":"", "budget_seconds":None})
            self.assertFalse(dialog.advanced_panel.isVisible())
            dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Ok).click()
            self.assertTrue(dialog.isVisible())
            self.assertTrue(dialog.problem.text())
            dialog.operator.setText("Reviewer A")
            dialog.advanced.click()
            self.assertTrue(dialog.purpose.isVisible())
            self.assertFalse(dialog.budget.isVisible())
            dialog.purpose.setCurrentIndex(dialog.purpose.findData("engineering"))
            self.assertTrue(dialog.budget.isVisible())
            dialog.budget.setValue(600)
            dialog.advanced.click()
            self.assertFalse(dialog.advanced_panel.isVisible())
            self.assertIn("Engineering", dialog.advanced.text())
            self.assertEqual(dialog.options(), {"purpose":"engineering", "operator_id":"Reviewer A", "budget_seconds":600})
            dialog._accept_contract()
            self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
        finally:
            dialog.close()
