"""Resolve saved fine judgments against current family and leaf membership."""
from __future__ import annotations

import copy


def projected_fine_form(item_id, current_issue_ids, saved_assignment, family_id, leaf_id, decisions, effective_ids):
    """Use the current fine decision, including an explicitly carried batch refinement."""
    refinement_id = (saved_assignment.get("legacy_refinement") or {}).get("decision_id")
    latest = max((row for row in decisions
                  if row["decision_id"] in effective_ids and item_id in row.get("affected_ids", ())
                  and (row.get("issue_id") in current_issue_ids or row["decision_id"] == refinement_id)),
                 key=lambda row: (row.get("catalogue_revision_after", -1), row["decision_id"]), default={})
    axes = next((record["axes"] for record in latest.get("item_axes", ()) if record["item_id"] == item_id),
                latest.get("axes"))
    if axes and (axes.get("family_id") != family_id or axes.get("leaf_id") != leaf_id
                 or (axes.get("family_status") == "assigned") != (family_id is not None)):
        # A later reassignment can leave an old issue in the audit. Its evidence
        # belongs to the earlier membership and cannot label the new one.
        latest, axes = {}, None
    return {"axes": copy.deepcopy(axes) if axes else None,
            "form_status": (axes or {}).get("form_status", saved_assignment.get("form_status")),
            "decision_id": latest.get("decision_id"),
            "axes_reason": latest.get("reason") or latest.get("rationale") or ""}
