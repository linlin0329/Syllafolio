"""Candidate grouping previews, frozen-input identity and explicit review changes."""
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from syllafolio.annotation_desktop_organization import compare_groupings
from syllafolio.annotation_desktop_project import create_project, open_project


def frozen_test_clusters(scaled):
    # Four compact modes with ten targets each; preserve two broader import groups.
    n = len(scaled)
    random = np.random.default_rng(52)
    embedding = random.normal(0, .02, (n, 10))
    embedding[:, 0] += np.repeat([0., 3., 30., 33.], 10)
    return embedding, np.repeat([0, 1], 20), np.ones(n), "fixed_umap_hdbscan"


class GroupingComparison(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.clips = self.root / "clips"
        self.clips.mkdir()
        self.project_dir = self.root / "project"

    def write_clips(self, count):
        t = np.arange(9600) / 48000
        for i in range(count):
            signal = np.hanning(len(t)) * .1 * np.sin(2*np.pi*(1000+20*i)*t)
            sf.write(self.clips / f"clip-{i:03d}.wav", signal, 48000)

    def test_preview_apply_reopen_and_restore_original(self):
        self.write_clips(40)
        with (patch("syllafolio.ei_b_assistance._cluster", side_effect=frozen_test_clusters),
              patch("syllafolio.acoustics.audio_processor.AudioProcessor.extract_features", return_value=np.arange(105)),
              patch("syllafolio.acoustics.log_mel.log_mel_patch", return_value=np.zeros(2048))):
            project = create_project(self.clips, self.project_dir)
        with project:
            initial = project.snapshot()
            controllers = {}
            writes_before = project.service.store._connection.total_changes
            with patch("syllafolio.ei_b_assistance._cluster", side_effect=AssertionError("global refit")):
                rows = compare_groupings(project, controllers_by_mode=controllers)
            self.assertEqual(writes_before, project.service.store._connection.total_changes)
            self.assertEqual([r["min_cluster_size"] for r in rows], [5, 10, 20])
            self.assertEqual([r["min_samples"] for r in rows], [5, 5, 5])
            self.assertEqual([r["candidate_group_count"] for r in rows[:2]], [4, 2])
            self.assertEqual(rows[0]["largest_group_size"], 10)
            self.assertEqual(rows[0]["unclustered_fraction"], 0)
            self.assertEqual(initial["revision"], project.snapshot()["revision"])
            self.assertIsNone(project.snapshot()["batch_review"]["organization"])
            default_groups = controllers["microclusters"].snapshot()["preview"]["groups"]
            initial_revision = initial["revision"]
            with self.assertRaisesRegex(ValueError, "Preview"):
                project.execute("batch_apply", {"mode": "fine"}, expected_revision=initial_revision,
                                idempotency_key="missing-preview")
            project.execute("batch_family", {"family_id": "F-existing", "title": "Existing",
                            "diagnostic_body": "An existing review criterion", "reason": "Created before regrouping"},
                            expected_revision=initial_revision, idempotency_key="family-before-comparison")
            with self.assertRaisesRegex(ValueError, "PREVIEW_CHANGED"):
                controllers["fine"].apply(project, generation=0, idempotency_key="stale")
            compare_groupings(project, controllers_by_mode=controllers)
            fine = controllers["fine"]
            with self.assertRaisesRegex(ValueError, "CONFIRMATION"):
                fine.apply(project, generation=0, idempotency_key="without-confirmation")
            impact = fine.snapshot()["preview"]["impact"]
            fine.acknowledge(acknowledged=True, impact_fingerprint=impact["fingerprint"])
            fine.apply(project, generation=0, idempotency_key="apply-fine")
            self.assertTrue(fine.apply(project, generation=0, idempotency_key="apply-fine")["replayed"])
            applied = project.snapshot()["batch_review"]["organization"]
            self.assertEqual(applied["mode"], "fine")
            self.assertIn("embedding_sha256", applied)
            self.assertEqual(applied["hdbscan_parameters"]["min_cluster_size"], 5)
            self.assertEqual(len(applied["groups"]), 4)
            prepared = project.execute("batch_prepare", {"group_id": applied["groups"][0]["group_id"],
                "route": "direct", "normalization": "per_item", "gap_seconds": .1},
                expected_revision=project.snapshot()["revision"], idempotency_key="prepare-first-fine-group")
            review_id = prepared["record"]["review"]["review_id"]
            project.service._futures[review_id].result(timeout=30)
            state = project.snapshot()
            page = state["batch_review"]["reviews"][review_id]["pages"][0]
            item = page["item_ids"][0]
            project.execute("batch_submit_v2", {"review_id": review_id, "page_id": page["page_id"],
                "current_family_id": "F-existing", "items": [{"item_id": item, "disposition": "current",
                "family_id": "F-existing"}]}, expected_revision=state["revision"], idempotency_key="save-one-item")
            saved_assignment = project.snapshot()["batch_review"]["assignments"][item]
        with open_project(self.project_dir) as reopened:
            self.assertEqual(reopened.snapshot()["batch_review"]["organization"]["groups"], applied["groups"])
            self.assertTrue(any(row["local_type_id"] == "F-existing" for row in reopened.snapshot()["memberships"]))
            rows = compare_groupings(reopened, controllers_by_mode=controllers)
            self.assertTrue(rows[0]["already_applied"])
            with self.assertRaisesRegex(ValueError, "ALREADY_APPLIED"):
                controllers["fine"].apply(reopened, generation=0, idempotency_key="repeat-fine")
            default = controllers["microclusters"]
            self.assertEqual(default.snapshot()["preview"]["impact"]["saved_assignment_count"], 1)
            default.acknowledge(acknowledged=True, impact_fingerprint=default.snapshot()["preview"]["impact"]["fingerprint"])
            default.apply(reopened, generation=0, idempotency_key="restore-original")
            state = reopened.snapshot()
            self.assertEqual(state["batch_review"]["organization"]["groups"], default_groups)
            self.assertEqual(state["batch_review"]["assignments"][item], saved_assignment)
            family = next(row for row in state["memberships"] if row["local_type_id"] == "F-existing")
            self.assertIn(item, family["member_ids"])
            embedding = reopened.service.dataset_dir.parent / "global" / "embedding_10d.npy"
            original = embedding.read_bytes()
            embedding.write_bytes(original + b"changed")
            with self.assertRaisesRegex(ValueError, "GLOBAL_CANDIDATES_CHANGED"):
                compare_groupings(reopened)

    def test_small_legacy_style_batch_keeps_default_and_cancellation_is_read_only(self):
        self.write_clips(2)
        with create_project(self.clips, self.project_dir) as project:
            controllers = {}
            with patch("syllafolio.annotation_batch_acoustics._partition_labels",
                       side_effect=AssertionError("No fit is allowed without a saved embedding")):
                rows = compare_groupings(project, controllers_by_mode=controllers)
            self.assertEqual([row["available"] for row in rows], [False, True, False])
            self.assertEqual(rows[1]["unclustered_count"], 2)
            initial_revision = project.snapshot()["revision"]
            unused = {}
            with self.assertRaisesRegex(ValueError, "CANCELLED"):
                compare_groupings(project, cancelled=lambda: True, controllers_by_mode=unused)
            self.assertEqual(unused, {})
            self.assertEqual(initial_revision, project.snapshot()["revision"])


if __name__ == "__main__":
    unittest.main()
