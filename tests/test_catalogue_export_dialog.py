"""Exercise export consent, frozen scope disclosure and report selection in the real dialog."""
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QApplication, QWidget

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from syllafolio.annotation_desktop_export import CHOOSE, ExportDialog, ExportError


class Owner(QWidget):
    operationFailed = Signal(str)


class CatalogueExportDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.owner = Owner()
        self.addCleanup(self.owner.close)
        self.owner._busy = False
        self.owner._close_coordinator = SimpleNamespace(generation=1)
        self.inventory = dict(all_item_ids=["a", "b", "c"], scope_item_ids=["b"], outside_scope_item_ids=["a", "c"],
                              example_item_ids=["a", "b"], outside_scope_example_ids=["a"], audit_scope="task-wide")
        self.options = [dict(job_id=f"assessment-{i:020x}", status="completed", eligible_for_complete=True,
                             stale_for_current=False, created_at="2026-11-15T12:00:00+00:00") for i in (1, 2)]
        section = dict(scope=dict(declared=True, target_level="family", scope_ids=["b"]),
                       counts=dict(n_scope=1, n_task=3, media_examples=2, assigned=1, unreviewed=0),
                       assessment_options=self.options, inventory=self.inventory, human_completion_current=True,
                       can_export_draft=True, can_export_complete=True, current_fingerprint="snapshot", jobs=[])
        self.owner.state = dict(revision=1, catalogue_scope=dict(purpose="Share the family criterion"),
                                final_review={"publication_report": section})
        self.owner.project = SimpleNamespace(project_dir=Path(temporary.name), metadata={"project_id":"p"},
            dataset=SimpleNamespace(input_fingerprint="audio"), service=SimpleNamespace(run_id="run"),
            snapshot=lambda:self.owner.state)
        self.dialog = ExportDialog(self.owner)
        self.addCleanup(self.dialog.close)
        self.dialog.show()

    def test_scope_disclosure_and_incoming_report(self):
        d = self.dialog
        self.assertIn("1 of 3", d.scope_text.text())
        self.assertIn("all 3 syllables (2 outside", d.inventory_text.text())
        self.assertIn("including 1 outside", d.inventory_text.text())
        d.members_button.click()
        self.assertIn("Examples outside this scope (1)\na", d.inventory_detail.toPlainText())
        self.assertEqual(d.assessment.currentData(), CHOOSE)
        exact = self.options[0]["job_id"]
        d.select_assessment(exact)
        self.assertEqual(d.mode.currentData(), "complete")
        self.assertEqual(d.assessment.currentData(), exact)
        self.assertEqual(d.purpose.text(), "Share the family criterion")
        d.reason.setText("Requested by reviewer")
        self.assertFalse(d.start_button.isEnabled())
        d.confirmation.click()
        self.assertTrue(d.start_button.isEnabled())
        with patch.object(d, "_pick_path", return_value=""), patch.object(d, "_run") as submit:
            d.start_export()
            submit.assert_not_called()
        self.options[0].update(stale_for_current=True, eligible_for_complete=False)
        self.owner.state["revision"] += 1
        d.refresh()
        self.assertEqual(d.assessment.currentData(), exact)
        self.assertEqual(d.mode.currentData(), "complete")
        self.assertFalse(d.confirmation.isChecked())
        d.confirmation.click()
        self.assertFalse(d.start_button.isEnabled())

    def test_scope_change_during_save_chooser_refuses_assent(self):
        d = self.dialog
        d.select_assessment(self.options[0]["job_id"])
        d.reason.setText("Deliver selected report")
        d.confirmation.click()
        def choose(_suggested):
            self.owner.state["revision"] += 1
            return "different.zip"
        with patch.object(d, "_pick_path", side_effect=choose), patch.object(d, "_run") as submit:
            with self.assertRaisesRegex(ExportError, "REVISION_CONFLICT"):
                d.start_export()
            submit.assert_not_called()
