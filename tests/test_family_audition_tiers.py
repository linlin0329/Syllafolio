"""Playback order and page boundaries for ordinary human family review."""
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
from scipy.cluster.hierarchy import leaves_list, linkage
from scipy.spatial.distance import pdist

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from syllafolio.annotation_batch_acoustics import prepare_review
from syllafolio.annotation_family_acoustics import family_audition_route


class ExampleAudio:
    """Distinct clips with three widely separated locations in the frozen feature space."""

    def __init__(self):
        self.item_ids = [f"s-{i:02d}" for i in range(31)]
        self.sample_rate_hz = 48000
        self.input_fingerprint = "fixture"
        self.input_versions = {}
        self._provenance = {}
        self.preset_id = "fixture"
        self.preset = {"local": {"minimum_recluster_rows": 20}}
        self._parents = {"parent": {"sha256": "fixture"}}
        self._items = {i: {"source_clip_id": "parent", "source_frame_start": 0, "source_frame_stop": 2400}
                       for i in self.item_ids}
        axis = np.arange(2400) / self.sample_rate_hz
        self.audio = {i: (.05*np.sin(2*np.pi*(1400+40*k)*axis)).astype("float32")
                      for k, i in enumerate(self.item_ids)}
        positions = np.random.default_rng(64).normal(0, .02, (len(self.item_ids), 94))
        positions[:, 0] += (np.arange(len(self.item_ids)) % 3) * 100.
        self._global94 = dict(zip(self.item_ids, positions))

    def _select(self, ids, *_args, **_kwargs):
        assert len(set(ids)) == len(ids) and set(ids) <= set(self.item_ids)
        return list(ids)

    def read_audio(self, ident):
        return self.audio[ident], self.sample_rate_hz, {"source_clip_id": "parent"}

    def run_local(self, action, ids, refs, folder, *, check_cancelled=None):
        assert action == "local_recluster" and len(ids) == 31 and not refs
        return {"fit_ids": ids, "result_id": "fixture-clustered", "ordered_ids": ids[::-1],
                "assignments": [{"item_id": item, "candidate_cluster_id": None if k == 30 else str(k % 2)}
                                for k, item in enumerate(ids)]}


class FamilyAuditionTiers(unittest.TestCase):
    def test_family_boundaries_order_independent_of_page_size(self):
        ds = ExampleAudio()
        layout = {"mode": "fixed", "count": 7, "ms_per_pixel": 5.,
                  "plot_width_px": 560, "label_width_px": 110}
        with TemporaryDirectory() as temporary:
            for n, route in ((10, "direct"), (11, "family_order"), (30, "family_order"), (31, "family_cluster")):
                ids = ds.item_ids[:n]
                self.assertEqual(family_audition_route(n), route)
                if n in (11, 30):
                    z = linkage(pdist([ds._global94[i] for i in ids]), method="average", optimal_ordering=True)
                    expected = [ids[i] for i in leaves_list(z)]
                    self.assertNotEqual(expected, ids)
                orderings = []
                for mode in ("fixed", "auto"):
                    options = dict(layout, mode=mode)
                    folder = Path(temporary) / f"{n}-{mode}"
                    result = prepare_review(ds, ids[::-1], folder, route, .1, "per_item", page_layout=options)
                    pages = result["pages"]
                    flat = [item for page in pages for item in page["item_ids"]]
                    self.assertCountEqual(flat, ids)
                    self.assertTrue(all(p["page_layout"] == options for p in pages))
                    if n < 31:
                        self.assertEqual(flat, ids if n == 10 else expected)
                        self.assertFalse(result["clustering_performed"])
                        size = 7 if mode == "fixed" else 5
                        self.assertEqual([p["item_ids"] for p in pages],
                                         [flat[i:i+size] for i in range(0, n, size)])
                    else:
                        self.assertTrue(result["clustering_performed"])
                        for page in pages:
                            for item in page["item_ids"]:
                                k = ids.index(item)
                                self.assertEqual(page["subcluster_id"], "unclustered" if k == 30 else str(k % 2))
                    orderings.append(flat)
                self.assertEqual(*orderings)

    def test_identical_features_keep_deterministic_complete_order(self):
        ds = ExampleAudio()
        ids = ds.item_ids[:11]
        ds._global94 = dict.fromkeys(ids, np.zeros(94))
        with TemporaryDirectory() as temporary:
            result = prepare_review(ds, ids[::-1], Path(temporary)/"tied", family_audition_route(11), .1, "per_item")
            self.assertEqual([i for page in result["pages"] for i in page["item_ids"]], ids)
            self.assertFalse(result["clustering_performed"])
