"""Verify directional out-of-fold examples, representative identity and rendered PCM."""
from pathlib import Path
import json
import sys
import time
from types import SimpleNamespace
import unittest

import numpy as np
from PySide6.QtWidgets import QApplication, QDialog

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from syllafolio.annotation_desktop_assessment_listen import PairAuditionDialog, confusion_cases
from syllafolio.annotation_desktop_review_media import DisplayController


def fixture():
    truth = {"one": "a", "two": "a", "three": "a", "four": "b"}
    sources = {"one": "s1", "two": "s2", "three": "s1", "four": "s3"}
    predictions = []
    for repeat, errors in enumerate(({"one", "two"}, {"two"}, {"one"}), 1):
        rows = [{"item_id": i, "source_group_id": sources[i], "true_family_id": fid,
                 "predicted_family_id": ("b" if i in errors else "a" if i == "four" else fid)}
                for i, fid in truth.items()]
        predictions.append({"repeat": repeat, "predictions": rows})
    return {
        "result": {"status": "completed", "eligible_family_ids": ["a", "b"], "repeats": predictions},
        "frozen_snapshot": {
            "items": [{"item_id": i, "source_group_id": sources[i], "family_id": fid, "in_scope": True,
                       "label_status": "confirmed"} for i, fid in truth.items()],
            "definitions": {"a": {"card_id": "card-a"}, "b": {"card_id": "card-b"}},
            "memberships": [{"local_type_id": "a", "member_ids": ["one", "two", "three"]},
                            {"local_type_id": "b", "member_ids": ["four"]}],
        },
    }


class AssessmentListening(unittest.TestCase):
    def test_direction_counts_distinct_syllables_across_repeats(self):
        report = fixture()
        cases = confusion_cases(report, "a", "b")
        self.assertEqual([(r["item_id"], r["source_group_id"], r["hits"], r["total"]) for r in cases],
                         [("one", "s1", 2, 3), ("two", "s2", 2, 3)])
        self.assertEqual([r["item_id"] for r in confusion_cases(report, "b", "a")], ["four"])
        row = report["result"]["repeats"][0]["predictions"][0]
        report["result"]["repeats"][0]["predictions"].append(row)
        with self.assertRaisesRegex(ValueError, "predictions"):
            confusion_cases(report, "a", "b")

    def test_example_and_matched_family_representatives_share_audition(self):
        app = QApplication.instance() or QApplication([])
        rate = 48000
        pcm = {i: (.1 * np.sin(np.arange(4800) * hz * 2*np.pi/rate)).astype("float32")
               for i, hz in zip(("one", "two", "three", "four"), (1100, 2200, 3300, 4400))}
        job = {"job_id": "j", "status": "completed", "stale_for_current": False, "report_sha256": "sha"}
        cards = {f: {"card_id": "card-"+f, "title": f, "representative_ids": ids}
                 for f, ids in (("a", ["one", "three"]), ("b", ["four"]))}
        state = {"revision": 1, "type_cards": cards, "final_review": {"assessment": {"jobs": [job]}}}
        project = SimpleNamespace(snapshot=lambda: state, dataset=SimpleNamespace(sample_rate_hz=rate),
                                  read_audio=lambda i: (pcm[i], rate, None))
        controller = SimpleNamespace(project=project, job=lambda *_: job,
                                     report_reader=lambda *_: lambda: json.dumps(fixture()).encode())
        layout = {"mode": "fixed", "count": 1, "ms_per_pixel": 5.0, "plot_width_px": 600, "label_width_px": 80}
        host = QDialog()
        host.owner = SimpleNamespace(state=state, gap=SimpleNamespace(value=lambda: .2),
                                     review_page_layout=lambda: layout, display_controller=DisplayController(),
                                     _stop_review_audio=lambda: None)
        host._selected_job = "j"
        host.is_current = lambda: True
        dlg = PairAuditionDialog(host, controller, "j", "a", "b")
        dlg.show()

        def wait(condition):
            deadline = time.monotonic() + 30
            while not condition():
                app.processEvents()
                if time.monotonic() > deadline:
                    self.fail((dlg.legend.text(), dlg.audition.status.text()))
                time.sleep(.01)
        try:
            wait(dlg.audition.play_button.isEnabled)
            # First example is also the selected representative. Hear it once, with both roles.
            self.assertEqual([s["item_id"] for s in dlg.audition.canvas.slots], ["one", "four"])
            self.assertIn("Example / a representative", dlg.legend.text())
            dlg.next_button.click()
            dlg.true_rep.setCurrentIndex(1)
            wait(dlg.audition.play_button.isEnabled)
            slots = dlg.audition.canvas.slots
            self.assertEqual([s["item_id"] for s in slots], ["two", "three", "four"])
            for slot in slots:
                np.testing.assert_array_equal(dlg.audition.player.samples[slot["start_frame"]:slot["stop_frame"]],
                                              pcm[slot["item_id"]])
            self.assertEqual(dlg.audition.canvas._ms_per_pixel, 5.0)
            self.assertFalse(dlg.next_button.isEnabled())
            # A previous calculation cannot present newer membership as its frozen reference set.
            state["revision"] = 2
            job["stale_for_current"] = True
            wait(lambda: not dlg.isVisible())
            self.assertEqual(len(dlg.audition.player.samples), 0)
        finally:
            dlg.close()
            host.close()
