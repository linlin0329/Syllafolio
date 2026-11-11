"""Context menus for candidate groups and human families in the review sidebar."""
from PySide6.QtCore import QModelIndex, QPersistentModelIndex, Qt, Signal
from PySide6.QtGui import QContextMenuEvent
from PySide6.QtWidgets import QApplication, QAbstractItemView, QLineEdit, QMenu, QStyledItemDelegate, QTreeWidget


class _FamilyNameDelegate(QStyledItemDelegate):
    renameRequested = Signal(str, str, str)

    def createEditor(self, parent, option, index):
        data = index.data(Qt.ItemDataRole.UserRole) or {}
        if not self.parent().family_actions_enabled or index.column() != 0 or data.get("kind") != "family":
            return None
        editor = QLineEdit(parent)
        editor.setObjectName("familyNameEditor")
        editor.setAccessibleName("Family name")
        editor.setStyleSheet("QLineEdit { padding: 0px 2px; }")
        return editor

    def updateEditorGeometry(self, editor, option, index):
        editor.setGeometry(option.rect)

    def setEditorData(self, editor, index):
        editor.setText(index.data(Qt.ItemDataRole.DisplayRole))
        editor.selectAll()

    def setModelData(self, editor, model, index):
        data = index.data(Qt.ItemDataRole.UserRole) or {}
        if data.get("kind") == "family":
            # Display the authoritative title until the project accepts the rename.
            self.renameRequested.emit(data["id"], index.data(Qt.ItemDataRole.DisplayRole), editor.text())


class ReviewTree(QTreeWidget):
    createFamilyRequested = Signal()
    renameFamilyRequested = Signal(str, str, str)

    def __init__(self):
        super().__init__()
        self.family_actions_enabled = False
        self.candidate_menu = QMenu(self)
        self.candidate_menu.setObjectName("candidateGroupsMenu")
        self.model().modelAboutToBeReset.connect(self.candidate_menu.hide)
        self.setEditTriggers(QAbstractItemView.EditTrigger.SelectedClicked)
        delegate = _FamilyNameDelegate(self)
        self.setItemDelegate(delegate)
        delegate.renameRequested.connect(self.renameFamilyRequested)

    def _context_index_at(self, pos):
        index = self.indexAt(pos)
        data = index.siblingAtColumn(0).data(Qt.ItemDataRole.UserRole) or {}
        if data.get("kind") in ("candidates_root", "families_root", "family"):
            return index
        return QModelIndex()

    def mousePressEvent(self, event):
        # A context click should not navigate away from the active audition.
        if event.button() == Qt.MouseButton.RightButton and self._context_index_at(event.position().toPoint()).isValid():
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.RightButton:
            index = self._context_index_at(event.position().toPoint())
            if index.isValid():
                self._show_context_menu(index, event.globalPosition().toPoint())
                event.accept()
                return
        super().mouseReleaseEvent(event)

    def contextMenuEvent(self, event):
        if event.reason() == QContextMenuEvent.Reason.Keyboard:
            index = self.currentIndex()
            global_pos = self.viewport().mapToGlobal(self.visualRect(index).center())
        else:
            index = self.indexAt(event.pos())
            global_pos = event.globalPos()
        if not self._show_context_menu(index, global_pos):
            super().contextMenuEvent(event)
            return
        event.accept()

    def _show_context_menu(self, index, global_pos):
        name = index.siblingAtColumn(0)
        data = name.data(Qt.ItemDataRole.UserRole) or {}
        kind = data.get("kind")
        if kind not in ("candidates_root", "families_root", "family"):
            return False
        popup = QApplication.activePopupWidget()
        if isinstance(popup, QMenu) and popup.parentWidget() is self:
            return True
        if kind == "candidates_root":
            self.candidate_menu.popup(global_pos)
            return True
        anchor = QPersistentModelIndex(name)
        menu = QMenu(self)
        menu.setObjectName("familyTreeMenu")
        menu.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        create = menu.addAction("New human family…")
        create.setObjectName("newHumanFamily")
        create.setEnabled(self.family_actions_enabled)
        def request_create():
            if anchor.isValid() and self.family_actions_enabled:
                self.createFamilyRequested.emit()
        create.triggered.connect(request_create)
        if data["kind"] == "family" and index.column() == 0:
            rename = menu.addAction("Rename")
            rename.setObjectName("renameHumanFamily")
            rename.setEnabled(self.family_actions_enabled)
            rename.triggered.connect(lambda: self._edit_family_name(QModelIndex(anchor)))
        menu.popup(global_pos)
        return True

    def _edit_family_name(self, index):
        data = index.data(Qt.ItemDataRole.UserRole) or {}
        if self.isEnabled() and self.family_actions_enabled and index.column() == 0 and data.get("kind") == "family":
            self.editItem(self.itemFromIndex(index), 0)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_F2:
            self._edit_family_name(self.currentIndex())
            event.accept()
            return
        super().keyPressEvent(event)
