"""协议0.5的逐组审听和双来源候选池；保留0.4任务兼容；共用现有 SQLite、目录与审计。

这里的分组和计算仅组织审听。人工处置是明确的逐音节记录，机器不能创建
family、补造观察、删除原音频或以清空待定池作为结束条件。
"""
from __future__ import annotations

import copy
import hashlib
import math
from pathlib import Path
import re
import threading
import time
import uuid

from .annotation_assistant_contracts import canonical_hash, require
from .annotation_state import _require
from .annotation_service import utc

BATCH_ACTIONS = {
    "batch_preview", "batch_apply", "batch_prepare", "batch_prepare_pool", "batch_submit", "batch_submit_v2",
    "batch_close", "batch_reopen", "batch_family", "batch_cancel", "batch_prepare_family", "batch_archive_stale", "batch_upgrade",
    "family_rename",
}
ACTIVE = {"queued", "running", "cancel_requested"}
CLOSE_CHECKS = {"coverage_confirmed", "comparisons_completed", "affected_relations_checked",
                "no_required_steps_pending"}
LEGACY_DISPOSITIONS = {"current", "other", "pool", "discard"}
DISPOSITIONS = LEGACY_DISPOSITIONS | {"new_family"}
ASSIGNED_DISPOSITIONS = {"current", "other", "new_family"}
DEFAULT_PROTOCOL_VERSION = "0.5"
VERSION = "annotation-batch-review-v2"
EXCEPTION_POLICY = "direct-exception-binding-v1"
ORGANIZATION_INTERFACE = "organization-recommendation-v1"
COMPACT_SUBMISSION_VERSION = "compact_v1"
COMPACT_REASON_CODES = {
    "pool": {
        "insufficient_evidence": "Insufficient evidence",
        "conflicting_evidence": "Conflicting evidence",
        "poor_audio": "Poor audio quality",
        "missing_context": "Missing context",
        "needs_boundary_check": "Boundary needs checking",
        "other": "Other unresolved reason",
    },
    "discard": {
        "not_target": "Not a target syllable",
        "poor_audio": "Poor audio quality",
        "duplicate": "Duplicate",
        "boundary_error": "Segmentation boundary error",
        "other": "Other exclusion reason",
    },
}


def _compact_review_scope(review):
    return (review.get("stage") in {"first", "pool", "family"}
            and not review.get("directed_issue_id") and not review.get("directed_context"))


def _compact_submission_row(row, previous):
    """保存操作者的代码/原文；选择保存不等于声称实际听音。"""
    reason = row.get("reason", "")
    _require(isinstance(reason, str) and len(reason) <= 20000, "BATCH_REASON",
             'The note may be empty and must be no longer than 20,000 characters.')
    disposition = row.get("disposition")
    code = row.get("reason_code")
    if disposition in COMPACT_REASON_CODES:
        _require(isinstance(code, str) and code in COMPACT_REASON_CODES[disposition],
                 "BATCH_REASON_CODE", 'Choose a reason for leaving this item unresolved or excluding it.')
    else:
        _require(code is None or code == "", "BATCH_REASON_CODE", 'A family assignment does not take an unresolved or exclusion reason.')
        code = None
    # 省略Details不能清除已有必要回查；显式字段仍走原布尔/文本校验。
    _require(type(row.get("boundary_flag", False)) is bool, "BATCH_BOUNDARY", 'The boundary flag must be true or false.')
    row.setdefault("boundary_flag", previous.get("boundary_flag", False))
    row.setdefault("pending_steps", previous.get("pending_steps", ""))
    if disposition == "pool" and code == "needs_boundary_check":
        row["boundary_flag"] = True
    row.update(reason=reason, reason_code=code,
               reason_source="operator_note" if reason.strip() else "operator_code" if code else "not_provided",
               submission_contract_version=COMPACT_SUBMISSION_VERSION)


def organization_change_impact(state, preview):
    """重建队列的影响只读呈现；确认不能用一段文字或自动备注替代。"""
    batch = state.get("batch_review", blank())
    previous = current_organization(batch)
    def membership_map(groups):
        return {item: row["group_id"] for row in groups for item in row["item_ids"]}
    before = membership_map((previous or {}).get("groups", []))
    after = membership_map(preview["groups"])
    changed = sorted(i for i in set(before) | set(after) if before.get(i) != after.get(i))
    reviews = batch.get("reviews", {})
    active_ids = set(batch.get("active_reviews", {}).values()) | set(batch.get("active_family_reviews", {}).values())
    if batch.get("latest_pool_review_id"): active_ids.add(batch["latest_pool_review_id"])
    current = [r for key, r in reviews.items() if key in active_ids and not r.get("archived")]
    human = {key: state.get(key) for key in ("memberships", "observations", "decisions", "type_cards", "catalogue_scope", "current_completion_id")}
    required = bool(reviews or any(human.values()) or batch.get("assignments"))
    basis = {"previous_organization_id": (previous or {}).get("organization_id"),
             "catalogue_revision": state["catalogue_revision"], "human": human,
             "reviews": {key: {f: row.get(f) for f in ("status", "closed", "archived", "decisions", "scope_stale")}
                         for key, row in reviews.items()}, "assignments": batch.get("assignments", {}),
             "target_groups": after, "input_fingerprint": preview["source_fingerprint"]}
    return {"requires_confirmation": required, "fingerprint": canonical_hash(basis),
            "previous_organization_id": (previous or {}).get("organization_id"),
            "previous_group_count": len((previous or {}).get("groups", [])), "new_group_count": len(preview["groups"]),
            "changed_group_item_ids": changed, "changed_group_item_count": len(changed),
            "current_review_count": len(current), "closed_current_review_count": sum(bool(r.get("closed")) for r in current),
            "saved_assignment_count": len(batch.get("assignments", {})),
            "reset_scope_item_count": len(set(before) | set(after)),
            "message": ('An earlier review or annotation exists. Applying this choice creates a new grouping and review queue. Previous media, decisions and human families are retained. Check first-pass and pool completion against the new grouping.'
                        if required else 'The first grouping creates machine candidates only. Human families are created during review.')}


def organization_system_summary(preview):
    requested = preview.get("requested_mode", preview["mode"])
    source = {"auto": "Automatic recommendation", "ward": "Manual Ward cut",
              "fine": "Fine HDBSCAN grouping", "coarse": "Coarse HDBSCAN grouping"}.get(requested, "Original microclusters")
    resolved = f"Ward merge height {preview['height']:.6g}" if preview["mode"] == "ward" else 'No Ward merge'
    cluster_groups = [g for g in preview["groups"] if g.get("kind") != "unclustered"]
    return (f"Grouping: {source}. {resolved}. {preview['microcluster_count']} microclusters → {len(cluster_groups)} candidate groups. "
            f"{preview['unclustered_count']} unclustered syllables retained for review.")


def organization_preview(dataset_dir, candidates, state, mode, height=None):
    from .annotation_batch_acoustics import ward_preview
    preview = ward_preview(dataset_dir, candidates, mode=mode, height=height,
                           protocol_version=state.get("batch_review", blank()).get("protocol_version", "0.4"))
    preview["impact"] = organization_change_impact(state, preview)
    preview["preview_fingerprint"] = canonical_hash(preview)
    return preview


def same_organization_partition(current, preview):
    """Compare auditory groups and initial pool membership independently of their IDs or names."""
    if not current:
        return False
    def members(organization):
        pool = set(organization.get("algorithmic_unclustered_ids", []))
        groups = set()
        for row in organization["groups"]:
            if row.get("kind") == "unclustered" or row["group_id"] == "machine-unclustered":
                pool.update(row["item_ids"])
            else:
                groups.add(frozenset(row["item_ids"]))
        return groups, pool
    return members(current) == members(preview)


def direct_exception_option(batch, previous, binding):
    """仅复用当前相同范围的真实聚类失败；取消不生成新的例外资格。"""
    denied = lambda code, message: {"allowed": False, "code": code, "message": message,
                                    "policy_version": EXCEPTION_POLICY}
    if not previous or previous.get("status") not in {"failed", "cancelled"}:
        return denied("NO_CLUSTER_FAILURE", 'Cluster the full current scope first so any failure or inapplicability is recorded.')
    source = previous
    if previous.get("route") == "direct_exception":
        # 直接审听本身取消/失败后可重试，但资格仍来自原聚类失败，不能链式造证据。
        evidence = previous.get("exception_evidence", {})
        if evidence.get("binding_sha256") != canonical_hash(binding):
            return denied("EXCEPTION_SCOPE_CHANGED", 'The members, inputs or criteria have changed. Cluster the current scope again.')
        source = batch["reviews"].get(evidence.get("source_review_id"))
    if source and source.get("route") in {"cluster", "family_cluster", "pool_cluster"} and source.get("status") == "cancelled":
        return denied("CANCELLED_NOT_FAILURE", 'Clustering was cancelled. Retry the calculation or leave the review open.')
    if not source or source.get("route") not in {"cluster", "family_cluster", "pool_cluster"} or source.get("status") != "failed":
        return denied("NO_CLUSTER_FAILURE", 'There is no verified clustering failure for this scope. Cancellation or failed direct playback does not qualify for an exception.')
    evidence = source.get("clustering_failure")
    if not evidence or not source.get("started_at") or evidence.get("policy_version") != EXCEPTION_POLICY:
        return denied("NO_BOUND_FAILURE", 'This record has no clustering failure tied to the current scope. Run clustering again.')
    if (source.get("preparation_binding") != binding
            or evidence.get("binding_sha256") != canonical_hash(binding)):
        return denied("EXCEPTION_SCOPE_CHANGED", 'Membership, input, grouping or criteria have changed since the failure. Cluster this scope again.')
    return {"allowed": True, "policy_version": EXCEPTION_POLICY,
            "source_review_id": source["review_id"], "binding_sha256": canonical_hash(binding),
            "error_code": evidence["error_code"], "error_message": evidence["error_message"],
            "message": 'A clustering failure matches this scope. Enter a reason to prepare all items for direct review.'}


def blank(protocol_version=None):
    version = protocol_version or DEFAULT_PROTOCOL_VERSION
    return {"schema_version": VERSION if version == "0.5" else "annotation-batch-review-v1",
            "protocol_version": version, "organizations": {}, "active_organization_id": None,
            "reviews": {}, "active_reviews": {}, "assignments": {}, "history": [],
            "latest_pool_review_id": None, "legacy_dirty_ids": [], "active_family_reviews": {}}


def new_protocol(batch):
    return batch.get("protocol_version", "0.4") == "0.5"


def current_organization(batch):
    original = batch["organizations"].get(batch["active_organization_id"])
    if not original or not new_protocol(batch):
        return original
    # Existing organizations are immutable historical inputs. Upgrading derives a
    # candidate-only view instead of rewriting old organization/review records.
    org = copy.deepcopy(original)
    noise = set(org.get("algorithmic_unclustered_ids", []))
    groups = []
    for group in org["groups"]:
        if group.get("kind") == "unclustered" or group["group_id"] == "machine-unclustered":
            noise.update(group["item_ids"])
        else:
            groups.append(group)
    org.update(groups=groups, algorithmic_unclustered_ids=sorted(noise), projection_protocol_version="0.5")
    return org


def assignment_current(batch, row, algorithmic_ids=None):
    if (row.get("stage") == "family" or bool(row.get("family_origin_id"))
            or row.get("organization_id") == batch["active_organization_id"]):
        return True
    if not new_protocol(batch):
        return False
    ids = _algorithmic_ids(batch) if algorithmic_ids is None else algorithmic_ids
    original = batch["organizations"].get(batch["active_organization_id"], {})
    return (row.get("item_id") in ids and row.get("input_fingerprint") == original.get("input_fingerprint"))


def _algorithmic_ids(batch):
    if not new_protocol(batch):
        return set()
    org = batch["organizations"].get(batch["active_organization_id"], {})
    ids = set(org.get("algorithmic_unclustered_ids", []))
    for group in org.get("groups", []):
        if group.get("kind") == "unclustered" or group["group_id"] == "machine-unclustered":
            ids.update(group["item_ids"])
    return ids


def active_pool(batch):
    algorithmic_ids = _algorithmic_ids(batch)
    dirty = set(batch.get("legacy_dirty_ids", []))
    result = {i for i, row in batch["assignments"].items()
              if row["disposition"] == "pool" and assignment_current(batch, row, algorithmic_ids)}
    prior = batch.get("algorithmic_prior_assignments", {})
    for item in algorithmic_ids:
        row = batch["assignments"].get(item)
        if item in dirty:
            result.add(item)
            continue
        if row and assignment_current(batch, row, algorithmic_ids):
            if row.get("boundary_flag") or row.get("pending_steps", "").strip():
                result.add(item)
            continue  # A real human result takes precedence; required work remains visible.
        if item not in prior:
            result.add(item)
    return sorted(result)


def _effective_active_reviews(batch):
    org = current_organization(batch)
    groups = {row["group_id"] for row in (org or {}).get("groups", [])}
    return {key: value for key, value in batch["active_reviews"].items()
            if not new_protocol(batch) or key in groups}


