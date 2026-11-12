"""原生A工作区只读摘要；复用输入检查，不提取/拟合/发布或补写来源。"""
from __future__ import annotations

import copy
import hashlib
from pathlib import Path

from . import annotation_stage_a as stage_a
from .annotation_run_info import feature_scheme
from .annotation_source_registry import LIMITATION
from .annotation_assistant_contracts import canonical_hash


SCHEMA = "syllafolio-desktop-stage-a-view-v1"
INSPECTION_SCOPE = "source_fingerprint, original has_sidecar, segments.json byte SHA256, validated plan items; no numeric fitting"


def input_inspection_identity(input_dir, plan):
    """Bind review-only original names/descriptor bytes without changing PCM identity.

    Call with the existing validated plan to avoid another audio inspection.
    Frozen segments.json is generated anew, so this identity is retained as an
    original-input review receipt and must not be reconstructed from that file.
    """
    descriptor = Path(input_dir).expanduser() / "segments.json"
    has_sidecar = descriptor.is_file()
    try:
        descriptor_sha = hashlib.sha256(descriptor.read_bytes()).hexdigest() if has_sidecar else None
    except OSError as exc:
        raise stage_a.StageAError("INPUT_CHANGED", 'segments.json changed during inspection. Check the input again.') from exc
    return {"source_fingerprint": plan["source_fingerprint"], "has_sidecar": has_sidecar,
            "segments_json_sha256": descriptor_sha, "items": copy.deepcopy(plan["items"])}


def inspect_input(input_dir):
    """Return the existing first-error input check as a reviewable preflight.

    The caller keeps StageAError's original code/message. This function does not
    gather a second error list or reinterpret unsupported audio/label fields.
    """
    plan = stage_a.inspect_source(input_dir)
    inspection = input_inspection_identity(input_dir, plan)
    declared = plan.get("raw_recording_declarations", {})
    summary = {key: copy.deepcopy(plan.get(key)) for key in (
        "item_count", "total_duration_s", "sample_rate_hz", "context_available_count",
        "audio_bytes", "source_fingerprint")}
    summary.update(has_sidecar=inspection["has_sidecar"],
                   segment_only_count=sum(row.get("context_kind") == "segment_only" for row in plan["items"]))
    if plan.get("skipped_audio_files"):
        summary["skipped_audio_files"] = plan["skipped_audio_files"]
    return {"schema_version": SCHEMA, "status": "validated_input", "summary": summary,
            "items": copy.deepcopy(plan["items"]),
            "sources": {"counts": {"declared": declared.get("declared_items"),
                                     "unknown": declared.get("unknown_items")}, "limitation": LIMITATION},
            "recipe": {"feature_scheme": feature_scheme(), "parameters": copy.deepcopy(plan.get("parameters")),
                       "preset_id": plan.get("preset_id"), "status": "planned_fixed_recipe"},
            "duplicate_pcm_groups": copy.deepcopy(plan.get("duplicate_pcm_groups", [])),
            "input_requirements": copy.deepcopy(plan.get("input_requirements", {})),
            "limits": copy.deepcopy(plan.get("limits", {})), "first_error_policy": True,
            "inspection_identity": inspection, "inspection_fingerprint": canonical_hash(inspection),
            "inspection_scope": INSPECTION_SCOPE}


def _optional_json(path, key, missing):
    if not path.is_file():
        missing.append(key)
        return {}
    value = stage_a._read(path)
    if not isinstance(value, dict):
        raise stage_a.StageAError("STAGE_A_VIEW_JSON", 'The frozen summary file must contain a JSON object.')
    return value


def _frozen_view(project):
    """The service has verified these immutable inputs when opening its owner."""
    dataset_dir = Path(project.service.dataset_dir)
    identity = (id(project.service), project.service.run_id, str(dataset_dir))
    cached = getattr(project, "_desktop_stage_a_frozen_view", None)
    if cached is not None and cached["identity"] == identity:
        return cached
    missing = []
    run_root = dataset_dir.parent
    cached = {"identity": identity, "manifest": stage_a._read(dataset_dir / "manifest.json"),
              "provenance": _optional_json(run_root / "global/provenance.json", "global_provenance", missing),
              "receipt": _optional_json(run_root / "coordinator/source/import_receipt.json", "import_receipt", missing),
              "missing": missing}
    project._desktop_stage_a_frozen_view = cached
    return cached


