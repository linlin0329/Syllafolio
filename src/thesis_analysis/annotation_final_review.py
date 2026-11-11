'Final checks and directed-review bridge; existing issues, type cards and completion records remain authoritative.'
from __future__ import annotations

import copy
import re

from .annotation_assistant_contracts import require, canonical_hash
from .annotation_state import _require
from .annotation_english import system_text


def _definition_versions(state, type_ids):
    from .annotation_manual_protocol import definition_fingerprint
    return {key: definition_fingerprint(state["type_cards"][key]) if key in state.get("type_cards", {}) else None
            for key in type_ids}


def _current_pool_coverage_complete(state, batch, pool, active_directed):
    from .annotation_batch_review import active_pool, first_pass_complete
    if not pool.get("closed") or not first_pass_complete(batch):
        return False
    for item in active_pool(batch):
        row = batch.get("assignments", {}).get(item, {})
        if not row.get("record_id"):
            return False
        # A bound directed round may be in progress without reopening its real
        # completed baseline pool. Unrelated incoming pool objects are not covered.
        if any(row.get("review_id") == review["review_id"] and
               review.get("directed_context", {}).get("baseline_pool_closed") and
               review.get("directed_context", {}).get("baseline_pool_review_id") == pool["review_id"]
               for review in active_directed):
            continue
        if item in batch.get("legacy_dirty_ids", []) or row.get("boundary_flag") or row.get("pending_steps", "").strip():
            return False
        carried = pool.get("carried_assignment_record_ids", {}).get(item)
        if row.get("review_id") == pool["review_id"] or (carried and carried == row["record_id"]
                and pool.get("decisions", {}).get(item, {}).get("record_id") == carried):
            continue
        if any(directed_return_current(state, review, row=row, pool=pool)
               for review in batch.get("reviews", {}).values()):
            continue
        return False
    return True


