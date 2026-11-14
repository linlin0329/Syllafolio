"""The display dialog previews page layout without changing the current round."""
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

import numpy as np
from PySide6.QtWidgets import QApplication, QDialog

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from syllafolio.annotation_desktop_comparison_audition import prepare_audition
from syllafolio.annotation_desktop_page_settings import ReviewPageSettings
from syllafolio.annotation_desktop_review_media import DisplayController, PREFERENCES


class PageSettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_preview_fixed_count_above_ten_and_restore_saved_project_preferences(self):
        ids = [f"syllable-{i:04}" for i in range(14)]
        items = {i: {"source_frame_start": 0, "source_frame_stop": 4800} for i in ids}
        dataset = SimpleNamespace(sample_rate_hz=48000, _items=items, input_fingerprint="test",
                                  public_items=lambda: [{"duration_s": .1} for _ in ids])
        signal = np.sin(np.arange(4800) * .1).astype("float32")
        with TemporaryDirectory() as root:
            project = SimpleNamespace(project_dir=Path(root), service=SimpleNamespace(run_id="r"),
                                      dataset=dataset, read_audio=lambda _i: (signal, 48000, {}))
            controller = DisplayController()
            controller.load_preferences(project)
            group = {"group_id": "g", "item_ids": ids}
            owner = SimpleNamespace(project=project, display_controller=controller, selected_group="g",
                                    state={"batch_review": {"organization": {"groups": [group]}}},
                                    review_page_layout=lambda prefs: controller.page_layout(800, 100, prefs),
                                    gap=SimpleNamespace(value=lambda: .05),
                                    canvas=SimpleNamespace(drawable_width_px=lambda: 800))
            dialog = QDialog()
            controls = ReviewPageSettings(owner, dialog)
            self.assertTrue(controls.preview.isEnabled())
            self.assertFalse(controls.apply.isEnabled())
            controls.mode.setCurrentIndex(controls.mode.findData("fixed"))
            controls.count.setValue(14)
            controls.scale_mode.setCurrentIndex(controls.scale_mode.findData("manual"))
            controls.scale.setValue(5.)
            self.assertTrue(controls.apply.isEnabled())
            self.assertEqual(controls.first_ids, ids)
            self.assertIn("1 page, 14 per page", controls.estimate.text())
            pcm, rate, page, _ = prepare_audition(project, controls.first_ids, owner.gap.value(),
                                                 page_layout=controls.layout_snapshot)
            self.assertEqual(page["page_layout"]["ms_per_pixel"], 5.)
            self.assertEqual(len(page["slots"]), 14)
            self.assertEqual(len(pcm), 14 * 4800 + 13 * 2400)
            self.assertEqual(rate, 48000)
            self.assertFalse((project.project_dir / PREFERENCES).exists())
            controls.save()
            restored = DisplayController()
            restored.load_preferences(project)
            self.assertEqual(restored.review_pages, {"mode": "fixed", "count": 14, "ms_per_pixel": 5.})
            self.assertFalse(controls.apply.isEnabled())
            dialog.close()