def _retired_noise_review(batch, review):
    return (new_protocol(batch) and review.get("stage") == "first"
            and review.get("organization_id") == batch["active_organization_id"]
            and review.get("group_id") not in {g["group_id"] for g in batch["organizations"].get(batch["active_organization_id"], {}).get("groups", [])
                                                 if g.get("kind") != "unclustered" and g["group_id"] != "machine-unclustered"})


def _capture_existing_algorithmic_assignments(state, batch):
    """Link existing catalogue evidence without manufacturing review decisions."""
    links = batch.setdefault("algorithmic_prior_assignments", {})
    algorithmic_ids = _algorithmic_ids(batch)
    for item in algorithmic_ids:
        row = batch["assignments"].get(item)
        if row and assignment_current(batch, row, algorithmic_ids):
            links.pop(item, None)
            continue
        families = sorted(x["local_type_id"] for x in state["memberships"]
                          if x["parent_type_id"] is None and item in x["member_ids"])
        if families:
            links[item] = {"kind": "existing_catalogue_assignment", "family_ids": families,
                           "decision_ids": sorted(k for k, v in state["decisions"].items()
                                                  if item in v.get("affected_ids", []))}
        else:
            links.pop(item, None)


def _current_protocol_restore(batch):
    return next((r for r in reversed(batch.get("protocol_restores", []))
                 if r["restore_id"] == batch.get("current_protocol_restore_id")), {})


def carried_over_context(batch, row, *, algorithmic_ids=None, retired_review_ids=None, restore_context=None):
    """One policy for valid algorithmic-source decisions retained across scope changes.

    The old scope stays open in its own history. Only its obsolete batch-level
    closure gate is replaced; pending checks and independent manual issues stay
    mandatory. Family-review scopes never receive this exemption.
    """
    ids = _algorithmic_ids(batch) if algorithmic_ids is None else algorithmic_ids
    if (not new_protocol(batch) or row.get("item_id") not in ids or row.get("stage") == "family"
            or not assignment_current(batch, row, ids)):
        return None
    review = batch["reviews"].get(row["review_id"], {})
    retired = row["review_id"] in retired_review_ids if retired_review_ids is not None else _retired_noise_review(batch, review)
    reorganised = row.get("organization_id") != batch.get("active_organization_id")
    if not retired and not reorganised:
        return None
    restored = _current_protocol_restore(batch) if restore_context is None else restore_context
    checkpoint = restored.get("checkpoint_id") if restored.get("checkpoint_assignment_record_ids", {}).get(row["item_id"]) == row["record_id"] else None
    return {"record_id": row["record_id"], "original_review_id": row["review_id"],
            "source_organization_id": row.get("organization_id"),
            "source_review_closed": review.get("closed", False),
            "carry_over_basis": "checkpoint_restore" if checkpoint else "organization_change" if reorganised else "protocol_upgrade",
            "restored_from_checkpoint": checkpoint,
            "disposition": row["disposition"], "family_id": row.get("family_id"),
            "required_checks_pending": bool(row["item_id"] in batch.get("legacy_dirty_ids", [])
                                            or row.get("boundary_flag") or row.get("pending_steps", "").strip())}


def carried_over_links(batch):
    ids = _algorithmic_ids(batch)
    retired = {key for key, review in batch["reviews"].items() if _retired_noise_review(batch, review)}
    restored = _current_protocol_restore(batch)
    result = {}
    for item, row in batch["assignments"].items():
        context = carried_over_context(batch, row, algorithmic_ids=ids,
                                       retired_review_ids=retired, restore_context=restored)
        if context is not None:
            result[item] = context
    return result


def restore_batch_protocol(state, current, restored, *, checkpoint_id, checkpoint_protocol, reason):
    """Restore human content without undoing an explicit execution-rule upgrade.

    No decision, review or upgrade is generated here. The caller keeps append-only
    archives/events and supplies the checkpoint's actual editable content.
    """
    if new_protocol(current) or new_protocol(restored):
        restored.update(schema_version=VERSION, protocol_version="0.5")
    def merged_records(name, identity):
        result = {}
        for row in current.get(name, []) + restored.get(name, []):
            key = row[identity]
            _require(key not in result or canonical_hash(result[key]) == canonical_hash(row),
                     "BATCH_RESTORE_HISTORY", 'The checkpoint conflicts with the immutable history.')
            result[key] = copy.deepcopy(row)
        return list(result.values())
    restored["protocol_upgrades"] = merged_records("protocol_upgrades", "record_id")
    restores = merged_records("protocol_restores", "restore_id")
    origins = copy.deepcopy(current.get("new_family_origins", {}))
    for key, value in restored.get("new_family_origins", {}).items():
        _require(key not in origins or canonical_hash(origins[key]) == canonical_hash(value),
                 "BATCH_RESTORE_HISTORY", "The new family's creation history conflicts with the checkpoint.")
        origins[key] = copy.deepcopy(value)
    restored["new_family_origins"] = origins
    state["batch_review"] = restored
    if new_protocol(restored):
        # Memberships/type cards have already been restored by the parent store.
        # These are links to real checkpoint evidence, never synthetic decisions.
        restored["algorithmic_prior_assignments"] = {}
        _capture_existing_algorithmic_assignments(state, restored)
    provenance = {"restore_id": "batch-restore-" + str(state["revision"] + 1),
                  "checkpoint_id": checkpoint_id, "reason": reason,
                  "restored_at": state["last_restore"]["restored_at"],
                  "from_checkpoint_protocol": checkpoint_protocol,
                  "retained_protocol_version": restored.get("protocol_version", "0.4"),
                  "checkpoint_active_organization_id": restored.get("active_organization_id"),
                  "checkpoint_assignment_record_ids": {i: row["record_id"] for i, row in restored["assignments"].items()},
                  "retained_upgrade_record_ids": [row["record_id"] for row in restored["protocol_upgrades"]],
                  "carried_over": {}}
    restores.append(provenance)
    restored["protocol_restores"] = restores
    restored["current_protocol_restore_id"] = provenance["restore_id"]
    provenance["carried_over"] = carried_over_links(restored)
    state["last_restore"].update(from_checkpoint_protocol=checkpoint_protocol,
                               retained_protocol_version=restored.get("protocol_version", "0.4"),
                               batch_protocol_restore_id=provenance["restore_id"])
    restored["restore_archives"][-1].update(from_checkpoint_protocol=checkpoint_protocol,
                                            retained_protocol_version=restored.get("protocol_version", "0.4"),
                                            batch_protocol_restore_id=provenance["restore_id"])
    return provenance


def pool_sources(batch):
    """Source metadata survives resolution; this is provenance, not a decision."""
    result = {}
    if new_protocol(batch):
        for org_id, original in batch["organizations"].items():
            noise = set(original.get("algorithmic_unclustered_ids", []))
            for group in original["groups"]:
                if group.get("kind") == "unclustered" or group["group_id"] == "machine-unclustered":
                    noise.update(group["item_ids"])
            for item in sorted(noise):
                result.setdefault(item, []).append({"kind": "algorithmic_unclustered", "organization_id": org_id,
                    "source_fingerprint": original.get("source_fingerprint"),
                    "input_fingerprint": original.get("input_fingerprint")})
    seen = set()
    for record in batch["history"]:
        for row in record.get("items", []):
            if row.get("disposition") != "pool":
                continue
            pair = (row["item_id"], record["record_id"])
            if pair in seen:
                continue
            seen.add(pair)
            review = batch["reviews"].get(record.get("review_id"), {})
            result.setdefault(row["item_id"], []).append({"kind": "human_unresolved", "record_id": record["record_id"],
                "review_id": record.get("review_id"), "review_origin": review.get("review_origin", review.get("stage"))})
    for item, row in batch["assignments"].items():
        if row.get("decision_source") == "manual_fine_decision" and row.get("disposition") == "pool" and (item, row["record_id"]) not in seen:
            result.setdefault(item, []).append({"kind": "human_unresolved", "record_id": row["record_id"],
                "review_id": row.get("review_id"), "review_origin": row.get("review_origin"), "decision_source": "manual_fine_decision"})
    return result


def pool_entries(batch, state=None):
    sources = pool_sources(batch)
    algorithmic_ids = _algorithmic_ids(batch)
    dirty = set(batch.get("legacy_dirty_ids", []))
    entries = {}
    for item in active_pool(batch):
        row = batch["assignments"].get(item)
        row = row if row and assignment_current(batch, row, algorithmic_ids) else None
        pending = bool(item in dirty or row and (row.get("boundary_flag") or row.get("pending_steps", "").strip()))
        from .annotation_final_review import directed_return_current
        directed_done = bool(row and directed_return_current(state, batch["reviews"].get(row.get("review_id"), {}), row))
        status = "pending_required_checks" if pending else "directed_reviewed_unresolved" if directed_done else "algorithmic_unreviewed" if row is None else (
            "pooled_reviewed_unresolved" if row["stage"] == "pool" and not pending else "human_unresolved")
        entries[item] = {"state": status, "sources": sources.get(item, []),
                         "pending_required_checks": pending, "latest_review_id": row.get("review_id") if row else None,
                         "review_origin": row.get("review_origin") if row else "algorithmic_intake"}
    return entries


def family_members(state, family_id):
    row = next((x for x in state["memberships"] if x["local_type_id"] == family_id
                and x["parent_type_id"] is None), None)
    _require(row is not None, "BATCH_TARGET_FAMILY", 'Select an existing human family. Machine groups and leaf types cannot be used here.')
    return sorted(row["member_ids"])


def family_title(state, family_id):
    """Current display name; cardless legacy families retain names in the audit."""
    card = state.get("type_cards", {}).get(family_id)
    if card is not None:
        return card.get("title") or family_id
    for record in reversed(state.get("batch_review", {}).get("history", [])):
        if record.get("action") == "family_rename" and record.get("family_id") == family_id:
            return record["title"]
    return family_id


def _family_rename(state, family_id, title, *, expected_title=None, check_expected=False):
    _require(isinstance(family_id, str) and any(row["local_type_id"] == family_id
             and row["parent_type_id"] is None for row in state["memberships"]),
             "FAMILY_NOT_FOUND", 'Select a current human family. Machine groups and leaf types cannot be used here.')
    _require(isinstance(title, str) and bool(title.strip()), "FAMILY_TITLE_REQUIRED", 'Enter a family name.')
    title = title.strip()
    previous = family_title(state, family_id)
    if check_expected:
        _require(isinstance(expected_title, str) and expected_title == previous,
                 "FAMILY_TITLE_CHANGED", 'The family name changed. Refresh it before renaming.')
    _require(not any(row["local_type_id"] != family_id and row["parent_type_id"] is None
             and family_title(state, row["local_type_id"]).strip().casefold() == title.casefold()
             for row in state["memberships"]), "FAMILY_TITLE_CONFLICT", 'Another family already uses this name.')
    _require(title != previous, "FAMILY_TITLE_UNCHANGED", 'The family name is unchanged.')
    return title, previous


def family_member_fingerprint(state, family_id):
    return canonical_hash({"family_id": family_id, "member_ids": family_members(state, family_id)})


def family_definition_fingerprint(state, family_id):
    from .annotation_manual_protocol import DEFINITION_FIELDS
    card = state.get("type_cards", {}).get(family_id, {})
    return canonical_hash({key: sorted(card.get(key, [])) if isinstance(card.get(key), list)
                           else card.get(key) for key in DEFINITION_FIELDS})


def item_version(state, item_id):
    assignment = state.get("batch_review", {}).get("assignments", {}).get(item_id, {})
    return canonical_hash({
        "memberships": sorted(x["local_type_id"] for x in state["memberships"] if item_id in x["member_ids"]),
        "batch_record": assignment.get("record_id"),
        "legacy_decisions": sorted(key for key, row in (state["decisions"].items() if isinstance(state["decisions"], dict)
                                   else ((r["decision_id"], r) for r in state["decisions"]))
                                   if item_id in row.get("affected_ids", [])),
    })


def current_pool_review(review):
    return review.get("stage") == "pool" and review.get("pool_mode") == "current_pool"


