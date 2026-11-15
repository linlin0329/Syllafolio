"""Definition shortcuts use the clicked family and the selected confirmed members."""
from pathlib import Path
import gc
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtGui import QAction
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMenu, QTreeWidgetItem

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from syllafolio.annotation_desktop import MainWindow
from syllafolio.annotation_desktop_review_tree import ReviewTree


class DefinitionEntryPoints(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_context_action_preserves_tree_selection_and_uses_clicked_family(self):
        tree = ReviewTree()
        self.addCleanup(tree.close)
        tree.setColumnCount(3)
        current = QTreeWidgetItem(tree, ["Candidate"])
        current.setData(0, Qt.ItemDataRole.UserRole, {"kind": "group", "id": "cg"})
        family = QTreeWidgetItem(tree, ["g02", "8"])
        family.setData(0, Qt.ItemDataRole.UserRole, {"kind": "family", "id": "F-2"})
        tree.setCurrentItem(current)
        tree.show()
        self.app.processEvents()
        index = tree.indexFromItem(family, 1)
        opened = []
        tree.familyDefinitionRequested.connect(opened.append)
        QTest.mouseClick(tree.viewport(), Qt.MouseButton.RightButton, Qt.KeyboardModifier.NoModifier,
                         tree.visualRect(index).center())
        menu = tree.findChild(QMenu, "familyTreeMenu")
        self.assertIsNotNone(menu)
        action = menu.findChild(QAction, "openFamilyDefinition")
        self.assertTrue(action.isEnabled())
        QTest.mouseClick(menu, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                         menu.actionGeometry(action).center())
        self.assertEqual(opened, ["F-2"])
        self.assertIs(tree.currentItem(), current)
        QTest.mouseClick(tree.viewport(), Qt.MouseButton.RightButton, Qt.KeyboardModifier.NoModifier,
                         tree.visualRect(index).center())
        pending = QApplication.activePopupWidget()
        self.assertIsInstance(pending, QMenu)
        stale = pending.findChild(QAction, "openFamilyDefinition")
        tree.clear()  # The project changed while a context menu was open.
        stale.trigger()
        self.assertEqual(opened, ["F-2"])
        self.assertIsNone(QApplication.activePopupWidget())

    def test_dismissing_family_popup_and_closing_view_survives_next_event_loop(self):
        def open_and_close():
            tree = ReviewTree()
            family = QTreeWidgetItem(tree, ["g01"])
            family.setData(0, Qt.ItemDataRole.UserRole, {"kind": "family", "id": "F1"})
            tree.show()
            self.app.processEvents()
            QTest.mouseClick(tree.viewport(), Qt.MouseButton.RightButton, Qt.KeyboardModifier.NoModifier,
                             tree.visualItemRect(family).center())
            menu = QApplication.activePopupWidget()
            self.assertIsInstance(menu, QMenu)
            action = menu.findChild(QAction, "openFamilyDefinition")
            QTest.mouseClick(menu, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                             menu.actionGeometry(action).center())
            tree.close()
        open_and_close()
        gc.collect()
        self.app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.assertIsNone(QApplication.activePopupWidget())

    def test_representatives_require_saved_same_family_and_no_pending_reassignment(self):
        ids = ["a", "b", "c", "undecided"]
        owner = SimpleNamespace(_page={"item_ids": ids}, _rows={}, _draft_base={},
                                selected_item_ids=("a", "b"), _row=MainWindow._row)
        state = {"memberships": [
            {"local_type_id": "f1", "parent_type_id": None, "member_ids": ["a", "b"]},
            {"local_type_id": "leaf", "parent_type_id": "f1", "member_ids": ["a"]},
            {"local_type_id": "f2", "parent_type_id": None, "member_ids": ["c"]},
        ]}
        # A new review can have no decisions while the family already has
        # confirmed members. Page defaults alone confer no such membership.
        owner._rows = {i: MainWindow._row(i, None) for i in ids}
        def target():
            return MainWindow._representative_target(owner, state)[0]
        self.assertEqual(target(), "f1")
        owner.selected_item_ids = ("a", "undecided")
        owner._rows["undecided"].update(disposition="current", family_id="f1")
        self.assertIsNone(target())
        owner.selected_item_ids = ("a", "c")
        self.assertIsNone(target())
        owner.selected_item_ids = ("a", "b")
        owner._rows["a"].update(disposition="other", family_id="f2")
        self.assertIsNone(target())
        owner._rows["a"].update(disposition="discard", family_id="")
        self.assertIsNone(target())
        owner._rows["a"].update(disposition="current", family_id="f1", reason="Spoken note")
        self.assertEqual(target(), "f1")

    def test_continue_directed_round_opens_first_page_still_needing_a_decision(self):
        review = {"status": "ready", "pages": [
            {"page_id": "p1", "item_ids": ["a", "b"]}, {"page_id": "p2", "item_ids": ["c"]}],
            "decisions": {"a": {}, "b": {}}}
        owner = SimpleNamespace(project=True, _busy=False, _page=None, _page_load_error=None,
                                _close_coordinator=SimpleNamespace(state="open"),
                                _review=lambda: review, _directed_editor=lambda: True,
                                _pending_final_item=None, selected_group="family:f", selected_review_id="r",
                                _page_for_final_item=MainWindow._page_for_final_item, _load_page=Mock())
        MainWindow._load_first_available_page(owner)
        owner._load_page.assert_called_with("p2", expected_review_id="r", rollback=None)
        owner._pending_final_item = ("family:f", "b")
        MainWindow._load_first_available_page(owner)
        owner._load_page.assert_called_with("p1", expected_review_id="r", rollback=None)
        owner._pending_final_item = None
        review["decisions"]["c"] = {}
        MainWindow._load_first_available_page(owner)
        owner._load_page.assert_called_with("p1", expected_review_id="r", rollback=None)
