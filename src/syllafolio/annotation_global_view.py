"""流程a候选的人工视图；与人工目录分开，不开放新的助手工具。"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

from .annotation_assistant_contracts import require


def load_global_candidates(dataset_dir):
    directory = Path(dataset_dir).resolve()
    manifest = json.loads((directory / "manifest.json").read_text())
    link = manifest.get("global_candidates")
    if link is None:
        return {"available": False}
    require(isinstance(link, dict), "GLOBAL_CANDIDATES_INVALID", "The global-candidate link is invalid.")
    for label, filename in (("suggestions", "suggestions.json"), ("provenance", "provenance.json")):
        path = (directory / link.get(label + "_path", "")).resolve()
        require(path == (directory.parent / "global" / filename).resolve() and path.is_file(),
                "GLOBAL_CANDIDATES_PATH", "The global-candidate file is outside the registered result directory.")
        require(hashlib.sha256(path.read_bytes()).hexdigest() == link.get(label + "_sha256"),
                "GLOBAL_CANDIDATES_CHANGED", "The global-candidate file has changed; check the input version.")
    from .annotation_stage_a import validate_stage_a
    validate_stage_a(directory.parent)
    payload = json.loads((directory.parent / "global/suggestions.json").read_text())
    provenance = json.loads((directory.parent / "global/provenance.json").read_text())
    allowed = {row["item_id"] for row in manifest["items"]}
    require({row["item_id"] for row in payload["items"]} == allowed,
            "GLOBAL_CANDIDATES_SCOPE", "Global candidates do not cover the complete current run.")
    # 仅公开匿名候选和数值说明；原文件路径及协调者来源不会经该接口外发。
    safe = {key: copy.deepcopy(payload[key]) for key in (
        "schema_version", "items", "clusters", "unclustered_ids", "fit_item_ids",
        "input_versions", "fit_status", "semantics", "unavailable_ward_levels") if key in payload}
    safe["available"] = True
    safe["provenance"] = {key: copy.deepcopy(provenance[key]) for key in (
        "schema_version", "parameters", "fit_item_count", "fit_item_ids", "fit_status",
        "candidate_clusters", "unclustered_items", "input_versions", "created_at",
        "source_code_sha256", "dependency_versions", "not_measured", "noise_policy") if key in provenance}
    safe["provenance"].update(suggestions_sha256=link["suggestions_sha256"],
                              provenance_sha256=link["provenance_sha256"],
                              is_human_classification=False)
    return safe
