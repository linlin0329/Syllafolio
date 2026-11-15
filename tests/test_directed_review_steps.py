"""Verify a directed question binds one family, resumes its frozen pages and rejects another question."""
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import time
import unittest
import uuid

import numpy as np
import soundfile as sf
from PySide6.QtWidgets import QApplication

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from syllafolio.annotation_batch_review import CLOSE_CHECKS
from syllafolio.annotation_desktop import MainWindow
from syllafolio.annotation_desktop_catalogue import CatalogueReviewError
from syllafolio.annotation_desktop_organization import OrganizationController
from syllafolio.annotation_desktop_project import create_project
from syllafolio.annotation_desktop_review_media import ReferenceStore


class DirectedReviewSteps(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def wait(self, predicate, diagnose=lambda: ""):
        deadline = time.monotonic() + 90
        while not predicate():
            self.app.processEvents()
            if time.monotonic() > deadline:
                self.fail(f"Timed out waiting for the review page: {diagnose()}")
            time.sleep(.01)

    def test_confirm_scope_then_open_and_continue_same_question(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            clips = root / "clips"
            clips.mkdir()
            t = np.arange(6000) / 48000
            for i in range(4):
                sf.write(clips / f"clip-{i}.wav", .08 * np.sin(2*np.pi*(1600+i*200)*t), 48000)
            window = MainWindow(diagnostics_dir=root / "logs")
            errors = []
            window.operationFailed.connect(errors.append)
            try:
                window.show()
                project = create_project(clips, root / "project")
                window._install_project(project)

                def command(action, payload):
                    return project.execute(action, payload, expected_revision=project.snapshot()["revision"],
                                           idempotency_key=uuid.uuid4().hex)
                org = OrganizationController()
                org.preview(project, generation=0, idempotency_key="compare")
                org.apply(project, generation=0, idempotency_key="apply")
                family = "F-test"
                command("batch_family", dict(family_id=family, title="Test family", reason="Test scope",
                                             diagnostic_body="Repeated short notes"))
                prior = command("batch_prepare_pool", dict(route="pool_cluster", pool_mode="current_pool",
                                normalization="per_item", gap_seconds=.1))["record"]["review"]["review_id"]
                project.service._futures[prior].result(timeout=60)
                targets = list(project.dataset.item_ids[:-1])
                reference = project.dataset.item_ids[-1]
                for page in project.snapshot()["batch_review"]["reviews"][prior]["pages"]:
                    decisions = [dict(item_id=i, disposition="current", family_id=family) if i in targets else
                                 dict(item_id=i, disposition="discard", reason_code="not_target") for i in page["item_ids"]]
                    command("batch_submit_v2", dict(review_id=prior, page_id=page["page_id"],
                                                    current_family_id=family, items=decisions))
                command("batch_close", dict(review_id=prior, checks=dict.fromkeys(CLOSE_CHECKS, True),
                                            note="Explicit scripted records."))
                ReferenceStore(project).set([targets[0], reference])
                window.state = project.snapshot()
                window._refresh()
                self.app.processEvents()
                self.wait(lambda: not window._busy and not window._sampling_pending)
                before = window.state["revision"]
                dialog = window._show_catalogue_review(family)
                dialog.show_directed(family)
                self.assertEqual(dialog.wizard_pages.currentIndex(), 0)
                self.assertFalse(dialog.inspect_scope_button.isEnabled())
                dialog.directed_texts["question"].setText("Do the short notes share the same ending?")
                dialog.directed_texts["reason"].setText("Compare the new examples.")
                dialog.basis.setCurrentIndex(dialog.basis.findData("new_evidence"))
                self.assertTrue(dialog.inspect_scope_button.isEnabled())
                dialog.inspect_scope_button.click()
                self.assertEqual(dialog.wizard_pages.currentIndex(), 1)
                self.assertIn("3 syllables", dialog.directed_scope.text())
                self.assertIn("1 (read-only)", dialog.directed_scope.text())
                self.assertEqual(project.snapshot()["revision"], before)
                dialog.prepare_button.click()
                self.wait(lambda: window._page is not None and not window._busy and window._review().get("directed_issue_id"),
                          lambda: (errors, dialog.message.text(), window.status_label.text(),
                                   [(v["stage"], v["status"]) for v in project.snapshot()["batch_review"]["reviews"].values()]))
                review = window._review()
                self.assertEqual(set(i for page in review["pages"] for i in page["item_ids"]), set(targets))
                rid = review["review_id"]
                rev = project.snapshot()["revision"]

                dialog = window._show_catalogue_review(family)
                dialog.show_directed(family)
                self.assertTrue(dialog.continue_button.isVisible() and dialog.continue_button.isEnabled())
                dialog.continue_button.click()
                self.wait(lambda: not window._busy and not dialog.isVisible() and window._review()["review_id"] == rid)
                self.assertEqual(project.snapshot()["revision"], rev)

                dialog = window._show_catalogue_review(family)
                dialog.prefill_directed_question(dict(question="Is the onset different?", family_id=family,
                                                      reason="Assessment follow-up", affected_type_ids=[family]))
                self.assertEqual(dialog.wizard_pages.currentIndex(), 0)
                dialog.basis.setCurrentIndex(dialog.basis.findData("reinterpretation"))
                with self.assertRaises(CatalogueReviewError) as raised:
                    dialog._inspect_directed()
                self.assertEqual(raised.exception.code, "DIRECTED_REVIEW_IN_PROGRESS")
                self.assertEqual(project.snapshot()["revision"], rev)
                self.assertEqual(errors, [])
            finally:
                window.close()
                self.wait(lambda: window.project is None)