def _current_sources(manifest_items, snapshot):
    registry = snapshot.get("source_registry", {})
    mappings = registry.get("mappings", [])
    if isinstance(mappings, dict):
        mappings = mappings.values()
    current = {row["item_id"]: row for row in mappings}
    rows = []
    for item in manifest_items:
        ident = item["item_id"]
        if ident in current:
            source = copy.deepcopy(current[ident])
        else:
            raw = item.get("raw_recording_id")
            evidence = item.get("raw_recording_evidence", "")
            source = {"item_id": ident, "source_clip_id": item.get("source_clip_id"),
                      "raw_recording_id": raw, "evidence": evidence,
                      "origin": "input_declaration" if raw or evidence else "unknown"}
        source["status"] = "declared" if source.get("raw_recording_id") else "unknown"
        rows.append(source)
    known = [row for row in rows if row.get("raw_recording_id")]
    return {"version": registry.get("version"), "mappings": rows,
            "projection_source": "current_registry" if "source_registry" in snapshot else "frozen_input_declaration",
            "counts": {"total": len(rows), "declared": len(known), "unknown": len(rows) - len(known),
                       "raw_recording_groups": len({row["raw_recording_id"] for row in known})},
            "limitation": registry.get("limitation", LIMITATION), "declaration_only": True}


def _candidate_view(global_candidates, snapshot):
    batch = snapshot.get("batch_review", {})
    pool = batch.get("pool", {})
    organization = batch.get("organization")
    groups = (organization or {}).get("groups", [])
    candidate_groups = [row for row in groups if row.get("kind") != "unclustered"
                        and row.get("group_id") != "machine-unclustered"]
    microclusters = [{"candidate_cluster_id": row["candidate_cluster_id"],
                      "member_count": len(set(row.get("item_ids", []))),
                      "item_ids": sorted(set(row.get("item_ids", [])))}
                     for row in global_candidates.get("clusters", [])]
    initial_noise = sorted(set(global_candidates.get("unclustered_ids", [])))
    current_pool = sorted(set(pool.get("active_ids", [])))
    return {"available": bool(global_candidates.get("available")),
            "fit_status": global_candidates.get("fit_status"),
            "microcluster_count": len(microclusters) if global_candidates.get("available") else None,
            "microclusters": microclusters,
            "organization_id": (organization or {}).get("organization_id"),
            "organization_group_count": len(candidate_groups) if organization else None,
            "organization_groups": [{key: copy.deepcopy(row[key]) for key in
                ("group_id", "label", "kind", "item_ids", "source_cluster_ids", "candidate_cluster_ids") if key in row} for row in candidate_groups],
            "initial_unclustered_ids": initial_noise,
            "initial_unclustered_count": len(initial_noise) if global_candidates.get("available") else None,
            "current_pool_ids": current_pool, "current_pool_count": len(current_pool),
            "pool_entries": copy.deepcopy(pool.get("entries", {})),
            "pool_sources": copy.deepcopy(pool.get("source_history", {})),
            "pool_first_pass_complete": pool.get("first_pass_complete"),
            "pool_can_prepare": pool.get("can_prepare"), "pool_stage_complete": pool.get("stage_complete"),
            "initial_unclustered_intake_pending": organization is None and bool(initial_noise),
            "semantics": "machine_candidates_not_human_types",
            "noise_is_not_exclusion": True, "human_type_assignment_created": False}


