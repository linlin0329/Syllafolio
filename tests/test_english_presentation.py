"""English system text must not change user-authored values in older projects."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from syllafolio.annotation_english import system_error, system_fields, system_text
from syllafolio.annotation_material_origin import classify_material


class EnglishPresentation(unittest.TestCase):
    def test_legacy_diagnostic_and_operator_text_are_distinct(self):
        original = "\u5f53\u524d\u6ca1\u6709\u8be5\u590d\u6838\u8f6e\u6b21"
        legacy = "\u5ba1\u542c\u8f6e\u6b21\u4e0d\u5b58\u5728"
        source = {"error": {"code": "BATCH_REVIEW", "message": legacy}, "title": legacy,
                  "reason": legacy, "operator_note": original}
        view = system_fields(source)
        self.assertEqual(view["error"]["message"], "Review round not found.")
        self.assertEqual(system_text("BATCH_REVIEW: " + legacy), "BATCH_REVIEW: Review round not found.")
        self.assertEqual(view["title"], legacy)
        self.assertEqual(view["reason"], legacy)
        self.assertEqual(view["operator_note"], original)
        self.assertEqual(source["error"]["message"], legacy)

    def test_frozen_preset_gets_an_english_display_copy(self):
        import json
        path = Path(__file__).resolve().parents[1]/"configs/annotation_assistant/p1/preset.json"
        preset = json.loads(path.read_bytes())
        note = system_fields(preset)["provenance"]["note"]
        self.assertTrue(note.startswith("The 94-dimensional acoustic and log-mel views"))
        self.assertNotEqual(note, preset["provenance"]["note"])
        self.assertIn("not verified", classify_material(["0" * 64])["label"])

    def test_grouped_windows_copy_error_keeps_names_and_translates_system_detail(self):
        import shutil
        name = "test-\u97f3\u8282.wav"
        localized = "[WinError 3] \u7cfb\u7edf\u627e\u4e0d\u5230\u6307\u5b9a\u7684\u8def\u5f84\u3002"
        detail = system_error(shutil.Error([(name, "backup", localized)]))
        self.assertIn(name, detail)
        self.assertNotIn(localized, detail)
        self.assertIn("3", detail)
