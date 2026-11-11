"""Fixed anonymous acoustic candidate kernels; no corpus, paper or study IO."""
from __future__ import annotations

import numpy as np


CONDITION_ID = "B_ASSIST_105D_94D_WARD_ENG_V1"

TASK_MODE = "open_type_discovery_and_curation"

EXCLUDED_INDICES = (0, 1, 2, 3, 4, 5, 25, 45, 65, 103, 104)

FEATURE_NAMES = (
    ["envelope_max", "envelope_mean", "envelope_std", "envelope_ptp", "envelope_diff_rate"]
    + [f"{prefix}{i}_{stat}" for prefix in ("mfcc_", "mfcc_delta_")
       for stat in ("mean", "std") for i in range(20)]
    + [f"spectral_{name}_{stat}" for name in ("centroid", "bandwidth", "rolloff", "flatness")
       for stat in ("mean", "std")]
    + [f"spectral_contrast_{i}" for i in range(7)]
    + ["duration", "zcr_mean", "zcr_std", "rms_mean", "rms_std"]
)

PARAMETERS = {
    "scaler": {"method": "RobustScaler", "quantile_range": [25.0, 75.0],
               "with_centering": True, "with_scaling": True, "unit_variance": False},
    "umap": {"n_components": 10, "n_neighbors": 10, "min_dist": 0.0,
             "metric": "euclidean", "random_state": 52, "transform_seed": 52,
             "init": "spectral", "n_jobs": 1},
    "hdbscan": {"min_cluster_size": 10, "min_samples": 5,
                "cluster_selection_method": "leaf", "metric": "euclidean",
                "gen_min_span_tree": True, "prediction_data": True,
                "core_dist_n_jobs": 1},
    "ward": {"candidate_levels": [12, 20, 30], "linkage": "ward", "metric": "euclidean",
             "optimal_ordering": True, "prototype": "coordinate_median_scaled94",
             "prototype_weight": "equal", "excluded_105D_indices": list(EXCLUDED_INDICES)},
    "neighbors": {"count": 5, "metric": "euclidean_scaled94", "tie_break": "opaque_id"},
    "small_batch": {"minimum_umap_rows": 12, "rule": "skip_umap_hdbscan_retain_all_as_unclustered"},
    "insufficient_prototypes": "omit_unavailable_ward_levels_preserve_all_items",
    "parameter_selection": "fixed_engineering_configuration_no_search_or_final_labels",
}

def _cluster(scaled):
    """重计算部分；单一固定配置，不读取终审类别或搜索参数。"""
    if len(scaled) < PARAMETERS["small_batch"]["minimum_umap_rows"]:
        return np.empty((len(scaled), 0)), np.full(len(scaled), -1, dtype=int), np.zeros(len(scaled)), "small_batch_no_embedding"
    import umap
    import hdbscan
    embedding = umap.UMAP(**PARAMETERS["umap"]).fit_transform(scaled)
    model = hdbscan.HDBSCAN(**PARAMETERS["hdbscan"])
    labels = model.fit_predict(embedding)
    return np.asarray(embedding), np.asarray(labels), np.asarray(model.probabilities_), "fixed_umap_hdbscan"

def _neighbors(distances, ids):
    count = PARAMETERS["neighbors"]["count"]
    indices = np.array([sorted((j for j in range(len(ids)) if j != i),
                               key=lambda j: (float(distances[i, j]), ids[j]))[:count]
                        for i in range(len(ids))], dtype=int)
    if not ids:
        return np.empty((0, 0), dtype=int), np.empty((0, 0))
    return indices, np.take_along_axis(distances, indices, axis=1)

def _organize(scaled, labels, probabilities, item_ids):
    from scipy.cluster.hierarchy import cut_tree, leaves_list, linkage
    from scipy.spatial.distance import cdist

    retained = [i for i in range(105) if i not in EXCLUDED_INDICES]
    perceptual = scaled[:, retained]
    indices, neighbor_distances = _neighbors(cdist(perceptual, perceptual), item_ids)
    cluster_values = sorted(set(int(x) for x in labels if x >= 0),
                            key=lambda x: min(item_ids[i] for i in np.flatnonzero(labels == x)))
    cluster_ids = [f"c_{i + 1:04d}" for i in range(len(cluster_values))]
    id_by_label = dict(zip(cluster_values, cluster_ids))
    prototypes = np.array([np.median(perceptual[labels == value], axis=0) for value in cluster_values]).reshape(-1, 94)
    distances = cdist(prototypes, prototypes)
    proto_neighbors, proto_neighbor_distances = _neighbors(distances, cluster_ids)
    tree = linkage(prototypes, method="ward", metric="euclidean", optimal_ordering=True) if len(prototypes) >= 2 else np.empty((0, 4))
    cuts, unavailable = {}, []
    for k in PARAMETERS["ward"]["candidate_levels"]:
        if len(prototypes) < k:
            unavailable.append({"level": k, "reason": "fewer_prototypes_than_requested_groups"})
            continue
        labels_k = cut_tree(tree, n_clusters=k).ravel()
        ordered = list(dict.fromkeys(int(labels_k[i]) for i in leaves_list(tree)))
        cuts[f"level_{k}"] = [f"w{k}_{ordered.index(int(v)) + 1:03d}" for v in labels_k]
    items = []
    for i, item_id in enumerate(item_ids):
        cluster_id = id_by_label.get(int(labels[i]))
        position = cluster_ids.index(cluster_id) if cluster_id else None
        items.append({"item_id": item_id, "candidate_cluster_id": cluster_id,
                      "cluster_membership_strength": float(probabilities[i]),
                      "uncertainty": "unclustered" if cluster_id is None else "candidate_requires_review",
                      "neighbors": [{"item_id": item_ids[j], "distance_94d": float(d)}
                                    for j, d in zip(indices[i], neighbor_distances[i])],
                      "ward_candidates": {k: v[position] for k, v in cuts.items()} if position is not None else {}})
    clusters = [{"candidate_cluster_id": cid, "item_ids": [item_ids[j] for j in np.flatnonzero(labels == value)],
                 "prototype_neighbors": [{"candidate_cluster_id": cluster_ids[j], "distance_94d": float(d)}
                                         for j, d in zip(proto_neighbors[i], proto_neighbor_distances[i])]}
                for i, (cid, value) in enumerate(zip(cluster_ids, cluster_values))]
    payload = {"schema_version": "ei-b-suggestions-v1", "condition_id": CONDITION_ID, "task_mode": TASK_MODE,
               "score_semantics": {"distance_94d": "Euclidean distance in declared scaled representation",
                                   "cluster_membership_strength": "HDBSCAN strength; not a calibrated type probability"},
               "items": items, "clusters": clusters, "unavailable_ward_levels": unavailable}
    matrices = {"prototypes_94d": prototypes, "prototype_distances_94d": distances, "ward_linkage_94d": tree,
                "item_neighbor_indices": indices, "item_neighbor_distances_94d": neighbor_distances}
    mapping = [{"prototype_row_index": i, "candidate_cluster_id": cid, "fitted_cluster_label": value,
                "member_count": int((labels == value).sum()), "ward_candidates": {k: v[i] for k, v in cuts.items()}}
               for i, (cid, value) in enumerate(zip(cluster_ids, cluster_values))]
    return payload, matrices, mapping
