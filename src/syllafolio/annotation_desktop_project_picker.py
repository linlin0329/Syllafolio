"""Browse project directories without shell extensions or a live filesystem model."""
from pathlib import Path
from queue import Empty, SimpleQueue
import os
import threading

from PySide6.QtCore import QDir, Qt, QTimer
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QHBoxLayout,
                               QLabel, QLineEdit, QListWidget, QPushButton, QVBoxLayout)

from .annotation_desktop_project import PROJECT_FILENAME


def _read_folder(path, opening, results):
    # The worker never touches Qt. A dismissed dialog or a newer request may
    # discard this queue while a slow network directory is still being read.
    try:
        if opening and (path / PROJECT_FILENAME).is_file():
            results.put(("project", path))
            return
        with os.scandir(path) as entries:
            names = sorted((e.name for e in entries if e.is_dir()), key=str.casefold)
        results.put(("folders", (path, names, opening)))
    except (OSError, ValueError):
        results.put(("error", "Cannot read this folder. Check its path and access permissions."))


class ProjectFolderDialog(QDialog):
    def __init__(self, parent, start):
        super().__init__(parent)
        self.setWindowTitle("Open Syllafolio project")
        self.setObjectName("projectFolderDialog")
        self.resize(610, 420)
        self.selected_project = None
        self.current_folder = Path(start)
        layout = QVBoxLayout(self)
        bar = QHBoxLayout()
        self.roots = QComboBox()
        self.roots.setAccessibleName("Drive")
        self.roots.addItems([os.path.normpath(d.absoluteFilePath()) for d in QDir.drives()])
        self.roots.textActivated.connect(lambda root: self._request(Path(root)))
        self.roots.setVisible(os.name == "nt")
        bar.addWidget(self.roots)
        self.up = QPushButton("Up")
        self.up.setAutoDefault(False)
        self.up.clicked.connect(lambda: self._request(self.current_folder.parent))
        bar.addWidget(self.up)
        self.path_edit = QLineEdit()
        self.path_edit.setAccessibleName("Folder path")
        self.path_edit.setToolTip("For a legacy P5 project, open a backup copy.")
        self.path_edit.setPlaceholderText("Project folder path")
        self.path_edit.textEdited.connect(self._path_edited)
        bar.addWidget(self.path_edit, 1)
        layout.addLayout(bar)
        self.folders = QListWidget()
        self.folders.setAccessibleName("Folders")
        self.folders.itemActivated.connect(lambda item: self._request(self.current_folder / item.text()))
        layout.addWidget(self.folders)
        self.message = QLabel()
        layout.addWidget(self.message)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Open | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self._open_selection)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self._timer = QTimer(self)
        self._timer.setInterval(25)
        self._timer.timeout.connect(self._receive)
        self.finished.connect(self._timer.stop)
        self._request(self.current_folder)
        self.path_edit.setFocus()
        self.path_edit.selectAll()

    def _path_edited(self):
        self._timer.stop()
        self.folders.clearSelection()
        self.message.clear()

    def _open_selection(self):
        selected = self.folders.selectedItems()
        text = self.path_edit.text().strip()
        if selected:
            target = self.current_folder / selected[0].text()
        elif text:
            try:
                target = self.current_folder / Path(text).expanduser()
            except RuntimeError:
                self.message.setText("Enter an existing folder path.")
                return
        else:
            self.message.setText("Enter or select a project folder.")
            return
        self._request(target, opening=True)

    def _request(self, path, *, opening=False):
        path = Path(os.path.abspath(path))
        self.path_edit.setText(str(path))
        self.folders.clear()
        self.message.setText("Reading folder…")
        self._results = SimpleQueue()
        threading.Thread(target=_read_folder, args=(path, opening, self._results), daemon=True).start()
        self._timer.start()

    def _receive(self):
        try:
            kind, result = self._results.get_nowait()
        except Empty:
            return
        self._timer.stop()
        if kind == "project":
            self.selected_project = result
            self.accept()
        elif kind == "error":
            self.message.setText(result)
        else:
            self.current_folder, names, opening = result
            self.folders.addItems(names)
            self.up.setEnabled(self.current_folder != self.current_folder.parent)
            self.roots.setCurrentIndex(self.roots.findText(self.current_folder.anchor, Qt.MatchFlag.MatchFixedString))
            self.message.setText(f"Choose a project folder containing {PROJECT_FILENAME}." if opening else "")
