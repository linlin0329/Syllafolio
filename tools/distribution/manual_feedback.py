"""Collect explicit user feedback and only the selected current-run attachments."""
from pathlib import Path
from datetime import datetime
import platform
import sys
sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parent
FIELDS = [
    ("readable_ui", "Controls and spectrogram are readable/reachable at my Windows display scaling"),
    ("source_audio", "Playback matches the user-chosen source audio I actually checked"),
    ("loop_boundaries", "Current-syllable loop, stop and switching targets behave correctly"),
    ("five_outcomes", "Five outcomes and other-family target selection are usable"),
    ("save_reopen", "My saved decision is retained after closing and reopening the project"),
    ("real_import", "I imported my own supported segmented audio folder and it completed"),
]


def create_dialog(parent=None):
    from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox,
                                  QFormLayout, QLabel, QPlainTextEdit, QVBoxLayout)
    dialog = QDialog(parent)
    dialog.setWindowTitle("Syllafolio - feedback")
    dialog.resize(850, 520)
    layout = QVBoxLayout(dialog)
    label = QLabel("Record only checks you actually performed. Automatic checks do not establish audibility or annotation accuracy.")
    label.setWordWrap(True)
    layout.addWidget(label)
    form = QFormLayout()
    choices = {}
    for key, text in FIELDS:
        choice = QComboBox()
        choice.setObjectName("feedback_" + key)
        choice.addItem("Not tested", "not_tested")
        choice.addItem("Pass - I checked this", "pass")
        choice.addItem("Problem observed", "problem")
        form.addRow(text, choice)
        choices[key] = choice
    layout.addLayout(form)
    notes = QPlainTextEdit()
    notes.setObjectName("feedback_notes")
    notes.setPlaceholderText("Describe any problem and the steps that reproduce it. Include the input format and Windows scaling if relevant.")
    layout.addWidget(notes)
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)
    return dialog, choices, notes


def save_result(root, choices, notes):
    from syllafolio.annotation_desktop_reports import collect_feedback
    value = {"native_windows_process": sys.platform == "win32", "platform": platform.platform(),
             "checks": {key: control.currentData() for key, control in choices.items()},
             "notes": notes.toPlainText(), "recorded_at": datetime.now().astimezone().isoformat(),
             "protocol_or_biological_accuracy_test": False}
    return collect_feedback(root, value)


def main():
    from runtime_paths import configure_runtime
    config = configure_runtime(ROOT)
    from PySide6.QtCore import QLocale, Qt
    from PySide6.QtWidgets import QApplication, QDialog, QMessageBox
    QLocale.setDefault(QLocale("en_US"))
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_DontUseNativeDialogs, True)
    from syllafolio.annotation_desktop_theme import apply_light_theme
    from syllafolio.annotation_desktop_diagnostics import DesktopDiagnostics
    diagnostics = DesktopDiagnostics(config["diagnostics_dir"])
    application = QApplication.instance() or QApplication([])
    apply_light_theme(application)
    dialog, choices, notes = create_dialog()
    if dialog.exec() != QDialog.DialogCode.Accepted:
        diagnostics.close()
        return 0
    try:
        path = save_result(ROOT, choices, notes)
    except Exception as error:
        diagnostics.record_exception(error, "collect feedback", {"bundle_root": str(ROOT)})
        diagnostics.close()
        QMessageBox.critical(None, "Feedback was not saved", str(error))
        raise
    QMessageBox.information(None, "Feedback saved", "Return this small report archive:\n" + str(path))
    print("Feedback saved: " + str(path))
    diagnostics.close()
    return 0


if __name__ == "__main__":
    from installation_lease import InstallationLease
    with InstallationLease(ROOT, mode="shared"):
        raise SystemExit(main())
