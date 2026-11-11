"""纯人工协议的事务规则与状态投影；字段完整性不能认证声学判断正确。"""
from __future__ import annotations

import copy

from .annotation_state import AnnotationStateStore, _hash, _require

TERMINAL = {"decided", "reviewed_unresolved"}
ACTIVE_JOBS = {"queued", "running", "cancel_requested"}
REVIEW_CHECKS = {"comparisons_completed", "affected_relations_checked", "no_required_steps_pending"}
COMPLETION_CHECKS = {"coverage_confirmed", "type_definitions_reviewed", "quality_checks_passed",
                     "unresolved_reported", "exclusions_reported"}
REOPEN_BASES = {"new_evidence", "input_error", "systematic_error", "reinterpretation", "criteria_change"}
REFRESH_BASIS = "catalogue_revision_changed"
MANUAL_FIELDS = ("type_cards", "type_card_history", "catalogue_scope", "scope_history",
                 "review_status_records", "reopen_records", "completion_records", "current_completion_id",
                 "definition_review_requirements", "definition_review_history", "batch_review")
DEFINITION_FIELDS = ("diagnostic_body", "main_variant_difference", "representative_ids", "atypical_ids",
                     "counterexample_ids", "competing_type_ids", "uncertainties")


def text_required(value, code, message):
    _require(isinstance(value, str) and bool(value.strip()), code, message)


def membership_fingerprint(row):
    return _hash({"local_type_id": row["local_type_id"], "parent_type_id": row["parent_type_id"],
                  "member_ids": sorted(row["member_ids"])})


def definition_content(card):
    return {key: (sorted(card[key]) if isinstance(card[key], list) else " ".join(card[key].split()))
            for key in DEFINITION_FIELDS}


def definition_fingerprint(card):
    return _hash(definition_content(card))


def superseded_targets(state, issue_id):
    return {item for row in state["issues"].values() if row.get("parent_issue_id") == issue_id
            for item in row.get("target_ids", [])}


def issue_projection(state, issue, superseded_index=None):
    children = superseded_targets(state, issue["issue_id"]) if superseded_index is None else superseded_index.get(issue["issue_id"], set())
    covered = set(issue["target_ids"]) & children
    remaining = set(issue["target_ids"]) - covered
    stale = issue["catalogue_revision_at_request"] < state["catalogue_revision"]
    return {"stale_for_current_catalogue": stale, "superseded_target_ids": sorted(covered),
            "remaining_target_ids": sorted(remaining), "is_superseded": bool(covered) and not remaining,
            "effective_status": "superseded" if covered and not remaining else issue["status"],
            "can_reopen": issue["status"] in TERMINAL or (stale and bool(remaining))}


def item_projection(state):
    """沿显式重开谱系投影当前状态；旧归属不冒充本轮重新确认。"""
    result = {}
    issues = state["issues"]
    by_item = {item: {} for item in state["contract"]["allowed_item_ids"]}
    for key, row in issues.items():
        for item in row.get("target_ids", []):
            if item in by_item:
                by_item[item][key] = row
    for item in state["contract"]["allowed_item_ids"]:
        related = by_item[item]
        superseded = {row.get("parent_issue_id") for row in related.values()}
        current = {key: row for key, row in related.items() if key not in superseded}
        values = [row.get("member_processing_states", {}).get(item, "unfinished") for row in current.values()]
        saved_processing = ("unfinished" if not values or "unfinished" in values else
                            "reviewed_unresolved" if "reviewed_unresolved" in values else "decided")
        processing = ("unfinished" if any(row["status"] == "unfinished" or row.get("pending_steps", "").strip()
                                         for row in current.values()) else saved_processing)
        active = {key: row for key, row in current.items()
                  if row["status"] not in TERMINAL or not row.get("closure_confirmed", False)}
        candidates = active or current
        chosen = max(candidates, key=lambda key: (
            candidates[key].get("opened_at_revision", 0),
            candidates[key].get("catalogue_revision_at_request", 0),
            candidates[key].get("revision", 0), key), default=None)
        result[item] = {"processing_state": processing,
                        "saved_processing_state": saved_processing,
                        "review_status": candidates[chosen]["status"] if chosen else "not_reviewed",
                        "issue_ids": sorted(related), "current_issue_ids": sorted(current),
                        "current_issue_id": chosen,
                        "closure_confirmation_required": bool(active)}
    from .annotation_batch_review import batch_projection
    for item, fields in batch_projection(state).items():
        result[item].update(fields)
    return result