def current_pool_item_state(state, review):
    """Frozen pages stay intact; external work locks only the affected IDs."""
    batch = state.get("batch_review", blank())
    frozen = list(review.get("item_ids", []))
    current = set(active_pool(batch))
    overlaps, overlap_labels = {}, {}
    group_labels = {row["group_id"]: row.get("label") or row["group_id"]
                    for row in (current_organization(batch) or {}).get("groups", [])}
    active = set(_effective_active_reviews(batch).values()) | set(batch.get("active_family_reviews", {}).values())
    for rid in sorted(active):
        other = batch["reviews"].get(rid, {})
        if (rid == review.get("review_id") or other.get("closed") or other.get("archived")
                or other.get("status") not in ACTIVE | {"ready"}):
            continue
        for item in set(frozen) & set(other.get("item_ids", [])):
            overlaps.setdefault(item, []).append(rid)
            label = ("Human family " + family_title(state, other["family_id"]) if other.get("stage") == "family" else
                     "Candidate group " + group_labels.get(other.get("group_id"), other.get("group_id") or rid))
            overlap_labels.setdefault(item, []).append(label)
    locks, changed, withdrawn, covered = {}, [], [], []
    for item in frozen:
        assignment = batch.get("assignments", {}).get(item, {})
        token_changed = item_version(state, item) != review.get("expected_item_versions", {}).get(item)
        if token_changed:
            changed.append(item)
        left_pool = (token_changed and item not in current and assignment
                     and assignment_current(batch, assignment) and assignment.get("disposition") != "pool")
        if left_pool:
            withdrawn.append(item)
            locks[item] = {"code": "BATCH_ITEM_WITHDRAWN", "review_ids": overlaps.get(item, []),
                           "message": "Changed elsewhere; this item has left the current pool."}
        elif item in overlaps:
            locks[item] = {"code": "BATCH_ITEM_OVERLAP", "review_ids": overlaps[item],
                           "review_labels": overlap_labels[item],
                           "message": "Locked by " + "; ".join(overlap_labels[item]) + ". Other pool items remain editable."}
        elif token_changed:
            locks[item] = {"code": "BATCH_ITEM_VERSION_CHANGED", "review_ids": [],
                           "message": "This item changed elsewhere. Prepare the current pool again with a reason."}
        saved = review.get("decisions", {}).get(item, {})
        if (not token_changed and saved.get("record_id") and saved.get("record_id") == assignment.get("record_id")):
            covered.append(item)
    required = [item for item in frozen if item not in withdrawn]
    pending = any(review.get("decisions", {}).get(item, {}).get("boundary_flag") or
                  review.get("decisions", {}).get(item, {}).get("pending_steps", "").strip() for item in required)
    added = sorted(current - set(frozen))
    return {"item_locks": locks, "editable_item_ids": [item for item in frozen if item not in locks],
            "changed_item_ids": changed, "withdrawn_item_ids": withdrawn, "covered_item_ids": covered,
            "required_item_ids": required, "new_pool_item_ids": added,
            "pool_refresh_required": bool(added or changed),
            "can_close_current_pool": set(required) <= set(covered) and not pending}


def _carry_current_pool_decisions(state, review):
    batch = state.get("batch_review", blank())
    from .annotation_final_review import directed_return_current
    decisions, links = {}, {}
    for item in review["item_ids"]:
        row = batch.get("assignments", {}).get(item)
        if (not row or row.get("disposition") != "pool" or not row.get("record_id")
                or item in batch.get("legacy_dirty_ids", []) or not assignment_current(batch, row)):
            continue
        source = batch.get("reviews", {}).get(row.get("review_id"), {})
        if source.get("stage") != "pool" and not directed_return_current(state, source, row=row,
                pool=batch.get("reviews", {}).get(batch.get("latest_pool_review_id"))):
            continue
        decisions[item] = dict(copy.deepcopy(row), inherited_from_record_id=row["record_id"],
                               inherited_from_review_id=row.get("review_id"))
        links[item] = row["record_id"]
    review.update(decisions=decisions, carried_assignment_record_ids=links)


def review_stale_reason(state, review):
    if review.get("closed"):
        return ""
    if current_pool_review(review):
        return ""  # Item locks, not one stale bit, guard the current-pool profile.
    if review.get("directed_context"):
        from .annotation_final_review import _definition_versions
        context = review["directed_context"]
        if _definition_versions(state, context["affected_type_ids"]) != context["type_definition_fingerprints"]:
            return 'The type definition for this directed review changed. Confirm the question and current members again.'
    if review.get("stage") == "family":
        try:
            if family_members(state, review["family_id"]) != review["expected_family_members"]:
                return 'Family membership changed elsewhere. Check the current members and prepare a new round.'
            if not review.get("directed_context") and family_definition_fingerprint(state, review["family_id"]) != review["family_definition_fingerprint"]:
                return 'The family criteria changed. Check them before preparing a new round.'
        except ValueError:
            return 'The original human family no longer exists. Review the catalogue changes.'
    if any(item_version(state, i) != token for i, token in review.get("expected_item_versions", {}).items()):
        return 'Some targets have newer decisions. Check the updated members before preparing another round.'
    return ""


def require_fresh_review(state, review, item_ids=None):
    if current_pool_review(review):
        if item_ids is not None:
            locks = current_pool_item_state(state, review)["item_locks"]
            blocked = [item for item in item_ids if item in locks]
            _require(not blocked, locks[blocked[0]]["code"] if blocked else "BATCH_ITEM_VERSION_CHANGED",
                     "These items are locked by current review/version conflicts: " + ", ".join(blocked))
        return
    reason = review_stale_reason(state, review)
    _require(not reason, "BATCH_SCOPE_STALE", reason)


def pending_family_reviews(batch):
    return [batch["reviews"][rid] for rid in batch.get("active_family_reviews", {}).values()
            if rid in batch["reviews"] and not batch["reviews"][rid].get("closed")]


def pool_stage_complete(batch, state=None):
    pool = batch["reviews"].get(batch.get("latest_pool_review_id"), {})
    if current_pool_review(pool) and not first_pass_complete(batch):
        return False
    pending = any(i in batch.get("legacy_dirty_ids", []) or batch["assignments"].get(i, {}).get("boundary_flag")
                  or batch["assignments"].get(i, {}).get("pending_steps", "").strip() for i in active_pool(batch)) if new_protocol(batch) else False
    from .annotation_final_review import directed_return_current
    valid_directed = {key: r for key, r in batch["reviews"].items() if directed_return_current(state, r, pool=pool)}
    return (bool(pool.get("closed")) and pool.get("organization_id") == batch["active_organization_id"]
            and not pending and not pending_family_reviews(batch)
            and all(batch["assignments"].get(i, {}).get("review_id") == pool.get("review_id") or (
                current_pool_review(pool) and isinstance(pool.get("carried_assignment_record_ids", {}).get(i), str)
                and bool(pool.get("carried_assignment_record_ids", {}).get(i))
                and pool.get("carried_assignment_record_ids", {}).get(i) == batch["assignments"].get(i, {}).get("record_id")
                and pool.get("decisions", {}).get(i, {}).get("record_id") == batch["assignments"].get(i, {}).get("record_id")) or (
                batch["assignments"].get(i, {}).get("review_id") in valid_directed
                and valid_directed[batch["assignments"][i]["review_id"]]["directed_return"]["covered_record_ids"].get(i) == batch["assignments"][i]["record_id"])
                    for i in active_pool(batch)))


def reject_review_overlap(state, item_ids, exclude_review_id=None, *, allow_current_pool_overlap=False):
    batch = state.get("batch_review", blank())
    active_ids = set(_effective_active_reviews(batch).values()) | set(batch.get("active_family_reviews", {}).values())
    if batch.get("latest_pool_review_id"):
        active_ids.add(batch["latest_pool_review_id"])
    for ident in active_ids - {exclude_review_id}:
        review = batch["reviews"].get(ident, {})
        if allow_current_pool_overlap and current_pool_review(review):
            continue
        if (not review.get("closed") and review.get("status") in ACTIVE | {"ready"}
                and set(item_ids) & set(review.get("item_ids", []))):
            _require(False, "BATCH_OVERLAPPING_REVIEW", 'These syllables are being reviewed elsewhere. Finish the overlapping round first.')


def link_legacy_decision(state, record):
    """同一family的细化沿用真实三轴决定；真正改family才要求重新核对批量去向。"""
    batch = state.get("batch_review")
    if not batch:
        return
    axes_by_item = {r["item_id"]: r["axes"] for r in record.get("item_axes", [])}
    for item in record.get("affected_ids", []):
        previous = batch["assignments"].get(item)
        axes = axes_by_item.get(item, record.get("axes")) or {}
        family = axes.get("family_id")
        same_assigned = (previous and previous["disposition"] in ASSIGNED_DISPOSITIONS
                         and axes.get("family_status") == "assigned" and family == previous.get("family_id")
                         and any(x["local_type_id"] == family and item in x["member_ids"] for x in state["memberships"]))
        same_unresolved = (previous and previous["disposition"] == "pool" and axes.get("family_status") == "unresolved"
                           and not any(x["parent_type_id"] is None and item in x["member_ids"] for x in state["memberships"]))
        if same_assigned or same_unresolved:
            saved = copy.deepcopy(previous)
            saved["legacy_refinement"] = {"decision_id": record["decision_id"], "issue_id": record["issue_id"],
                "processing_state": record["processing_state"], "form_status": axes.get("form_status"),
                "leaf_id": axes.get("leaf_id"), "catalogue_revision": state["catalogue_revision"]}
            batch["assignments"][item] = saved
        else:
            batch["legacy_dirty_ids"] = sorted(set(batch.get("legacy_dirty_ids", [])) | {item})
    if new_protocol(batch):
        _capture_existing_algorithmic_assignments(state, batch)


def inherited_legacy_refinement(state, item_id, family_id, effective_ids=None, prior_refinement=None):
    """衔接旧工作台有效已闭合的判断；沿用证据不等于本轮重新评估末级。"""
    from .annotation_decision_history import effective_rows
    rows = [row for row in effective_rows(state, effective_ids) if item_id in row.get("affected_ids", [])]
    if not rows:
        return None
    decision = max(rows, key=lambda row: (row.get("catalogue_revision_after", -1), row["decision_id"]))
    issue = state["issues"].get(decision["issue_id"], {})
    superseded = any(row.get("parent_issue_id") == decision["issue_id"] and item_id in row.get("target_ids", [])
                     for row in state["issues"].values())
    if superseded:
        return None
    per_item = {row["item_id"]: row["axes"] for row in decision.get("item_axes", [])}
    axes = per_item.get(item_id, decision.get("axes")) or {}
    if axes.get("family_status") != "assigned" or axes.get("family_id") != family_id:
        return None
    leaves = {row["local_type_id"] for row in state["memberships"]
              if row["parent_type_id"] == family_id and item_id in row["member_ids"]}
    form = axes.get("form_status")
    same_leaf = leaves == ({axes["leaf_id"]} if axes.get("leaf_id") else set())
    if (prior_refinement and prior_refinement.get("decision_id") == decision["decision_id"]
            and prior_refinement.get("issue_id") == decision["issue_id"]
            and prior_refinement.get("form_status") == form and same_leaf):
        # 可保留仍待结束的本轮细化，但不能将旧family/旧leaf指针贴到新归属。
        return copy.deepcopy(prior_refinement)
    if (not issue.get("closure_confirmed") or issue.get("status") not in {"decided", "reviewed_unresolved"}
            or decision.get("processing_state") not in {"decided", "reviewed_unresolved"}):
        return None
    if ((form in {"main", "variant"} and leaves != {axes.get("leaf_id")}) or
            (form not in {"main", "variant"} and (leaves or form not in {"unresolved", "candidate_form"}))):
        return None
    return {"decision_id": decision["decision_id"], "issue_id": decision["issue_id"],
            "processing_state": decision["processing_state"], "form_status": form,
            "leaf_id": axes.get("leaf_id"), "catalogue_revision": decision["catalogue_revision_after"],
            "inherited_existing_evidence": True, "new_leaf_assessment": False}


def first_pass_complete(batch):
    org = current_organization(batch)
    dirty = set(batch.get("legacy_dirty_ids", []))
    if new_protocol(batch):
        dirty &= {i for group in (org or {}).get("groups", []) for i in group["item_ids"]}
    return bool(org) and not dirty and all(
        batch["reviews"].get(batch["active_reviews"].get(g["group_id"]), {}).get("closed", False)
        for g in org["groups"])


