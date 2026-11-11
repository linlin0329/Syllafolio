"""原始录音来源声明：与可恢复的标签状态分离，不推断录音或个体独立性。"""
from __future__ import annotations

import copy
import hashlib
import re

import numpy as np
import soundfile as sf

from .annotation_assistant_contracts import canonical_hash, require

SCHEMA = "annotation-source-registry-v1"
ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$")
LIMITATION = "Original-recording groups are declared by the operator. The application checks scope and known shared-source or duplicate consistency; it does not verify original-recording identity or individual independence."


def pcm_hash(path, start=0, stop=None):
    """以解码PCM识别相同文件内容；不因WAV头不同误认独立来源。"""
    digest = hashlib.sha256()
    with sf.SoundFile(path) as stream:
        rate = stream.samplerate
        stream.seek(start)
        remaining = (len(stream) if stop is None else stop) - start
        frames = remaining
        while remaining:
            block = stream.read(min(remaining, 262144), dtype="float32", always_2d=False)
            require(len(block) > 0 and np.isfinite(block).all(), "SOURCE_PCM", "The source PCM read is incomplete.")
            digest.update(np.asarray(block, dtype="<f4").tobytes())
            remaining -= len(block)
    return canonical_hash({"sample_rate_hz": rate, "frames": frames, "pcm_sha256": digest.hexdigest()})


def components(rows):
    """只构建禁止拆分的同源约束；绝不据此自动填写raw_recording_id。"""
    parent = {row["item_id"]: row["item_id"] for row in rows}
    def find(key):
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key
    seen = {}
    for row in rows:
        for field in ("source_clip_id", "parent_pcm_identity", "segment_pcm_identity"):
            key = (field, row[field])
            if key in seen:
                parent[find(row["item_id"])] = find(seen[key])
            else:
                seen[key] = row["item_id"]
    groups = {}
    for item in parent:
        groups.setdefault(find(item), []).append(item)
    return [sorted(ids) for ids in groups.values()]


def declaration_conflicts(groups, mappings):
    result = []
    for ids in groups:
        raw = sorted({mappings.get(i, {}).get("raw_recording_id") for i in ids
                      if mappings.get(i, {}).get("raw_recording_id")})
        if len(raw) > 1:
            result.append({"code": "SOURCE_CONFLICT", "message": "Items with the same parent source, identical parent PCM or exactly duplicate syllable PCM were declared as different original-recording groups.",
                           "item_ids": ids, "raw_recording_ids": raw})
    return result


def build_index(dataset):
    parents = {key: pcm_hash(value["path"]) for key, value in dataset._parents.items()}
    rows = []
    for ident in dataset.item_ids:
        item = dataset._items[ident]
        rows.append({"item_id": ident, "source_clip_id": item["source_clip_id"],
                     "parent_pcm_identity": parents[item["source_clip_id"]],
                     "segment_pcm_identity": pcm_hash(item["_path"], item["source_frame_start"], item["source_frame_stop"])})
    return {"items": rows, "components": components(rows), "fingerprint": canonical_hash(rows)}


def initial_registry(dataset, index):
    mapping = {}
    for ident in dataset.item_ids:
        item = dataset._items[ident]
        raw, evidence = item.get("raw_recording_id"), item.get("raw_recording_evidence", "")
        require(raw is None or isinstance(raw, str) and ID_PATTERN.fullmatch(raw), "SOURCE_ID", "An original-recording ID must be a path-free identifier.")
        require(isinstance(evidence, str) and len(evidence) <= 4000 and (raw is None or evidence.strip()),
                "SOURCE_EVIDENCE", "Imported source declarations must be valid text; known sources require evidence.")
        mapping[ident] = {"item_id": ident, "source_clip_id": item["source_clip_id"],
                          "raw_recording_id": raw, "status": "declared" if raw else "unknown",
                          "evidence": evidence, "origin": "input_declaration" if raw or evidence else "unknown"}
    conflicts = declaration_conflicts(index["components"], mapping)
    require(not conflicts, "SOURCE_CONFLICT", "The input source declarations conflict with shared-source or duplicate-PCM constraints.")
    return {"schema_version": SCHEMA, "version": 0, "mappings": mapping, "history": [],
            "identity_fingerprint": index["fingerprint"], "input_fingerprint": dataset.input_fingerprint}


def effective_registry(state, initial):
    return state.get("source_registry", initial)


def source_counts(mapping):
    known = [row for row in mapping.values() if row["raw_recording_id"]]
    return {"total": len(mapping), "declared": len(known), "unknown": len(mapping) - len(known),
            "raw_recording_groups": len({row["raw_recording_id"] for row in known})}


def projection(registry):
    return {"schema_version": SCHEMA, "version": registry["version"],
            "mappings": copy.deepcopy(list(registry["mappings"].values())),
            "counts": source_counts(registry["mappings"]), "history": copy.deepcopy(registry["history"]),
            "limitation": LIMITATION, "restored_with_labels": False}


