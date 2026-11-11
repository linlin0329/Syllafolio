"""普通人工family的小样本试听组织；不更改全局表示、预设或人工类型。"""
from __future__ import annotations

from pathlib import Path

import numpy as np
from scipy.cluster.hierarchy import fcluster, leaves_list, linkage
from scipy.spatial.distance import pdist, squareform

from .annotation_acoustics import _cancel, _hash, _require, _sha, _write_json

FAMILY_CLUSTER_POLICY = "ordinary-family-cluster-v1"
POOL_CLUSTER_POLICY = "ordinary-pool-cluster-v1"
SMALL_METHOD = "family-small-global94-average-linkage-v1"


def prepare_small_family(dataset, item_ids, output_dir, *, check_cancelled=None, scope_kind="family"):
    """Use frozen 94D distances, avoiding rank-saturated PCA whitening on tiny sets."""
    _require(scope_kind in {"family", "pool"}, "SMALL_AUDITION_SCOPE", "Small-sample organization supports only ordinary family reviews or the complete candidate pool.")
    ids = sorted(dataset._select(item_ids, "item_ids", nonempty=True))
    minimum = dataset.preset["local"]["minimum_recluster_rows"]
    _require(len(ids) < minimum, "FAMILY_SMALL_SCOPE" if scope_kind == "family" else "POOL_SMALL_SCOPE",
             "This scope requires the original fixed local-clustering pipeline.")
    _cancel(check_cancelled)
    count = len(ids)
    cut = None
    if count == 1:
        distances = np.zeros((1, 1), dtype=np.float64)
        tree = np.empty((0, 4), dtype=np.float64)
        labels, order = np.ones(1, dtype=int), [0]
        method, cut_reason = "single_member_no_clustering", "single_member"
    else:
        try:
            values = np.asarray([dataset._global94[ident] for ident in ids], dtype=np.float64)
            _require(values.shape == (count, 94) and np.isfinite(values).all(),
                     "INVALID_FEATURE", "The frozen 94D representation is incomplete or contains non-finite values.")
            _cancel(check_cancelled)
            condensed = pdist(values, metric="euclidean")
            distances = squareform(condensed)
            tree = linkage(condensed, method="average", optimal_ordering=True)
            order = leaves_list(tree).tolist()
            heights = tree[:, 2]
            tolerance = np.finfo(np.float64).eps * max(1.0, float(heights[-1])) * 64
            labels = np.ones(count, dtype=int)
            method = SMALL_METHOD if scope_kind == "family" else "pool-small-global94-average-linkage-v1"
            cut_reason = "two_or_three_members_keep_root_for_comparison"
            if count >= 4:
                gaps = np.diff(heights)
                split = int(np.argmax(gaps))
                if float(gaps[split]) > tolerance:
                    cut = float(heights[split] + gaps[split] / 2)
                    labels = fcluster(tree, cut, criterion="distance")
                    cut_reason = "midpoint_of_largest_positive_adjacent_merge_height_gap"
                else:
                    cut_reason = "no_distinguishable_merge_height_gap_keep_root"
            if float(heights[-1]) <= tolerance:
                order = list(range(count))
                cut_reason = "no_distinguishable_distances_keep_root"
            _cancel(check_cancelled)
        except Exception as exc:
            exc.batch_failure_phase = "clustering"
            raise
    # Label numbers follow the stable-ID identities, not SciPy's arbitrary labels.
    buckets = sorted({int(label): sorted(ids[i] for i in range(count) if labels[i] == label)
                      for label in labels}.values(), key=lambda members: tuple(members))
    by_id = {ident: f"{scope_kind}-candidate-{index:03d}" for index, members in enumerate(buckets, 1) for ident in members}
    metadata = {"policy_version": FAMILY_CLUSTER_POLICY if scope_kind == "family" else POOL_CLUSTER_POLICY, "method": method,
        "member_count": count, "fixed_local_minimum": minimum,
        "representation": "existing_frozen_global_RobustScaler_scaled94", "refit_representation": False,
        "distance": "euclidean", "linkage": "average" if count > 1 else None,
        "optimal_ordering": count > 1, "cut_rule": cut_reason, "cut_height": cut,
        "group_count": len(buckets), "singleton_groups_retained": True,
        "two_or_three_comparison_page": count in (2, 3),
        "is_human_classification": False, "scientific_cluster_validity_established": False}
    out = Path(output_dir)
    _require(not out.exists(), "OUTPUT_EXISTS", "The audition output already exists; use a new review round.")
    out.mkdir(parents=True)
    np.save(out / "distances.npy", distances, allow_pickle=False)
    np.save(out / "ordering_linkage.npy", tree, allow_pickle=False)
    result = {"result_id": f"{scope_kind}-small-" + _hash({"input": dataset.input_fingerprint, "ids": ids,
        "parameters": metadata})[:24], "action": f"{scope_kind}_small_audition", "fit_ids": ids if count > 1 else [],
        "target_ids": ids, "comparison_ids": [], "ordered_ids": [ids[i] for i in order],
        "assignments": [{"item_id": ident, "candidate_cluster_id": by_id[ident],
            "state": "single_member" if count == 1 else "unsupervised_listening_candidate"} for ident in ids],
        f"{scope_kind}_clustering": metadata, "input_fingerprint": dataset.input_fingerprint,
        "input_versions": dataset.input_versions, "preset_id": dataset.preset_id,
        "refitted_steps": ["average_linkage"] if count > 1 else [], "comparison_used_for_fitting": False,
        "provenance": {"representation": dataset._provenance, "source_code_sha256": _sha(__file__)},
        "artifacts": {name: {"path": name, "sha256": _sha(out / name)}
                      for name in ("distances.npy", "ordering_linkage.npy")}}
    _cancel(check_cancelled)
    _write_json(out / "result.json", result)
    return result


def prepare_small_pool(dataset, item_ids, output_dir, *, check_cancelled=None):
    return prepare_small_family(dataset, item_ids, output_dir, check_cancelled=check_cancelled, scope_kind="pool")
