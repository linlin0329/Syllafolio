"""Cover queue protection and all-unclustered audition in the comparison dialog."""
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QApplication, QDoubleSpinBox, QWidget

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from syllafolio.annotation_desktop_grouping_comparison import GroupingComparisonDialog


class Owner(QWidget):
    operationFailed = Signal(str)


class GroupingComparisonDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_initial_pool_is_auditionable_with_or_without_candidate_groups(self):
        owner = Owner()
        owner.project = object()
        owner._close_coordinator = SimpleNamespace(generation=1)
        owner.gap = QDoubleSpinBox(owner)
        dialog = GroupingComparisonDialog(owner)
        self.addCleanup(dialog.close)
        pool = [f"pooled-{i}" for i in range(17)]
        group = {"group_id": "c_1", "item_ids": ["member"], "item_count": 1}
        impact = {"requires_confirmation": True, "message": "Starting a new queue.",
                  "current_review_count": 1, "saved_assignment_count": 1, "reset_scope_item_count": 18}
        rows = []
        for mode, label, size in (("fine", "fine", 5), ("microclusters", "default", 10),
                                  ("coarse", "coarse", 20)):
            rows.append(dict(mode=mode, granularity=label, min_cluster_size=size,
                             available=mode != "coarse", already_applied=mode == "microclusters",
                             reason="Missing embedding." if mode == "coarse" else "",
                             candidate_group_count=int(mode == "microclusters"), largest_group_size=1,
                             unclustered_count=17, unclustered_fraction=17/18))
        rows[0].update(largest_group_size=0, unclustered_count=18, unclustered_fraction=1)
        summaries = {"fine": dict(groups=[], unclustered_ids=pool + ["member"], impact=impact),
                     "microclusters": dict(groups=[group], unclustered_ids=pool, impact=impact)}
        dialog._closed = False
        with patch.object(dialog.audition, "set_items") as listen:
            dialog._prepared((rows, dict.fromkeys(summaries, object()), summaries))
            self.assertEqual(dialog.table.currentRow(), 1)
            self.assertFalse(dialog.apply_button.isEnabled())
            self.assertFalse(dialog.table.item(2, 0).flags() & Qt.ItemFlag.ItemIsEnabled)
            self.assertIn("Missing embedding.", dialog.message.text())
            listen.assert_called_with(["member"])
            dialog.group.setCurrentIndex(1)
            self.assertTrue(dialog.group.currentText().startswith("Initial global pool"))
            sample = listen.call_args.args[0]
            self.assertEqual(len(set(sample)), 10)
            self.assertEqual((sample[0], sample[-1]), (pool[0], pool[-1]))
            dialog.table.selectRow(0)
            self.assertEqual(dialog.group.count(), 1)
            self.assertTrue(dialog.group.currentText().startswith("Initial global pool"))
            self.assertEqual(listen.call_args.args[0][-1], "member")
            self.assertFalse(dialog.apply_button.isEnabled())
            dialog.acknowledgement.setChecked(True)
            self.assertTrue(dialog.apply_button.isEnabled())
            dialog.table.selectRow(1)
            self.assertFalse(dialog.acknowledgement.isChecked())
            self.assertFalse(dialog.apply_button.isEnabled())