def project_stage_a_view(project, snapshot, timing=None):
    """Project already owns validated frozen inputs; this function only reads.

    Current raw-recording declarations come from the supplied owner snapshot.
    No snapshot/command, source inspection, media decoding or numeric fitting is
    performed here. Missing historical facts remain explicitly unrecorded.
    """
    cached = _frozen_view(project)
    manifest, provenance, receipt = cached["manifest"], cached["provenance"], cached["receipt"]
    missing = list(cached["missing"])
    frozen_items = manifest["items"]
    original = {row["item_id"]: row for row in receipt.get("original_mapping", [])}
    sources = _current_sources(frozen_items, snapshot)
    sources_by_id = {row["item_id"]: row for row in sources["mappings"]}
    global_candidates = getattr(project.service, "global_candidates_data", {})
    candidates = _candidate_view(global_candidates, snapshot)
    if not global_candidates.get("available"):
        missing.append("global_candidates")
    pool_ids = set(candidates["current_pool_ids"])
    initial_noise = set(candidates["initial_unclustered_ids"])
    snapshot_items = {row["item_id"]: row for row in snapshot.get("items", [])}
    rows = []
    for item in frozen_items:
        ident = item["item_id"]
        mapping, source = original.get(ident, {}), sources_by_id[ident]
        context = item.get("context_kind")
        row = {key: copy.deepcopy(item.get(key)) for key in (
            "item_id", "source_clip_id", "source_frame_start", "source_frame_stop",
            "sample_rate_hz", "parent_audio_sha256", "decoded_pcm_sha256")}
        row.update(original_item_id=mapping.get("original_item_id"), original_file=mapping.get("original_file"),
                   original_segment_sha256=mapping.get("segment_sha256"),
                   storage_audio_relative=item.get("wav_path"), context_kind=context,
                   has_parent_context=(context == "source_parent") if context is not None else None,
                   raw_recording_id=source.get("raw_recording_id"), raw_recording_status=source["status"],
                   raw_recording_evidence=source.get("evidence", ""), raw_recording_origin=source.get("origin"),
                   initially_unclustered=ident in initial_noise, in_current_pool=ident in pool_ids,
                   current_pool_entry=copy.deepcopy(candidates["pool_entries"].get(ident)),
                   processing_state=snapshot_items.get(ident, {}).get("processing_state"))
        row["not_recorded"] = [key for key in ("original_item_id", "original_file", "original_segment_sha256",
            "context_kind", "decoded_pcm_sha256") if row.get(key) is None]
        rows.append(row)
    origin = project.metadata.get("origin")
    timing_view = copy.deepcopy(timing) if timing is not None else {
        "status": "not_recorded", "origin": origin, "total_seconds": None, "phases": [],
        "observed_lower_bound": False, "not_recorded": True, "diagnostics": []}
    input_summary = timing_view.get("input_summary", {})
    recorded_sidecar = input_summary.get("has_sidecar")
    bound_input = timing_view.get("input_identity", {}).get("source_fingerprint")
    frozen_input = provenance.get("source_fingerprint", receipt.get("source_fingerprint"))
    timing_matches = (origin == "imported_audio" and timing_view.get("origin") == origin
                      and timing_view.get("status") != "invalid" and bound_input is not None
                      and bound_input == frozen_input)
    has_sidecar = recorded_sidecar if type(recorded_sidecar) is bool and timing_matches else None
    if type(recorded_sidecar) is bool and not timing_matches:
        missing.append("sidecar_fact_not_bound_to_current_import")
    if has_sidecar is None:
        missing.append("original_has_sidecar")
    if timing_view.get("not_recorded"):
        missing.append("import_timing")
    if not provenance.get("parameters"):
        missing.append("frozen_parameters")
    if any(row["not_recorded"] for row in rows):
        missing.append("item_fields")
    known_context = [row for row in rows if row["context_kind"] is not None]
    rates = {row.get("sample_rate_hz") for row in rows}
    all_timed = all(type(row.get("sample_rate_hz")) is int and row["sample_rate_hz"] > 0
                    and type(row.get("source_frame_start")) is int and type(row.get("source_frame_stop")) is int for row in rows)
    summary = {"item_count": len(rows), "sample_rate_hz": next(iter(rates)) if len(rates) == 1 else None,
               "total_duration_s": sum((row["source_frame_stop"] - row["source_frame_start"]) / row["sample_rate_hz"] for row in rows) if all_timed else None,
               "context_available_count": sum(row["context_kind"] == "source_parent" for row in known_context),
               "segment_only_count": sum(row["context_kind"] == "segment_only" for row in known_context),
               "context_not_recorded_count": len(rows) - len(known_context), "has_sidecar": has_sidecar,
               "source_fingerprint": provenance.get("source_fingerprint", receipt.get("source_fingerprint")),
               "original_mapping_count": len(original), "prepared_replay": origin == "prepared_engineering_replay",
               "preparation_used_feature_extraction": origin == "imported_audio",
               "frozen_identity_validation": "verified_when_project_owner_opened"}
    return {"schema_version": SCHEMA, "status": "ready", "origin": origin, "summary": summary,
            "items": rows, "sources": sources,
            "recipe": {"feature_scheme": feature_scheme(), "parameters": copy.deepcopy(provenance.get("parameters")),
                       **({"audio_preprocessing": copy.deepcopy(manifest["audio_preprocessing"])}
                          if manifest.get("audio_preprocessing") else {}),
                       "feature_parameters": copy.deepcopy(provenance.get("feature_parameters")),
                       "preset_id": provenance.get("preset_id"), "fit_status": provenance.get("fit_status"),
                       "source_code_sha256": copy.deepcopy(provenance.get("source_code_sha256")),
                       "dependency_versions": copy.deepcopy(provenance.get("dependency_versions")),
                       "configuration_evidence": "frozen" if provenance.get("parameters") else "not_recorded",
                       "feature_description_is_current": True,
                       "ward_note": "94D coordinate-median prototypes, one equal-weight point per microcluster; Ward merge height is not a similarity percentage or a maximum pairwise distance.",
                       "parameters_editable": False},
            "candidates": candidates, "timing": timing_view, "not_recorded": sorted(set(missing))}