def completion_report(state):
    """仅报告记录缺项；限定用途是否可接受须由操作者提交明确确认。"""
    scope = state.get("catalogue_scope")
    ids = scope["scope_ids"] if scope else state["contract"]["allowed_item_ids"]
    projection = item_projection(state)
    counts = {name: sum(projection[i]["processing_state"] == name for i in ids)
              for name in ("decided", "reviewed_unresolved", "unfinished")}
    missing = []
    if scope is None:
        missing.append({"code": "SCOPE_REQUIRED", "message": 'Declare the delivery scope, purpose, classification level and quality requirements first.'})
    unfinished = [i for i in ids if projection[i]["processing_state"] == "unfinished"]
    if unfinished:
        missing.append({"code": "UNFINISHED_ITEMS", "message": 'Some syllables in this scope are still unfinished.', "item_ids": unfinished})
    relevant_issues = {key for i in ids for key in projection[i]["current_issue_ids"]}
    pending = sorted(key for key in relevant_issues if state["issues"][key]["status"] not in TERMINAL
                     or not state["issues"][key].get("closure_confirmed", False))
    if pending:
        missing.append({"code": "OPEN_REVIEWS", "message": 'Some related issues have not been explicitly closed.', "issue_ids": pending})
    jobs = [key for key, row in state["jobs"].items()
            if row.get("issue_id") in relevant_issues and row["status"] in ACTIVE_JOBS]
    if jobs:
        missing.append({"code": "ACTIVE_JOBS", "message": 'A related local calculation is still running.', "job_ids": jobs})
    types = {row["local_type_id"]: row for row in state["memberships"]}
    required_types = [key for key, row in types.items() if set(ids) & set(row["member_ids"])
                      and (not scope or scope["target_level"] == "leaf" or row["parent_type_id"] is None)]
    cards = state.get("type_cards", {})
    absent = [key for key in required_types if key not in cards or cards[key]["definition_status"] != "reviewed"]
    if absent:
        missing.append({"code": "TYPE_DEFINITIONS", "message": 'Some related types lack a confirmed definition card.', "type_ids": absent})
    stale = [key for key in required_types if key in cards and
             (cards[key]["membership_fingerprint"] != membership_fingerprint(types[key]) or
              not set(cards[key]["competing_type_ids"]) <= set(types))]
    if stale:
        missing.append({"code": "STALE_TYPE_DEFINITIONS", "message": 'The type members or competing relationships have changed. Review the definition card.', "type_ids": stale})
    definition_pending = definition_review_gaps(state, ids)
    if definition_pending:
        missing.append({"code": "DEFINITION_REVIEW_REQUIRED",
                        "message": 'The type criteria changed. Reopen the affected items for a criteria change, then finish the review.',
                        "type_ids": sorted(definition_pending),
                        "item_ids": sorted({i for values in definition_pending.values() for i in values})})
    from .annotation_batch_review import batch_completion_gaps
    missing.extend(batch_completion_gaps(state, ids))
    return {"can_submit": not missing, "missing": missing, "counts": counts,
            "definition_review_gaps": copy.deepcopy(definition_pending),
            "scope_id": scope["scope_id"] if scope else None, "scope_ids": list(ids),
            "catalogue_status": state["catalogue_status"],
            "scientific_validity_certified": False}


