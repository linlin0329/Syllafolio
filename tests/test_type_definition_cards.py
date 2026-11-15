"""Card drafts keep hand-picked representatives, other edits and stale input distinct."""
import copy
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import RLock
from types import SimpleNamespace
import unittest

import numpy as np
from PySide6.QtCore import Signal
from PySide6.QtWidgets import QApplication, QWidget

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from syllafolio.annotation_desktop_catalogue import CatalogueDraftStore, CatalogueReviewDialog, CatalogueReviewError


class Owner(QWidget):
    operationFailed = Signal(str)


class TypeCards(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.owner = Owner()
        self.owner._busy = False
        self.owner._close_coordinator = SimpleNamespace(generation=3, accepts_callback=lambda g: g == 3)
        self.card = dict(local_type_id="A", title="g01", diagnostic_body="Rising onset", definition_status="reviewed",
                         main_variant_difference="", uncertainties="", reason="Listened to the full group",
                         representative_ids=["one"], atypical_ids=["three"], counterexample_ids=["outside"],
                         competing_type_ids=[], card_id="v1")
        row = dict(local_type_id="A", parent_type_id=None, member_count=3, card=self.card,
                   definition_state="reviewed")
        self.state = {"memberships": [dict(local_type_id="A", parent_type_id=None, member_ids=["one", "two", "three"])],
                      "type_cards": {"A": self.card}, "revision": 9,
                      "final_review": {"types": [row]}, "run_id": "run"}
        ids = ("one", "two", "three", "outside")
        data = SimpleNamespace(item_ids=ids, input_fingerprint="audio",
                               logmel=np.zeros((len(ids), 2048)),
                               _items={key: {"feature_row_index": i} for i, key in enumerate(ids)})
        self.project = SimpleNamespace(project_dir=Path(tmp.name), dataset=data, metadata={"project_id":"project"},
                                       service=SimpleNamespace(run_id="run", _lock=RLock()),
                                       snapshot=lambda: copy.deepcopy(self.state))
        self.owner.state = self.state
        self.owner.project = self.project
        self.dialog = CatalogueReviewDialog(self.owner)
        self.addCleanup(self.dialog.close)
        self.dialog.show_card("A")

    def test_representatives_merge_without_confirming_or_losing_edits(self):
        before = copy.deepcopy(self.state)
        self.dialog.texts["diagnostic_body"].setPlainText("Edited draft criterion")
        self.dialog.show_card()
        self.assertEqual(self.dialog.add_representatives("A", ["two","two"]), ["one", "two"])
        self.assertEqual(self.dialog.add_representatives("A", ["two"]), ["one", "two"])
        self.assertIn("Unsubmitted edits", self.dialog.type_details.text())
        self.assertFalse(self.dialog.representatives.item(1).icon().isNull())
        store = CatalogueDraftStore(self.project)
        record = store.get("card:A")
        self.assertEqual(record["submission_state"], "unsubmitted")
        self.assertFalse(record["review_confirmation"])
        self.assertEqual(record["payload"]["diagnostic_body"], "Edited draft criterion")
        self.assertEqual(record["payload"]["atypical_ids"], ["three"])
        self.assertEqual(record["payload"]["counterexample_ids"], ["outside"])
        self.assertEqual(record["payload"]["representative_ids"], ["one", "two"])
        with self.assertRaisesRegex(CatalogueReviewError, "TYPE_EXAMPLE_SCOPE"):
            self.dialog.add_representatives("A", ["three", "outside"])
        self.assertEqual(store.get("card:A"), record)
        self.assertEqual(before, self.state)

    def test_changed_formal_card_preserves_stale_draft_until_chosen(self):
        self.dialog.add_representatives("A", ["two"])
        store = CatalogueDraftStore(self.project)
        old = store.get("card:A")
        self.card.update(card_id="v2", diagnostic_body="Revised saved criterion")
        self.state["revision"] += 1
        self.dialog.show_card("A")
        self.assertEqual(self.dialog.texts["diagnostic_body"].toPlainText(), "Revised saved criterion")
        with self.assertRaisesRegex(CatalogueReviewError, "CATALOGUE_DRAFT_CHANGED"):
            self.dialog.add_representatives("A", ["three"])
        self.assertEqual(old, store.get("card:A"))
        self.dialog.restore_card_button.click()
        self.assertEqual(self.dialog.add_representatives("A", ["three"]), ["one", "two", "three"])
        self.assertEqual(self.card["representative_ids"], ["one"])
