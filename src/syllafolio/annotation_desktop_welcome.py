"""Project entry points and the offline desktop user guide."""
from pathlib import Path

from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton,
    QTextBrowser, QVBoxLayout, QWidget,
)


class WelcomePage(QWidget):
    def __init__(self, import_clips, import_recording, open_project):
        super().__init__()
        self.setObjectName("projectWelcome")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 12, 20, 12)
        outer.addStretch(2)
        panel = QFrame()
        panel.setObjectName("projectStartPanel")
        panel.setStyleSheet("QFrame#projectStartPanel { background:white; border:1px solid #ccdade; border-radius:6px; }")
        panel.setMaximumWidth(740)
        content = QVBoxLayout(panel)
        content.setContentsMargins(24, 20, 24, 20)
        content.setSpacing(12)
        title = QLabel("Open or create a project")
        font = title.font()
        font.setPointSizeF(max(14, font.pointSizeF() + 5))
        font.setBold(True)
        title.setFont(font)
        content.addWidget(title)

        entries = QGridLayout()
        entries.setHorizontalSpacing(16)
        entries.setVerticalSpacing(10)
        entries.setColumnStretch(1, 1)
        def add_entry(row, action, name, title, description):
            button = QPushButton(title)
            button.setObjectName(name)
            button.setAccessibleDescription(description)
            button.clicked.connect(action.trigger)
            entries.addWidget(button, row, 0)
            label = QLabel(description)
            label.setWordWrap(True)
            entries.addWidget(label, row, 1)
            return button

        self.syllables_button = add_entry(
            0, import_clips, "welcomeSyllables", "Import syllable files…",
            "Choose a folder with one audio file per syllable.")
        self.recording_button = add_entry(
            1, import_recording, "welcomeRecording", "Import recording and annotations…",
            "Choose one recording and its Raven, Praat TextGrid or Audacity interval annotations.")
        self.open_button = add_entry(
            2, open_project, "welcomeOpen", "Open project…",
            "Resume a saved Syllafolio project from its project folder.")
        content.addLayout(entries)
        audio = QLabel("Use mono or stereo WAV, FLAC, MP3, Ogg Vorbis or AIFF at 16 kHz or higher. "
                       "Files in a segmented batch must share a sample rate. Each syllable can be up to 30 seconds.")
        audio.setWordWrap(True)
        content.addWidget(audio)
        centered = QHBoxLayout()
        centered.addStretch(1)
        centered.addWidget(panel, 8)
        centered.addStretch(1)
        outer.addLayout(centered)
        outer.addStretch(3)


def _guide_path():
    root = Path(__file__).resolve().parents[2]
    # Source checkout: tools/distribution. Installed app/src: bundle root.
    for candidate in (root / "tools/distribution/USER_GUIDE.md", root.parent / "USER_GUIDE.md"):
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("The local user guide is missing from this copy of Syllafolio.")


class UserGuideDialog(QDialog):
    def __init__(self, owner):
        super().__init__(owner)
        self.setObjectName("userGuideWindow")
        self.setWindowTitle("Syllafolio user guide")
        screen = owner.screen().availableGeometry()
        self.resize(min(830, screen.width() - 32), min(720, screen.height() - 64))
        layout = QVBoxLayout(self)
        body = QTextBrowser()
        body.setObjectName("userGuideContents")
        body.setOpenExternalLinks(False)
        body.setMarkdown(_guide_path().read_text(encoding="utf-8"))
        layout.addWidget(body, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
