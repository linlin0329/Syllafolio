"""冻结Ward树的保守切分建议；只组织候选，不读取人工/历史类型标签。"""
from __future__ import annotations

import copy
from pathlib import Path

import numpy as np
from scipy.cluster.hierarchy import fcluster, is_valid_linkage

from .annotation_acoustics import _require, _sha

POLICY = {
    "version": "ward-height-gap-v1",
    "minimum_merge_observations": 3,
    "minimum_height_ratio": 1.5,
    "minimum_gap_to_other_median_ratio": 2.0,
    "maximum_new_group_item_fraction": 0.75,
    "automatic_minimum_groups": 2,
    "selection": "largest_qualified_height_gap_then_lowest_cut",
    "gap_background": "other_adjacent_merge_gaps_including_zero_ties",
    "fallback": "preserve_microclusters",
    "evidence_level": "engineering_heuristic_not_scientific_optimum",
}


def recommend_ward(tree, member_counts):
    """高度整体缩放不改建议；同高合并一起处理，0高度不伪造无穷比例。"""
    z = np.asarray(tree, dtype=float)
    counts = list(member_counts)
    n = len(counts)
    _require(all(type(x) is int and x > 0 for x in counts), "WARD_COUNTS", "Microcluster member counts must be positive integers.")
    _require(z.shape == (max(n - 1, 0), 4) and np.isfinite(z).all(), "WARD_TREE", "Recommendation requires a complete, finite Ward tree.")
    if len(z):
        _require(is_valid_linkage(z) and (z[:, 2] >= 0).all() and (np.diff(z[:, 2]) >= -1e-10).all(),
                 "WARD_TREE", "Recommendation requires non-negative, monotonic Ward heights.")
    levels = sorted(set(float(x) for x in z[:, 2]))
    result = {"policy": copy.deepcopy(POLICY), "policy_code_sha256": _sha(Path(__file__)),
              "recommended_mode": "microclusters", "height": None, "microcluster_count": n,
              "recommended_group_count": n, "recommended_group_sizes": list(counts),
              "distinct_heights": levels, "merge_heights": z[:, 2].tolist(), "evaluations": [], "scientific_optimum": False,
              "uses_type_labels": False, "refits_ward_or_global_clustering": False}
    if n < 2:
        result.update(reason_code="no_aggregation_needed", reason="Fewer than two valid microclusters are available, so Ward aggregation is unnecessary. Unclustered items remain available under the run protocol.")
        return result
    if (np.diff(z[:, 2]) < 0).any():
        result.update(reason_code="height_order_uncertain", reason="Merge heights have small ordering discrepancies. The original microclusters are retained conservatively; the frozen tree is not rewritten.")
        return result
    if len(z) < POLICY["minimum_merge_observations"]:
        result.update(reason_code="insufficient_merge_levels", reason="Too few comparable merge-height levels are available; the original microclusters are retained conservatively.")
        return result
    # 先按最大高度归一化，避免数值单位或整体尺度决定阈值，以及比值溢出。
    if len(levels) < 2:
        result.update(reason_code="no_supported_cut", reason="No identifiable jump in merge heights was found; retaining the original microclusters is recommended.")
        return result
    normalized = z[:, 2] / levels[-1]
    gaps = np.diff(normalized)
    eligible = []
    for index, gap in enumerate(gaps):
        if gap <= 0:
            continue
        low, high = map(float, z[index:index + 2, 2])
        low_n, high_n = normalized[index:index + 2]
        others = np.delete(gaps, index)
        background = float(np.median(others))
        ratio_ok = low_n == 0 or high_n >= POLICY["minimum_height_ratio"] * low_n
        prominence_ok = gap >= POLICY["minimum_gap_to_other_median_ratio"] * background
        labels = fcluster(z, t=low, criterion="distance")
        groups = []
        for label in sorted(set(labels.tolist())):
            selected = [i for i, value in enumerate(labels) if value == label]
            groups.append({"prototype_count": len(selected), "item_count": sum(counts[i] for i in selected)})
        merged = [g["item_count"] / sum(counts) for g in groups if g["prototype_count"] > 1]
        largest_new = max(merged, default=0.0)
        reasons = []
        if gap <= 0: reasons.append("no_resolvable_gap")
        if not ratio_ok: reasons.append("height_increase_not_large")
        if not prominence_ok: reasons.append("gap_not_prominent")
        if len(groups) < POLICY["automatic_minimum_groups"] or len(groups) == n: reasons.append("no_usable_partial_merge")
        if largest_new > POLICY["maximum_new_group_item_fraction"]: reasons.append("new_group_too_dominant")
        row = {"cut_height": low, "next_height": high, "normalized_gap": float(gap),
               "other_gap_median": background, "height_ratio_test_passed": bool(ratio_ok),
               "gap_prominence_test_passed": bool(prominence_ok), "group_count": len(groups),
               "group_sizes": [g["item_count"] for g in groups],
               "maximum_new_group_item_fraction": largest_new, "qualified": not reasons, "reasons": reasons}
        result["evaluations"].append(row)
        if not reasons: eligible.append(row)
    if not eligible:
        result.update(reason_code="no_supported_cut", reason="No cut satisfies both the height-jump and group-size rules. Retaining the original microclusters is recommended; the height can be adjusted manually in advanced settings.")
        return result
    best = min(eligible, key=lambda r: (-r["normalized_gap"], r["cut_height"]))
    result.update(recommended_mode="ward", height=best["cut_height"],
                  recommended_group_count=best["group_count"], recommended_group_sizes=best["group_sizes"],
                  selected_evidence=copy.deepcopy(best), reason_code="supported_height_gap",
                  reason="This Ward tree has a height jump that meets the fixed rules. The cut precedes that jump and limits the share of newly merged large groups. This recommendation organizes audition; it does not establish true type boundaries.")
    return result