def final_review_projection(state, report=None):
    from .annotation_manual_protocol import completion_report, definition_review_gaps, membership_fingerprint
    from .annotation_batch_review import (blank, current_organization, first_pass_complete,
        pool_stage_complete, current_pool_review, family_member_fingerprint, family_definition_fingerprint, family_title, review_stale_reason)
    report = completion_report(state) if report is None else copy.deepcopy(report)
    for row in report.get("missing", []):
        if "message" in row:
            row["message"] = system_text(row["message"])
    batch = state.get("batch_review", blank())
    org = current_organization(batch)
    groups = (org or {}).get("groups", [])
    first_pending = [g["group_id"] for g in groups if not batch["reviews"].get(batch["active_reviews"].get(g["group_id"]), {}).get("closed")]
    pool = batch["reviews"].get(batch.get("latest_pool_review_id"), {})
    directed = [r for r in batch["reviews"].values() if r.get("directed_issue_id") and not r.get("archived")]
    current_directed = [r for r in directed if r["directed_issue_id"] in state["issues"]
                        and batch.get("active_family_reviews", {}).get(r.get("family_id")) == r["review_id"]]
    current_directed_ids = {r["review_id"] for r in current_directed}
    active_directed = [r for r in current_directed if not r.get("closed")]
    all_gaps = report.get("definition_review_gaps", {})
    families = []
    family_metadata = {row["family_id"]: row for row in batch.get("families", [])}
    type_rows = []
    type_ids = {row["local_type_id"] for row in state["memberships"]}
    for row in state["memberships"]:
        card = state.get("type_cards", {}).get(row["local_type_id"])
        fingerprint = membership_fingerprint(row)
        definition_state = "missing" if card is None else "stale" if (
            card["membership_fingerprint"] != fingerprint or not set(card["competing_type_ids"]) <= type_ids) else card["definition_status"]
        type_rows.append({"local_type_id": row["local_type_id"], "parent_type_id": row["parent_type_id"],
                          "member_count": len(row["member_ids"]), "membership_fingerprint": fingerprint,
                          "definition_state": definition_state, "card": copy.deepcopy(card)})
        if card is None and row["parent_type_id"] is None:
            title = family_title(state, row["local_type_id"])
            if title != row["local_type_id"]:
                type_rows[-1]["title"] = title
    type_states = {row["local_type_id"]: row["definition_state"] for row in type_rows}
    for family in state["memberships"]:
        if family["parent_type_id"] is not None:
            continue
        key = family["local_type_id"]; members = sorted(family["member_ids"])
        card = state.get("type_cards", {}).get(key)
        gaps = {owner: sorted(set(ids) & set(members)) for owner, ids in all_gaps.items() if set(ids) & set(members)}
        families.append({"family_id": key, "member_ids": members, "member_count": len(members),
            "member_fingerprint": family_metadata[key]["member_fingerprint"] if key in family_metadata else family_member_fingerprint(state, key),
            "definition_fingerprint": family_metadata[key]["definition_fingerprint"] if key in family_metadata else family_definition_fingerprint(state, key), "card": copy.deepcopy(card),
            "definition_gaps": gaps, "required_type_ids": sorted({key, *gaps}),
            "definition_state": type_states[key],
            "current_directed_review_ids": [r["review_id"] for r in active_directed if r.get("family_id") == key]})
    pool_coverage = (_current_pool_coverage_complete(state, batch, pool, active_directed)
                     if current_pool_review(pool) else bool(pool.get("closed")))
    ordinary_family_pending = any(not batch["reviews"].get(rid, {}).get("closed")
        and not batch["reviews"].get(rid, {}).get("directed_context")
        for rid in batch.get("active_family_reviews", {}).values() if rid in batch["reviews"])
    definition_priority = pool_coverage and (not current_pool_review(pool) or not ordinary_family_pending)
    gaps = []
    for gap in report["missing"]:
        row = copy.deepcopy(gap); code = row["code"]
        view = "candidates" if code == "BATCH_FIRST_PASS_PENDING" else "review" if code in {"BATCH_POOL_PENDING", "BATCH_FAMILY_PENDING", "BATCH_REVIEW_OPEN", "ACTIVE_JOBS", "OPEN_REVIEWS"} else "catalogue" if code in {"TYPE_DEFINITIONS", "STALE_TYPE_DEFINITIONS", "DEFINITION_REVIEW_REQUIRED", "BATCH_LEAF_UNASSESSED"} else "final"
        related = set(row.get("item_ids", [])); types = set(row.get("type_ids", []))
        family_ids = {f["family_id"] for f in families if set(f["member_ids"]) & related or (not related and f["family_id"] in types)}
        if not related:
            family_ids.update(m["parent_type_id"] for m in state["memberships"] if m["local_type_id"] in types and m["parent_type_id"] is not None)
        if code == "BATCH_FIRST_PASS_PENDING" and not first_pending:
            related = set(batch.get("legacy_dirty_ids", [])); view = "final"
            family_ids = {f["family_id"] for f in families if set(f["member_ids"]) & related}
        if code == "BATCH_POOL_PENDING" and definition_priority:
            if active_directed:
                row["review_ids"] = [r["review_id"] for r in active_directed]
                row["message"] = 'The original whole-pool scope is closed; finish the current directed review first, without automatically reopening the whole pool.'
            elif all_gaps:
                related = {item for values in all_gaps.values() for item in values}
                types = set(all_gaps)
                family_ids = {f["family_id"] for f in families if set(f["member_ids"]) & related}
                row["item_ids"] = sorted(related)
                row["type_ids"] = sorted(types)
                row["message"] = 'The original whole-pool scope is closed; the current catalogue still has definition-review gaps for affected types; review their definitions first.'
                view = "final"
        row["source_type_ids"] = sorted(types)
        row["location"] = {"view": view, "family_ids": sorted(family_ids), "item_ids": sorted(related),
                           "review_ids": row.get("review_ids", []), "issue_ids": row.get("issue_ids", []), "source_type_ids": sorted(types)}
        gaps.append(row)
    pool_current = batch.get("pool", {}).get("stage_complete")
    if pool_current is None:
        pool_current = pool_stage_complete(batch, state)
    pending_definition_families = sorted({f["family_id"] for f in families
                                          if any(set(f["member_ids"]) & set(values) for values in all_gaps.values())})
    pool_message = ('The original whole-pool scope is closed; finish the current directed review first, without automatically reopening the pool' if active_directed and pool.get("closed") else
                    'The original whole-pool scope is closed; review definitions for the affected families first; the current catalogue has not passed the final gate' if all_gaps and pool.get("closed") else
                    'Reviewed unresolved items may remain; directed completion does not replace an unfinished whole-pool scope')
    if current_pool_review(pool):
        if not pool.get("closed"):
            pool_message = 'The current frozen pool round is still open; unlocked items can be saved, while final checks assess complete coverage separately.'
        elif first_pending:
            pool_message = 'The current frozen pool round is closed; some initial candidate reviews remain unfinished, so final whole-pool coverage is not implied.'
        elif not pool_coverage:
            pool_message = 'The previous frozen pool round is closed; the current pool has uncovered items or required steps; check its scope and prepare again if needed.'
        elif ordinary_family_pending:
            pool_message = 'The current frozen pool is covered; finish the active human family review before confirming final completion.'
    stages = {
        "first": {"status": "not_applicable" if not org else "complete" if first_pass_complete(batch) else "in_progress",
                  "message": 'Use initial coverage of the current candidate families' if org else 'No global candidate organization is declared; check the current human scope',
                  "total": len(groups), "completed": len(groups)-len(first_pending), "pending_ids": first_pending, "next_view": "candidates"},
        "pool": {"status": "not_applicable" if not org else "complete" if pool_current else "in_progress" if pool else "not_started",
                 "message": pool_message,
                 "coverage_complete": pool_coverage, "current_for_final": pool_current,
                 "blocked_by_directed_ids": [r["review_id"] for r in active_directed], "total": len(pool.get("item_ids", [])),
                 "completed": len(pool.get("decisions", {})), "pending_ids": [pool["review_id"]] if pool and not pool.get("closed") else [],
                 "next_view": "final" if definition_priority and all_gaps else "review",
                 "next_family_ids": pending_definition_families if pool.get("closed") else [],
                 "source_type_ids": sorted(all_gaps) if pool.get("closed") else []},
        "directed": {"status": "in_progress" if active_directed else "complete" if current_directed else "not_started",
                     "message": 'A specific question and directed review of frozen current members', "total": len(current_directed),
                     "completed": sum(bool(r.get("closed")) for r in current_directed),
                     "pending_ids": [r["review_id"] for r in active_directed], "next_view": "review"},
        "final": {"status": "scoped_complete" if state.get("current_completion_id") else "ready" if report["can_submit"] else "blocked",
                  "message": 'Confirm human review only for the declared scope; internal assessment and catalogue delivery are recorded separately', "total": len(report["scope_ids"]),
                  "completed": report["counts"]["decided"] + report["counts"]["reviewed_unresolved"],
                  "pending_ids": [g["code"] for g in gaps], "next_view": "final"}}
    if current_pool_review(pool):
        stages["pool"].update(pool_mode="current_pool", frozen_round_complete=bool(pool.get("closed")))
    superseded = {issue.get("parent_issue_id") for issue in state["issues"].values()}
    fine_issues = []
    from .annotation_decision_history import effective_rows
    decision_rows = effective_rows(state)
    latest_by_issue = {}
    for decision in decision_rows:
        key = decision["issue_id"]
        if key not in latest_by_issue or decision.get("catalogue_revision_after", -1) >= latest_by_issue[key].get("catalogue_revision_after", -1):
            latest_by_issue[key] = decision
    for key, issue in state["issues"].items():
        if not issue.get("final_item_review"):
            continue
        latest = latest_by_issue.get(key, {})
        item = issue["target_ids"][0]
        saved_axes = next((row["axes"] for row in latest.get("item_axes", []) if row["item_id"] == item), latest.get("axes"))
        current_family = next((r["local_type_id"] for r in state["memberships"] if r["parent_type_id"] is None and item in r["member_ids"]), None)
        current_leaf = next((r["local_type_id"] for r in state["memberships"] if r["parent_type_id"] is not None and item in r["member_ids"]), None)
        axes_current = bool(saved_axes and saved_axes.get("family_id") == current_family
                            and (saved_axes.get("family_status") == "assigned") == (current_family is not None)
                            and saved_axes.get("leaf_id") == current_leaf)
        fine_issues.append({"issue_id": key, "item_id": issue["target_ids"][0], "question": issue["question"],
                           "status": issue["status"], "pending_steps": issue.get("pending_steps", ""),
                           "axes": copy.deepcopy(saved_axes) if axes_current else None,
                           "decision_id": latest.get("decision_id"), "axes_current_for_membership": axes_current,
                           "historical_axes": copy.deepcopy(saved_axes) if not axes_current else None,
                           "directed_review_id": issue.get("directed_parent_review_id"),
                           "can_continue": key not in superseded})
    return {"completion": report, "stages": stages, "gaps": gaps, "families": families, "types": type_rows, "fine_issues": fine_issues,
            "directed_reviews": [{"review_id": r["review_id"], "family_id": r["family_id"],
                "issue_id": r["directed_issue_id"], "closed": bool(r.get("closed")),
                "is_current": r["review_id"] in current_directed_ids,
                "historical_only": r["review_id"] not in current_directed_ids,
                "current_for_definitions": directed_return_current(state, r),
                "scope_stale": bool(review_stale_reason(state, r)), "question": r.get("directed_context", {}).get("question"),
                "return_view": "final", "item_ids": list(r["item_ids"])} for r in directed],
            "assessment": {"implemented": False, "status": "not_implemented"},
            "publication_report": {"implemented": False, "status": "not_implemented"},
            "scientific_validity_certified": False}