def batch_projection(state):
    """机器组、人工family及池复核分别投影；历史范围不冒充当前确认。"""
    batch = state.get("batch_review")
    if not batch:
        return {}
    org = current_organization(batch)
    family_reviews = [batch["reviews"].get(rid, {}) for rid in batch.get("active_family_reviews", {}).values()]
    latest_pool = batch["reviews"].get(batch.get("latest_pool_review_id"), {})
    result = {}
    pool_details = pool_entries(batch, state)
    algorithmic_ids = _algorithmic_ids(batch)
    active_groups = {item: batch["reviews"].get(batch["active_reviews"].get(g["group_id"]), {})
                     for g in (org or {}).get("groups", []) for item in g["item_ids"]}
    retired = {key for key, review in batch["reviews"].items() if _retired_noise_review(batch, review)}
    restored = _current_protocol_restore(batch)
    for item in state["contract"]["allowed_item_ids"]:
        row = batch["assignments"].get(item)
        families = [r for r in family_reviews if item in r.get("item_ids", []) and not r.get("closed")]
        if not row or not assignment_current(batch, row, algorithmic_ids):
            if not families and item in batch.get("algorithmic_prior_assignments", {}) and item not in pool_details:
                continue  # Preserve the existing manual-protocol projection and its closure gates.
            if org or families:
                result[item] = {"processing_state": "unfinished", "review_status": "not_reviewed",
                                "batch_stage": "family" if families else "pool" if item in algorithmic_ids else "first", "batch_disposition": None,
                                "pool_state": pool_details.get(item, {}).get("state"),
                                "batch_review_id": families[-1]["review_id"] if families else None,
                                "closure_confirmation_required": True}
            continue
        review = batch["reviews"].get(row["review_id"], {})
        active_group = active_groups.get(item, {})
        pending = bool(row.get("pending_steps", "").strip() or row.get("boundary_flag"))
        status = "unfinished" if pending or item in batch.get("legacy_dirty_ids", []) else (
            "reviewed_unresolved" if row["disposition"] == "pool" or (
                row["disposition"] in ASSIGNED_DISPOSITIONS and row.get("form_status") == "unresolved") else "decided")
        from .annotation_final_review import directed_return_current
        directed_done = directed_return_current(state, review, row)
        if row["disposition"] == "pool" and row["stage"] in {"first", "family"} and not directed_done:
            status = "unfinished"
        if latest_pool and not latest_pool.get("closed") and item in latest_pool.get("item_ids", []) and row["review_id"] != latest_pool["review_id"]:
            status = "unfinished"
        if active_group and not active_group.get("closed") and row["review_id"] != active_group["review_id"]:
            status = "unfinished"
        if any(row["review_id"] != r["review_id"] or review_stale_reason(state, r) for r in families):
            status = "unfinished"
        carry_context = carried_over_context(batch, row, algorithmic_ids=algorithmic_ids,
                                             retired_review_ids=retired, restore_context=restored)
        carried = carry_context is not None
        refinement = row.get("legacy_refinement")
        legacy_issue = state["issues"].get(refinement["issue_id"], {}) if refinement else {}
        if refinement and status != "unfinished":
            status = legacy_issue.get("member_processing_states", {}).get(item, "unfinished")
        result[item] = {"processing_state": status, "saved_processing_state": status,
                        "review_status": "carried_over" if carried else "closed" if review.get("closed") else "under_human_review",
                        "carried_over_record_id": row["record_id"] if carried else None,
                        "carry_over_basis": carry_context["carry_over_basis"] if carried else None,
                        "source_organization_id": row.get("organization_id") if carried else None,
                        "original_review_id": row["review_id"] if carried else None,
                        "source_review_closed": review.get("closed", False) if carried else None,
                        "upgrade_record_id": (batch.get("protocol_upgrades") or [{}])[-1].get("record_id") if carried else None,
                        "restored_from_checkpoint": restored.get("checkpoint_id") if restored.get("checkpoint_assignment_record_ids", {}).get(item) == row["record_id"] else None,
                        "batch_stage": row["stage"], "batch_disposition": row["disposition"],
                        "batch_review_id": families[-1]["review_id"] if families else row["review_id"],
                        "closure_confirmation_required": (not review.get("closed", False) and not carried) or bool(families)
                            or bool(active_group and not active_group.get("closed"))
                            or bool(refinement and not legacy_issue.get("closure_confirmed"))}
    return result


def batch_completion_gaps(state, ids):
    from .annotation_final_review import directed_return_current
    batch = state.get("batch_review")
    if not batch:
        return []
    gaps = []
    if current_organization(batch):
        if not first_pass_complete(batch):
            gaps.append({"code": "BATCH_FIRST_PASS_PENDING", "message": 'Some candidate groups still need their first review.' if new_protocol(batch) else 'The first-pass groups and the unclustered queue have not all been marked complete.'})
        if not pool_stage_complete(batch, state):
            gaps.append({"code": "BATCH_POOL_PENDING", "message": 'Finish the current global pool review. Items added since the last round still need review.'})
    elif set(ids) & set(active_pool(batch)) and any(
            not directed_return_current(
                state, batch["reviews"].get(batch["assignments"].get(i, {}).get("review_id"), {}), batch["assignments"].get(i))
            for i in set(ids) & set(active_pool(batch))):
        gaps.append({"code": "BATCH_POOL_PENDING", "message": 'The family review returned items to the pool. Declare and finish the full first-pass scope before marking the workflow complete.'})
    family_pending = [r["review_id"] for r in pending_family_reviews(batch) if set(ids) & set(r["item_ids"])]
    if family_pending:
        gaps.append({"code": "BATCH_FAMILY_PENDING", "message": 'A related family review is still open.', "review_ids": family_pending})
    dirty = sorted(set(ids) & set(batch.get("legacy_dirty_ids", [])))
    if dirty:
        gaps.append({"code": "BATCH_EXTERNAL_CHANGE", "message": 'Further review changed some items. Check the batch records.', "item_ids": dirty})
    unclosed = sorted({row["batch_review_id"] for i, row in batch_projection(state).items()
                       if i in ids and row.get("batch_review_id") and row["closure_confirmation_required"]})
    if unclosed:
        gaps.append({"code": "BATCH_REVIEW_OPEN", "message": 'A related batch review is still open.', "review_ids": unclosed})
    if (state.get("catalogue_scope") or {}).get("target_level") == "leaf":
        unassessed = [i for i in ids if batch["assignments"].get(i, {}).get("disposition") in ASSIGNED_DISPOSITIONS
                      and batch["assignments"][i].get("legacy_refinement", {}).get("form_status",
                          batch["assignments"][i].get("form_status", "not_assessed")) == "not_assessed"]
        if unassessed:
            gaps.append({"code": "BATCH_LEAF_UNASSESSED", "message": 'These syllables have a confirmed family but no leaf assessment yet.', "item_ids": unassessed})
    return gaps


def _text(value, message):
    _require(isinstance(value, str) and bool(value.strip()) and len(value) <= 20000, "BATCH_REASON", message)
    return value.strip()


def _all_ids(state, values):
    _require(isinstance(values, list) and all(isinstance(i, str) for i in values)
             and len(values) == len(set(values)), "BATCH_SCOPE", 'Syllable IDs must form a list with no duplicates.')
    _require(set(values) <= set(state["contract"]["allowed_item_ids"]), "OUT_OF_SCOPE", 'The syllable is outside the current task.')


def _current_review(batch, review_id):
    _require(isinstance(review_id, str), "BATCH_REVIEW_NOT_FOUND", 'Specify the review round ID.')
    _require(review_id in batch["reviews"], "BATCH_REVIEW_NOT_FOUND", 'Review round not found.')
    row = batch["reviews"][review_id]
    if row["stage"] == "family":
        _require(batch.get("active_family_reviews", {}).get(row["family_id"]) == review_id, "BATCH_VERSION", 'A newer review round exists for this family.')
        return row
    _require(row["organization_id"] == batch["active_organization_id"], "BATCH_VERSION", 'The grouping has changed. This older round is available as history.')
    if row["stage"] == "first":
        _require(not _retired_noise_review(batch, row), "BATCH_REVIEW_SUPERSEDED", 'The protocol upgrade moved these unclustered items to pooled review. The old pages are read-only.')
        _require(batch["active_reviews"].get(row["group_id"]) == review_id, "BATCH_VERSION", 'A newer review round exists for this group.')
    else:
        _require(batch.get("latest_pool_review_id") == review_id, "BATCH_VERSION", 'A newer global pool round exists.')
    return row


def _review_ancestors(batch, review_id):
    result = set()
    while review_id and review_id not in result:
        result.add(review_id)
        review_id = batch["reviews"].get(review_id, {}).get("parent_review_id")
    return result


def _register_new_families(store, state, batch, review, record):
    definitions = record.get("new_families", {})
    _require(isinstance(definitions, dict), "BATCH_NEW_FAMILY", 'New family definitions must map draft keys to drafts.')
    new_rows = [row for row in record["items"] if row.get("disposition") == "new_family"]
    _require(new_protocol(batch) or not definitions and not new_rows,
             "BATCH_PROTOCOL_UPGRADE_REQUIRED", 'Upgrade to protocol 0.5 before creating a family through an item outcome.')
    if not new_protocol(batch):
        return
    origins = batch.setdefault("new_family_origins", {})
    ancestors = _review_ancestors(batch, review["review_id"])
    keys = [row.get("new_family_key") for row in new_rows]
    _require(all(isinstance(key, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}", key)
                 for key in keys), "BATCH_NEW_FAMILY_KEY", 'Give each new family a stable temporary key.')
    used = set(keys)
    _require(set(definitions) <= used, "BATCH_NEW_FAMILY_UNUSED", 'A new family needs at least one syllable assigned to it in this submission.')
    resolved = {}
    for key in sorted(used):
        spec = definitions.get(key)
        requested = [row for row in new_rows if row["new_family_key"] == key]
        existing = [(fid, origin) for fid, origin in origins.items()
                    if origin["client_key"] == key and origin["review_id"] in ancestors]
        if spec is not None:
            _require(isinstance(spec, dict) and set(spec) <= {"family_id", "title", "diagnostic_body", "reason"},
                     "BATCH_NEW_FAMILY", 'The new family draft has unexpected or missing fields.')
            _text(spec.get("title"), 'Enter a name for the new family.')
            _text(spec.get("reason"), 'Record your basis for creating the new family.')
            _require(isinstance(spec.get("diagnostic_body", ""), str), "BATCH_NEW_FAMILY", 'The diagnostic criteria must be text. You can leave them blank and complete the type card later.')
            _require(spec.get("family_id") is None or isinstance(spec.get("family_id"), str),
                     "BATCH_FAMILY_ID", 'The optional new family ID must be text.')
            normalized = {"title": spec["title"], "diagnostic_body": spec.get("diagnostic_body", ""), "reason": spec["reason"]}
            if spec.get("family_id"):
                normalized["family_id"] = spec["family_id"]
            if existing:
                _require(len(existing) == 1, "BATCH_NEW_FAMILY_KEY", 'This draft key has more than one creation source. Specify the creation record.')
                fid, origin = existing[0]
                _require(origin["definition_fingerprint"] == canonical_hash(normalized),
                         "BATCH_NEW_FAMILY_CONFLICT", 'The same draft key cannot have different definitions. Use a new key or revise the type card.')
            else:
                fid = spec.get("family_id") or "F-" + hashlib.sha256((record["record_id"] + ":" + key).encode()).hexdigest()[:16]
                _require(isinstance(fid, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}", fid),
                         "BATCH_FAMILY_ID", 'Invalid new family ID.')
                _require(fid not in origins and all(row["local_type_id"] != fid for row in state["memberships"]),
                         "BATCH_FAMILY_ID_CONFLICT", 'This family ID already exists. Select the existing family.')
                origin = {"family_id": fid, "client_key": key, "creation_record_id": record["record_id"],
                          "review_id": review["review_id"], "page_id": record["page_id"],
                          "definition_fingerprint": canonical_hash(normalized), "definition": copy.deepcopy(normalized),
                          "reason": spec["reason"], "created_at": record["recorded_at"], "source": record["source"]}
                state["memberships"].append({"local_type_id": fid, "parent_type_id": None, "member_ids": [], "supporting_decision_ids": []})
                store._type_card(state, {"local_type_id": fid,
                    "card_id": "card-" + record["record_id"] + "-" + hashlib.sha256(key.encode()).hexdigest()[:12],
                    "title": spec["title"], "diagnostic_body": spec.get("diagnostic_body", ""), "reason": spec["reason"],
                    "definition_status": "draft", "main_variant_difference": "", "uncertainties": "",
                    "representative_ids": [], "atypical_ids": [], "counterexample_ids": [], "competing_type_ids": [],
                    "source": record["source"], "experiment_enabled": False, "recorded_at": record["recorded_at"]})
                origins[fid] = origin
        else:
            _require(all(isinstance(row.get("family_id"), str) for row in requested),
                     "BATCH_NEW_FAMILY_REFERENCE", 'A saved new-family reference must include its family ID.')
            ids = {row["family_id"] for row in requested}
            _require(len(ids) == 1 and next(iter(ids)) in origins,
                     "BATCH_NEW_FAMILY_REFERENCE", 'Provide a draft for a new family. For saved items, reference the original family and creation record.')
            fid = next(iter(ids)); origin = origins[fid]
            _require(origin["client_key"] == key and origin["review_id"] in ancestors,
                     "BATCH_NEW_FAMILY_REFERENCE", 'This family was not created in this round or its parent. To reassign the item, select Other family.')
            _require(all(row.get("creation_record_id") == origin["creation_record_id"] for row in requested),
                     "BATCH_NEW_FAMILY_REFERENCE", 'The creation record reference does not match.')
        _require(any(row["local_type_id"] == fid and row["parent_type_id"] is None for row in state["memberships"]),
                 "BATCH_NEW_FAMILY_REFERENCE", 'The new family was removed or has become a leaf type.')
        for row in requested:
            _require(row.get("family_id") in {None, "", fid}, "BATCH_NEW_FAMILY_REFERENCE", 'The family draft key does not match the target ID.')
            if row.get("creation_record_id"):
                _require(row["creation_record_id"] == origin["creation_record_id"], "BATCH_NEW_FAMILY_REFERENCE", 'The creation record reference does not match.')
            row.update(family_id=fid, creation_record_id=origin["creation_record_id"])
        resolved[key] = fid
    record["new_family_ids"] = resolved


