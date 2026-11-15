"""Fine review guided choices, evidence confirmation and stable draft type identities."""
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QApplication, QWidget

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from syllafolio.annotation_desktop_fine_review import CHOOSE, NEW_LEAF, FineReviewDialog


class Owner(QWidget):
    operationFailed = Signal(str)


class GuidedFineReview(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = {"items": [{"item_id":"a", "current_issue_ids":[]}],
                      "memberships":[{"local_type_id":"F-a","parent_type_id":None,"member_ids":["a"]}],
                      "type_cards":{"F-a":{"title":"Trill","diagnostic_body":"Two sweeps"}},
                      "batch_review":{"assignments":{"a":{"family_id":"F-a","form_status":"not_assessed"}}},
                      "effective_decision_ids":[], "decisions":{}, "revision":1, "input_fingerprint":"test"}
        self.owner = Owner()
        self.addCleanup(self.owner.close)
        self.owner._busy = False
        self.owner.state = self.state
        self.owner._close_coordinator = SimpleNamespace(generation=1, state="open")
        self.owner.project = SimpleNamespace(
            metadata={"project_id":"p"},service=SimpleNamespace(run_id="r"),project_dir=Path(tmp.name),
            dataset=SimpleNamespace(input_fingerprint="test", _items={"a":{"context_kind":"segment_only"}}),
            snapshot=lambda: self.state)
        self.owner._run = lambda _label, call, callback: callback(call(None,None))

    def dialog(self):
        d = FineReviewDialog(self.owner, "a")
        self.addCleanup(d.close)
        d.show()
        self.app.processEvents()
        return d

    def choose(self, combo, value):
        i = combo.findData(value)
        self.assertGreaterEqual(i,0,str(value))
        combo.setCurrentIndex(i)

    def fill_judgment(self, d, region, component, form, progress):
        for key, value in (("diagnostic_region",region),("component_status",component),("form_status",form),
                           ("boundary_status","no_issue_reported")):
            self.choose(d.axes[key], value)
        self.choose(d.processing, progress)

    def test_masked_component_remains_an_explicit_finer_uncertainty(self):
        d = self.dialog()
        self.assertIn("Trill",d.context_label.text())
        self.assertIn("Not assessed", d.saved_label.text())
        self.assertEqual(d.axes["component_status"].currentData(), CHOOSE)
        self.assertFalse(d.play_context.isEnabled())
        self.fill_judgment(d,"unassessable","not_observed_when_assessable","unresolved","reviewed_unresolved")
        self.assertFalse(d.next_button.isEnabled())
        self.choose(d.axes["component_status"],"undetermined")
        d.next_button.click()
        self.assertIs(d.pages.currentWidget(),d.evidence_page)
        self.assertIn("Trill",d.summary.text())
        self.assertIn("Unresolved after review",d.summary.text())
        self.assertTrue(d.texts["missing_information"].isVisible())
        self.assertFalse(d.texts["pending_steps"].isVisible())
        self.assertFalse(d.audition.isChecked())
        for key,edit in d.texts.items():
            if edit.isVisible():edit.setPlainText("Test observation for " + key)
        self.assertFalse(d.submit_button.isEnabled())
        for check in [*d.checks.values(),d.audition]:check.click()
        self.assertTrue(d.submit_button.isEnabled())
        d.texts["reason"].setPlainText("The ending is masked.")
        self.assertFalse(d.audition.isChecked())
        self.assertFalse(d.submit_button.isEnabled())
        payload=d.payload()
        self.assertEqual(payload["axes"]["family_id"],"F-a")
        self.assertEqual(payload["axes"]["form_status"],"unresolved")
        self.assertIsNone(payload["axes"]["leaf_id"])
        self.assertNotIn("new_leaf",payload)

    def test_candidate_needs_no_leaf_and_new_leaf_has_a_stable_draft_identity(self):
        d = self.dialog()
        self.fill_judgment(d,"assessable","present","candidate_form","decided")
        self.assertTrue(d.next_button.isEnabled())
        self.assertIsNone(d.payload()["axes"]["leaf_id"])
        self.assertNotIn("new_leaf", d.payload())
        self.choose(d.axes["leaf_id"],NEW_LEAF)
        for k,v in {"title":"Descending ending","reason":"Inspect recurring new ending",
                    "main_variant_difference":"Short descending terminal component."}.items():
            d.new_leaf_fields[k].setText(v)
        self.assertTrue(d.next_button.isEnabled())
        original=d.payload()
        leaf_id=original["new_leaf"]["local_type_id"]
        self.assertTrue(leaf_id.startswith("L-"))
        self.assertEqual(original["axes"]["leaf_id"],leaf_id)
        self.assertEqual(original["draft_new_leaf_parent"],"F-a")
        d.next_button.click()
        d.audition.click()
        d.close()
        reopened = self.dialog()
        self.assertEqual(reopened.axes["form_status"].currentData(),CHOOSE)
        reopened.restore_draft()
        self.assertEqual(reopened.payload()["new_leaf"],original["new_leaf"])
        self.assertEqual(reopened.payload()["axes"],original["axes"])
        self.assertFalse(reopened.audition.isChecked())
        self.assertNotIn(leaf_id,self.state["type_cards"])

    def test_decision_wording_does_not_supply_observations_or_confirm_review(self):
        d = self.dialog()
        self.fill_judgment(d, "unassessable", "undetermined", "unresolved", "reviewed_unresolved")
        d.next_button.click()
        d.texts["question"].setPlainText("Did the target contain a second upsweep?")
        d.wording_button.click()
        self.assertEqual(d.payload()["question"], "Did the target contain a second upsweep?")
        self.assertIn("Trill", d.payload()["answer"])
        self.assertEqual(d.payload()["unresolved_level"], "Fine form")
        for key in ("observation_text", "reason", "comparison_note", "missing_information"):
            self.assertEqual(d.payload()[key], "")
        self.assertFalse(any(d.payload()["checks"].values()))
        self.assertFalse(d.submit_button.isEnabled())
        d.back_button.click()
        self.choose(d.axes["form_status"], "not_assessed")
        self.assertEqual(d.texts["answer"].toPlainText(), "")
        self.assertEqual(d.texts["question"].toPlainText(), "Did the target contain a second upsweep?")
