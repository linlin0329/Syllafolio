"""Preparation, historical runs and report selection in the assessment workspace."""
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import patch

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QApplication, QWidget

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from syllafolio.annotation_desktop_assessment import AssessmentDialog, AssessmentError


class Owner(QWidget):
    operationFailed = Signal(str)


class AssessmentWorkspace(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.owner = Owner()
        self.addCleanup(self.owner.close)
        self.owner._busy = False
        self.owner._close_coordinator = SimpleNamespace(state="open", generation=9)
        self.data = dict(can_start=True, current_fingerprint="context",
                         coverage=dict(n_scope=8, n_total=8, n_eligible=0, n_families=1, n_eligible_families=0),
                         support=[dict(family_id="f1", n_confirmed=7, n_in_scope=7, n_known_source=6,
                                       n_unknown_source=1, n_source_groups=2, eligible=False,
                                       reasons=[dict(code="INSUFFICIENT_ITEMS"), dict(code="INSUFFICIENT_SOURCE_GROUPS")])],
                         jobs=[], current_job_id=None, latest_job_id=None)
        s = dict(revision=3, final_review=dict(assessment=self.data), current_completion_id="done",
                 memberships=[dict(local_type_id="f1", parent_type_id=None, member_ids=[])],
                 type_cards=dict(f1=dict(title="Trill")), catalogue_scope=dict(scope_ids=["s1"]))
        self.owner.state = s
        self.owner.project = SimpleNamespace(
            service=SimpleNamespace(run_id="run"), project_dir=Path(temp.name),
            dataset=SimpleNamespace(input_fingerprint="input"), metadata={"project_id": "project"}, snapshot=lambda: s)

    def open_dialog(self):
        self.dialog = AssessmentDialog(self.owner)
        self.addCleanup(self.dialog.close)
        self.dialog.show()
        self.app.processEvents()
        return self.dialog

    def test_support_only_report_is_available_after_confirmation(self):
        d = self.open_dialog()
        self.assertEqual(d.tabs.currentIndex(), 0)
        self.assertEqual(d.support.item(0, 0).text(), "Trill")
        self.assertIn("10 confirmed", d.support.item(0, 4).text())
        self.assertIn("3 original recordings", d.support.item(0, 4).text())
        self.assertTrue(d.run_button.isEnabled())
        self.assertIn("support report", d.run_button.text())
        d.run_button.click()
        self.assertTrue(d.run_panel.isVisible())
        self.assertFalse(d.start_button.isEnabled())
        d.reason.setPlainText("Document the available family support.")
        d.start_confirm.click()
        self.assertTrue(d.start_button.isEnabled())
        self.data.update(can_start=False, blocking_reasons=[dict(code="HUMAN_COMPLETION_REQUIRED")])
        self.owner.state.update(revision=4, current_completion_id=None)
        d.refresh()
        self.assertFalse(d.run_panel.isVisible())
        self.assertFalse(d.start_confirm.isChecked())
        self.assertFalse(d.run_button.isEnabled())
        got = []
        d.finalChecksRequested.connect(lambda: got.append(1))
        d.scope_button.click()
        self.assertEqual(got, [1])

    def test_latest_failure_never_reuses_historical_scores(self):
        support_job = dict(job_id="old", status="not_estimable", report_sha256="old-sha",
                           stale_for_current=True, stale_reasons=["source_version"], report_url="/report",
                           report=dict(status="not_estimable", coverage=self.data["coverage"], support=self.data["support"],
                                       reasons=[dict(code="INSUFFICIENT_ELIGIBLE_FAMILIES", message="Too few eligible families.")]))
        failed = dict(job_id="last", status="failed", stale_for_current=False,
                      error=dict(code="NUMERICAL_FAILURE", message="Calculation failed."))
        self.data.update(latest_job_id="last", jobs=[support_job, failed])
        d = self.open_dialog()
        self.assertEqual(d.tabs.currentIndex(), 1)
        self.assertEqual(d.report_selector.currentData(), "last")
        self.assertFalse(d.export_button.isEnabled())
        self.assertFalse(d.catalogue_button.isEnabled())
        self.assertFalse(d.scored_panel.isVisible())
        self.assertIn("failed", d.result_reason.text().lower())
        d.open_report("old")
        self.assertEqual(d.report_selector.currentData(), "old")
        self.assertIn("Historical", d.report_status.text())
        self.assertIn("Too few", d.result_reason.text())
        self.assertTrue(d.export_button.isEnabled())
        self.assertFalse(d.catalogue_button.isEnabled())
        self.assertTrue(d.report_support.isVisible())
        self.assertFalse(d.scored_panel.isVisible())
        with self.assertRaises(AssessmentError):
            d.open_report("missing")

    def test_handoff_uses_selected_current_run_and_rechecks_on_click(self):
        def run(ident):
            return dict(job_id=ident, status="not_estimable", report_sha256=ident, report_url="/report",
                        stale_for_current=False, report=dict(status="not_estimable", coverage=self.data["coverage"],
                                                              support=self.data["support"], reasons=[]))
        earlier, latest = run("first"), run("latest")
        self.data.update(latest_job_id="latest", jobs=[earlier, latest])
        d = self.open_dialog()
        received = []
        d.catalogueRequested.connect(received.append)
        d.open_report("first")
        self.assertTrue(d.catalogue_button.isEnabled())
        d.catalogue_button.click()
        self.assertEqual(received, ["first"])
        earlier["stale_for_current"] = True
        # A project change can occur before the status timer redraws the button.
        with self.assertRaises(AssessmentError):
            d.request_catalogue()
        self.assertEqual(received, ["first"])

    def test_before_declaration_support_is_a_read_only_whole_project_preview(self):
        items = [{"item_id": f"s{i}", "processing_state": "decided"} for i in range(12)]
        self.owner.state.update(items=items, catalogue_scope=None, current_completion_id=None)
        self.owner.state["memberships"][0]["member_ids"] = [r["item_id"] for r in items]
        self.owner.state["source_registry"] = {
            "mappings": [dict(item_id=row["item_id"], raw_recording_id=f"recording-{i % 3}")
                         for i, row in enumerate(items)]}
        self.data.update(can_start=False, blocking_reasons=[dict(code="HUMAN_COMPLETION_REQUIRED")])
        d = self.open_dialog()
        self.assertIn("Provisional preview", d.scope_text.text())
        self.assertEqual([d.support.item(0, c).text() for c in range(1, 4)], ["12", "3", "Eligible"])
        self.assertTrue(d.scope_button.isVisible())
        self.assertFalse(d.run_button.isEnabled())
        self.assertIsNone(self.owner.state["catalogue_scope"])

    def test_current_scores_auto_open_and_activate_the_exact_direction(self):
        coverage = dict(self.data["coverage"], n_eligible=8, n_eligible_families=2, n_families=2)
        ids = ["f1", "f2"]
        pairs = [dict(actual_family_id="f1", predicted_family_id="f2", rate_mean=.25, rate_sd=.05)]
        report = dict(coverage=coverage, metrics=dict(balanced_accuracy=dict(mean=.8, sd=.02)),
                      confusion=dict(family_ids=ids, matrix_mean_row_normalized=[[.75,.25],[0,1]], top_pairs=pairs))
        self.data.update(jobs=[dict(job_id="scores", status="completed", report=report,
                                   report_sha256="score-sha", report_url="/report")], latest_job_id="scores")
        d = self.open_dialog()
        self.assertEqual(d.tabs.currentIndex(), 1)
        self.assertEqual(d.pairs.item(0,0).text(),"Trill")
        self.assertIn("80.0%", d.metrics.text())
        class Comparison(QWidget):
            def __init__(self, parent, controller, job, actual, predicted):
                super().__init__(parent)
                self.direction = job, actual, predicted
        with patch("syllafolio.annotation_desktop_assessment_listen.PairAuditionDialog", Comparison):
            d.pairs.selectRow(0)
            d.pairs.cellClicked.emit(0,1)
            self.assertEqual(d.pair_dialog.direction, ("scores","f1","f2"))
            self.data["jobs"][0].update(stale_for_current=True, stale_reasons=["catalogue_revision"])
            self.owner.state["revision"] += 1
            d.refresh()
            self.assertIsNone(d.pair_dialog)
            self.assertFalse(d.listen_button.isEnabled())
