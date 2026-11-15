"""范围与限定完成、标签检查点；共同服务保持唯一权威。"""
from __future__ import annotations
from .annotation_english import system_fields, system_text, system_error

import copy
import hashlib
import json
import threading
import uuid

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog,
    QFormLayout, QHeaderView, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton,
    QScrollArea, QStackedWidget, QTableWidget, QTableWidgetItem, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget)

from .annotation_assistant_contracts import canonical_hash
from .annotation_decision_history import effective_rows
from .annotation_manual_protocol import COMPLETION_CHECKS


SCOPE_TEXTS = ("purpose", "coverage_plan", "quality_requirements", "unresolved_policy", "exclusions_policy")
COMPLETION_TEXTS = ("quality_evidence", "unresolved_summary", "exclusions_summary", "limitations")
SCHEMA = "syllafolio-desktop-final-form-draft-v1"
FILENAME = "desktop_final_draft.json"
CHOOSE = "__final_choose__"


class FinalReviewError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(f"{code}: {message}")


def _require(value, code, message):
    if not value:
        raise FinalReviewError(code, message)


def _items(state, scope_ids):
    latest = {}
    for decision in sorted(effective_rows(state), key=lambda row: (row.get("catalogue_revision_after", -1), row["decision_id"])):
        for item in decision.get("affected_ids", []):
            latest[item] = next((row["axes"] for row in decision.get("item_axes", []) if row["item_id"] == item), decision.get("axes"))
    memberships = state.get("memberships", [])
    assignments = state.get("batch_review", {}).get("assignments", {})
    rows = []
    for item in state.get("items", []):
        ident = item["item_id"]
        families = sorted(row["local_type_id"] for row in memberships if row["parent_type_id"] is None and ident in row["member_ids"])
        leaves = sorted(row["local_type_id"] for row in memberships if row["parent_type_id"] is not None and ident in row["member_ids"])
        saved = assignments.get(ident, {})
        axes = latest.get(ident) or {}
        axes_current = (axes.get("family_id") == (families[0] if len(families) == 1 else None)
                        and axes.get("leaf_id") == (leaves[0] if len(leaves) == 1 else None))
        form = axes.get("form_status") if axes_current and axes else saved.get("form_status") if saved.get("family_id") in families else None
        disposition = item.get("batch_disposition", saved.get("disposition"))
        processing = item.get("processing_state", "unfinished")
        category = ("unfinished" if processing == "unfinished" else "excluded" if disposition == "discard" else
                    "family_known_form_unresolved" if families and form in {None, "not_assessed", "unresolved", "candidate_form"} else
                    "reviewed_unresolved" if processing == "reviewed_unresolved" else "decided")
        rows.append({"item_id": ident, "in_scope": ident in scope_ids, "processing_state": processing,
                     "category": category, "family_ids": families, "leaf_ids": leaves, "form_status": form,
                     "disposition": disposition, "review_id": item.get("batch_review_id"),
                     "closure_confirmation_required": item.get("closure_confirmation_required", False)})
    return rows


_GAP_TITLES = {
    "SCOPE_REQUIRED": "Declare the review scope",
    "UNFINISHED_ITEMS": "Review unfinished syllables",
    "OPEN_REVIEWS": "Finish open reviews",
    "ACTIVE_JOBS": "Check running tasks",
    "TYPE_DEFINITIONS": "Review type definitions",
    "STALE_TYPE_DEFINITIONS": "Update type definitions",
    "DEFINITION_REVIEW_REQUIRED": "Recheck members against the revised criteria",
    "BATCH_FIRST_PASS_PENDING": "Finish the first review of candidate groups",
    "BATCH_POOL_PENDING": "Complete global pool coverage",
    "BATCH_FAMILY_PENDING": "Finish open family reviews",
    "BATCH_EXTERNAL_CHANGE": "Recheck changed syllables",
    "BATCH_REVIEW_OPEN": "Close outstanding review rounds",
    "BATCH_LEAF_UNASSESSED": "Assess the fine form of assigned syllables",
}