def prepare_directed_context(state, review, context):
    from .annotation_manual_protocol import REOPEN_BASES, definition_review_gaps
    from .annotation_batch_review import family_definition_fingerprint
    require(isinstance(context, dict) and set(context) <= {"question", "basis", "reason", "affected_type_ids", "parent_issue_id", "observation_ids"},
            "DIRECTED_CONTEXT", 'Specify the question, evidence and affected types')
    for key in ("question", "reason"):
        require(isinstance(context.get(key), str) and bool(context[key].strip()), "DIRECTED_CONTEXT", 'Directed review requires an actual question and supporting evidence')
    require(context.get("basis") in REOPEN_BASES, "REOPEN_BASIS", 'Select the actual basis for reopening')
    types = {x["local_type_id"]: x for x in state["memberships"]}
    affected = context.get("affected_type_ids")
    require(isinstance(affected, list) and all(isinstance(x, str) for x in affected) and len(set(affected)) == len(affected)
            and review["family_id"] in affected and set(affected) <= set(types), "DIRECTED_AFFECTED_TYPES", 'Specify this family and the known types actually affected')
    observations = context.get("observation_ids", [])
    require(isinstance(observations, list) and all(isinstance(x, str) and x in state["observations"] for x in observations),
            "OBSERVATION_NOT_FOUND", 'The referenced observation does not exist')
    parent = context.get("parent_issue_id")
    require(parent is None or isinstance(parent, str) and parent in state["issues"], "ISSUE_NOT_FOUND", 'The parent issue does not exist')
    batch = state.get("batch_review", {}); pool_id = batch.get("latest_pool_review_id")
    delivery_level = (state.get("catalogue_scope") or {}).get("target_level", "family")
    pending_definitions = definition_review_gaps(state, review["item_ids"])
    leaf_requirements = {}
    for key in affected:
        if types[key]["parent_type_id"] is not None and key in pending_definitions:
            requirement = state["definition_review_requirements"][key]
            required_fine = set(requirement.get("fine_required_item_ids", types[key]["member_ids"]))
            leaf_requirements[key] = sorted(required_fine & set(pending_definitions[key]) & set(review["item_ids"]))
    fine_required_ids = sorted({item for ids in leaf_requirements.values() for item in ids})
    level = "leaf" if fine_required_ids else "family"
    return {**copy.deepcopy(context), "observation_ids": list(observations), "parent_issue_id": parent,
            "target_ids": list(review["item_ids"]), "family_id": review["family_id"], "verification_level": level,
            "delivery_target_level": delivery_level, "fine_required_ids": fine_required_ids,
            "fine_requirement_sources": copy.deepcopy(leaf_requirements),
            "member_fingerprint": review["family_member_fingerprint"],
            "definition_fingerprint": family_definition_fingerprint(state, review["family_id"]),
            "type_definition_fingerprints": _definition_versions(state, affected),
            "type_card_ids": {key: state.get("type_cards", {}).get(key, {}).get("card_id") for key in affected},
            "definition_requirement_ids": {key: state.get("definition_review_requirements", {}).get(key, {}).get("requirement_id") for key in affected},
            "organization_id": batch.get("active_organization_id"), "baseline_pool_review_id": pool_id,
            "baseline_pool_closed": bool(batch.get("reviews", {}).get(pool_id, {}).get("closed")), "return_view": "final"}


