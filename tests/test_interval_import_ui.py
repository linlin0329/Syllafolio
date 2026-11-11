"""Native-widget interval import tests with generated audio and real services."""
import hashlib
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch

import numpy as np
import soundfile as sf
import shiboken6
from PySide6.QtCore import QEventLoop, QSettings, QTimer, Qt
from PySide6.QtWidgets import QApplication, QDialog, QPushButton

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from syllafolio import annotation_desktop_interval_import as ui
from syllafolio.annotation_desktop import MainWindow, ProjectContractDialog
from syllafolio.annotation_interval_import import open_preview


def wait_for(predicate, seconds=15):
    if predicate():
        return
    loop = QEventLoop()
    timer = QTimer(); timer.setInterval(10)
    expired = []
    timer.timeout.connect(lambda: loop.quit() if predicate() else None)
    deadline = QTimer(); deadline.setSingleShot(True)
    deadline.timeout.connect(lambda: (expired.append(True), loop.quit()))
    timer.start(); deadline.start(round(seconds * 1000)); loop.exec()
    timer.stop(); deadline.stop()
    if expired and not predicate():
        raise AssertionError("The UI operation did not settle before its deadline.")


class IntervalImportUi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = TemporaryDirectory(prefix="ri-ui-")
        self.root = Path(self.tmp.name)
        QSettings.setDefaultFormat(QSettings.Format.IniFormat)
        QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(self.root / "settings"))
        self.old_hook = sys.excepthook
        self.window = MainWindow(diagnostics_dir=self.root / "diagnostics")
        self.window.show()
        self.recording = self.root / "recording.wav"
        self.rate = 48000
        self.pcm = (.2 * np.sin(2 * np.pi * 3200 * np.arange(self.rate) / self.rate)).astype("float32")
        sf.write(self.recording, self.pcm, self.rate, subtype="FLOAT")
        self.annotation = self.root / "labels.TextGrid"
        self.annotation.write_text('''File type = "ooTextFile"
Object class = "TextGrid"
xmin = 0
xmax = 1
tiers? <exists>
size = 2
item []:
    item [1]:
        class = "IntervalTier"
        name = "first"
        xmin = 0
        xmax = 1
        intervals: size = 2
        intervals [1]:
            xmin = 0
            xmax = 0.25
            text = "call A"
        intervals [2]:
            xmin = 0.25
            xmax = 1
            text = ""
    item [2]:
        class = "IntervalTier"
        name = "second"
        xmin = 0
        xmax = 1
        intervals: size = 3
        intervals [1]:
            xmin = 0
            xmax = 0.5
            text = ""
        intervals [2]:
            xmin = 0.5
            xmax = 0.75
            text = "call B"
        intervals [3]:
            xmin = 0.75
            xmax = 1
            text = ""
''', encoding="utf-8")
        self.errors = []
        self.window.operationFailed.connect(self.errors.append)
        self.gates = []

    def tearDown(self):
        for gate in self.gates: gate.set()
        if self.window._interval_import_dialog:
            self.window._interval_import_dialog.close()
        if self.window._worker:
            self.window._worker.cancelled.set()
        wait_for(lambda: not self.window._busy)
        if not self.window._closing_approved:
            self.window.request_close("cancel")
            wait_for(lambda: self.window._closing_approved)
        self.window.close()
        sys.excepthook = self.old_hook
        self.tmp.cleanup()

    def dialog(self):
        result = self.window._show_interval_import()
        result.recording_input.setText(str(self.recording))
        result.annotation_input.setText(str(self.annotation))
        return result

    def loaded(self, dialog):
        dialog.preview_button.click()
        wait_for(lambda: dialog._pcm is not None and not self.window._busy and not dialog._waiting)
        self.assertFalse(self.errors)

    def test_real_preview_tiers_pcm_spectrum_and_laptop_layout(self):
        dialog = self.dialog()
        self.assertEqual(self.window.interval_import_action.objectName(), "importRecordingAnnotations")
        self.assertEqual(dialog.kind.currentData(), "auto")
        self.loaded(dialog)
        self.assertEqual(dialog.regions_table.rowCount(), 1)
        np.testing.assert_array_equal(dialog._pcm, self.pcm[:12000])
        self.assertGreater(dialog.spectrum_image.image.size, 0)
        self.assertTrue(np.isfinite(dialog.spectrum_image.image).all())
        self.assertEqual(dialog.selected_region().start_frame, 0)
        self.assertIn("Skipped 1 empty", dialog.summary.text())
        dialog.resize(940, 690)
        wait_for(lambda: dialog.spectrogram.height() > 100)
        for widget in (dialog.regions_table, dialog.waveform, dialog.spectrogram, dialog.play_button, dialog.confirm_button):
            top = widget.mapTo(dialog, widget.rect().topLeft())
            bottom = widget.mapTo(dialog, widget.rect().bottomRight())
            self.assertGreaterEqual(top.y(), 0)
            self.assertLessEqual(bottom.y(), dialog.height())
        self.assertFalse(dialog.waveform.getAxis("bottom").isVisible())
        self.assertEqual(dialog.wave_title.textFormat(), Qt.TextFormat.PlainText)
        self.assertEqual(dialog.message.textFormat(), Qt.TextFormat.PlainText)
        dialog.include_empty.setChecked(True)
        wait_for(lambda: dialog.regions_table.rowCount() == 2 and not self.window._busy and not dialog._waiting and dialog._pcm is not None)
        self.assertIsNone(self.window.project)
        self.assertFalse(list(self.root.rglob("*.sqlite*")))

    def test_cancel_and_window_close_hide_and_release_exactly_once(self):
        for use_button in (True, False):
            dialog = self.dialog(); self.loaded(dialog)
            preview = dialog.preview
            close = preview.close
            with patch.object(preview, "close", wraps=close) as closed:
                if use_button: dialog.cancel_button.click()
                else: dialog.close()
                self.assertFalse(dialog.isVisible())
                self.assertEqual(closed.call_count, 1)
                self.assertIsNone(self.window._interval_import_dialog)
        self.assertIsNone(self.window.project)

    def test_cancel_during_open_discards_late_preview(self):
        entered, gate = threading.Event(), threading.Event(); self.gates.append(gate)
        finished = []
        def late(*args, **kwargs):
            entered.set(); gate.wait(5)
            preview = open_preview(*args, **{**kwargs, "cancelled": None})
            close = preview.close
            def release(): finished.append(preview); close()
            preview.close = release
            return preview
        dialog = self.dialog()
        with patch.object(ui, "open_preview", side_effect=late):
            dialog.preview_button.click(); wait_for(entered.is_set)
            dialog.cancel_button.click()
            self.assertFalse(dialog.isVisible())
            gate.set(); wait_for(lambda: not self.window._busy)
        self.assertEqual(len(finished), 1)
        self.assertIsNone(self.window.project)

    def test_input_change_discards_stale_open_result(self):
        entered, gate = threading.Event(), threading.Event(); self.gates.append(gate)
        releases = []
        def late(*args, **kwargs):
            entered.set(); gate.wait(5)
            preview = open_preview(*args, **kwargs)
            close = preview.close
            preview.close = lambda: (releases.append(True), close())[-1]
            return preview
        dialog = self.dialog()
        with patch.object(ui, "open_preview", side_effect=late):
            dialog.preview_button.click(); wait_for(entered.is_set)
            dialog.recording_input.setText(str(self.root / "changed.wav"))
            gate.set(); wait_for(lambda: not self.window._busy)
        self.assertEqual(releases, [True])
        self.assertIsNone(dialog.preview)
        self.assertEqual(dialog.regions_table.rowCount(), 0)
        self.assertFalse(dialog.confirm_button.isEnabled())

    def test_tier_change_cannot_install_late_interval_pcm(self):
        entered, gate = threading.Event(), threading.Event(); self.gates.append(gate)
        first = [True]
        def inspect(*args, **kwargs):
            preview = open_preview(*args, **kwargs)
            original = preview.samples
            def samples(region):
                if first[0]:
                    first[0] = False; entered.set(); gate.wait(5)
                return original(region)
            preview.samples = samples
            return preview
        dialog = self.dialog()
        with patch.object(ui, "open_preview", side_effect=inspect):
            dialog.preview_button.click(); wait_for(entered.is_set)
            dialog.tier.setCurrentIndex(1)
            gate.set()
            wait_for(lambda: dialog._pcm is not None and not self.window._busy and not dialog._waiting)
        self.assertEqual(dialog.selected_region().start_frame, 24000)
        self.assertEqual(dialog._pcm_key[-2:], (24000, 36000))
        np.testing.assert_array_equal(dialog._pcm, self.pcm[24000:36000])

    def test_destination_and_contract_cancel_do_not_create_project(self):
        dialog = self.dialog(); self.loaded(dialog)
        destination = self.root / "not-created"
        with patch("syllafolio.annotation_desktop.QFileDialog.getSaveFileName", return_value=("", "")):
            dialog.confirm_button.click()
        self.assertIsNotNone(dialog.preview)
        with patch("syllafolio.annotation_desktop.QFileDialog.getSaveFileName", return_value=(str(destination), "")), patch.object(ProjectContractDialog, "exec", return_value=QDialog.DialogCode.Rejected):
            dialog.confirm_button.click()
        self.assertFalse(destination.exists())
        self.assertTrue(dialog.isVisible())
        self.assertIsNone(self.window.project)

    def test_confirm_real_project_uses_contract_and_closes_preview(self):
        dialog = self.dialog(); self.loaded(dialog)
        preview = dialog.preview
        destination = self.root / "created"
        def accept_contract(contract):
            contract.purpose.setCurrentIndex(contract.purpose.findData("engineering"))
            contract.operator.setText("ENGINEERING_ONLY")
            contract.budget.setValue(3600)
            return QDialog.DialogCode.Accepted
        with patch("syllafolio.annotation_desktop.QFileDialog.getSaveFileName", return_value=(str(destination), "")), patch.object(ProjectContractDialog, "exec", accept_contract), patch.object(preview, "close", wraps=preview.close) as closed:
            dialog.confirm_button.click()
            self.assertFalse(dialog.isVisible())
            wait_for(lambda: not self.window._busy and (self.window.project is not None or self.errors), 120)
            self.assertFalse(self.errors)
            self.assertEqual(closed.call_count, 1)
        self.assertFalse(self.errors)
        self.assertEqual(self.window.project.metadata["purpose"], "engineering")
        self.assertEqual(len(self.window.project.dataset.item_ids), 1)
        evidence = destination / "source_annotations/import.json"
        self.assertTrue(evidence.is_file())

    def test_audio_stop_dispatch_and_worker_cancel_on_close(self):
        dialog = self.dialog(); self.loaded(dialog)
        with patch.object(self.window.audio, "stop") as main_stop, patch.object(dialog.player, "stop") as interval_stop, patch.object(dialog.player, "play", return_value=True) as played:
            dialog.play_selected()
            main_stop.assert_called()
            interval_stop.assert_called()
            played.assert_called_once()
        dialog.close()
        self.assertTrue(not shiboken6.isValid(dialog) or not dialog.isVisible())

    def test_abort_queued_switch_releases_transferred_preview_without_creation(self):
        dialog = self.dialog(); self.loaded(dialog)
        preview = dialog.preview
        selection = dialog.take_selection()
        target = self.root / "aborted-project"
        def hold_transition(action):
            self.window._close_coordinator.begin({"jobs": []})
            self.window._transition = {"action": action, "closing_window": False}
            return True
        with patch.object(self.window, "_request_transition", side_effect=hold_transition), patch.object(preview, "close", wraps=preview.close) as closed:
            self.assertTrue(self.window.import_interval_project(selection, target))
            self.assertIs(self.window._interval_import_ticket, selection)
            self.window.abort_transition()
            self.assertEqual(closed.call_count, 1)
        self.assertFalse(target.exists())
        self.assertFalse(dialog.isVisible())
        self.assertIsNone(self.window._interval_import_ticket)

    def test_window_close_during_open_settles_and_releases_late_result(self):
        entered, gate = threading.Event(), threading.Event(); self.gates.append(gate)
        released = []
        def late(*args, **kwargs):
            entered.set(); gate.wait(5)
            preview = open_preview(*args, **{**kwargs, "cancelled": None})
            close = preview.close
            preview.close = lambda: (released.append(True), close())[-1]
            return preview
        dialog = self.dialog()
        with patch.object(ui, "open_preview", side_effect=late):
            dialog.preview_button.click(); wait_for(entered.is_set)
            self.window.request_close("cancel")
            self.assertEqual(self.window._close_coordinator.state, "draining")
            gate.set()
            wait_for(lambda: self.window._closing_approved)
        self.assertEqual(released, [True])
        self.assertIsNone(self.window.project)
        self.assertTrue(not shiboken6.isValid(dialog) or not dialog.isVisible())


if __name__ == "__main__":
    unittest.main()