def _gap_rows(state, items):
    """Map actual completion blockers to the operation that can resolve each one."""
    final = state.get("final_review", {})
    batch = state.get("batch_review", {})
    reviews = batch.get("reviews", {})
    types = {r["local_type_id"]: r for r in state.get("memberships", [])}
    cards = state.get("type_cards", {})
    groups = {g["group_id"]: g for g in (batch.get("organization") or {}).get("groups", [])}
    stages = final.get("stages", {})
    def type_label(key):
        return cards.get(key, {}).get("title") or key

    result = []
    card_task = None
    for gap in final.get("gaps", state.get("completion", {}).get("missing", [])):
        code = gap["code"]
        location = gap.get("location", {})
        targets = {}
        def add(kind, ident, label):
            targets[kind, ident] = {"kind": kind, "id": ident, "label": label}

        def add_round(rid):
            review = reviews.get(rid, {})
            if review and not review.get("archived") and not review.get("closed"):
                family = review.get("family_id")
                group = groups.get(review.get("group_id"), {})
                name = type_label(family) if family else group.get("label") or (
                    "Global pool" if review.get("stage") == "pool" else rid)
                add("review", rid, "Open review · " + name)

        if code == "SCOPE_REQUIRED":
            add("scope", "scope", "Set review scope")
        elif code == "ACTIVE_JOBS":
            add("tasks", "tasks", "View running tasks")
        elif code == "BATCH_FIRST_PASS_PENDING":
            for key in stages.get("first", {}).get("pending_ids", []):
                if key in groups:
                    add("group", key, "Open group · " + (groups[key].get("label") or key))
            if not targets:  # Previously saved rows changed outside the original round.
                for key in location.get("item_ids", []):
                    if key in items:
                        add("item", key, "Review " + key)
        elif code in {"TYPE_DEFINITIONS", "STALE_TYPE_DEFINITIONS"}:
            for key in gap.get("type_ids", []):
                if key in types:
                    add("type", key, "Open type card · " + type_label(key))
        elif code in {"BATCH_FAMILY_PENDING", "BATCH_REVIEW_OPEN"}:
            for key in gap.get("review_ids", []):
                add_round(key)
                # An assignment can still point at its closed source round
                # while a replacement round or a later fine review needs closure.
                source = reviews.get(key, {})
                if source.get("closed"):
                    if source.get("group_id") in groups:
                        gid = source["group_id"]
                        add("group", gid, "Open group · " + (groups[gid].get("label") or gid))
                    elif source.get("stage") == "pool":
                        add("pool", "pool", "Open global pool")
                    else:
                        for item in state.get("items", []):
                            if item.get("batch_review_id") == key and item.get("closure_confirmation_required"):
                                add("fine", item["item_id"], "Open fine review · " + item["item_id"])
        elif code == "OPEN_REVIEWS":
            for key in gap.get("issue_ids", []):
                issue = state.get("issues", {}).get(key, {})
                rid = issue.get("directed_review_id") or issue.get("directed_parent_review_id")
                if rid in reviews and not reviews[rid].get("closed") and not reviews[rid].get("archived"):
                    add_round(rid)
                else:
                    for ident in issue.get("target_ids", []):
                        if ident in items:
                            add("fine", ident, "Open fine review · " + ident)
        elif code in {"DEFINITION_REVIEW_REQUIRED", "BATCH_POOL_PENDING"}:
            # The backend can identify a directed review as the remaining
            # prerequisite even after the original global pool round closed.
            for rid in gap.get("review_ids", []):
                add_round(rid)
            if not targets:
                for key in location.get("family_ids", []):
                    if key in types and (code == "DEFINITION_REVIEW_REQUIRED" or location.get("view") == "final"):
                        active_id = batch.get("active_family_reviews", {}).get(key)
                        if reviews.get(active_id, {}).get("directed_issue_id"):
                            add_round(active_id)
                        if ("review", active_id) not in targets:
                            add("directed", key, "Review changed criteria · " + type_label(key))
            if not targets and code == "DEFINITION_REVIEW_REQUIRED":
                for key in gap.get("item_ids", []):
                    if key in items:
                        add("fine", key, "Open fine review · " + key)
            if not targets and code == "BATCH_POOL_PENDING":
                add("pool", "pool", "Open global pool")
        else:
            kind = "fine" if code == "BATCH_LEAF_UNASSESSED" else "item"
            for key in gap.get("item_ids", []):
                if key in items:
                    add(kind, key, "Review " + key)
        message = system_text(gap.get("message", ""))
        task = {"code": code, "title": _GAP_TITLES.get(code, message),
                "message": message, "targets": list(targets.values())}
        if code in {"TYPE_DEFINITIONS", "STALE_TYPE_DEFINITIONS"}:
            if card_task is not None:
                # A draft definition can also have outdated membership. Opening
                # and reviewing the card resolves both reasons in one place.
                card_task["codes"].append(code)
                card_task["title"] = _GAP_TITLES["TYPE_DEFINITIONS"]
                card_task["message"] += "\n" + message
                seen = {t["id"] for t in card_task["targets"]}
                card_task["targets"].extend(t for t in task["targets"] if t["id"] not in seen)
                continue
            card_task = task
            card_task["codes"] = [code]
        result.append(task)
    return result


class FinalReviewController:
    """投影不产生完成标志；四类写动作只传原payload/CAS/幂等键。"""
    def __init__(self, project):
        self.project = project
        self.requests, self.pending = {}, {}
        self.base_ticket = self.ticket()

    def identity(self):
        return {"project_id": self.project.metadata["project_id"], "run_id": self.project.service.run_id,
                "input_fingerprint": self.project.dataset.input_fingerprint}

    def ticket(self, state=None):
        state = self.project.snapshot() if state is None else state
        return {**self.identity(), "revision": state["revision"],
                "scope_id": (state.get("catalogue_scope") or {}).get("scope_id")}

    def rebase(self, state=None):
        self.base_ticket = self.ticket(state)
        return copy.deepcopy(self.base_ticket)

    def snapshot(self, state=None):
        state = self.project.snapshot() if state is None else state
        report = copy.deepcopy(state.get("completion", {}))
        final = state.get("final_review", {})
        ids = set(report.get("scope_ids", []))
        rows = _items(state, ids)
        checkpoints = state.get("checkpoints", [])
        if isinstance(checkpoints, dict):
            checkpoints = [{"checkpoint_id": key, **{field: row.get(field) for field in ("reason", "created_at")}} for key, row in checkpoints.items()]
        else:
            checkpoints = copy.deepcopy(checkpoints)
        locations = _gap_rows(state, {row["item_id"] for row in rows})
        outcome_counts = dict.fromkeys(("assigned", "reviewed_unresolved", "excluded", "unfinished"), 0)
        for row in rows:
            if row["in_scope"]:
                outcome = ("unfinished" if row["processing_state"] == "unfinished" else
                           "excluded" if row["disposition"] == "discard" else
                           "assigned" if row["family_ids"] else "reviewed_unresolved")
                outcome_counts[outcome] += 1
        reports = [{"kind": kind, "job_id": row["job_id"], "status": row["status"],
                    "stale_for_current": bool(row.get("stale_for_current", False)), "stale_reasons": copy.deepcopy(row.get("stale_reasons", []))}
                   for kind, section in (("assessment", final.get("assessment", {})), ("catalogue_export", final.get("publication_report", {})))
                   for row in section.get("jobs", [])]
        return {"identity": self.identity(), "revision": state["revision"], "ticket": self.ticket(state),
                "scope": copy.deepcopy(state.get("catalogue_scope")), "scope_declared": state.get("catalogue_scope") is not None,
                "completion": report, "current_completion_id": state.get("current_completion_id"),
                "stages": copy.deepcopy(final.get("stages", {})), "gaps": locations, "items": rows,
                "outcome_counts": outcome_counts,
                "display_categories": {key: sum(row["in_scope"] and row["category"] == key for row in rows) for key in
                    ("unfinished", "reviewed_unresolved", "excluded", "family_known_form_unresolved", "decided")},
                "checkpoints": checkpoints, "reports": reports,
                "history": {"saved_decisions": len(state.get("decisions", [])),
                    "effective_decisions": len(state.get("effective_decision_ids", [])),
                    "completion_records": len(state.get("completion_records", {})),
                    "source_version": state.get("source_registry", {}).get("version"),
                    "last_restore": copy.deepcopy(state.get("last_restore"))},
                "active_tasks": copy.deepcopy(state.get("desktop_jobs", {}).get("blocking_jobs", [])),
                "scientific_validity_certified": False}

    def resolve_location(self, kind, ident, state=None):
        state = self.project.snapshot() if state is None else state
        gaps = _gap_rows(state, {r["item_id"] for r in state["items"]})
        _require(any(target["kind"] == kind and target["id"] == ident
                     for gap in gaps for target in gap["targets"]),
                 "FINAL_LOCATION_SCOPE", "This task is no longer outstanding. Refresh the final checks.")
        return {"kind": kind, "id": ident}

    def _execute(self, action, payload, *, ticket=None, idempotency_key=None):
        _require(idempotency_key is None or isinstance(idempotency_key, str) and 0 < len(idempotency_key) <= 160,
                 "IDEMPOTENCY_REQUIRED", "An explicit request key must be a nonempty string")
        ticket = copy.deepcopy(self.base_ticket if ticket is None else ticket)
        _require(all(ticket.get(key) == value for key, value in self.identity().items()), "FINAL_CONTEXT_CHANGED", "The command belongs to another project/run/input")
        request = {"action": action, "payload": copy.deepcopy(payload), "ticket": ticket}
        signature = canonical_hash(request)
        key = idempotency_key or self.pending.get(signature) or "desktop-final-" + uuid.uuid4().hex
        if key in self.requests:
            _require(self.requests[key] == request, "IDEMPOTENCY_CONFLICT", "Retry the original exact final-review request")
        else:
            self.requests[key] = request
            self.pending[signature] = key
        return self.project.execute(action, copy.deepcopy(payload), expected_revision=ticket["revision"], idempotency_key=key)

    def declare_scope(self, payload, **kwargs):
        return self._execute("catalogue_scope", payload, **kwargs)

    def complete(self, payload, *, ticket=None, **kwargs):
        ticket = self.base_ticket if ticket is None else ticket
        _require(payload.get("scope_id") == ticket.get("scope_id") and payload.get("scope_id") is not None,
                 "SCOPE_CHANGED", "Confirm the currently displayed saved scope, not an unsent scope draft")
        return self._execute("scoped_complete", payload, ticket=ticket, **kwargs)

    def save_checkpoint(self, reason, **kwargs):
        _require(isinstance(reason, str) and bool(reason.strip()), "REASON_REQUIRED", "Enter the actual checkpoint purpose")
        return self._execute("checkpoint", {"reason": reason}, **kwargs)

    def restore_checkpoint(self, checkpoint_id, *, confirmed, **kwargs):
        _require(confirmed is True, "CHECKPOINT_CONFIRMATION", "Explicitly confirm restoring active labels while retaining history")
        _require(isinstance(checkpoint_id, str) and bool(checkpoint_id), "CHECKPOINT_NOT_FOUND", "Select an existing checkpoint")
        return self._execute("restore", {"checkpoint_id": checkpoint_id}, **kwargs)