def prepare_registration(registry, index, payload, *, record_id, meta):
    require(set(payload) == {"expected_source_version", "mappings", "reason", "evidence"},
            "SOURCE_FIELDS", "Source registration accepts only mappings, the current version, a reason and source evidence; labels and external paths are not accepted.")
    require(type(payload["expected_source_version"]) is int and payload["expected_source_version"] == registry["version"],
            "SOURCE_VERSION_CONFLICT", "The source version has changed; refresh and review it.")
    for field in ("reason", "evidence"):
        require(isinstance(payload[field], str) and bool(payload[field].strip()) and len(payload[field]) <= 4000,
                "SOURCE_EVIDENCE", "Enter the actual source evidence and the reason for this registration, each no longer than 4000 characters.")
    entries = payload["mappings"]
    require(isinstance(entries, list) and 0 < len(entries) <= len(registry["mappings"]), "SOURCE_MAPPING", "The source mapping must be a non-empty list within this run.")
    mapped = copy.deepcopy(registry["mappings"])
    targets = {}
    for entry in entries:
        require(isinstance(entry, dict) and set(entry) in ({"item_id", "raw_recording_id"}, {"source_clip_id", "raw_recording_id"}),
                "SOURCE_FIELDS", "Each row must contain only item_id or source_clip_id, together with raw_recording_id.")
        raw = entry["raw_recording_id"]
        require(raw is None or isinstance(raw, str) and ID_PATTERN.fullmatch(raw), "SOURCE_ID", "Original-recording IDs must be path-free identifiers; use null for unknown sources.")
        if "item_id" in entry:
            require(isinstance(entry["item_id"], str) and entry["item_id"] in mapped, "SOURCE_SCOPE", "The source mapping contains a syllable outside this run.")
            ids = [entry["item_id"]]
        else:
            require(isinstance(entry["source_clip_id"], str), "SOURCE_SCOPE", "The parent-source ID is invalid.")
            ids = [i for i, row in mapped.items() if row["source_clip_id"] == entry["source_clip_id"]]
            require(bool(ids), "SOURCE_SCOPE", "The source mapping contains a parent source outside this run.")
        for ident in ids:
            require(ident not in targets or targets[ident] == raw, "SOURCE_MAPPING", "The request gives different sources for the same syllable.")
            targets[ident] = raw
            mapped[ident].update(raw_recording_id=raw, status="declared" if raw else "unknown",
                                 evidence=payload["evidence"], origin="operator_declaration", record_id=record_id)
    conflicts = declaration_conflicts(index["components"], mapped)
    changed = sorted(i for i in targets if mapped[i] != registry["mappings"][i])
    record = dict(meta, record_id=record_id, version=registry["version"] + 1,
                  previous_version=registry["version"], reason=payload["reason"], evidence=payload["evidence"],
                  mappings=mapped, changed_item_ids=changed, conflicts=conflicts,
                  identity_fingerprint=index["fingerprint"], input_fingerprint=registry["input_fingerprint"])
    preview = {"valid": not conflicts, "conflicts": conflicts, "expected_source_version": registry["version"],
               "proposed_version": record["version"], "changed_item_ids": changed, "counts": source_counts(mapped)}
    return record, preview


def apply_registration(state, record):
    current = state.get("source_registry")
    require(current is None and record["previous_version"] == 0 or current is not None and current["version"] == record["previous_version"],
            "SOURCE_VERSION_CONFLICT", "The source version has changed.")
    require(not record["conflicts"], "SOURCE_CONFLICT", "Source declarations conflict for related or duplicate syllables; check the source mapping first.")
    require(set(record["mappings"]) == set(state["contract"]["allowed_item_ids"]), "SOURCE_SCOPE", "The source mapping scope does not match the run.")
    history = copy.deepcopy(current["history"] if current else [])
    history.append({key: copy.deepcopy(value) for key, value in record.items() if key not in {"mappings", "conflicts"}})
    state["source_registry"] = {"schema_version": SCHEMA, "version": record["version"],
                                "mappings": copy.deepcopy(record["mappings"]), "history": history,
                                "identity_fingerprint": record["identity_fingerprint"], "input_fingerprint": record["input_fingerprint"]}


def retain_assessment_on_label_restore(state, payload):
    """标签检查点不含来源登记/评估；仅记录恢复来源，不回滚最新明确声明。"""
    if "source_registry" in state or "label_assessment" in state:
        payload["assessment_restore_policy"] = {"source_version_retained": state.get("source_registry", {}).get("version", 0),
            "assessment_history_retained": True, "checkpoint_id": payload["checkpoint_id"],
            "report_applicability": "recomputed_from_current_label_scope_completion_and_source"}