def definition_review_gaps(state, scope_ids):
    types = {row["local_type_id"] for row in state["memberships"]}
    cards = state.get("type_cards", {})
    result = {}
    for key, requirement in state.get("definition_review_requirements", {}).items():
        if key not in types or key not in cards:
            continue
        missing = []
        for item in set(scope_ids) & set(requirement["affected_item_ids"]):
            matched = False
            for issue in state["issues"].values():
                anchor = issue
                seen = set()
                # 显式目录续办只更新版本；沿父谱系保留原判据变化问题，不伪造新证据。
                while anchor.get("basis") == REFRESH_BASIS and anchor["issue_id"] not in seen:
                    seen.add(anchor["issue_id"])
                    parent = state["issues"].get(anchor.get("parent_issue_id"))
                    if parent is None:
                        break
                    anchor = parent
                if (item not in issue.get("target_ids", []) or not issue.get("closure_confirmed") or
                        issue["status"] not in TERMINAL or anchor.get("basis") != "criteria_change" or
                        key not in issue.get("affected_type_ids", []) or
                        key not in anchor.get("affected_type_ids", []) or
                        anchor.get("opened_at_revision", 0) <= requirement["created_at_revision"]):
                    continue
                confirmation = state.get("review_status_records", {}).get(issue.get("last_review_status_id"), {})
                if confirmation.get("type_definition_fingerprints", {}).get(key) == definition_fingerprint(cards[key]):
                    matched = True
                    break
            if not matched:
                missing.append(item)
        if missing:
            result[key] = sorted(missing)
    return result