class _FormDraft:
    def __init__(self, project):
        self.project, self.path = project, project.project_dir / FILENAME
        self.identity = {"project_id": project.metadata["project_id"], "run_id": project.service.run_id,
                         "input_fingerprint": project.dataset.input_fingerprint}
        if not hasattr(project.service, "_desktop_final_draft_lock"):
            project.service._desktop_final_draft_lock = threading.RLock()
        self.lock = project.service._desktop_final_draft_lock
        self.warning, self.digest = None, None

    def load(self):
        with self.lock:
            if not self.path.exists() and not self.path.is_symlink():
                self.digest = None
                return None
            _require(not self.path.is_symlink() and self.path.resolve().is_relative_to(self.project.project_dir), "FINAL_DRAFT_PATH", "Final draft must remain in this project")
            raw = self.path.read_bytes(); self.digest = hashlib.sha256(raw).hexdigest()
            try:
                def pairs(values):
                    result = {}
                    for key, item in values:
                        _require(key not in result, "FINAL_DRAFT_JSON", "Duplicate draft field")
                        result[key] = item
                    return result
                def invalid(value):
                    raise FinalReviewError("FINAL_DRAFT_JSON", "Nonfinite draft value: " + value)
                value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=invalid)
                _require(isinstance(value, dict) and value.get("schema_version") == SCHEMA
                         and all(value.get(key) == expected for key, expected in self.identity.items())
                         and isinstance(value.get("form"), dict) and value.get("submission_state") == "unsubmitted",
                         "FINAL_DRAFT_IDENTITY", "Final draft schema or identity does not match")
                form = value["form"]
                _require(form.get("scope_mode") in {CHOOSE, "all", "local"}
                         and form.get("target_level") in {CHOOSE, "family", "leaf"}
                         and isinstance(form.get("scope_ids"), list) and all(isinstance(item, str) for item in form["scope_ids"])
                         and isinstance(form.get("scope_texts"), dict) and set(form["scope_texts"]) == set(SCOPE_TEXTS)
                         and all(isinstance(item, str) for item in form["scope_texts"].values())
                         and isinstance(form.get("completion_texts"), dict) and set(form["completion_texts"]) == set(COMPLETION_TEXTS)
                         and all(isinstance(item, str) for item in form["completion_texts"].values())
                         and isinstance(form.get("checkpoint_reason"), str)
                         and form.get("checks") == dict.fromkeys(COMPLETION_CHECKS, False)
                         and form.get("restore_confirmed") is False,
                         "FINAL_DRAFT_FORM", "Final form draft fields are malformed")
                return value
            except (ValueError, UnicodeError, TypeError) as exc:
                backup = self.path.with_name("desktop_final_draft.recovery-" + self.digest[:16] + ".json")
                if backup.exists():
                    _require(backup.read_bytes() == raw, "FINAL_DRAFT_BACKUP", "Recovery copy has different bytes")
                else:
                    with backup.open("xb") as stream: stream.write(raw)
                self.warning = "Original form draft retained: " + backup.name + ": " + system_error(exc)
                return None

    def save(self, form, binding):
        with self.lock:
            self.load()
            value = {"schema_version": SCHEMA, **self.identity, "binding": copy.deepcopy(binding),
                     "submission_state": "unsubmitted", "form": copy.deepcopy(form)}
            value["form"]["checks"] = dict.fromkeys(COMPLETION_CHECKS, False)
            value["form"]["restore_confirmed"] = False
            temporary = self.path.with_name(".final-draft-" + uuid.uuid4().hex + ".tmp")
            try:
                temporary.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + "\n", encoding="utf-8")
                current = hashlib.sha256(self.path.read_bytes()).hexdigest() if self.path.exists() else None
                _require(not self.path.is_symlink() and current == self.digest, "FINAL_DRAFT_CHANGED", "Draft changed while saving; no original was overwritten")
                temporary.replace(self.path)
            finally:
                temporary.unlink(missing_ok=True)


