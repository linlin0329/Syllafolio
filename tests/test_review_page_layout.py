"""Verify paginated listening media and the frozen review-page contract."""
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from syllafolio.annotation_desktop_project import create_project, open_project
from syllafolio.annotation_page_layout import page_slices


class ReviewPageLayoutTests(unittest.TestCase):
    auto = {"mode": "auto", "count": 10, "ms_per_pixel": 5.0, "plot_width_px": 1200, "label_width_px": 80}

    def test_auto_pages_use_duration_and_labels_and_preserve_long_syllables(self):
        def counts(frames, width, labels):
            layout = dict(self.auto, plot_width_px=width, label_width_px=labels)
            return [s.stop - s.start for s in page_slices(frames, 1000, 300, layout)]

        self.assertEqual(counts([300] * 13, 1200, 80), [10, 3])
        self.assertEqual(counts([300] * 13, 600, 80), [5, 5, 3])
        self.assertEqual(counts([1500] * 7, 1200, 80), [3, 3, 1])
        self.assertEqual(counts([120, 120, 8000, 200, 200], 800, 350), [2, 1, 2])
        self.assertEqual(counts([100] * 12, 1200, 110), [10, 2])
        self.assertEqual(counts([700] * 5, 340, 40), [2, 2, 1])  # Exactly 1700 ms including the gap.

    def test_fixed_review_pages_over_ten_survive_save_and_reopen(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            clips = root / "clips"
            clips.mkdir()
            rate = 48000
            # Thirteen distinct syllables, including the two that used to be
            # unreachable on a page because the reducer enforced ten slots.
            for i in range(13):
                t = np.arange(3000 + 7*i) / rate
                sf.write(clips / f"s-{i:02}.wav", .08 * np.sin(2*np.pi*(2100 + 97*i)*t), rate)
            embedding = np.arange(130).reshape(13, 10)
            with patch("syllafolio.ei_b_assistance._cluster",
                       return_value=(embedding, np.zeros(13, dtype=int), np.ones(13), "fixed_umap_hdbscan")):
                project = create_project(clips, root / "project")
            def execute(action, data):
                return project.execute(action, data, expected_revision=project.snapshot()["revision"],
                                       idempotency_key=uuid4().hex)
            with project:
                from syllafolio.annotation_desktop_organization import OrganizationController
                organization = OrganizationController()
                organization.preview(project, generation=0, idempotency_key="preview")
                organization.apply(project, generation=0, idempotency_key="apply")
                group = project.snapshot()["batch_review"]["organization"]["groups"][0]
                layout = dict(self.auto, mode="fixed", count=12)
                req = dict(group_id=group["group_id"], gap_seconds=.05, route="direct", normalization="per_item")
                revision = project.snapshot()["revision"]
                with self.assertRaisesRegex(ValueError, "PAGE_LAYOUT"):
                    execute("batch_prepare", dict(req, page_layout=dict(layout, ms_per_pixel=0)))
                self.assertEqual(revision, project.snapshot()["revision"])
                rid = execute("batch_prepare", dict(req, page_layout=layout))["record"]["review"]["review_id"]
                project.service._futures[rid].result(timeout=45)
                review = project.snapshot()["batch_review"]["reviews"][rid]
                self.assertEqual(review["status"], "ready", review.get("error"))
                pages = review["pages"]
                self.assertEqual([len(p["slots"]) for p in pages], [12, 1])
                self.assertEqual([p["page_layout"] for p in pages], [layout, layout])
                self.assertEqual(pages[0]["item_ids"] + pages[1]["item_ids"], sorted(group["item_ids"]))
                for p in pages:
                    audio, sr = sf.read(project.media_path(p["audio_url"]), dtype="float32")
                    self.assertEqual(sr, rate)
                    self.assertEqual(len(audio), p["slots"][-1]["stop_frame"])
                    for slot in p["slots"]:
                        samples, _, _ = project.read_audio(slot["item_id"])
                        np.testing.assert_array_equal(audio[slot["start_frame"]:slot["stop_frame"]], samples)
                execute("batch_family", dict(family_id="F-layout", title="Fixture", diagnostic_body="Test pattern",
                                             reason="Exercise saving a twelve-slot page."))
                execute("batch_submit_v2", dict(review_id=rid, page_id=pages[0]["page_id"], current_family_id="F-layout",
                    items=[dict(item_id=i, disposition="current", family_id="F-layout") for i in pages[0]["item_ids"]]))
            with open_project(root / "project") as reopened:
                batch = reopened.snapshot()["batch_review"]
                self.assertEqual(batch["reviews"][rid]["pages"], pages)
                self.assertEqual(set(batch["assignments"]), set(pages[0]["item_ids"]))