def open_directed_issue(store, state, review, source):
    context = review["directed_context"]
    _require(_definition_versions(state, context["affected_type_ids"]) == context["type_definition_fingerprints"],
             "DIRECTED_DEFINITION_CHANGED", 'The type definition has changed; confirm the review scope again')
    issue = {"issue_id": review["directed_issue_id"], "question": context["question"],
             "observation_ids": list(context["observation_ids"]), "action": "direct_compare", "fit_ids": [],
             "target_ids": list(review["item_ids"]), "eligible_change_ids": list(review["item_ids"]), "comparison_ids": [],
             "scope_reason": context["reason"], "reason": context["reason"], "basis": context["basis"],
             "parent_issue_id": context.get("parent_issue_id"), "affected_type_ids": list(context["affected_type_ids"]),
             "status": "open", "revision": 0, "catalogue_revision_at_request": state["catalogue_revision"],
             "source": source["source"], "experiment_enabled": False, "recorded_at": source["recorded_at"],
             "operator_id": source.get("operator_id"), "directed_review_id": review["review_id"],
             "directed_family_id": review["family_id"],
             "verification_level": context["verification_level"], "fine_required_ids": list(context["fine_required_ids"]),
             "batch_organization_route": review["route"],
             "type_definition_fingerprints_at_request": copy.deepcopy(context["type_definition_fingerprints"]),
             "problem_source": "human_explicit_request", "return_view": "final"}
    if issue["parent_issue_id"]:
        parent = state["issues"][issue["parent_issue_id"]]
        issue.update(parent_issue_revision=parent["revision"], parent_catalogue_revision=parent["catalogue_revision_at_request"],
                     downstream_recheck=context["reason"])
        store._reopen(state, issue)
    else:
        store._apply(state, "open_issue", {"record": issue})