def _table(headers):
    table = QTableWidget(0, len(headers)); table.setHorizontalHeaderLabels(headers)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    return table


class FinalReviewDialog(QDialog):
    locationRequested = Signal(dict)
    assessmentRequested = Signal()
    changed = Signal(object)

    _scope_fields = {
        "purpose": ("Purpose", "Document the syllable types in this scope."),
        "coverage_plan": ("Coverage", "Review each syllable in the declared scope."),
        "quality_requirements": ("Quality criteria", "Check the review coverage, type definitions and examples."),
        "unresolved_policy": ("Unresolved items", "Keep reviewed but unresolved items and record their reasons."),
        "exclusions_policy": ("Exclusions", "Retain excluded items with their reasons."),
    }
    _evidence_labels = {
        "quality_evidence": "Checks performed",
        "unresolved_summary": "Reviewed uncertainties",
        "exclusions_summary": "Exclusions",
        "limitations": "Limitations",
    }
    _check_labels = {
        "coverage_confirmed": "I checked coverage of the declared scope.",
        "type_definitions_reviewed": "I reviewed the relevant type definitions.",
        "quality_checks_passed": "I confirm the stated quality checks passed.",
        "unresolved_reported": "I have accounted for unresolved items.",
        "exclusions_reported": "I have accounted for exclusions.",
    }

    def __init__(self, owner, *, history_only=False):
        super().__init__(owner)
        self.history_only = history_only
        self.owner, self.project = owner, owner.project
        self.generation = owner._close_coordinator.generation
        self.controller = FinalReviewController(self.project)
        self.store = None if history_only else _FormDraft(self.project)
        self._draft_available = bool(self.store and self.store.load())
        self._revision, self._view = None, None
        self._dirty = self._loading = self._waiting = False
        self._scope_modified = False
        self._invalid = self._restore_ticket = None
        self._auto_summaries = {}
        self.setWindowTitle("Checkpoints and history" if history_only else "Final checks")
        area = owner.screen().availableGeometry() if owner.screen() else None
        self.resize(min(850, area.width() - 32) if area else 850,
                    min(620, area.height() - 64) if area else 620)
        layout = QVBoxLayout(self)
        self.pages = QStackedWidget()
        layout.addWidget(self.pages, 1)
        if history_only:
            self._build_history()
        else:
            self._build_overview()
            self._build_scope()
            self._build_completion()
        self.message = QLabel()
        self.message.setWordWrap(True)
        self.message.setVisible(False)
        layout.addWidget(self.message)
        self.close_button = QPushButton("Close")
        self.close_button.clicked.connect(self.reject)
        footer = QHBoxLayout()
        if not history_only:
            self.restore_draft_button = QPushButton("Restore form draft…")
            self.restore_draft_button.clicked.connect(lambda: self._guard(self.restore_draft))
            footer.addWidget(self.restore_draft_button)
        footer.addStretch()
        if not history_only:
            footer.addWidget(self.begin_button)
            self.assessment_button = QPushButton("Open internal assessment")
            self.assessment_button.setObjectName("finalOpenAssessment")
            self.assessment_button.clicked.connect(lambda: self.assessmentRequested.emit())
            footer.addWidget(self.assessment_button)
        footer.addWidget(self.close_button)
        layout.addLayout(footer)
        self.timer = QTimer(self)
        self.timer.setInterval(350)
        self.timer.timeout.connect(lambda: self._guard(self.refresh))
        self.finished.connect(lambda _result: self.timer.stop())
        owner.operationFailed.connect(self._operation_failed)
        self.refresh(force=True)
        if self.store and self.store.warning:
            self._notice(self.store.warning)

    @staticmethod
    def _heading(text):
        label = QLabel(text)
        font = label.font()
        font.setBold(True)
        label.setFont(font)
        return label

    def _page(self, name):
        page = QWidget()
        page.setObjectName(name)
        outer = QVBoxLayout(page)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        inner = QVBoxLayout(body)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)
        self.pages.addWidget(page)
        return page, outer, inner

    def _build_overview(self):
        self.overview_page = QWidget()
        self.overview_page.setObjectName("finalOverview")
        self.pages.addWidget(self.overview_page)
        layout = QVBoxLayout(self.overview_page)
        scope_header = QHBoxLayout()
        scope_header.addWidget(self._heading("Review scope"))
        scope_header.addStretch()
        self.edit_scope_button = QPushButton("Edit scope…")
        self.edit_scope_button.setObjectName("finalEditScope")
        self.edit_scope_button.clicked.connect(self.show_scope)
        scope_header.addWidget(self.edit_scope_button)
        layout.addLayout(scope_header)
        self.saved_scope = QLabel()
        self.saved_scope.setWordWrap(True)
        self.saved_scope.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.saved_scope)
        self.outcomes = QLabel()
        self.outcomes.setWordWrap(True)
        layout.addWidget(self.outcomes)
        layout.addSpacing(10)
        self.todo_heading = self._heading("Items to resolve")
        layout.addWidget(self.todo_heading)
        self.gaps = QTreeWidget()
        self.gaps.setObjectName("finalTasks")
        self.gaps.setHeaderLabels(["Review task", "Action"])
        self.gaps.header().setStretchLastSection(False)
        self.gaps.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.gaps.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.gaps.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.gaps.itemActivated.connect(lambda item, _column: self._guard(lambda: self._activate_task(item)))
        self.gaps.itemClicked.connect(lambda item, column: self._guard(lambda: self._activate_task(item))
                                      if item.parent() is not None and column == 1 else None)
        layout.addWidget(self.gaps, 1)
        self.ready_message = QLabel()
        self.ready_message.setWordWrap(True)
        layout.addWidget(self.ready_message, 1, Qt.AlignmentFlag.AlignTop)
        self.begin_button = QPushButton("Confirm completion…")
        self.begin_button.setObjectName("finalBeginConfirmation")
        self.begin_button.clicked.connect(lambda: self._guard(self.show_confirmation))

    def _back_button(self, layout):
        button = QPushButton("Back to overview")
        button.clicked.connect(self.show_overview)
        layout.addWidget(button)

    def _build_scope(self):
        self.scope_page, layout, inner = self._page("finalScopePage")
        inner.addWidget(self._heading("Review scope"))
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.scope_mode = QComboBox()
        self.scope_mode.addItem("All project syllables", "all")
        self.scope_mode.addItem("Selected syllables", "local")
        self.level = QComboBox()
        self.level.addItem("Family", "family")
        self.level.addItem("Leaf / fine form", "leaf")
        form.addRow("Syllables", self.scope_mode)
        form.addRow("Target level", self.level)
        self.scope_mode.currentIndexChanged.connect(self._scope_changed)
        self.level.currentIndexChanged.connect(self._scope_changed)
        inner.addLayout(form)
        self.scope_items = _table(["Include", "Syllable", "Review state"])
        self.scope_items.setMinimumHeight(140)
        self.scope_items.horizontalHeader().setStretchLastSection(True)
        self.scope_items.itemChanged.connect(self._scope_changed)
        form.addRow(self.scope_items)
        self.scope_texts = {}
        self.policy_summary = QLabel()
        self.policy_summary.setWordWrap(True)
        self.policy_summary.setTextFormat(Qt.TextFormat.PlainText)
        inner.addWidget(self.policy_summary)
        self.policy_toggle = QPushButton("Edit review policies…")
        self.policy_toggle.setCheckable(True)
        inner.addWidget(self.policy_toggle, 0, Qt.AlignmentFlag.AlignLeft)
        self.policy_panel = QWidget()
        policy_form = QFormLayout(self.policy_panel)
        policy_form.setContentsMargins(0, 0, 0, 0)
        inner.addWidget(self.policy_panel)
        self.policy_panel.hide()
        self.policy_toggle.toggled.connect(self._toggle_policies)
        for key, (label, default) in self._scope_fields.items():
            edit = QPlainTextEdit(default)
            edit.setObjectName("finalScope_" + key)
            edit.setFixedHeight(55)
            edit.textChanged.connect(self._scope_changed)
            self.scope_texts[key] = edit
            (form if key == "purpose" else policy_form).addRow(label, edit)
        inner.addStretch(1)
        row = QHBoxLayout()
        self._back_button(row)
        self.reset_scope_button = QPushButton("Reset edits")
        self.reset_scope_button.clicked.connect(self._reset_scope)
        row.addWidget(self.reset_scope_button)
        row.addStretch()
        self.scope_button = QPushButton("Declare scope")
        self.scope_button.setObjectName("finalDeclareScope")
        self.scope_button.clicked.connect(lambda: self._guard(self.declare_scope))
        row.addWidget(self.scope_button)
        layout.addLayout(row)

    def _toggle_policies(self, expanded):
        self.policy_panel.setVisible(expanded)
        self.policy_summary.setVisible(not expanded)
        self.policy_toggle.setText("Hide policy editor" if expanded else "Edit review policies…")
        self._refresh_policy_summary()

    def _refresh_policy_summary(self):
        self.policy_summary.setText("\n".join(
            f"{label}: {self.scope_texts[key].toPlainText().strip()}"
            for key, (label, _) in self._scope_fields.items() if key != "purpose"))

    def _build_completion(self):
        self.confirmation_page, layout, inner = self._page("finalCompletionPage")
        inner.addWidget(self._heading("Confirm completion"))
        self.confirm_scope = QLabel()
        self.confirm_scope.setWordWrap(True)
        inner.addWidget(self.confirm_scope)
        form = QFormLayout()
        self.completion_texts = {}
        for key, label in self._evidence_labels.items():
            edit = QPlainTextEdit()
            edit.setFixedHeight(54)
            edit.setObjectName("finalEvidence_" + key)
            edit.setPlaceholderText({"quality_evidence": "Which checks did you perform?",
                "limitations": "Describe any limitations, or enter None if none apply."}.get(key, ""))
            edit.textChanged.connect(self._evidence_changed)
            self.completion_texts[key] = edit
            form.addRow(label, edit)
        inner.addLayout(form)
        self.checks = {}
        for key, label in self._check_labels.items():
            check = QCheckBox(label)
            check.setObjectName("finalCheck_" + key)
            check.toggled.connect(self._buttons)
            self.checks[key] = check
            inner.addWidget(check)
        inner.addStretch()
        row = QHBoxLayout()
        self._back_button(row)
        row.addStretch()
        self.complete_button = QPushButton("Confirm completion")
        self.complete_button.setObjectName("finalSubmitCompletion")
        self.complete_button.clicked.connect(lambda: self._guard(self.complete))
        row.addWidget(self.complete_button)
        layout.addLayout(row)

    def _build_history(self):
        self.history_page, layout, inner = self._page("finalHistoryPage")
        inner.addWidget(self._heading("Label checkpoints"))
        self.history_summary = QLabel()
        self.history_summary.setWordWrap(True)
        inner.addWidget(self.history_summary)
        self.checkpoints = _table(["Checkpoint ID", "Purpose", "Recorded at"])
        self.checkpoints.setMinimumHeight(155)
        self.checkpoints.itemSelectionChanged.connect(self._checkpoint_selected)
        inner.addWidget(self.checkpoints)
        row = QHBoxLayout()
        self.checkpoint_reason = QLineEdit()
        self.checkpoint_reason.setPlaceholderText("Checkpoint purpose")
        self.checkpoint_reason.textChanged.connect(self._buttons)
        row.addWidget(self.checkpoint_reason)
        self.checkpoint_button = QPushButton("Save checkpoint")
        self.checkpoint_button.clicked.connect(lambda: self._guard(self.save_checkpoint))
        row.addWidget(self.checkpoint_button)
        inner.addLayout(row)
        warning = QLabel("Restoring a checkpoint replaces the current labels with the selected version. Later decisions, job history and the latest source mapping are retained. Completion and report applicability are checked again.")
        warning.setWordWrap(True)
        inner.addWidget(warning)
        self.restore_confirm = QCheckBox("I confirm these restore effects")
        self.restore_confirm.toggled.connect(self._restore_acknowledged)
        inner.addWidget(self.restore_confirm)
        self.restore_button = QPushButton("Restore label checkpoint")
        self.restore_button.clicked.connect(lambda: self._guard(self.restore_checkpoint))
        inner.addWidget(self.restore_button)
        inner.addWidget(self._heading("Report history"))
        self.report_history = QPlainTextEdit()
        self.report_history.setReadOnly(True)
        inner.addWidget(self.report_history)

    def _notice(self, text):
        self.message.setText(text)
        self.message.setVisible(bool(text))

    def is_current(self):
        return (self.owner.project is self.project and self.owner._close_coordinator.generation == self.generation
                and self.owner._close_coordinator.state == "open" and self._invalid is None)

    def _guard(self, function):
        try:
            return function()
        except Exception as exc:
            self._notice(system_error(exc))
            self.owner._error(exc, operation=self.windowTitle())

    def _operation_failed(self, message):
        if self._waiting:
            self._waiting = False
            self._notice(message)
            self._buttons()

    def _clear_confirmations(self):
        if self.history_only:
            self._restore_ticket = None
            self.restore_confirm.setChecked(False)
        else:
            for check in self.checks.values():
                check.setChecked(False)

    def _scope_changed(self, *_args):
        if not self._loading:
            if self.scope_mode.currentData() == "local" and self.scope_items.rowCount() == 0:
                self._loading = True
                try:
                    self._populate_subset(set())
                finally:
                    self._loading = False
            self._dirty = self._scope_modified = True
            self._refresh_policy_summary()
            self._clear_confirmations()
            self._buttons()

    def _evidence_changed(self):
        if not self._loading:
            self._dirty = True
            self._clear_confirmations()
            self._buttons()

    def _scope_payload(self):
        ids = ([row["item_id"] for row in self._view["items"]] if self.scope_mode.currentData() == "all" else
               [self.scope_items.item(i, 1).text() for i in range(self.scope_items.rowCount())
                if self.scope_items.item(i, 0).checkState() == Qt.CheckState.Checked])
        return {"scope_ids": ids, "target_level": self.level.currentData(),
                **{key: edit.toPlainText().strip() for key, edit in self.scope_texts.items()}}

    def _scope_matches(self):
        saved = self._view["scope"]
        if saved is None:
            return False
        if not self._scope_modified:
            return True
        value = self._scope_payload()
        return (set(value["scope_ids"]) == set(saved["scope_ids"]) and
                all(value[key] == saved.get(key) for key in ("target_level", *SCOPE_TEXTS)))

    def _buttons(self, *_args):
        if not self._view or self._loading:
            return
        available = self.is_current() and not self.owner._busy and not self._waiting
        self.pages.setEnabled(available)
        self._available = available
        if self.history_only:
            self.checkpoint_button.setEnabled(bool(self.checkpoint_reason.text().strip()))
            self.restore_button.setEnabled(self._selected_checkpoint() is not None and self.restore_confirm.isChecked()
                                           and self._restore_ticket is not None)
            return
        on_scope = self.pages.currentWidget() is self.scope_page
        self.scope_items.setVisible(self.scope_mode.currentData() == "local")
        if on_scope:
            payload = self._scope_payload()
            self.reset_scope_button.setVisible(self._scope_modified)
            self.scope_button.setEnabled(bool(payload["scope_ids"]) and all(payload[k] for k in SCOPE_TEXTS)
                                         and not self._scope_matches())
        self.restore_draft_button.setVisible(self._draft_available and self.pages.currentWidget() is not self.overview_page)
        self.restore_draft_button.setEnabled(available)
        can_confirm = (self._view["scope_declared"] and self._view["completion"].get("can_submit", False)
                       and not self._view["current_completion_id"] and self._scope_matches())
        self.begin_button.setVisible(self.pages.currentWidget() is self.overview_page and not self._view["current_completion_id"])
        self.begin_button.setEnabled(can_confirm and available)
        self.assessment_button.setVisible(self.pages.currentWidget() is self.overview_page
                                          and bool(self._view["current_completion_id"]))
        self.assessment_button.setEnabled(available and self._scope_matches())
        self.complete_button.setEnabled(can_confirm and self.pages.currentWidget() is self.confirmation_page
                                       and all(c.isChecked() for c in self.checks.values())
                                       and all(w.toPlainText().strip() for w in self.completion_texts.values()))

    def _selected_checkpoint(self):
        rows = self.checkpoints.selectionModel().selectedRows()
        return self.checkpoints.item(rows[0].row(), 0).text() if rows else None

    def _checkpoint_selected(self):
        if not self._loading:
            self.restore_confirm.setChecked(False)
            self._restore_ticket = None
            self._buttons()

    def _restore_acknowledged(self, checked):
        self._restore_ticket = copy.deepcopy(self._view["ticket"]) if checked else None
        self._buttons()

    def _set_scope_form(self, mode, ids, level, texts):
        self._loading = True
        try:
            self.scope_mode.setCurrentIndex(max(0, self.scope_mode.findData(mode)))
            self.level.setCurrentIndex(max(0, self.level.findData(level)))
            for key, edit in self.scope_texts.items():
                edit.setPlainText(texts[key])
            self.scope_items.setRowCount(0)
            if mode == "local":
                self._populate_subset(set(ids))
        finally:
            self._loading = False
        self._refresh_policy_summary()

    def _populate_subset(self, selected):
        rows = self._view["items"]
        self.scope_items.setRowCount(len(rows))
        for i, row in enumerate(rows):
            include = QTableWidgetItem()
            include.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsUserCheckable)
            include.setCheckState(Qt.CheckState.Checked if row["item_id"] in selected else Qt.CheckState.Unchecked)
            self.scope_items.setItem(i, 0, include)
            self.scope_items.setItem(i, 1, QTableWidgetItem(row["item_id"]))
            self.scope_items.setItem(i, 2, QTableWidgetItem(row["processing_state"].replace("_", " ")))

    def show_scope(self):
        if self.history_only:
            return
        self.pages.setCurrentWidget(self.scope_page)
        self._clear_confirmations()
        self._notice("")
        self._buttons()

    def show_overview(self):
        if self.history_only:
            return
        self.pages.setCurrentWidget(self.overview_page)
        self._clear_confirmations()
        if not (self.store and self.store.warning):
            pending = self._scope_modified and not self._scope_matches()
            self._notice("Scope edits have not been declared. Open the scope editor to apply or reset them." if pending else "")
        self._buttons()

    def show_confirmation(self):
        self.refresh()
        _require(self.begin_button.isEnabled(), "COMPLETION_MISSING", "Resolve outstanding tasks and save the scope first.")
        self._clear_confirmations()
        counts = self._view["outcome_counts"]
        form_unresolved = sum(row["in_scope"] and bool(row["family_ids"]) and row["form_status"] == "unresolved"
                              for row in self._view["items"])
        defaults = {
            "unresolved_summary": f"Family unresolved: {counts['reviewed_unresolved']}. Assigned family with unresolved fine form: {form_unresolved}.",
            "exclusions_summary": f"Excluded syllables: {counts['excluded']}.",
        }
        self._loading = True
        for key, text in defaults.items():
            field = self.completion_texts[key]
            if not field.toPlainText() or field.toPlainText() == self._auto_summaries.get(key):
                field.setPlainText(text)
        self._loading = False
        self._auto_summaries = defaults
        self.confirm_scope.setText(self.saved_scope.text())
        self.pages.setCurrentWidget(self.confirmation_page)
        self._notice("")
        self._buttons()

    def refresh(self, force=False):
        if not self.is_current():
            self.invalidate("This project has changed or is closing.")
            return
        if self.owner._busy and not force:
            if getattr(self, "_available", True):
                self._buttons()
            return
        state = self.owner.state if self.owner.state is not None else self.project.snapshot()
        if not force and self._revision == state["revision"]:
            if getattr(self, "_available", None) != (not self.owner._busy and not self._waiting):
                self._buttons()
            return
        if force or self._revision != state["revision"]:
            old_scope = self._view["scope"] if self._view else None
            first = self._view is None
            self._revision = state["revision"]
            self._view = self.controller.snapshot(state)
            self.controller.rebase(state)
            self._clear_confirmations()
            if self.history_only:
                self._refresh_history()
            else:
                scope = self._view["scope"]
                if not self._scope_modified and (first or old_scope != scope):
                    self._reset_scope()
                if self.pages.currentWidget() is self.confirmation_page:
                    self.show_overview()
                    self._notice("The project changed. Review the current scope before confirming again.")
                self._refresh_overview()
        self._buttons()

    def _reset_scope(self):
        saved = self._view["scope"]
        if saved:
            mode = "all" if len(saved["scope_ids"]) == len(self._view["items"]) else "local"
            self._set_scope_form(mode, saved["scope_ids"], saved["target_level"], saved)
        else:
            self._set_scope_form("all", [], "family", {key: pair[1] for key,pair in self._scope_fields.items()})
        self._scope_modified = False
        self._clear_confirmations()
        self._buttons()

    def _refresh_overview(self):
        scope = self._view["scope"]
        n = len(self._view["items"])
        if scope:
            level = "Family level" if scope["target_level"] == "family" else "Leaf / fine form"
            size = f"All {n} syllables" if len(scope["scope_ids"]) == n else f"{len(scope['scope_ids'])} of {n} syllables"
            self.saved_scope.setText(f"{size} · {level}\n{scope['purpose']}")
            self.edit_scope_button.setText("Edit scope…")
        else:
            self.saved_scope.setText(f"No scope declared. Suggested scope: all {n} syllables · Family level.")
            self.edit_scope_button.setText("Set scope…")
        counts = self._view["outcome_counts"]
        self.outcomes.setText(f"Assigned {counts['assigned']}   ·   Reviewed unresolved {counts['reviewed_unresolved']}   ·   "
                              f"Excluded {counts['excluded']}   ·   Unfinished {counts['unfinished']}")
        expanded = {self.gaps.topLevelItem(i).data(0, Qt.ItemDataRole.UserRole)["code"]
                    for i in range(self.gaps.topLevelItemCount()) if self.gaps.topLevelItem(i).isExpanded()}
        gaps = self._view["gaps"]
        self.gaps.clear()
        self.todo_heading.setText(f"Items to resolve · {len(gaps)} {'task' if len(gaps) == 1 else 'tasks'}")
        for gap in gaps:
            targets = gap["targets"]
            title = gap.get("title") or gap["message"]
            item = QTreeWidgetItem([f"{title} ({len(targets)})" if len(targets) > 1 else title, ""])
            item.setToolTip(0, gap["message"])
            action = targets[0] if targets else None
            item.setData(0, Qt.ItemDataRole.UserRole, {"code": gap["code"], "target": action})
            self.gaps.addTopLevelItem(item)
            if action is not None:
                # Only the visible category has a button. Children use the tree's
                # standard click/keyboard handling even for large review scopes.
                button = QPushButton(action["label"])
                button.setMaximumWidth(290)
                button.setToolTip(action["label"])
                button.setAccessibleName(title + " — " + action["label"])
                button.setObjectName("finalOpenTask")
                button.clicked.connect(lambda _checked=False, item=item: self._guard(lambda: self._activate_task(item)))
                self.gaps.setItemWidget(item, 1, button)
            if len(targets) > 1:
                for target in targets:
                    child = QTreeWidgetItem([target["id"], target["label"]])
                    child.setData(0, Qt.ItemDataRole.UserRole, {"code": gap["code"], "target": target})
                    item.addChild(child)
                item.setExpanded(gap["code"] in expanded)
        self.gaps.setVisible(bool(gaps))
        self.ready_message.setVisible(not gaps)
        self.ready_message.setText("Completion confirmed for this scope." if self._view["current_completion_id"]
                                   else "All review tasks for this scope are resolved.")

    def _activate_task(self, item):
        _require(self.is_current() and not self.owner._busy, "FINAL_CONTEXT_CHANGED", "Wait for the active project.")
        data = item.data(0, Qt.ItemDataRole.UserRole)
        target = data.get("target")
        if not target:
            return
        checked = self.controller.resolve_location(target["kind"], target["id"])
        if checked["kind"] == "scope":
            self.show_scope()
            return
        self.locationRequested.emit(dict(checked, gap_code=data["code"]))

    def _refresh_history(self):
        selected = self._selected_checkpoint()
        self._loading = True
        try:
            checkpoints = self._view["checkpoints"]
            self.checkpoints.setRowCount(len(checkpoints))
            for i, row in enumerate(checkpoints):
                for j, key in enumerate(("checkpoint_id", "reason", "created_at")):
                    self.checkpoints.setItem(i, j, QTableWidgetItem(str(row.get(key, ""))))
                if row["checkpoint_id"] == selected:
                    self.checkpoints.selectRow(i)
            history = self._view["history"]
            self.history_summary.setText(f"{len(checkpoints)} saved checkpoints · {history['completion_records']} completion records")
            self.report_history.setPlainText(json.dumps(system_fields({"label_history": history, "reports": self._view["reports"],
                "current_completion_id": self._view["current_completion_id"], "active_tasks": self._view["active_tasks"]}),
                ensure_ascii=False, indent=2))
        finally:
            self._loading = False

    def _run(self, label, function, after=None):
        _require(self.is_current() and not self.owner._busy and not self._waiting,
                 "FINAL_CONTEXT_CHANGED", "Wait for the active operation before submitting.")
        self._waiting = True
        self._buttons()

        def done(result):
            self._waiting = False
            if not self.is_current():
                return
            self.owner.state = self.project.snapshot()
            self.refresh(force=True)
            if after:
                after()
            self._notice(label + " saved.")
            self.changed.emit(result)

        try:
            self.owner._run(label, lambda _progress, _cancelled: function(), done)
        except Exception:
            self._waiting = False
            self._buttons()
            raise

    def declare_scope(self):
        payload, ticket = self._scope_payload(), copy.deepcopy(self._view["ticket"])
        self.save_draft()

        def after():
            self._reset_scope()
            self.show_overview()
        self._run("Review scope", lambda: self.controller.declare_scope(payload, ticket=ticket), after)

    def complete(self):
        _require(self.pages.currentWidget() is self.confirmation_page and self.complete_button.isEnabled(),
                 "COMPLETION_MISSING", "Review the current scope, evidence and five confirmations.")
        ticket = copy.deepcopy(self._view["ticket"])
        payload = {"scope_id": ticket["scope_id"], "checks": {key: check.isChecked() for key, check in self.checks.items()},
                   **{key: edit.toPlainText().strip() for key, edit in self.completion_texts.items()}}
        self.save_draft()
        self._run("Completion", lambda: self.controller.complete(payload, ticket=ticket), self.show_overview)

    def save_checkpoint(self):
        reason, ticket = self.checkpoint_reason.text(), copy.deepcopy(self._view["ticket"])
        self._run("Checkpoint", lambda: self.controller.save_checkpoint(reason, ticket=ticket), self.checkpoint_reason.clear)

    def restore_checkpoint(self):
        ident, ticket, confirmed = self._selected_checkpoint(), copy.deepcopy(self._restore_ticket), self.restore_confirm.isChecked()
        _require(confirmed and ticket is not None, "CHECKPOINT_CONFIRMATION", "Explicitly confirm the selected restore.")
        hook = getattr(self.owner, "before_checkpoint_restore", None)
        if hook is not None:
            hook()
        self._run("Restored checkpoint", lambda: self.controller.restore_checkpoint(ident, confirmed=confirmed, ticket=ticket))

    def save_draft(self):
        if self.history_only or not self._dirty:
            return True
        selected = []
        if self.scope_mode.currentData() == "local":
            selected = self._scope_payload()["scope_ids"]
        with self.store.lock:
            old = (self.store.load() or {}).get("form", {})
            # Preserve legacy checkpoint inputs in pre-existing form drafts.
            form = {"scope_mode": self.scope_mode.currentData(), "scope_ids": selected, "target_level": self.level.currentData(),
                    "scope_texts": {key: edit.toPlainText() for key, edit in self.scope_texts.items()},
                    "completion_texts": {key: edit.toPlainText() for key, edit in self.completion_texts.items()},
                    "checkpoint_reason": old.get("checkpoint_reason", ""), "selected_checkpoint_id": old.get("selected_checkpoint_id")}
            self.store.save(form, (self._view or {}).get("ticket", self.controller.base_ticket))
        self._dirty = False
        self._draft_available = True
        return True

    def restore_draft(self):
        _require(self.is_current() and not self.owner._busy, "FINAL_CONTEXT_CHANGED", "Wait for the active project.")
        value = self.store.load()
        _require(value is not None, "FINAL_DRAFT_MISSING", self.store.warning or "No final-check draft is stored.")
        form = value["form"]
        self._set_scope_form(form["scope_mode"], form["scope_ids"], form["target_level"], form["scope_texts"])
        self._loading = True
        for key, edit in self.completion_texts.items():
            edit.setPlainText(form["completion_texts"][key])
        self._loading = False
        self._scope_modified = self._dirty = True
        self._clear_confirmations()
        if not self._scope_matches():
            self.pages.setCurrentWidget(self.scope_page)
        self._buttons()
        self._notice("Form draft restored. Review it against the saved scope before confirming.")

    def invalidate(self, reason):
        self._invalid = reason
        self.timer.stop()
        self._clear_confirmations()
        self._buttons()
        self._notice(reason)

    def stop(self):
        self.timer.stop()

    def reject(self):
        try:
            self.save_draft()
        except Exception as exc:
            self._notice(system_error(exc))
            return
        self._clear_confirmations()
        super().reject()

    def closeEvent(self, event):
        try:
            self.save_draft()
        except Exception as exc:
            event.ignore()
            self._notice(system_error(exc))
            return
        self.stop()
        self._clear_confirmations()
        super().closeEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        if self.is_current():
            self.refresh(force=True)
            self.timer.start()
