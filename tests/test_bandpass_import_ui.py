"""Import preprocessing UI: explicit opt-in, validation and preview binding."""
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

import numpy as np
from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox

import test_interval_import_ui as base_ui
from syllafolio import annotation_desktop as desktop
from syllafolio.annotation_desktop_stage_a import inspect_input
from syllafolio.annotation_desktop_stage_a_ui import BandpassControls, InputInspectionDialog


class BandpassControlUi(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_default_off_and_explicit_settings(self):
        controls = BandpassControls()
        self.assertIsNone(controls.settings(48000))
        self.assertFalse(controls.enable_checkbox.isChecked())
        self.assertFalse(controls.low_hz.isEnabled())
        self.assertEqual((controls.low_hz.value(), controls.high_hz.value(), controls.order.value()), (500, 15000, 4))
        self.assertEqual((controls.order.minimum(), controls.order.maximum()), (1, 8))
        controls.enable_checkbox.setChecked(True)
        self.assertEqual(controls.settings(48000), {"low_hz": 500.0, "high_hz": 15000.0, "order": 4})
        controls.close()

    def test_inspection_rejects_nyquist_before_accepting(self):
        dialog = InputInspectionDialog({"summary": {"sample_rate_hz": 32000}, "items": []})
        dialog.show()
        dialog.bandpass.enable_checkbox.setChecked(True)
        dialog.bandpass.high_hz.setValue(16000)
        button = dialog.buttons.button(QDialogButtonBox.StandardButton.Ok)
        button.click()
        self.assertTrue(dialog.isVisible())
        self.assertTrue(dialog.problem.text())
        self.assertNotEqual(dialog.result(), QDialog.DialogCode.Accepted)
        dialog.bandpass.high_hz.setValue(15000)
        button.click()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
        self.assertEqual(dialog.bandpass_settings(), {"low_hz": 500.0, "high_hz": 15000.0, "order": 4})
        dialog.close()

    def test_disabled_setting_does_not_call_filter_validation(self):
        controls = BandpassControls()
        with patch("syllafolio.acoustics.bandpass.validate_bandpass", side_effect=AssertionError("Disabled filtering must stay off")):
            self.assertIsNone(controls.settings(48000))
        controls.close()


class BandpassImportUi(TestCase):
    # Reuse fixture methods without inheriting and repeating the original tests.
    @classmethod
    def setUpClass(cls):
        base_ui.IntervalImportUi.setUpClass.__func__(cls)

    setUp = base_ui.IntervalImportUi.setUp
    tearDown = base_ui.IntervalImportUi.tearDown
    dialog = base_ui.IntervalImportUi.dialog
    loaded = base_ui.IntervalImportUi.loaded

    @staticmethod
    def accept_contract(contract):
        contract.purpose.setCurrentIndex(contract.purpose.findData("engineering"))
        contract.operator.setText("ENGINEERING_ONLY")
        contract.budget.setValue(3600)
        return QDialog.DialogCode.Accepted

    def test_segmented_confirmation_passes_filter_and_preserves_raw_inspection(self):
        view = inspect_input(self.root)
        fingerprint = view["inspection_fingerprint"]
        def accept_input(dialog):
            dialog.bandpass.enable_checkbox.setChecked(True)
            dialog.bandpass.order.setValue(8)
            dialog.accept()
            return dialog.result()
        with patch.object(InputInspectionDialog, "exec", accept_input), patch.object(desktop.ProjectContractDialog, "exec", self.accept_contract), patch.object(self.window, "import_project_path") as imported:
            self.window._confirm_inspected_import(self.root, self.root / "segmented", view)
        self.assertEqual(imported.call_args.kwargs["bandpass"], {"low_hz": 500.0, "high_hz": 15000.0, "order": 8})
        self.assertEqual(imported.call_args.kwargs["expected_inspection_fingerprint"], fingerprint)
        self.assertEqual(view["inspection_fingerprint"], fingerprint)
        self.assertIsNone(self.window.project)

    def test_recording_setting_change_invalidates_then_previews_filtered_pcm(self):
        dialog = self.dialog(); self.loaded(dialog)
        raw = dialog._pcm.copy()
        previous = dialog.preview
        with patch.object(previous, "close", wraps=previous.close) as closed:
            dialog.bandpass.enable_checkbox.setChecked(True)
            self.assertEqual(closed.call_count, 1)
        self.assertIsNone(dialog.preview)
        self.assertIsNone(dialog._pcm)
        self.assertFalse(dialog.confirm_button.isEnabled())
        dialog.bandpass.low_hz.setValue(5000)
        self.loaded(dialog)
        self.assertEqual(dialog.preview.bandpass, {"low_hz": 5000.0, "high_hz": 15000.0, "order": 4})
        self.assertFalse(np.array_equal(raw, dialog._pcm))
        self.assertLess(float(np.sqrt(np.mean(dialog._pcm**2))), float(np.sqrt(np.mean(raw**2))) * .2)
        np.testing.assert_array_equal(dialog._pcm, dialog.preview.samples(dialog.selected_region()))
        np.testing.assert_array_equal(dialog.player.samples, dialog._pcm)
        self.assertGreater(dialog.spectrum_image.image.size, 0)
        self.assertIsNone(self.window.project)

    def test_invalid_cutoff_does_not_start_preview(self):
        dialog = self.dialog()
        dialog.bandpass.enable_checkbox.setChecked(True)
        dialog.bandpass.low_hz.setValue(15000)
        dialog.bandpass.high_hz.setValue(10000)
        with patch.object(self.window, "_run") as started:
            dialog.preview_button.click()
        started.assert_not_called()
        self.assertIsNone(dialog.preview)
        self.assertFalse(dialog.confirm_button.isEnabled())
        self.assertTrue(dialog.message.text())

    def test_confirmed_project_keeps_auditioned_filter_and_shows_details(self):
        dialog = self.dialog()
        dialog.bandpass.enable_checkbox.setChecked(True)
        dialog.bandpass.low_hz.setValue(5000)
        self.loaded(dialog)
        auditioned = dialog._pcm.copy()
        destination = self.root / "filtered-project"
        with patch.object(desktop.QFileDialog, "getSaveFileName", return_value=(str(destination), "")), patch.object(desktop.ProjectContractDialog, "exec", self.accept_contract):
            dialog.confirm_button.click()
            base_ui.wait_for(lambda: not self.window._busy and (self.window.project is not None or self.errors), 120)
        self.assertFalse(self.errors)
        project = self.window.project
        self.assertIsNotNone(project)
        ident, = project.dataset.item_ids
        samples, rate, _ = project.read_audio(ident)
        self.assertEqual(rate, self.rate)
        np.testing.assert_array_equal(samples, auditioned)
        details = self.window._project_identity_text()
        self.assertIn("Import band-pass filter: 5000–15000 Hz", details)
        self.assertIn("Filter order: 4", details)
        self.assertIn("Filter method:", details)