def apply_batch(store, state, payload):
    """事务内再核对状态机；payload只接收服务生成的已登记输入和媒体。"""
    action, record = payload["action"], copy.deepcopy(payload["record"])
    store._engineering_record(record, state)
    batch = state.setdefault("batch_review", blank())
    batch.setdefault("active_family_reviews", {})
    ident = record["record_id"]
    _require(not any(x["record_id"] == ident for x in batch["history"]), "IMMUTABLE_RECORD", 'A batch record cannot be overwritten.')
    if action == "family_rename":
        title, previous = _family_rename(state, record.get("family_id"), record.get("title"),
                                        expected_title=record.get("previous_title"), check_expected=True)
        record.update(action="family_rename", title=title, previous_title=previous)
        card = state.get("type_cards", {}).get(record["family_id"])
        if card is not None:
            # Only the current display label changes. The immutable card history,
            # definition identity, evidence and all workflow states stay intact.
            card["title"] = title
        batch["history"].append(copy.deepcopy(record))
        payload["record"] = copy.deepcopy(record)
        return
    if action == "upgrade":
        _require(not new_protocol(batch), "BATCH_ALREADY_UPGRADED", 'This task already uses protocol 0.5.')
        _require(record.get("target_protocol_version") == "0.5", "BATCH_PROTOCOL_VERSION", 'Only an upgrade to protocol 0.5 is supported.')
        _text(record.get("reason"), 'Give the reason for adopting the new workflow while retaining the existing manual records.')
        _require(not any(r["status"] in ACTIVE for r in batch["reviews"].values()), "BATCH_BUSY", 'Wait for the batch calculation to finish or stop it first.')
        original = current_organization(batch)
        record.update(previous_protocol_version=batch.get("protocol_version", "0.4"),
                      previous_schema_version=batch.get("schema_version"),
                      organization_id=batch.get("active_organization_id"),
                      original_organization_fingerprint=canonical_hash(original) if original else None,
                      carried_assignment_ids=sorted(batch["assignments"]))
        batch.update(schema_version=VERSION, protocol_version="0.5")
        _capture_existing_algorithmic_assignments(state, batch)
        record["retired_review_ids"] = sorted(k for k, v in batch["reviews"].items() if _retired_noise_review(batch, v))
        record["carried_over"] = carried_over_links(batch)
        record["algorithmic_unreviewed_ids"] = [i for i in active_pool(batch) if i not in batch["assignments"]]
        batch.setdefault("protocol_upgrades", []).append(copy.deepcopy(record))
    elif action == "apply":
        _require(not any(r["status"] in ACTIVE for r in batch["reviews"].values()), "BATCH_BUSY", 'Wait for the grouping job to finish or cancel it first.')
        org = record["organization"]
        ids = [i for g in org["groups"] for i in g["item_ids"]]
        if new_protocol(batch):
            ids += org.get("algorithmic_unclustered_ids", [])
        _all_ids(state, ids)
        _require(set(ids) == set(state["contract"]["allowed_item_ids"]), "BATCH_COVERAGE", 'The candidate groups must cover the complete current input.')
        record["previous_organization_id"] = batch.get("active_organization_id")
        batch["organizations"][org["organization_id"]] = org
        batch["active_organization_id"] = org["organization_id"]
        batch["active_reviews"] = {}
        batch["latest_pool_review_id"] = None
        if new_protocol(batch):
            _capture_existing_algorithmic_assignments(state, batch)
            record["carried_over"] = carried_over_links(batch)
    elif action == "prepare":
        review = record["review"]
        if review.get("route") == "family_cluster":
            _require(review.get("stage") == "family" and review.get("review_origin") == "family_recheck"
                     and not review.get("directed_context") and not review.get("directed_issue_id"),
                     "BATCH_FAMILY_ROUTE", 'This clustering method requires the full scope of an ordinary human-family review.')
        if review.get("route") == "pool_cluster":
            _require(review.get("stage") == "pool" and review.get("review_origin") == "pooled_review"
                     and not review.get("directed_context") and not review.get("directed_issue_id"),
                     "BATCH_POOL_ROUTE", 'This clustering method requires the complete global pool.')
        _require(review["stage"] == "family" or review["organization_id"] == batch["active_organization_id"], "BATCH_VERSION", 'The candidate grouping version has changed.')
        _all_ids(state, review["item_ids"])
        _require(not any(r["status"] in ACTIVE for r in batch["reviews"].values()), "BATCH_BUSY", 'A batch calculation is running. Wait for it to finish or cancel it first.')
        if review["stage"] == "family":
            _require(review["item_ids"] == family_members(state, review["family_id"]),
                     "BATCH_FAMILY_CHANGED", 'Family membership has changed. Check the current members.')
            _require(review["family_member_fingerprint"] == family_member_fingerprint(state, review["family_id"]),
                     "BATCH_FAMILY_CHANGED", 'The family membership version does not match.')
            batch["active_family_reviews"][review["family_id"]] = review["review_id"]
        elif review["stage"] == "first":
            groups = {g["group_id"]: g for g in current_organization(batch)["groups"]}
            _require(review["group_id"] in groups and review["item_ids"] == groups[review["group_id"]]["item_ids"],
                     "BATCH_SCOPE", 'The first pass must cover every member of the selected group.')
            batch["active_reviews"][review["group_id"]] = review["review_id"]
            if not current_pool_review(batch["reviews"].get(batch.get("latest_pool_review_id"), {})):
                batch["latest_pool_review_id"] = None
        else:
            if not current_pool_review(review) or not review["item_ids"]:
                _require(first_pass_complete(batch), "BATCH_FIRST_PASS_PENDING", 'Finish all first-pass groups before preparing this global pool round.')
                _require(not pending_family_reviews(batch), "BATCH_FAMILY_PENDING", 'Finish the active family reviews before freezing this pool.')
            _require(review["item_ids"] == active_pool(batch), "BATCH_POOL_CHANGED", 'Pool membership has changed. Check the current members.')
            if current_pool_review(review):
                _require(review["route"] in {"pool_cluster", "direct_exception"}, "BATCH_POOL_MODE", 'Select an explicit ordinary pool review method for current-pool mode.')
                _require(review["expected_item_versions"] == {i: item_version(state, i) for i in review["item_ids"]},
                         "BATCH_POOL_CHANGED", 'The version of a current pool item has changed.')
                _carry_current_pool_decisions(state, review)
            batch["latest_pool_review_id"] = review["review_id"]
        if review.get("directed_context"):
            from .annotation_final_review import open_directed_issue
            open_directed_issue(store, state, review, record)
        batch["reviews"][review["review_id"]] = review
    elif action == "compute":
        review = batch["reviews"].get(record["review_id"])
        _require(review is not None, "BATCH_REVIEW_NOT_FOUND", 'Review round for this job not found.')
        status = record["status"]
        edges = {"queued": {"running", "cancel_requested", "failed", "cancelled"},
                 "running": {"ready", "cancel_requested", "failed", "cancelled"},
                 "cancel_requested": {"cancelled", "failed"}}
        _require(status in edges.get(review["status"], set()), "BATCH_JOB_STATE", 'Invalid batch job state transition.')
        if status == "running":
            _require(not state["deadline_locked"], "DEADLINE_LOCKED", 'A new batch calculation cannot start after the deadline.')
        if status == "ready":
            pages = record["pages"]
            flat = [i for page in pages for i in page["item_ids"]]
            _require(len(flat) == len(set(flat)) and set(flat) == set(review["item_ids"]), "BATCH_COVERAGE", 'A concatenated page has missing or duplicate items.')
            _require(all(0 < len(p["item_ids"]) <= 10 and len(p["slots"]) == len(p["item_ids"]) for p in pages),
                     "BATCH_PAGES", 'Each page must contain 1–10 items and retain its slot mapping.')
            _require(len({p["page_id"] for p in pages}) == len(pages) and all(
                [slot["item_id"] for slot in p["slots"]] == p["item_ids"] for p in pages),
                "BATCH_PAGES", 'Page IDs must be unique and slots must match the full syllable order.')
        review.update({k: copy.deepcopy(v) for k, v in record.items() if k not in {"record_id", "source", "experiment_enabled"}})
    elif action == "family":
        key = record["family_id"]
        _require(isinstance(key, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}", key) is not None,
                 "BATCH_FAMILY_ID", 'A family ID may contain only letters, digits, underscores, periods, colons and hyphens.')
        existing = next((x for x in state["memberships"] if x["local_type_id"] == key), None)
        _require(existing is None or existing["parent_type_id"] is None, "BATCH_FAMILY_ID", 'Select a family rather than a leaf type.')
        prior_card = state.get("type_cards", {}).get(key)
        _require(not prior_card or prior_card["definition_status"] != "reviewed", "BATCH_DEFINITION_REVIEWED", 'Use the type card to revise confirmed criteria and review the affected relationships.')
        if existing is None:
            state["memberships"].append({"local_type_id": key, "parent_type_id": None, "member_ids": [], "supporting_decision_ids": []})
        card = {"local_type_id": key, "card_id": "card-" + ident, "title": record["title"],
                "diagnostic_body": record["diagnostic_body"], "reason": record["reason"],
                "definition_status": "draft", "main_variant_difference": "", "uncertainties": 'Examples and confirmation pending',
                "representative_ids": [], "atypical_ids": [], "counterexample_ids": [], "competing_type_ids": [],
                "source": record["source"], "experiment_enabled": False, "recorded_at": record["recorded_at"]}
        store._type_card(state, card)
        state["catalogue_revision"] += 1
    elif action == "submit":
        review = _current_review(batch, record["review_id"])
        _require(review["status"] == "ready" and not review["closed"], "BATCH_REVIEW_CLOSED", 'Prepare or reopen the review round before submitting.')
        compact = record.get("submission_contract_version") == COMPACT_SUBMISSION_VERSION
        if compact:
            _require(_compact_review_scope(review), "BATCH_COMPACT_DIRECTED",
                     'Use the original submission contract, question and evidence requirements for this directed round.')
        pages = {p["page_id"]: p for p in review["pages"]}
        _require(isinstance(record.get("page_id"), str) and record["page_id"] in pages, "BATCH_PAGE", 'The page does not belong to the current round.')
        page = pages[record["page_id"]]
        rows = record["items"]
        _require(isinstance(rows, list) and bool(rows) and all(isinstance(x, dict) for x in rows), "BATCH_ITEMS", 'Specify the items you actually inspected on this page.')
        ids = [x.get("item_id") for x in rows]
        _all_ids(state, ids)
        _require(set(ids) <= set(page["item_ids"]), "BATCH_PAGE_SCOPE", 'Items outside this page and read-only references cannot be changed here.')
        require_fresh_review(state, review, item_ids=ids)
        _register_new_families(store, state, batch, review, record)
        family_ids = {x["local_type_id"] for x in state["memberships"] if x["parent_type_id"] is None}
        current_family = record.get("current_family_id")
        if review["stage"] == "family":
            _require(current_family == review["family_id"], "BATCH_CURRENT_FAMILY", 'Keep the source family as Current family. Select Other family to reassign the item.')
        for row in rows:
            row_fields = {"item_id", "disposition", "family_id", "reason", "boundary_flag", "form_status", "pending_steps", "new_family_key", "creation_record_id"}
            if compact:
                row_fields.add("reason_code")
            _require(set(row) <= row_fields, "REQUEST_FIELDS", 'The outcome contains unsupported fields.')
            disposition = row.get("disposition")
            _require(isinstance(disposition, str) and disposition in (DISPOSITIONS if new_protocol(batch) else LEGACY_DISPOSITIONS), "BATCH_DISPOSITION", 'Select one of the five outcomes.' if new_protocol(batch) else 'Select one of the four outcomes.')
            if disposition != "new_family":
                _require(not row.get("new_family_key") and not row.get("creation_record_id"), "BATCH_NEW_FAMILY_REFERENCE", 'Only the New family outcome can include a creation reference.')
            if compact:
                _compact_submission_row(row, batch["assignments"].get(row["item_id"], {}))
            else:
                _text(row.get("reason"), 'Provide the basis for each outcome. A shared entry can cover items you checked and judged alike.')
            family = row.get("family_id")
            if disposition in ASSIGNED_DISPOSITIONS:
                _require(isinstance(family, str) and family in family_ids, "BATCH_TARGET_FAMILY", 'Select a human family. If the assignment is unclear, send the item to the global pool.')
                if disposition == "current":
                    _require(current_family == family, "BATCH_CURRENT_FAMILY", 'The selected family must match the current human family.')
                elif disposition == "other":
                    _require(not current_family or current_family != family, "BATCH_OTHER_FAMILY", 'The other family must differ from the current family.')
            else:
                _require(family is None or family == "", "BATCH_DISPOSITION_FAMILY", 'An unresolved or excluded item cannot also have a confirmed family.')
                family = None
            _require(type(row.get("boundary_flag", False)) is bool, "BATCH_BOUNDARY", 'The boundary flag must be true or false.')
            _require(row.get("form_status", "not_assessed") in {"not_assessed", "unresolved"}, "BATCH_FORM", 'This batch step confirms the family. Use type review for a detailed leaf decision.')
            _require(isinstance(row.get("pending_steps", ""), str), "BATCH_PENDING", 'The follow-up must be text.')
            before = {x["local_type_id"] for x in state["memberships"] if row["item_id"] in x["member_ids"]}
            prior_assignment = batch["assignments"].get(row["item_id"])
            same_family = disposition in ASSIGNED_DISPOSITIONS and any(
                x["local_type_id"] == family and row["item_id"] in x["member_ids"] for x in state["memberships"])
            for member in state["memberships"]:
                if same_family and member["parent_type_id"] == family:
                    continue  # Family-only review does not silently erase a supported leaf assignment.
                if row["item_id"] in member["member_ids"]:
                    member["member_ids"].remove(row["item_id"])
                if member["local_type_id"] == family:
                    member["member_ids"].append(row["item_id"])
                    if ident not in member["supporting_decision_ids"]:
                        member["supporting_decision_ids"].append(ident)
            saved = dict(row, family_id=family, record_id=ident, review_id=review["review_id"],
                         page_id=page["page_id"], slot=page["item_ids"].index(row["item_id"]) + 1,
                         stage=review["stage"], review_origin=review.get("review_origin", review["stage"]),
                         decision_protocol_version=batch.get("protocol_version", "0.4"),
                         source_group_id=review.get("group_id"),
                         source_family_id=review.get("family_id"),
                         family_origin_id=review.get("family_id") if review["stage"] == "family" else (prior_assignment or {}).get("family_origin_id") if review["stage"] == "pool" else None,
                         organization_id=review["organization_id"], input_fingerprint=state["contract"]["input_fingerprint"],
                         previous_type_ids=sorted(before), previous_disposition=copy.deepcopy(batch["assignments"].get(row["item_id"])),
                         recorded_at=record["recorded_at"], saved_at_revision=state["revision"] + 1,
                         catalogue_revision_before=state["catalogue_revision"],
                         catalogue_revision_after=state["catalogue_revision"] + 1,
                         input_versions=copy.deepcopy(state["contract"]["input_versions"]),
                         operator_id=state["contract"]["operator_id"], source=record["source"], experiment_enabled=False,
                         form_status=row.get("form_status", "not_assessed"))
            if state["contract"].get("schema_version") == "0.3.0":
                from .annotation_project_contract import contract_metadata
                identity = contract_metadata(state["contract"])
                saved.update({key: identity[key] for key in ("schema_version", "operator_id", "record_kind", "source",
                                                            "project_id", "run_id", "contract_sha256")})
                saved["project_purpose"] = identity["purpose"]
            # Avoid recursively copying the entire lifetime of a repeatedly reassigned item.
            if saved["previous_disposition"]:
                saved["previous_disposition"].pop("previous_disposition", None)
            if same_family:
                effective_ids = store.effective_decision_ids(state)
                prior_refinement = (prior_assignment or {}).get("legacy_refinement")
                refinement = inherited_legacy_refinement(state, row["item_id"], family, effective_ids, prior_refinement)
                if refinement:
                    saved["legacy_refinement"] = copy.deepcopy(refinement)
            review["decisions"][row["item_id"]] = saved
            review.get("carried_assignment_record_ids", {}).pop(row["item_id"], None)
            batch["assignments"][row["item_id"]] = saved
            batch["legacy_dirty_ids"] = [i for i in batch.get("legacy_dirty_ids", []) if i != row["item_id"]]
        if "expected_item_versions" in review:
            review["expected_item_versions"].update({i: item_version(state, i) for i in ids})
        if review["stage"] == "family":
            review["expected_family_members"] = family_members(state, review["family_id"])
        state["catalogue_revision"] += 1
        if review.get("directed_issue_id"):
            from .annotation_final_review import sync_directed_results
            sync_directed_results(state, review)
        record["saved_at_revision"] = state["revision"] + 1
        record["catalogue_revision_after"] = state["catalogue_revision"]
        record["clock_status"] = state["clock"]["status"]
        record["engineering_elapsed_seconds"] = state["clock"]["elapsed_seconds"]
        record["timing_complete"] = state["clock"]["timing_complete"] and state["clock"]["status"] == "running"
    elif action == "close":
        review = _current_review(batch, record["review_id"])
        _require(review["status"] == "ready" and not review["closed"], "BATCH_REVIEW_CLOSED", 'The round is not ready or has already closed.')
        require_fresh_review(state, review)
        _require(set(record["checks"]) == CLOSE_CHECKS and all(v is True for v in record["checks"].values()),
                 "BATCH_CLOSE_CHECKS", 'Confirm coverage, comparisons and relationships, and that no required follow-up remains.')
        current = current_pool_item_state(state, review) if current_pool_review(review) else None
        required = current["required_item_ids"] if current is not None else review["item_ids"]
        covered = current["covered_item_ids"] if current is not None else list(review["decisions"])
        _require(set(required) <= set(covered) if current is not None else set(required) == set(covered),
                 "BATCH_REVIEW_COVERAGE", 'Every valid frozen target needs a current outcome. Refresh conflicting items before reviewing them.')
        _require(not any(review["decisions"][i].get("pending_steps", "").strip() or review["decisions"][i].get("boundary_flag") for i in required),
                 "BATCH_PENDING", 'Boundary checks or other required steps are still pending.')
        _text(record["note"], "Record what you concluded from this round's comparisons and relationship checks.")
        if review.get("directed_issue_id"):
            from .annotation_final_review import close_directed_review
            close_directed_review(store, state, review, record)
        review.update(closed=True, closed_at=record["recorded_at"], closure_record_id=ident,
                      closure_note=record["note"], closure_checks=record["checks"])
        if current is not None:
            record["withdrawn_item_ids"] = list(current["withdrawn_item_ids"])
            record["covered_item_ids"] = list(current["covered_item_ids"])
            record["new_pool_item_ids_not_in_this_round"] = list(current["new_pool_item_ids"])
            review.update(closed_target_ids=list(required), withdrawn_item_ids_at_close=list(current["withdrawn_item_ids"]))
    elif action == "archive_stale":
        review = _current_review(batch, record["review_id"])
        _require(not review["closed"] and review["status"] not in ACTIVE,
                 "BATCH_ARCHIVE_STATE", 'Only an open, stale round whose calculation has stopped can be archived.')
        stale = review_stale_reason(state, review)
        _require(bool(stale), "BATCH_NOT_STALE", 'This round is still valid. Continue it or finish it through the normal review.')
        _text(record.get("reason"), 'After archiving the stale round, specify the current scope that needs checking.')
        record["detected_stale_reason"] = stale
        review.update(archived=True, archived_at=record["recorded_at"], archive_record_id=ident,
                      archive_reason=record["reason"], archive_stale_reason=stale)
        if review["stage"] == "family":
            batch["active_family_reviews"].pop(review["family_id"], None)
        elif review["stage"] == "first":
            batch["active_reviews"].pop(review["group_id"], None)
        else:
            batch["latest_pool_review_id"] = None
    elif action == "reopen":
        old = _current_review(batch, record["parent_review_id"])
        _require(old["closed"], "BATCH_NOT_CLOSED", 'Continue the open round.')
        new = record["review"]
        batch["reviews"][new["review_id"]] = new
        if new["stage"] == "first":
            batch["active_reviews"][new["group_id"]] = new["review_id"]
            if not current_pool_review(batch["reviews"].get(batch.get("latest_pool_review_id"), {})):
                batch["latest_pool_review_id"] = None
        else:
            batch["latest_pool_review_id"] = new["review_id"]
    else:
        raise ValueError("Unsupported batch reducer action: " + action)
    if (new_protocol(batch) or record.get("submission_contract_version") == COMPACT_SUBMISSION_VERSION
            or record.get("directed_summary") or record.get("review", {}).get("directed_context")
            or current_pool_review(record.get("review", {})) or (action == "close" and current_pool_review(review))):
        payload["record"] = copy.deepcopy(record)
    batch["history"].append(record)
    state["catalogue_status"] = "working"
    state["current_completion_id"] = None