class ManualAnnotationStateStore(AnnotationStateStore):
    """复用既有SQLite事务，不迁移或改写历史合同与已提交决定。"""

    def effective_decision_ids(self, state):
        from .annotation_decision_history import DecisionHistory
        with self._lock:
            if not hasattr(self, "_decision_history"):
                self._decision_history = DecisionHistory(self)
            return self._decision_history.identifiers(state)

    @staticmethod
    def _capture(state):
        captured = AnnotationStateStore._capture(state)
        captured.update({key: copy.deepcopy(state[key]) for key in MANUAL_FIELDS if key in state})
        return captured

    def _apply(self, state, operation, payload):
        if operation == "record_job" and payload.get("catalogue_export_event") is True:
            from .annotation_catalogue_export import apply_export
            apply_export(state, payload)
            return
        if operation == "manual_assessment" or (operation == "record_job" and payload.get("assessment_event") is True):
            from .annotation_assessment_service import apply_assessment
            apply_assessment(state, payload)
            return
        # D父议题与冻结批量范围由同一组合事务管理；旧公开动作不能只改一侧。
        managed_issue_id = None
        if operation in {"manual_review_status", "commit_decision"}:
            managed_issue_id = payload.get("record", {}).get("issue_id")
        elif operation == "manual_reopen_issue":
            managed_issue_id = payload.get("record", {}).get("parent_issue_id")
        elif operation == "update_issue":
            managed_issue_id = payload.get("issue_id")
        managed_issue = state["issues"].get(managed_issue_id, {})
        managed_review = state.get("batch_review", {}).get("reviews", {}).get(managed_issue.get("directed_review_id"), {})
        if managed_issue_id and managed_review.get("directed_context") and managed_review.get("directed_issue_id") == managed_issue_id:
            _require(False, "DIRECTED_PARENT_MANAGED", 'The main workspace manages this parent issue. Return to directed review to finish, refine or prepare the current members again.')
        if operation == "manual_final_item_review":
            from .annotation_final_review import apply_final_item
            apply_final_item(self, state, payload["record"])
            return
        if operation == "batch_review" or (operation == "record_job" and payload.get("batch_review_event") is True):
            from .annotation_batch_review import apply_batch
            apply_batch(self, state, payload)
            return
        if operation == "restore_checkpoint" and "batch_review" in state:
            from .annotation_batch_review import ACTIVE
            _require(not any(row["status"] in ACTIVE for row in state["batch_review"]["reviews"].values()),
                     "BATCH_BUSY", 'Wait for the batch job to finish or cancel it before restoring a checkpoint.')
        if operation == "commit_decision":
            # 全部成员有处理记录仍不等于规定比较和关系复核已经完成。
            payload = dict(payload, issue_status_after="under_human_review")
            super()._apply(state, operation, payload)
            state["issues"][payload["record"]["issue_id"]]["closure_confirmed"] = False
            state["current_completion_id"] = None
            if "batch_review" in state:
                from .annotation_batch_review import link_legacy_decision
                link_legacy_decision(state, payload["record"])
            return
        if operation == "manual_review_status":
            self._review_status(state, payload["record"])
            payload["record"] = copy.deepcopy(state["review_status_records"][payload["record"]["review_status_id"]])
        elif operation == "manual_reopen_issue":
            self._reopen(state, payload["record"])
        elif operation == "manual_type_card":
            self._type_card(state, payload["record"])
            payload["record"] = copy.deepcopy(state["type_cards"][payload["record"]["local_type_id"]])
        elif operation == "manual_catalogue_scope":
            self._scope(state, payload["record"])
            payload["record"] = copy.deepcopy(state["catalogue_scope"])
        elif operation == "manual_scoped_complete":
            self._complete(state, payload["record"])
            payload["record"] = copy.deepcopy(state["completion_records"][payload["record"]["completion_id"]])
        else:
            super()._apply(state, operation, payload)
            if operation == "open_issue":
                state["issues"][payload["record"]["issue_id"]].update(
                    opened_at_revision=state["revision"] + 1, closure_confirmed=False)
                self._invalidate(state)
            elif operation == "restore_checkpoint":
                from .annotation_source_registry import retain_assessment_on_label_restore
                retain_assessment_on_label_restore(state, payload)
                checkpoint = state["checkpoints"][payload["checkpoint_id"]]["snapshot"]
                # 只恢复可编辑的当前定义与范围；所有后续历史记录、决定和事件仍保留。
                state["type_cards"] = copy.deepcopy(checkpoint.get("type_cards", {}))
                state["catalogue_scope"] = copy.deepcopy(checkpoint.get("catalogue_scope"))
                state["definition_review_requirements"] = copy.deepcopy(checkpoint.get("definition_review_requirements", {}))
                state["current_completion_id"] = None
                if "batch_review" in state or "batch_review" in checkpoint:
                    from .annotation_batch_review import blank, ACTIVE, restore_batch_protocol
                    checkpoint_batch = checkpoint.get("batch_review")
                    checkpoint_protocol = checkpoint_batch.get("protocol_version", "0.4") if checkpoint_batch is not None else None
                    current = copy.deepcopy(state.get("batch_review")) if "batch_review" in state else blank(checkpoint_protocol)
                    restored = copy.deepcopy(checkpoint_batch) if checkpoint_batch is not None else blank(current.get("protocol_version", "0.4"))
                    # Append-only events and history retain all post-checkpoint decisions.
                    history = {r["record_id"]: r for r in current["history"] + restored["history"]}
                    restored["history"] = list(history.values())
                    archives = copy.deepcopy(current.pop("restore_archives", []))
                    archives.append({"checkpoint_id": payload["checkpoint_id"], "prior_batch_state": current})
                    restored["restore_archives"] = archives
                    for key, value in current["organizations"].items():
                        restored["organizations"].setdefault(key, value)
                    for key, value in current["reviews"].items():
                        restored["reviews"].setdefault(key, value)
                    for review in restored["reviews"].values():
                        if review["status"] in ACTIVE:
                            review.update(status="failed", closed=False, error={"code": "CHECKPOINT_INTERRUPTED",
                                "message": 'No worker is running the calculation stored in this checkpoint. Prepare it again.'})
                    state["batch_review"] = restored
                    provenance = restore_batch_protocol(state, current, restored,
                        checkpoint_id=payload["checkpoint_id"], checkpoint_protocol=checkpoint_protocol,
                        reason=payload["reason"])
                    payload["batch_protocol_restore"] = copy.deepcopy(provenance)

    @staticmethod
    def _invalidate(state):
        state["catalogue_status"] = "working"
        state["current_completion_id"] = None

    def _record(self, state, name, record, key):
        self._engineering_record(record, state)
        records = state.setdefault(name, {})
        _require(record[key] not in records, "IMMUTABLE_RECORD", 'An existing manual record cannot be overwritten.')
        records[record[key]] = copy.deepcopy(record)

    def _review_status(self, state, record):
        issue = state["issues"].get(record["issue_id"])
        _require(issue is not None, "ISSUE_NOT_FOUND", 'Issue not found.')
        _require(record["expected_issue_revision"] == issue["revision"], "ISSUE_REVISION_CONFLICT", 'The issue version has changed.')
        _require(not issue.get("closure_confirmed", False), "ISSUE_CLOSED", 'Reopen this closed issue to continue working on it.')
        text_required(record["answer"], "ANSWER_REQUIRED", 'Answer the question for this review round.')
        status = record["status"]
        _require(status in TERMINAL | {"unfinished"}, "ISSUE_STATE", 'Invalid manual processing status.')
        if status in TERMINAL:
            _require(set(record["checks"]) == REVIEW_CHECKS and all(v is True for v in record["checks"].values()),
                     "REVIEW_CHECKS", 'Confirm the required comparisons, affected relationships and follow-ups before finishing.')
            text_required(record["comparison_note"], "REVIEW_EVIDENCE", 'Describe the required comparisons you completed.')
            text_required(record["affected_relations_note"], "REVIEW_EVIDENCE", 'Describe the relationships you checked or explain why they do not apply.')
            _require(not record["pending_steps"].strip(), "PENDING_STEPS", 'Save as unfinished while required steps remain.')
            member_states = issue.get("member_processing_states", {})
            _require(all(member_states.get(i) in TERMINAL for i in issue["target_ids"]),
                     "ISSUE_COVERAGE", 'Unfinished or unrecorded members cannot be marked as reviewed but unresolved.')
            unresolved = any(member_states[i] == "reviewed_unresolved" for i in issue["target_ids"])
            _require(status == ("reviewed_unresolved" if unresolved else "decided"),
                     "ISSUE_UNRESOLVED", 'The final issue state must retain members that are still unresolved.')
            _require(not any(row.get("issue_id") == issue["issue_id"] and row["status"] in ACTIVE_JOBS
                             for row in state["jobs"].values()), "ACTIVE_JOBS", 'The local calculation has not finished.')
            if unresolved:
                text_required(record["unresolved_level"], "UNRESOLVED_DETAIL", 'Record the level at which the classification remains unresolved.')
                text_required(record["missing_information"], "UNRESOLVED_DETAIL", 'Describe the information needed to distinguish the alternatives.')
        else:
            text_required(record["pending_steps"], "PENDING_STEPS", 'List the remaining steps or the reason work stopped.')
        record = dict(record, member_processing_states_at_confirmation=copy.deepcopy(
            issue.get("member_processing_states", {})), type_definition_fingerprints={
                key: definition_fingerprint(card) for key, card in state.get("type_cards", {}).items()})
        self._record(state, "review_status_records", record, "review_status_id")
        issue.update(status=status, revision=issue["revision"] + 1,
                     closure_confirmed=status in TERMINAL, last_review_status_id=record["review_status_id"],
                     pending_steps=record["pending_steps"])
        self._invalidate(state)

    def _reopen(self, state, record):
        parent = state["issues"].get(record["parent_issue_id"])
        _require(parent is not None, "ISSUE_NOT_FOUND", 'Parent issue not found.')
        closed = parent["status"] in TERMINAL
        stale = parent["catalogue_revision_at_request"] < state["catalogue_revision"]
        directed_refresh = False
        if record.get("directed_review_id") and parent.get("directed_review_id"):
            from .annotation_batch_review import review_stale_reason, family_members
            prior_review = state.get("batch_review", {}).get("reviews", {}).get(parent["directed_review_id"], {})
            target_family = record.get("directed_family_id")
            current_members = family_members(state, target_family) if target_family else []
            directed_refresh = bool(prior_review.get("directed_context")
                                    and sorted(record["target_ids"]) == current_members
                                    and review_stale_reason(state, prior_review))
            if directed_refresh:
                record["directed_scope_refresh"] = {"parent_review_id": prior_review["review_id"],
                    "parent_was_closed": bool(parent.get("closure_confirmed")),
                    "reason": review_stale_reason(state, prior_review)}
        _require(closed or stale or directed_refresh, "ISSUE_NOT_CLOSED", 'Continue the open issue for the current version. If the catalogue or directed scope changed, explicitly resume it with the updated scope.')
        _require(parent["revision"] == record["parent_issue_revision"], "ISSUE_REVISION_CONFLICT", 'The parent issue version has changed.')
        _require(parent["catalogue_revision_at_request"] == record["parent_catalogue_revision"],
                 "CATALOGUE_REVISION_CONFLICT", 'Retain the catalogue version actually used by the parent issue.')
        _require(record["basis"] in REOPEN_BASES | ({REFRESH_BASIS} if stale and not closed else set()),
                 "REOPEN_BASIS", 'Provide concrete evidence to reopen. For a stale unfinished issue, record the catalogue update before resuming it.')
        _require(not any(row.get("issue_id") == parent["issue_id"] and row["status"] in ACTIVE_JOBS
                         for row in state["jobs"].values()), "ACTIVE_JOBS", "Wait for the parent issue's job to finish or cancel it explicitly. Its original input version is retained.")
        if not closed:
            covered = superseded_targets(state, parent["issue_id"])
            remaining = set(parent["target_ids"]) - covered
            _require(bool(set(record["target_ids"]) & remaining) and not (set(record["target_ids"]) & covered),
                     "ISSUE_SUPERSEDED", 'These targets have moved to a new round. Select the current issue to continue.')
        text_required(record["reason"], "REASON_REQUIRED", 'Give a reason for reopening the issue.')
        text_required(record["downstream_recheck"], "REOPEN_SCOPE", 'Identify the representations or downstream results that need review, or explain why they are unaffected.')
        known_types = {row["local_type_id"] for row in state["memberships"]}
        _require(set(record["affected_type_ids"]) <= known_types, "TYPE_NOT_FOUND", 'Choose affected types from the current catalogue.')
        super()._apply(state, "open_issue", {"record": record})
        state["issues"][record["issue_id"]].update(opened_at_revision=state["revision"] + 1, closure_confirmed=False)
        self._record(state, "reopen_records", record, "issue_id")
        self._invalidate(state)

    def _type_card(self, state, record):
        types = {row["local_type_id"]: row for row in state["memberships"]}
        key = record["local_type_id"]
        _require(key in types, "TYPE_NOT_FOUND", 'Create the type and its members with a manual decision first.')
        members = set(types[key]["member_ids"])
        for field in ("representative_ids", "atypical_ids", "counterexample_ids"):
            self._in_scope(state, record[field])
            _require(len(record[field]) == len(set(record[field])), "DUPLICATE_ID", 'Examples cannot be repeated.')
        _require(set(record["representative_ids"]) <= members and set(record["atypical_ids"]) <= members,
                 "TYPE_EXAMPLE_SCOPE", 'Representative and atypical examples must belong to this type.')
        _require(not members & set(record["counterexample_ids"]), "TYPE_EXAMPLE_SCOPE", 'A counterexample cannot also be a member of this type.')
        _require(set(record["competing_type_ids"]) <= set(types) - {key}, "TYPE_NOT_FOUND", 'Select an existing, different type as a competitor.')
        text_required(record["title"], "TYPE_TITLE", 'Enter a type name.')
        text_required(record["reason"], "REASON_REQUIRED", 'Give the basis for this definition or revision.')
        _require(record["definition_status"] in {"draft", "reviewed"}, "TYPE_CARD_STATUS", 'Invalid definition card status.')
        if record["definition_status"] == "reviewed":
            text_required(record["diagnostic_body"], "TYPE_DEFINITION", 'Provide the main diagnostic criteria before confirming the definition.')
            _require(bool(record["representative_ids"]), "TYPE_DEFINITION", 'Select at least one human-designated representative before confirming the definition.')
            if types[key]["parent_type_id"] is not None:
                text_required(record["main_variant_difference"], "TYPE_DEFINITION", 'For a leaf type, describe its main form or variant and the diagnostic difference.')
        prior = state.get("type_cards", {}).get(key)
        record = copy.deepcopy(record)
        versions = [row["revision"] for row in state.get("type_card_history", {}).values()
                    if row["local_type_id"] == key]
        record.update(revision=max(versions, default=0) + 1,
                      previous_card_id=prior["card_id"] if prior else None,
                      membership_fingerprint=membership_fingerprint(types[key]))
        self._record(state, "type_card_history", record, "card_id")
        state.setdefault("type_cards", {})[key] = record
        baseline = prior
        while baseline and baseline["definition_status"] != "reviewed":
            baseline = state.get("type_card_history", {}).get(baseline.get("previous_card_id"))
        if baseline and definition_content(baseline) != definition_content(record):
            related_types = {key} | set(baseline["competing_type_ids"]) | set(record["competing_type_ids"])
            if types[key]["parent_type_id"]:
                related_types.add(types[key]["parent_type_id"])
            affected = {item for ident in related_types if ident in types for item in types[ident]["member_ids"]}
            if "batch_review" in state:
                state["batch_review"]["legacy_dirty_ids"] = sorted(
                    set(state["batch_review"].get("legacy_dirty_ids", [])) | affected)
            confirmed = [ident for ident, row in state["issues"].items()
                         if row.get("closure_confirmed") and affected & set(row["target_ids"])]
            if affected:
                requirement = {"requirement_id": "definition-review-" + record["card_id"],
                    "local_type_id": key, "baseline_card_id": baseline["card_id"], "card_id": record["card_id"],
                    "affected_item_ids": sorted(affected), "affected_issue_ids": sorted(confirmed),
                    "fine_required_item_ids": sorted(types[key]["member_ids"]) if types[key]["parent_type_id"] is not None else [],
                    "affected_batch_review_ids": sorted(r["review_id"] for r in state.get("batch_review", {}).get("reviews", {}).values()
                                                        if r.get("closed") and affected & set(r.get("item_ids", []))),
                    "changed_fields": [field for field in DEFINITION_FIELDS if
                        definition_content(baseline)[field] != definition_content(record)[field]],
                    "created_at_revision": state["revision"] + 1,
                    "source": record["source"], "recorded_at": record["recorded_at"]}
                state.setdefault("definition_review_requirements", {})[key] = requirement
                state.setdefault("definition_review_history", {})[requirement["requirement_id"]] = copy.deepcopy(requirement)
        self._invalidate(state)

    def _scope(self, state, record):
        _require(bool(record["scope_ids"]), "SCOPE_REQUIRED", 'The delivery scope cannot be empty.')
        self._in_scope(state, record["scope_ids"])
        _require(record["target_level"] in {"family", "leaf"}, "TARGET_LEVEL", 'Select the family or leaf classification level.')
        for field in ("purpose", "coverage_plan", "quality_requirements", "unresolved_policy", "exclusions_policy"):
            text_required(record[field], "SCOPE_REQUIREMENT", 'Specify the scope, purpose, coverage, quality and policies for unresolved and excluded items.')
        prior = state.get("catalogue_scope")
        record = copy.deepcopy(record)
        record.update(revision=max((row["revision"] for row in state.get("scope_history", {}).values()), default=0) + 1,
                      previous_scope_id=prior["scope_id"] if prior else None)
        self._record(state, "scope_history", record, "scope_id")
        state["catalogue_scope"] = record
        self._invalidate(state)

    def _complete(self, state, record):
        scope = state.get("catalogue_scope")
        _require(scope is not None and record["scope_id"] == scope["scope_id"], "SCOPE_CHANGED", 'Save and check the current delivery scope first.')
        report = completion_report(state)
        _require(report["can_submit"], "COMPLETION_MISSING", 'There are unfinished members, open issues or missing type definitions.')
        _require(set(record["checks"]) == COMPLETION_CHECKS and all(v is True for v in record["checks"].values()),
                 "COMPLETION_CHECKS", 'Confirm each requirement for this scoped delivery.')
        for field in ("quality_evidence", "unresolved_summary", "exclusions_summary", "limitations"):
            text_required(record[field], "COMPLETION_EVIDENCE", 'Record the quality evidence, unresolved items, exclusions and limitations. Explicitly mark any that do not apply.')
        projected = item_projection(state)
        record = copy.deepcopy(record)
        record.update(catalogue_revision=state["catalogue_revision"] + 1,
                      scope=copy.deepcopy(scope), counts=report["counts"],
                      membership_snapshot=copy.deepcopy(state["memberships"]),
                      type_card_ids={key: row["card_id"] for key, row in state.get("type_cards", {}).items()},
                      review_status_ids={key: row.get("last_review_status_id") for key, row in state["issues"].items()},
                      item_processing_snapshot={i: projected[i] for i in scope["scope_ids"]},
                      scientific_validity_certified=False, confirmation_source="human_explicit")
        self._record(state, "completion_records", record, "completion_id")
        state["catalogue_revision"] += 1
        state["catalogue_status"] = "scoped_complete"
        state["current_completion_id"] = record["completion_id"]