def sync_directed_results(state, review):
    if not review.get("directed_issue_id"):
        return
    issue = state["issues"][review["directed_issue_id"]]
    _require(not issue.get("closure_confirmed"), "ISSUE_CLOSED", 'A closed directed issue must be explicitly reopened')
    states = {}; evidence = {}
    for item, row in review["decisions"].items():
        if row.get("boundary_flag") or row.get("pending_steps", "").strip() or row.get("fine_processing_state") == "unfinished":
            processing = "unfinished"
        else:
            processing = "reviewed_unresolved" if row["disposition"] == "pool" or row.get("form_status") == "unresolved" else "decided"
            if row.get("legacy_refinement"):
                processing = row["legacy_refinement"].get("processing_state", processing)
        states[item] = processing
        evidence[item] = {"record_id": row["record_id"], "source": row.get("decision_source", "batch_disposition"),
                          "fine_decision_id": row.get("legacy_refinement", {}).get("decision_id")}
    issue.update(member_processing_states=states, member_evidence=evidence, status="under_human_review",
                 revision=issue["revision"] + 1, catalogue_revision_at_request=state["catalogue_revision"])


def close_directed_review(store, state, review, record):
    from .annotation_manual_protocol import REVIEW_CHECKS
    context = review["directed_context"]
    _require(_definition_versions(state, context["affected_type_ids"]) == context["type_definition_fingerprints"],
             "DIRECTED_DEFINITION_CHANGED", 'Related type definitions have changed; this round cannot be closed')
    summary = record.get("directed_summary")
    fields = {"answer", "comparison_note", "affected_relations_note", "unresolved_level", "missing_information"}
    _require(isinstance(summary, dict) and set(summary) == fields and all(isinstance(x, str) for x in summary.values()),
             "DIRECTED_SUMMARY", 'Record the actual conclusion, comparisons and relationship checks; for unresolved results, state the level and missing information')
    if context["verification_level"] == "leaf":
        missing = []
        for item in context.get("fine_required_ids", []):
            row = review["decisions"][item]
            if row["disposition"] == "discard":
                continue
            refinement = row.get("legacy_refinement", {})
            decision = state["decisions"].get(refinement.get("decision_id"), {})
            fine_issue = state["issues"].get(refinement.get("issue_id"), {})
            if (decision.get("directed_review_id") != review["review_id"] or not fine_issue.get("closure_confirmed")
                    or decision.get("axes", {}).get("form_status") == "not_assessed"):
                missing.append(item)
        _require(not missing, "DIRECTED_LEAF_COVERAGE", 'Some items explicitly required by this round leaf-criterion change still lack actual three-axis review')
    sync_directed_results(state, review)
    issue = state["issues"][review["directed_issue_id"]]
    status = "reviewed_unresolved" if "reviewed_unresolved" in issue["member_processing_states"].values() else "decided"
    confirmation = {"review_status_id": "review-status-" + record["record_id"], "issue_id": issue["issue_id"],
                    "expected_issue_revision": issue["revision"], "status": status, **copy.deepcopy(summary),
                    "checks": {key: record["checks"][key] for key in REVIEW_CHECKS}, "pending_steps": "",
                    "source": record["source"], "experiment_enabled": False, "recorded_at": record["recorded_at"],
                    "operator_id": record.get("operator_id")}
    store._review_status(state, confirmation)
    review["directed_return"] = {"issue_id": issue["issue_id"], "review_status_id": confirmation["review_status_id"],
        "covered_record_ids": {item: row["record_id"] for item, row in review["decisions"].items()},
        "fine_verified_ids": list(context.get("fine_required_ids", [])),
        "type_definition_fingerprints": _definition_versions(state, context["affected_type_ids"]),
        "organization_id": context["organization_id"], "baseline_pool_review_id": context["baseline_pool_review_id"],
        "baseline_pool_closed": context["baseline_pool_closed"], "returned_at": record["recorded_at"], "return_view": "final"}
    record.update(directed_issue_id=issue["issue_id"], review_status_id=confirmation["review_status_id"], return_view="final")