class BatchReviewService:
    def __init__(self, service):
        self.service = service
        self.preparation_code_identity = {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
            for name in ("annotation_batch_acoustics.py", "annotation_acoustics.py", "ei_b_assistance.py")}

    def _preparation_binding(self, state, review, item_ids):
        batch = state.get("batch_review", blank())
        family = review.get("family_id")
        directed = review.get("directed_context")
        from .annotation_final_review import _definition_versions
        result = {"policy_version": EXCEPTION_POLICY, "stage": review["stage"],
            "protocol_version": batch.get("protocol_version", "0.4"),
            "organization_id": batch.get("active_organization_id"),
            "group_id": review.get("group_id"), "family_id": family,
            "review_origin": review.get("review_origin"),
            "item_ids": sorted(item_ids), "fit_ids": sorted(item_ids),
            "input_fingerprint": self.service.dataset.input_fingerprint,
            "input_versions": copy.deepcopy(self.service.dataset.input_versions),
            "local_preset": copy.deepcopy(self.service.dataset.preset["local"]),
            "code_identity": self.preparation_code_identity,
            "item_versions": {i: item_version(state, i) for i in sorted(item_ids)},
            "family_definition": family_definition_fingerprint(state, family) if family else None,
            "directed_definitions": _definition_versions(state, directed["affected_type_ids"]) if directed else None}
        if review.get("family_audition_policy"):
            result.update(family_audition_policy=review["family_audition_policy"],
                family_acoustic_code_sha256=hashlib.sha256(Path(__file__).with_name("annotation_family_acoustics.py").read_bytes()).hexdigest())
        if review.get("pool_audition_policy"):
            result.update(pool_audition_policy=review["pool_audition_policy"],
                pool_acoustic_code_sha256=hashlib.sha256(Path(__file__).with_name("annotation_family_acoustics.py").read_bytes()).hexdigest())
        if current_pool_review(review):
            result["pool_mode"] = "current_pool"
        return result

    def snapshot(self, state):
        batch = copy.deepcopy(state.get("batch_review", blank()))
        batch["organization_capabilities"] = {"version": ORGANIZATION_INTERFACE, "automatic_recommendation": True,
            "optional_operator_note": True, "reorganization_confirmation": True}
        batch["submission_capabilities"] = {"version": "family-disposition-capabilities-v1",
            "legacy_action": "batch_submit", "compact_action": "batch_submit_v2",
            "submission_contract_version": COMPACT_SUBMISSION_VERSION,
            "available_actions": ["batch_submit", "batch_submit_v2"],
            "reason_codes": {kind: [{"code": code, "label": label} for code, label in codes.items()]
                             for kind, codes in COMPACT_REASON_CODES.items()}}
        batch["exception_policy_version"] = EXCEPTION_POLICY
        batch.setdefault("active_family_reviews", {})
        batch["organization"] = current_organization(batch)
        ids = active_pool(batch)
        entries = pool_entries(batch, state)
        source_history = pool_sources(batch)
        complete = first_pass_complete(batch)
        family_pending = pending_family_reviews(batch)
        batch["pool"] = {"active_ids": ids, "entries": entries, "source_history": source_history,
                         "counts": {kind: sum(x["state"] == kind for x in entries.values())
                                    for kind in ("algorithmic_unreviewed", "human_unresolved", "pooled_reviewed_unresolved", "pending_required_checks", "directed_reviewed_unresolved")},
                         "algorithmic_unclustered_ids": sorted(_algorithmic_ids(batch)), "first_pass_complete": complete,
                         "can_prepare": complete and not family_pending and not any(r["status"] in ACTIVE for r in batch["reviews"].values()),
                         "pending_family_review_ids": [r["review_id"] for r in family_pending],
                         "latest_review_id": batch.get("latest_pool_review_id"),
                         "stage_complete": pool_stage_complete(batch, state)}
        batch["pool"]["can_prepare_current_pool"] = bool(ids and batch["organization"] and
            not any(r["status"] in ACTIVE for r in batch["reviews"].values())
            and not state["deadline_locked"] and not self.service.is_draining)
        algorithmic_ids = _algorithmic_ids(batch)
        rows = [r for r in batch["assignments"].values() if assignment_current(batch, r, algorithmic_ids)]
        batch["counts"] = {"assigned": sum(r["disposition"] in ASSIGNED_DISPOSITIONS for r in rows),
                           "pool": len(ids), "discarded": sum(r["disposition"] == "discard" for r in rows),
                           "unreviewed": len(state["contract"]["allowed_item_ids"]) - len(rows)}
        batch["families"] = [{"family_id": x["local_type_id"], "member_count": len(x["member_ids"]),
                              "member_ids": sorted(x["member_ids"]),
                              "member_fingerprint": family_member_fingerprint(state, x["local_type_id"]),
                              "definition_fingerprint": family_definition_fingerprint(state, x["local_type_id"]),
                              "active_review_id": batch["active_family_reviews"].get(x["local_type_id"]),
                              "title": family_title(state, x["local_type_id"]),
                              "diagnostic_body": state.get("type_cards", {}).get(x["local_type_id"], {}).get("diagnostic_body", "")}
                             for x in state["memberships"] if x["parent_type_id"] is None]
        busy = any(r["status"] in ACTIVE for r in batch["reviews"].values())
        batch["upgrade"] = {"available": not new_protocol(batch) and not busy and not state["deadline_locked"],
                            "target_protocol_version": "0.5",
                            "blocked_reason": 'Protocol 0.5 is already in use.' if new_protocol(batch) else 'The task deadline has passed.' if state["deadline_locked"] else 'Wait for the current batch calculation to finish or stop it first.' if busy else None}
        for review in batch["reviews"].values():
            retired = _retired_noise_review(batch, review)
            if review["stage"] == "family":
                obsolete = batch.get("active_family_reviews", {}).get(review["family_id"]) != review["review_id"]
            else:
                obsolete = review.get("organization_id") != batch.get("active_organization_id")
                if review["stage"] == "first":
                    obsolete |= batch["active_reviews"].get(review["group_id"]) != review["review_id"]
                else:
                    obsolete |= batch.get("latest_pool_review_id") != review["review_id"]
            review.update(read_only=retired or obsolete, superseded_by_protocol_upgrade=retired,
                          read_only_reason='The protocol upgrade retired the earlier unclustered first-pass round.' if retired else 'A newer grouping or review exists. This record is available as history.' if obsolete else None)
            reason = review_stale_reason(state, review)
            review.update(scope_stale=bool(reason), scope_stale_reason=reason)
            if current_pool_review(review):
                review.update(current_pool_item_state(state, review))
            review["compact_submit_allowed"] = (_compact_review_scope(review) and review.get("status") == "ready"
                and not review.get("closed") and not review["read_only"] and not reason and not state["deadline_locked"])
            if not review["read_only"]:
                current_ids = (next((f["member_ids"] for f in batch["families"] if f["family_id"] == review["family_id"]), []) if review["stage"] == "family" else
                    active_pool(batch) if review["stage"] == "pool" else
                    next((g["item_ids"] for g in (batch["organization"] or {}).get("groups", [])
                          if g["group_id"] == review.get("group_id")), []))
                binding = self._preparation_binding(state, review, current_ids)
                review["direct_exception"] = dict(direct_exception_option(batch, review, binding),
                    current_binding_sha256=canonical_hash(binding))
            if review["stage"] == "family":
                members = next((f["member_ids"] for f in batch["families"] if f["family_id"] == review["family_id"]), [])
                review["current_family_member_count"] = len(members)
                review["members_changed_since_preparation"] = members != review["item_ids"]
        return batch

    def _system(self, review_id, **changes):
        s = self.service
        with s._lock:
            state = s.store.snapshot(s.run_id)
            record = {"record_id": "batch-system-" + uuid.uuid4().hex, "review_id": review_id,
                      "source": s.source, "experiment_enabled": False, "recorded_at": utc(),
                      "after_deadline": state["deadline_locked"], **changes}
            record.update(s.record_metadata())
            return s.store.transact(s.run_id, expected_revision=state["revision"],
                idempotency_key=record["record_id"], operation="record_job",
                payload={"batch_review_event": True, "action": "compute", "record": record})

    def recover(self):
        for key, row in self.service.store.snapshot(self.service.run_id).get("batch_review", {}).get("reviews", {}).items():
            if row["status"] in ACTIVE:
                self._system(key, status="failed", ended_at=utc(),
                             error={"code": "PROCESS_INTERRUPTED", "message": 'The previous process exited. Prepare the round again to continue.'})

    def _worker(self, review_id, event):
        from .annotation_batch_acoustics import prepare_review
        s = self.service
        started = time.monotonic()
        try:
            if event.is_set():
                self._system(review_id, status="cancelled", ended_at=utc())
                return
            self._system(review_id, status="running", started_at=utc())
            with s._lock:
                review = copy.deepcopy(s.store.snapshot(s.run_id)["batch_review"]["reviews"][review_id])
            folder = s.work_dir / "batch_reviews" / review_id
            result = prepare_review(s.dataset, review["item_ids"], folder, review["route"],
                                    review["gap_seconds"], review["normalization"], check_cancelled=event.is_set)
            with s._lock:
                if event.is_set():
                    self._system(review_id, status="cancelled", ended_at=utc(), compute_wall_seconds=time.monotonic()-started)
                    return
                pages = copy.deepcopy(result["pages"])
                for page in pages:
                    for path_key, url_key in (("audio_path", "audio_url"), ("waveform_path", "waveform_url"),
                                              ("spectrogram_path", "spectrogram_url")):
                        relative = page.pop(path_key)
                        path = (folder / relative).resolve()
                        require(path.is_relative_to(folder.resolve()), "ASSET_SCOPE", "Concatenated media must be stored in this round's directory.")
                        checksum = result.get("artifacts", {}).get(relative, {}).get("sha256")
                        require(isinstance(checksum, str), "BATCH_ASSET_RECEIPT", 'The concatenated media is missing its generation receipt hash.')
                        page[url_key] = s._asset(path, checksum)
                metadata = {k: copy.deepcopy(v) for k, v in result.items() if k not in {"pages", "provenance"}}
                self._system(review_id, status="ready", pages=pages, result=metadata, ended_at=utc(),
                             compute_wall_seconds=time.monotonic()-started)
        except Exception as exc:
            with s._lock:
                row = s.store.snapshot(s.run_id).get("batch_review", {}).get("reviews", {}).get(review_id)
                if row and row["status"] in ACTIVE:
                    failure = {}
                    code = getattr(exc, "code", "BATCH_COMPUTATION_FAILED")
                    if (not event.is_set() and code not in {"CANCELLED", "INPUT_CHANGED", "ASSET_CHANGED", "SOURCE_CHANGED"}
                            and getattr(exc, "batch_failure_phase", None) == "clustering"
                            and row.get("route") in {"cluster", "family_cluster", "pool_cluster"} and row.get("preparation_binding")):
                        failure["clustering_failure"] = {"policy_version": EXCEPTION_POLICY,
                            "binding_sha256": canonical_hash(row["preparation_binding"]),
                            "error_code": code, "error_message": str(exc), "recorded_at": utc()}
                    self._system(review_id, status="cancelled" if event.is_set() else "failed", ended_at=utc(),
                                 compute_wall_seconds=time.monotonic()-started,
                                 error={"code": code, "message": str(exc)}, **failure)

    @staticmethod
    def _fields(payload, keys):
        require(set(payload) <= set(keys), "REQUEST_FIELDS", 'The request includes fields this action does not support.')

    def execute(self, action, payload, *, expected_revision, idempotency_key):
        s = self.service
        require(action in BATCH_ACTIONS and isinstance(payload, dict), "BATCH_ACTION", 'Unknown batch review action.')
        require(isinstance(idempotency_key, str) and 0 < len(idempotency_key) <= 160, "IDEMPOTENCY_REQUIRED", 'The request ID is missing.')
        if action in {"batch_prepare", "batch_prepare_pool", "batch_prepare_family"}:
            gap = payload.get("gap_seconds")
            require(type(gap) in {int, float} and math.isfinite(gap) and .05 <= gap <= 2,
                    "BATCH_GAP", 'Set the silence between clips to a value between 0.05 and 2 seconds.')
        request_hash = canonical_hash({"action": action, "payload": payload, "expected_revision": expected_revision})
        with s._lock:
            prior = s.store.lookup_event(s.run_id, idempotency_key)
            if prior:
                require(prior["payload"].get("service_request_hash") == request_hash, "IDEMPOTENCY_CONFLICT", 'This request ID has already been used for a different action.')
                return {"receipt": prior["receipt"], "record": prior["payload"].get("record"), "replayed": True}
            s.command_guard(action, payload)
            state = s.store.snapshot(s.run_id)
            valid_revision = type(expected_revision) is int and (
                0 <= expected_revision <= state["revision"] if action in {"batch_cancel", "batch_preview"} else expected_revision == state["revision"])
            require(valid_revision, "REVISION_CONFLICT", 'The state has changed. Refresh and check the submission.')
            batch = state.get("batch_review", blank())
            ident = "batch-" + hashlib.sha256((s.run_id+":"+idempotency_key).encode()).hexdigest()[:24]
            record = {"record_id": ident, "source": s.source, "experiment_enabled": False,
                      "recorded_at": utc(), "operator_id": state["contract"]["operator_id"]}
            record.update(s.record_metadata())
            reducer = action.removeprefix("batch_")
            start_job = None
            if action in {"batch_preview", "batch_apply"}:
                self._fields(payload, {"mode", "height", "reason", "note", "preview_fingerprint", "confirmation"} if action == "batch_apply" else {"mode", "height"})
                preview = organization_preview(s.dataset_dir, s.global_candidates_data, state,
                                               payload.get("mode", "auto"), payload.get("height"))
                if action == "batch_preview":
                    return {"preview": preview, "ward": preview.get("ward", {})}
                require(not any(r["status"] in ACTIVE for r in batch["reviews"].values()),
                        "BATCH_BUSY", 'Wait for the grouping job to finish or cancel it first.')
                if preview["requested_mode"] in {"fine", "coarse"}:
                    require("preview_fingerprint" in payload, "ORGANIZATION_PREVIEW_REQUIRED",
                            "Preview the selected clustering preset before applying it.")
                    require(not same_organization_partition(current_organization(batch), preview),
                            "ORGANIZATION_ALREADY_APPLIED", "This grouping already matches the current review.")
                require(not ("reason" in payload and "note" in payload), "ORGANIZATION_NOTE", 'Specify the note only once.')
                note = payload.get("note", payload.get("reason", ""))
                require(isinstance(note, str) and len(note) <= 4000, "ORGANIZATION_NOTE", 'The note may be empty and must be no longer than 4,000 characters.')
                if "preview_fingerprint" in payload:
                    require(payload["preview_fingerprint"] == preview["preview_fingerprint"],
                            "ORGANIZATION_PREVIEW_CHANGED", 'The preview inputs or manual records changed. Inspect the current preview.')
                confirmation = payload.get("confirmation")
                if confirmation is not None:
                    require(isinstance(confirmation, dict) and set(confirmation) == {"acknowledged", "impact_fingerprint"}
                            and confirmation["acknowledged"] is True
                            and confirmation["impact_fingerprint"] == preview["impact"]["fingerprint"],
                            "ORGANIZATION_CONFIRMATION", 'The impact confirmation no longer matches the records. Review the impact again.')
                require(not preview["impact"]["requires_confirmation"] or confirmation is not None,
                        "ORGANIZATION_CONFIRMATION", 'This project has review media or manual decisions. Inspect and confirm the impact of regrouping.')
                summary = organization_system_summary(preview)
                record.update(reason=note.strip() or summary,
                    reason_source="operator_note" if note.strip() else "system_operation_summary",
                    operator_note=note.strip(), system_summary=summary,
                    organization_interface_version=ORGANIZATION_INTERFACE,
                    impact_confirmation=copy.deepcopy(confirmation), organization={**preview, "organization_id": ident,
                    "operation_audit": {"system_summary": summary, "operator_note": note.strip(),
                        "summary_source": "system_operation_summary", "operator_id": record["operator_id"],
                        "recorded_at": record["recorded_at"], "interface_version": ORGANIZATION_INTERFACE},
                    "input_fingerprint": s.dataset.input_fingerprint, "created_at": record["recorded_at"]})
            elif action == "batch_upgrade":
                self._fields(payload, {"target_protocol_version", "reason"})
                require(not new_protocol(batch), "BATCH_ALREADY_UPGRADED", 'This task already uses protocol 0.5.')
                require(payload.get("target_protocol_version") == "0.5", "BATCH_PROTOCOL_VERSION", 'Only an upgrade to protocol 0.5 is supported.')
                _text(payload.get("reason"), 'Give the reason for the upgrade. Existing decisions will be retained.')
                record.update(payload)
            elif action == "family_rename":
                self._fields(payload, {"family_id", "title", "expected_title"})
                title, previous = _family_rename(state, payload.get("family_id"), payload.get("title"),
                    expected_title=payload.get("expected_title"), check_expected="expected_title" in payload)
                record.update(action="family_rename", family_id=payload["family_id"], title=title, previous_title=previous)
            elif action == "batch_family":
                self._fields(payload, {"family_id", "title", "diagnostic_body", "reason"})
                for key in ("title", "diagnostic_body", "reason"):
                    _text(payload.get(key), 'Enter a family name, diagnostic criteria and the basis for creating it.')
                record.update(payload, family_id=payload.get("family_id") or "F-"+ident[-10:])
            elif action in {"batch_prepare", "batch_prepare_pool", "batch_prepare_family"}:
                allowed = {"group_id", "route", "gap_seconds", "normalization", "exception_reason", "reopen_reason", "exception_source_review_id"}
                if action == "batch_prepare_pool":
                    allowed |= {"pool_mode"}
                if action == "batch_prepare_family":
                    allowed = (allowed - {"group_id"}) | {"family_id", "expected_member_fingerprint", "expected_definition_fingerprint", "reason", "review_origin", "directed_context"}
                self._fields(payload, allowed)
                require("pool_mode" not in payload or payload["pool_mode"] == "current_pool",
                        "BATCH_POOL_MODE", 'Select a current-pool mode.')
                require("pool_mode" not in payload or payload.get("route") in {"pool_cluster", "direct_exception"},
                        "BATCH_POOL_MODE", 'Current-pool mode requires ordinary pool clustering or a justified direct-audition exception.')
                org = current_organization(batch)
                stage = "family" if action == "batch_prepare_family" else "pool" if action == "batch_prepare_pool" else "first"
                family_id = None
                if stage == "family":
                    family_id = payload.get("family_id")
                    require(isinstance(family_id, str), "BATCH_TARGET_FAMILY", 'Select a human family.')
                    ids, group_id = family_members(state, family_id), None
                    require(bool(ids), "BATCH_FAMILY_EMPTY", 'This family has no members to prepare for review.')
                    require(payload.get("expected_member_fingerprint") == family_member_fingerprint(state, family_id),
                            "BATCH_FAMILY_CHANGED", 'Family membership has changed. Refresh and check its current members.')
                    if "expected_definition_fingerprint" in payload:
                        require(payload["expected_definition_fingerprint"] == family_definition_fingerprint(state, family_id),
                                "DIRECTED_DEFINITION_CHANGED", 'The type definition changed. Refresh and review it.')
                    _text(payload.get("reason"), 'State the purpose or basis for reviewing this human family.')
                    previous = batch["reviews"].get(batch.get("active_family_reviews", {}).get(family_id))
                    stale = bool(previous and review_stale_reason(state, previous))
                    require(not previous or previous["status"] != "ready" or previous.get("closed") or stale,
                            "BATCH_FAMILY_IN_PROGRESS", 'This family has a valid open round. Continue that round.')
                    if previous and stale:
                        _text(payload.get("reopen_reason"), 'The members or criteria changed. Give a reason for freezing a new scope.')
                    reject_review_overlap(state, ids, previous["review_id"] if previous else None,
                        allow_current_pool_overlap=payload.get("review_origin", "family_recheck") == "family_recheck"
                            and payload.get("directed_context") is None)
                else:
                    require(org is not None, "BATCH_ORGANIZATION", 'Apply candidate aggregation or explicitly choose to keep the original groups.')
                    if stage == "first":
                        groups = {g["group_id"]: g for g in org["groups"]}
                        require(payload.get("group_id") in groups, "BATCH_GROUP", 'Select a current candidate group.')
                        group = groups[payload["group_id"]]
                        ids, group_id = group["item_ids"], group["group_id"]
                        previous = batch["reviews"].get(batch["active_reviews"].get(group_id))
                    else:
                        ids, group_id = active_pool(batch), None
                        previous = batch["reviews"].get(batch.get("latest_pool_review_id"))
                        current_pool_request = (payload.get("pool_mode") == "current_pool" or
                            payload.get("route") == "direct_exception" and current_pool_review(previous or {}))
                        if not current_pool_request or not ids:
                            require(first_pass_complete(batch), "BATCH_FIRST_PASS_PENDING", 'Finish all first-pass groups before preparing this pool.')
                            require(not pending_family_reviews(batch), "BATCH_FAMILY_PENDING", 'Finish the active human-family reviews before freezing the whole pool.')
                        changed = bool(previous and (set(ids) != set(previous["item_ids"]) or any(
                            item_version(state, i) != token for i, token in previous.get("expected_item_versions", {}).items())))
                        replacing = bool(current_pool_request and previous and
                            (changed or not current_pool_review(previous)))
                        if replacing and previous["status"] == "ready" and not previous.get("closed"):
                            _text(payload.get("reopen_reason"), 'Pool membership or its version changed. Give a reason for preparing a new round.')
                        require(not previous or previous["status"] != "ready" or previous.get("closed") or replacing,
                                "BATCH_POOL_IN_PROGRESS", 'Review all targets in the current frozen pool and finish the round before preparing another.')
                    if stage != "pool" or not current_pool_request:
                        reject_review_overlap(state, ids, previous["review_id"] if previous else None,
                                              allow_current_pool_overlap=stage == "first")
                route = payload.get("route", "cluster" if stage == "pool" else None)
                require(route in {"direct", "cluster", "direct_exception", "family_cluster", "pool_cluster"}, "BATCH_ROUTE", 'Choose how to organize this round.')
                if previous and (previous.get("closed") or previous.get("decisions")):
                    _text(payload.get("reopen_reason"), 'A previous round has closed. Give the new evidence or reason for reopening it.')
                if route == "pool_cluster":
                    require(stage == "pool", "BATCH_POOL_ROUTE", 'This clustering method requires the complete global pool.')
                elif route == "family_cluster":
                    require(stage == "family" and payload.get("review_origin", "family_recheck") == "family_recheck"
                            and payload.get("directed_context") is None,
                            "BATCH_FAMILY_ROUTE", 'This clustering method requires the full scope of an ordinary human-family review.')
                elif route == "direct_exception":
                    _text(payload.get("exception_reason"), 'For direct audition, explain why the calculation failed or was inapplicable.')
                elif stage == "pool":
                    require(route == "cluster", "BATCH_POOL_ROUTE", 'Cluster the complete pool first. After a recorded failure you can request direct audition.')
                elif len(ids) < 30 or (new_protocol(batch) and len(ids) == 30):
                    require(route == "direct", "BATCH_ROUTE", 'Use direct concatenation for 30 items or fewer.' if new_protocol(batch) else 'Use direct audition for fewer than 30 items.')
                elif len(ids) > 30:
                    require(route == "cluster", "BATCH_ROUTE", 'Cluster within the group before audition for more than 30 items.')
                gap = payload.get("gap_seconds")
                require(type(gap) in {int, float} and math.isfinite(gap) and .05 <= gap <= 2,
                        "BATCH_GAP", 'Set the silence between clips to a value between 0.05 and 2 seconds.')
                normalization = payload.get("normalization")
                require(normalization in {"per_item", "whole_page"}, "BATCH_NORMALIZATION", 'Choose per-syllable or whole-page spectrogram normalization.')
                origin = payload.get("review_origin", "family_recheck") if stage == "family" else "pooled_review" if stage == "pool" else "initial_family_review"
                require(origin in {"initial_family_review", "pooled_review", "family_recheck", "directed_recheck"}, "BATCH_REVIEW_ORIGIN", 'Invalid review origin.')
                if stage == "family":
                    require(origin in {"family_recheck", "directed_recheck"}, "BATCH_REVIEW_ORIGIN", 'A family review must use family_recheck or directed_recheck.')
                sources = pool_sources(batch)
                source_availability = {i: {"source_clip_id": s.dataset._items[i]["source_clip_id"],
                    "context_kind": s.dataset._items[i].get("context_kind", "unspecified"),
                    "parent_context_available": (s.dataset._items[i]["context_kind"] == "source_parent") if "context_kind" in s.dataset._items[i] else None,
                    "independent_recording_identity_verified": False} for i in ids}
                review = {"review_id": ident, "review_origin": origin,
                          "protocol_version": batch.get("protocol_version", "0.4"),
                          "source_availability": source_availability, "organization_id": org["organization_id"] if org else None, "group_id": group_id,
                          "stage": stage, "item_ids": list(ids), "route": route, "gap_seconds": gap,
                          "normalization": normalization, "status": "queued" if ids else "ready",
                          "closed": not bool(ids), "pages": [], "decisions": {},
                          "created_at": record["recorded_at"], "parent_review_id": previous["review_id"] if previous else None,
                          "reopen_reason": payload.get("reopen_reason", ""),
                          "exception_reason": payload.get("exception_reason", ""),
                          "input_fingerprint": s.dataset.input_fingerprint,
                          "pool_sources": {i: {"sources": copy.deepcopy(sources.get(i, [])),
                                                   "prior_assignment": copy.deepcopy(batch["assignments"].get(i))}
                                           for i in ids} if stage == "pool" else {},
                          "empty_pool_skip": stage == "pool" and not bool(ids),
                          "expected_item_versions": {i: item_version(state, i) for i in ids}}
                if stage == "family":
                    review.update(family_id=family_id, reason=payload["reason"],
                                  family_member_fingerprint=family_member_fingerprint(state, family_id),
                                  family_definition_fingerprint=family_definition_fingerprint(state, family_id),
                                  catalogue_revision_at_preparation=state["catalogue_revision"],
                                  expected_family_members=list(ids),
                                  family_sources={i: copy.deepcopy(batch["assignments"].get(i)) for i in ids})
                    if route == "family_cluster" or (route == "direct_exception" and previous and previous.get("family_audition_policy")):
                        from .annotation_family_acoustics import FAMILY_CLUSTER_POLICY
                        review["family_audition_policy"] = FAMILY_CLUSTER_POLICY
                if stage == "pool" and (route == "pool_cluster" or
                        route == "direct_exception" and previous and previous.get("pool_audition_policy")):
                    from .annotation_family_acoustics import POOL_CLUSTER_POLICY
                    review["pool_audition_policy"] = POOL_CLUSTER_POLICY
                if stage == "pool" and current_pool_request:
                    review["pool_mode"] = "current_pool"
                if origin == "directed_recheck":
                    from .annotation_final_review import prepare_directed_context
                    require(payload.get("expected_definition_fingerprint") == family_definition_fingerprint(state, family_id),
                            "DIRECTED_DEFINITION_CHANGED", 'The type criteria changed. Refresh and check the current definition.')
                    review["directed_context"] = prepare_directed_context(state, review, payload.get("directed_context"))
                    if previous and previous.get("directed_issue_id") and not review["directed_context"].get("parent_issue_id"):
                        review["directed_context"]["parent_issue_id"] = previous["directed_issue_id"]
                    review.update(directed_issue_id="issue-" + ident, return_view="final")
                elif payload.get("directed_context") is not None:
                    require(False, "DIRECTED_CONTEXT", 'Directed-review context requires an explicit directed review.')
                review["preparation_binding"] = self._preparation_binding(state, review, ids)
                if route == "direct_exception":
                    option = direct_exception_option(batch, previous, review["preparation_binding"])
                    require(option["allowed"], "BATCH_EXCEPTION", option["message"])
                    require(payload.get("exception_source_review_id", option["source_review_id"]) == option["source_review_id"],
                            "BATCH_EXCEPTION", 'The failure record supporting direct audition changed. Refresh and check it.')
                    review["exception_evidence"] = {k: copy.deepcopy(v) for k, v in option.items() if k not in {"allowed", "message"}}
                else:
                    require(payload.get("exception_source_review_id") is None, "BATCH_EXCEPTION", 'The ordinary review method does not accept an exception failure record.')
                record["review"] = review
                reducer = "prepare"
                if ids:
                    start_job = ident
            elif action in {"batch_submit", "batch_submit_v2"}:
                self._fields(payload, {"review_id", "page_id", "current_family_id", "items", "new_families"})
                record.update(copy.deepcopy(payload))
                if action == "batch_submit_v2":
                    record["submission_contract_version"] = COMPACT_SUBMISSION_VERSION
                    reducer = "submit"
            elif action == "batch_close":
                self._fields(payload, {"review_id", "checks", "note", "directed_summary"})
                require(isinstance(payload.get("checks"), dict), "BATCH_CLOSE_CHECKS", 'Confirm the checks for finishing this round.')
                record.update(payload)
            elif action == "batch_archive_stale":
                self._fields(payload, {"review_id", "reason"})
                record.update(payload)
            elif action == "batch_cancel":
                self._fields(payload, {"review_id"})
                old = _current_review(batch, payload.get("review_id"))
                require(old["status"] in {"queued", "running"}, "BATCH_JOB_STATE", 'The job has finished or is being cancelled.')
                reducer = "compute"
                record.update(review_id=old["review_id"], status="cancel_requested",
                              requested_state_revision=expected_revision, applied_state_revision=state["revision"])
            elif action == "batch_reopen":
                self._fields(payload, {"review_id", "reason"})
                old = _current_review(batch, payload.get("review_id"))
                require(old["stage"] != "family", "BATCH_FAMILY_REPREPARE", 'Prepare the family review with its current members. Old pages contain items that have moved.')
                require(not current_pool_review(old), "BATCH_POOL_REPREPARE", 'Prepare a new round with the complete current pool instead of using the old pool pages.')
                require(old["closed"], "BATCH_NOT_CLOSED", 'Continue the unfinished round.')
                _text(payload.get("reason"), 'Describe the new evidence, error or change in criteria.')
                reject_review_overlap(state, old["item_ids"], old["review_id"], allow_current_pool_overlap=old["stage"] == "first")
                new = copy.deepcopy(old)
                new["expected_item_versions"] = {i: item_version(state, i) for i in old["item_ids"]}
                new.update(review_id=ident, closed=False, decisions={}, parent_review_id=old["review_id"],
                           reopened_at=record["recorded_at"], reopen_reason=payload["reason"])
                for key in ("closed_at", "closure_record_id", "closure_note", "closure_checks"):
                    new.pop(key, None)
                record.update(parent_review_id=old["review_id"], reason=payload["reason"], review=new)
            operation = "record_job" if action == "batch_cancel" else "batch_review"
            data = {"action": reducer, "record": record, "service_request_hash": request_hash}
            if action == "batch_cancel":
                data["batch_review_event"] = True
            receipt = s.store.transact(s.run_id, expected_revision=state["revision"] if action == "batch_cancel" else expected_revision,
                                      idempotency_key=idempotency_key, operation=operation, payload=data)
            if start_job:
                event = threading.Event()
                s._cancel[start_job] = event
                try:
                    s._futures[start_job] = s._executor.submit(self._worker, start_job, event)
                except Exception as exc:
                    self._system(start_job, status="failed", ended_at=utc(),
                                 error={"code": "WORKER_START_FAILED", "message": str(exc)})
            if action == "batch_cancel":
                event = s._cancel.get(record["review_id"])
                if event:
                    event.set()
            saved_event = s.store.lookup_event(s.run_id, idempotency_key)
            return {"receipt": receipt, "record": saved_event["payload"].get("record", record)}