def directed_return_current(state, review, row=None, pool=None):
    if state is None or not review.get("closed") or not review.get("directed_return"):
        return False
    result = review["directed_return"]
    issue = state["issues"].get(result["issue_id"], {})
    if (not issue.get("closure_confirmed") or issue.get("last_review_status_id") != result["review_status_id"]
            or _definition_versions(state, result["type_definition_fingerprints"]) != result["type_definition_fingerprints"]):
        return False
    if row is not None and result["covered_record_ids"].get(row["item_id"]) != row["record_id"]:
        return False
    if pool is not None and (not result["baseline_pool_closed"] or result["baseline_pool_review_id"] != pool.get("review_id")
                             or result["organization_id"] != pool.get("organization_id")):
        return False
    return True


def build_final_item_record(service, state, payload, meta, ident):
    from .annotation_manual_protocol import REVIEW_CHECKS
    from .annotation_batch_review import _current_review, require_fresh_review, _review_ancestors
    texts = {"observation_text", "question", "reason", "answer", "comparison_note", "affected_relations_note",
             "pending_steps", "unresolved_level", "missing_information"}
    service._fields(payload, texts | {"item_id", "axes", "processing_state", "new_leaf", "directed_review_id", "checks", "parent_issue_id"})
    item = service._ids([payload.get("item_id")], nonempty=True)[0]
    for key in texts:
        require(isinstance(payload.get(key, ""), str), "FINAL_REVIEW_TEXT", 'Human records must be text')
    for key in ("observation_text", "question", "reason"):
        require(bool(payload.get(key, "").strip()), "FINAL_REVIEW_EVIDENCE", 'Enter the actual observation, question and reasoning')
    axes = copy.deepcopy(payload.get("axes"))
    require(isinstance(axes, dict) and set(axes) == {"family_status", "family_id", "diagnostic_region", "component_status", "form_status", "leaf_id", "boundary_status"},
            "FINAL_REVIEW_AXES", 'The operator must explicitly select all seven judgments; they cannot be filled automatically')
    processing = payload.get("processing_state")
    require(processing in {"decided", "reviewed_unresolved", "unfinished"}, "PROCESSING_STATE", 'Invalid processing state')
    checks = service._checks(payload, REVIEW_CHECKS)
    if processing == "unfinished":
        require(bool(payload.get("pending_steps", "").strip()), "PENDING_STEPS", 'List the actual remaining steps for unfinished work')
    else:
        require(all(checks.values()), "REVIEW_CHECKS", 'Confirm each completed comparison and relationship check')
        require(not payload.get("pending_steps", "").strip() and axes.get("boundary_status") != "needs_review", "PENDING_STEPS", 'Required steps or boundary questions remain; completion cannot be confirmed')
        for key in ("answer", "comparison_note", "affected_relations_note"):
            require(bool(payload.get(key, "").strip()), "REVIEW_EVIDENCE", 'Record the actual conclusion, comparisons and relationship checks')
        if processing == "reviewed_unresolved":
            for key in ("unresolved_level", "missing_information"):
                require(bool(payload.get(key, "").strip()), "UNRESOLVED_DETAIL", 'Reviewed unresolved results require a level and missing information')
    types = {x["local_type_id"]: x for x in state["memberships"]}
    current_families = {x["local_type_id"] for x in state["memberships"] if x["parent_type_id"] is None and item in x["member_ids"]}
    family = axes.get("family_id")
    if family is not None:
        require(isinstance(family, str) and family in types and types[family]["parent_type_id"] is None,
                "TYPE_NOT_FOUND", 'Select an existing family; explicitly create a new family through the five disposition choices')
    directed_id = payload.get("directed_review_id")
    if directed_id:
        require(isinstance(directed_id, str), "DIRECTED_REVIEW_REQUIRED", 'Invalid directed-review ID')
        review = _current_review(state.get("batch_review", {}), directed_id)
        require(review.get("directed_issue_id") and not review.get("closed") and review.get("status") == "ready" and item in review["item_ids"],
                "DIRECTED_REVIEW_SCOPE", 'Select a current, valid directed review that includes this syllable')
        require_fresh_review(state, review)
        require(_definition_versions(state, review["directed_context"]["affected_type_ids"]) == review["directed_context"]["type_definition_fingerprints"],
                "DIRECTED_DEFINITION_CHANGED", 'Related criteria have changed; prepare the directed scope again')
    else:
        require(current_families == ({family} if axes.get("family_status") == "assigned" else set()),
                "FINAL_FAMILY_SCOPE", 'Individual fine review must retain the current family; first create a directed scope or use the five dispositions to reassign or exclude')
    new_leaf = payload.get("new_leaf")
    leaf = axes.get("leaf_id")
    if new_leaf is not None:
        require(isinstance(new_leaf, dict) and set(new_leaf) <= {"local_type_id", "title", "reason", "diagnostic_body", "main_variant_difference"},
                "FINAL_NEW_LEAF", 'Invalid new leaf-type draft fields')
        require(processing == "decided" and family is not None and axes.get("form_status") in {"main", "variant", "candidate_form"},
                "FINAL_NEW_LEAF", 'A new leaf type requires an explicit family and an actual form judgment')
        require(isinstance(leaf, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}", leaf)
                and new_leaf.get("local_type_id") == leaf, "FINAL_NEW_LEAF", 'The new leaf ID must match the explicitly selected leaf_id')
        reserved = {card["local_type_id"] for card in state.get("type_card_history", {}).values()}
        for previous_decision in state["decisions"].values():
            reserved.update(member["local_type_id"] for member in previous_decision.get("memberships_after", []))
        require(leaf not in types and leaf not in reserved, "TYPE_ID_CONFLICT", 'This type ID exists or has a creation history and cannot be overwritten')
        for key in ("title", "reason"):
            require(isinstance(new_leaf.get(key), str) and bool(new_leaf[key].strip()), "FINAL_NEW_LEAF", 'The new leaf-type name and actual creation basis are required')
        for key in ("diagnostic_body", "main_variant_difference"):
            require(isinstance(new_leaf.get(key, ""), str), "FINAL_NEW_LEAF", 'Draft criteria must be text and may be completed later')
    elif leaf is not None:
        require(isinstance(leaf, str) and leaf in types and types[leaf]["parent_type_id"] == family,
                "TYPE_NOT_FOUND", 'Select an existing leaf type in this family or explicitly create a draft')
    members = copy.deepcopy(state["memberships"])
    if processing != "unfinished":
        for row in members:
            if item in row["member_ids"]:
                row["member_ids"].remove(item)
        if new_leaf:
            members.append({"local_type_id": leaf, "parent_type_id": family, "member_ids": [], "supporting_decision_ids": []})
        for row in members:
            if row["local_type_id"] in {family, leaf}:
                row["member_ids"].append(item)
    observation = {"observation_id": "obs-final-" + ident, "item_ids": [item], "text": payload["observation_text"],
                   "source": meta["source"], "experiment_enabled": False, "recorded_at": meta["recorded_at"],
                   "clock_status": state["clock"]["status"], "timing_complete": state["clock"]["timing_complete"]}
    parent = payload.get("parent_issue_id")
    binding_ids = _review_ancestors(state.get("batch_review", {}), directed_id) if directed_id else {None}
    if parent is None:
        superseded = {row.get("parent_issue_id") for row in state["issues"].values()}
        matches = [key for key, old in state["issues"].items() if old.get("final_item_review")
                   and key not in superseded and old.get("target_ids") == [item] and not old.get("closure_confirmed")
                   and old.get("directed_parent_review_id") in binding_ids
                   and " ".join(old["question"].split()) == " ".join(payload["question"].split())]
        require(len(matches) <= 1, "FINAL_REVIEW_PARENT", 'Several matching unfinished issues exist; explicitly choose which refinement record to continue')
        parent = matches[0] if matches else None
    if parent is not None:
        prior = state["issues"].get(parent, {}) if isinstance(parent, str) else {}
        require(prior.get("final_item_review") and prior.get("target_ids") == [item]
                and prior.get("directed_parent_review_id") in binding_ids,
                "FINAL_REVIEW_PARENT", 'Only continue a matching refinement issue for this syllable; unrelated earlier issues cannot be closed')
        require(not any(job.get("issue_id") == parent and job["status"] in {"queued", "running", "cancel_requested"}
                        for job in state["jobs"].values()), "ACTIVE_JOBS", 'The parent issue still has active computation; wait or cancel it first')
    issue = {"issue_id": "issue-final-" + ident, "question": payload["question"], "observation_ids": [observation["observation_id"]],
             "action": "direct_compare", "scope_reason": payload["reason"], "target_ids": [item], "fit_ids": [],
             "comparison_ids": [], "eligible_change_ids": [item], "status": "open", "revision": 0, "parent_issue_id": parent,
             "catalogue_revision_at_request": state["catalogue_revision"], "source": meta["source"],
             "directed_parent_review_id": directed_id, "problem_source": "human_actual_observation", "final_item_review": True}
    temporary = copy.deepcopy(state)
    temporary["observations"][observation["observation_id"]] = observation
    temporary["issues"][issue["issue_id"]] = issue
    task, context = service._validation_context(temporary)
    service.rules.review_issue(service._validation_issue(issue), task, context)
    decision_payload = {"issue_id": issue["issue_id"], "operation": "defer_unknown" if processing == "reviewed_unresolved" else "retain" if processing == "unfinished" else "assign",
                        "observation_ids": [observation["observation_id"]], "affected_ids": [item], "processing_state": processing,
                        "axes": axes, "memberships_after": members, "reason": payload["reason"]}
    decision = service._decision(decision_payload, temporary, task, context, "final-" + ident, meta["recorded_at"])
    decision["final_item_review"] = True
    if directed_id:
        decision["directed_review_id"] = directed_id
    closure = {**copy.deepcopy(meta), "review_status_id": "review-status-final-" + ident, "issue_id": issue["issue_id"],
               "status": processing, "checks": checks, **{key: payload.get(key, "") for key in texts if key not in {"observation_text", "question", "reason"}}}
    leaf_card = None
    if new_leaf:
        leaf_card = {**copy.deepcopy(meta), "card_id": "card-final-" + ident, "local_type_id": leaf,
                     "title": new_leaf["title"], "reason": new_leaf["reason"],
                     "diagnostic_body": new_leaf.get("diagnostic_body", ""), "main_variant_difference": new_leaf.get("main_variant_difference", ""),
                     "uncertainties": "", "definition_status": "draft", "representative_ids": [], "atypical_ids": [],
                     "counterexample_ids": [], "competing_type_ids": []}
    return {**copy.deepcopy(meta), "final_item_review_id": "final-item-" + ident, "item_id": item,
            "observation": observation, "issue": issue, "decision": decision, "closure": closure,
            "leaf_card": leaf_card, "directed_review_id": directed_id,
            "observation_id": observation["observation_id"], "issue_id": issue["issue_id"], "decision_id": decision["decision_id"],
            "continued_issue_id": parent,
            "review_status_id": closure["review_status_id"], "new_leaf_id": leaf if new_leaf else None,
            "return_view": "review" if directed_id else "final"}


def apply_final_item(store, state, record):
    from .annotation_batch_review import _current_review, require_fresh_review, item_version, family_members
    # The import above is intentionally avoided in callers: all writes share the
    # surrounding SQLite transaction and retain the existing authority checks.
    directed = record.get("directed_review_id")
    review = None
    if directed:
        review = _current_review(state["batch_review"], directed)
        require_fresh_review(state, review)
        _require(not review.get("closed") and record["item_id"] in review["item_ids"], "DIRECTED_REVIEW_SCOPE", 'The directed scope has changed')
    store._apply(state, "record_observation", {"record": record["observation"]})
    store._apply(state, "open_issue", {"record": record["issue"]})
    decision = record["decision"]
    store._apply(state, "commit_decision", {"record": decision, "expected_catalogue_revision": state["catalogue_revision"],
                                           "memberships_after": decision["memberships_after"]})
    if record.get("leaf_card"):
        store._type_card(state, record["leaf_card"])
    closure = copy.deepcopy(record["closure"])
    closure["expected_issue_revision"] = state["issues"][closure["issue_id"]]["revision"]
    store._review_status(state, closure)
    if review is not None:
        batch = state["batch_review"]; item = record["item_id"]; axes = decision["axes"]
        previous = copy.deepcopy(batch["assignments"].get(item))
        if previous:
            previous.pop("previous_disposition", None)
        processing = decision["processing_state"]
        if processing == "unfinished" and previous:
            row = copy.deepcopy(previous)
            row.update(record_id=decision["decision_id"], pending_steps=closure["pending_steps"])
        else:
            family = axes["family_id"]
            row = {"item_id": item, "disposition": "pool" if axes["family_status"] == "unresolved" else "current" if family == review["family_id"] else "other",
                   "family_id": family, "reason": decision["reason"], "record_id": decision["decision_id"],
                   "pending_steps": closure["pending_steps"], "boundary_flag": axes["boundary_status"] == "needs_review"}
        row.update(review_id=directed, stage="family", review_origin="directed_recheck", organization_id=review["organization_id"],
                   family_origin_id=review["family_id"], source_family_id=review["family_id"],
                   input_fingerprint=state["contract"]["input_fingerprint"], form_status=axes["form_status"],
                   decision_source="manual_fine_decision", fine_processing_state=processing,
                   previous_disposition=previous, recorded_at=record["recorded_at"],
                   legacy_refinement={"decision_id": decision["decision_id"], "issue_id": decision["issue_id"],
                       "processing_state": processing, "form_status": axes["form_status"], "leaf_id": axes["leaf_id"],
                       "catalogue_revision": state["catalogue_revision"]})
        batch["assignments"][item] = row
        review["decisions"][item] = copy.deepcopy(row)
        batch["legacy_dirty_ids"] = [i for i in batch.get("legacy_dirty_ids", []) if i != item]
        review["expected_item_versions"][item] = item_version(state, item)
        review["expected_family_members"] = family_members(state, review["family_id"])
        sync_directed_results(state, review)
