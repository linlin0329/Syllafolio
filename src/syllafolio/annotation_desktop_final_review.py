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
    QFormLayout, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton,
    QScrollArea, QTabWidget, QTableWidget, QTableWidgetItem, QTreeWidget, QTreeWidgetItem,
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
        locations = []
        all_items = {row["item_id"] for row in rows}
        type_ids = {row["local_type_id"] for row in state.get("memberships", [])}
        reviews = state.get("batch_review", {}).get("reviews", {})
        groups = {row["group_id"] for row in (state.get("batch_review", {}).get("organization") or {}).get("groups", [])}
        for gap in final.get("gaps", report.get("missing", [])):
            found = []
            location = gap.get("location", {})
            for kind, values, allowed in (
                ("item", set(gap.get("item_ids", [])) | set(location.get("item_ids", [])), all_items),
                ("type", set(gap.get("type_ids", [])) | set(gap.get("source_type_ids", [])) | set(location.get("family_ids", [])) | set(location.get("source_type_ids", [])), type_ids),
                ("review", set(gap.get("review_ids", [])) | set(location.get("review_ids", [])), set(reviews)),
                ("group", set(gap.get("group_ids", [])) | (set(final.get("stages", {}).get("first", {}).get("pending_ids", [])) if gap["code"] == "BATCH_FIRST_PASS_PENDING" else set()), groups)):
                found.extend({"kind": kind, "id": value} for value in sorted(values & allowed))
            for issue_id in set(gap.get("issue_ids", [])) | set(location.get("issue_ids", [])):
                issue = state.get("issues", {}).get(issue_id, {})
                if issue.get("directed_review_id") in reviews:
                    found.append({"kind": "review", "id": issue["directed_review_id"]})
                else:
                    found.extend({"kind": "item", "id": value} for value in issue.get("target_ids", []) if value in all_items)
            if gap["code"] == "BATCH_POOL_PENDING":
                found.extend({"kind": "review", "id": value} for value in final.get("stages", {}).get("pool", {}).get("pending_ids", []) if value in reviews)
            if gap["code"] == "BATCH_FIRST_PASS_PENDING":
                active_reviews = state.get("batch_review", {}).get("active_reviews", {})
                found.extend({"kind": "review", "id": active_reviews[group_id]}
                    for group_id in final.get("stages", {}).get("first", {}).get("pending_ids", [])
                    if active_reviews.get(group_id) in reviews)
            locations.append({"code": gap["code"], "message": system_text(gap.get("message", "")),
                              "targets": [dict(kind=kind, id=ident) for kind, ident in sorted({(row["kind"], row["id"]) for row in found})]})
        reports = [{"kind": kind, "job_id": row["job_id"], "status": row["status"],
                    "stale_for_current": bool(row.get("stale_for_current", False)), "stale_reasons": copy.deepcopy(row.get("stale_reasons", []))}
                   for kind, section in (("assessment", final.get("assessment", {})), ("catalogue_export", final.get("publication_report", {})))
                   for row in section.get("jobs", [])]
        return {"identity": self.identity(), "revision": state["revision"], "ticket": self.ticket(state),
                "scope": copy.deepcopy(state.get("catalogue_scope")), "scope_declared": state.get("catalogue_scope") is not None,
                "completion": report, "current_completion_id": state.get("current_completion_id"),
                "stages": copy.deepcopy(final.get("stages", {})), "gaps": locations, "items": rows,
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
        allowed = {"item": {row["item_id"] for row in state["items"]},
                   "type": {row["local_type_id"] for row in state["memberships"]},
                   "review": set(state.get("batch_review", {}).get("reviews", {})),
                   "group": {row["group_id"] for row in (state.get("batch_review", {}).get("organization") or {}).get("groups", [])}}
        _require(kind in allowed and ident in allowed[kind], "FINAL_LOCATION_SCOPE", "The referenced target is not in the current project")
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
    itemRequested = Signal(str)
    typeRequested = Signal(str)
    groupRequested = Signal(str)
    reviewRequested = Signal(str)
    changed = Signal(object)

    def __init__(self, owner):
        super().__init__(owner)
        self.owner, self.project = owner, owner.project
        self.generation = owner._close_coordinator.generation
        self.controller = FinalReviewController(self.project)
        self.store = _FormDraft(self.project); self.store.load()
        self._revision, self._view = None, None
        self._dirty, self._loading, self._waiting, self._invalid = False, False, False, None
        self._restore_ticket = None
        self.setWindowTitle("Final checks, scope and label history")
        screen = owner.screen(); area = screen.availableGeometry() if screen else None
        self.resize(min(1080, area.width() - 32) if area else 1080, min(780, area.height() - 64) if area else 780)
        if area: self.move(area.left() + 8, area.top() + 8)
        layout = QVBoxLayout(self)
        self.status = QLabel(); self.status.setWordWrap(True); layout.addWidget(self.status)
        self.tabs = QTabWidget(); self.tabs.setUsesScrollButtons(True); self.tabs.setElideMode(Qt.TextElideMode.ElideRight)
        self.tabs.tabBar().setMinimumWidth(0); layout.addWidget(self.tabs, 1)
        self._build_scope(); self._build_completion(); self._build_history()
        self.message = QLabel(self.store.warning or "No completion is inferred from a page, group, draft or checkpoint. The shared service decides current completeness.")
        self.message.setWordWrap(True); layout.addWidget(self.message)
        self.footer_layout = QGridLayout(); self._footer_columns = None
        self.save_button = QPushButton("Save draft"); self.save_button.clicked.connect(lambda: self._guard(self.save_draft))
        self.restore_draft_button = QPushButton("Restore draft"); self.restore_draft_button.clicked.connect(lambda: self._guard(self.restore_draft))
        refresh = QPushButton("Refresh"); refresh.clicked.connect(lambda: self._guard(lambda: self.refresh(force=True)))
        close = QPushButton("Close"); close.clicked.connect(self.reject)
        self.footer_buttons = (self.save_button, self.restore_draft_button, refresh, close)
        layout.addLayout(self.footer_layout); self._layout_footer()
        self.timer = QTimer(self); self.timer.setInterval(350); self.timer.timeout.connect(lambda: self._guard(self.refresh))
        self.finished.connect(lambda _result: self.timer.stop())
        if hasattr(getattr(owner, "operationFailed", None), "connect"): owner.operationFailed.connect(self._operation_failed)
        self.refresh(force=True); self.load_current_scope()

    def _page(self, name):
        page = QWidget(); layout = QVBoxLayout(page)
        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        body = QWidget(); inner = QVBoxLayout(body); scroll.setWidget(body)
        layout.addWidget(scroll, 1); self.tabs.addTab(page, name)
        return layout, inner

    def _build_scope(self):
        layout, inner = self._page("Scope declaration")
        form = QFormLayout(); inner.addLayout(form)
        self.scope_mode = QComboBox(); self.scope_mode.addItem("Choose explicitly…", CHOOSE)
        self.scope_mode.addItem("All task members", "all"); self.scope_mode.addItem("Explicit local subset", "local")
        self.scope_mode.currentIndexChanged.connect(self._scope_changed)
        self.level = QComboBox(); self.level.addItem("Choose explicitly…", CHOOSE)
        self.level.addItem("Family", "family"); self.level.addItem("Leaf / fine form", "leaf")
        self.level.currentIndexChanged.connect(self._scope_changed)
        form.addRow("Scope", self.scope_mode); form.addRow("Target level", self.level)
        self.scope_note = QLabel(); self.scope_note.setWordWrap(True); inner.addWidget(self.scope_note)
        self.scope_texts = {}
        for key in SCOPE_TEXTS:
            edit = QPlainTextEdit(); edit.setMaximumHeight(76); edit.setObjectName("finalScope_" + key)
            edit.textChanged.connect(self._scope_changed); self.scope_texts[key] = edit; form.addRow(key.replace("_", " "), edit)
        self.scope_items = _table(["Include locally", "Stable item ID", "Processing", "Family", "Form / disposition"])
        self.scope_items.setMinimumHeight(180); self.scope_items.itemChanged.connect(self._scope_changed)
        inner.addWidget(self.scope_items)
        self.load_scope_button = QPushButton("Load current saved declaration"); self.load_scope_button.clicked.connect(lambda: self._guard(self.load_current_scope))
        inner.addWidget(self.load_scope_button)
        self.scope_button = QPushButton("Declare this scope"); self.scope_button.clicked.connect(lambda: self._guard(self.declare_scope))
        layout.addWidget(self.scope_button)

    def _build_completion(self):
        layout, inner = self._page("Final gaps & completion")
        self.saved_scope = QLabel(); self.saved_scope.setWordWrap(True); inner.addWidget(self.saved_scope)
        self.gaps = QTreeWidget(); self.gaps.setHeaderLabels(["Gap / target", "Current service explanation"])
        self.gaps.setMinimumHeight(180); self.gaps.itemDoubleClicked.connect(lambda item, _column: self._guard(lambda: self._open_location(item)))
        inner.addWidget(self.gaps)
        form = QFormLayout(); inner.addLayout(form)
        self.completion_texts = {}
        for key in COMPLETION_TEXTS:
            edit = QPlainTextEdit(); edit.setMaximumHeight(76); edit.setObjectName("finalEvidence_" + key)
            edit.textChanged.connect(self._changed); self.completion_texts[key] = edit; form.addRow(key.replace("_", " "), edit)
        self.checks = {}
        for key in sorted(COMPLETION_CHECKS):
            check = QCheckBox(key.replace("_", " ")); check.toggled.connect(self._changed); check.toggled.connect(self._buttons)
            self.checks[key] = check; form.addRow(check)
        note = QLabel("These five confirmations concern only the declared current scope and limits. They do not certify biological truth, annotation accuracy or scientific validity.")
        note.setWordWrap(True); inner.addWidget(note)
        self.complete_button = QPushButton("Confirm scoped completion")
        self.complete_button.clicked.connect(lambda: self._guard(self.complete)); layout.addWidget(self.complete_button)

    def _build_history(self):
        layout, inner = self._page("Label checkpoints & history")
        self.history_tab = layout.parentWidget()
        self.history_summary = QLabel(); self.history_summary.setWordWrap(True); inner.addWidget(self.history_summary)
        self.checkpoints = _table(["Checkpoint ID", "Purpose", "Recorded at"]); self.checkpoints.setMinimumHeight(160)
        self.checkpoints.itemSelectionChanged.connect(self._checkpoint_selected); inner.addWidget(self.checkpoints)
        row = QHBoxLayout(); self.checkpoint_reason = QLineEdit(); self.checkpoint_reason.setPlaceholderText("Explicit checkpoint purpose")
        self.checkpoint_reason.textChanged.connect(self._changed); row.addWidget(self.checkpoint_reason)
        self.checkpoint_button = QPushButton("Save checkpoint"); self.checkpoint_button.clicked.connect(lambda: self._guard(self.save_checkpoint)); row.addWidget(self.checkpoint_button)
        inner.addLayout(row)
        warning = QLabel("Restoring active labels is separate from restoring this unsubmitted form. Later decision/job history and the latest explicit source mapping are retained; current completion and report applicability are rechecked. Active jobs remain subject to the shared backend guards. The existing service records its fixed explicit-restore reason.")
        warning.setWordWrap(True); inner.addWidget(warning)
        self.restore_confirm = QCheckBox("I confirm these restore effects")
        self.restore_confirm.toggled.connect(self._restore_acknowledged); inner.addWidget(self.restore_confirm)
        self.report_history = QPlainTextEdit(); self.report_history.setReadOnly(True); self.report_history.setMaximumHeight(150)
        inner.addWidget(self.report_history)
        self.restore_button = QPushButton("Restore label checkpoint")
        self.restore_button.clicked.connect(lambda: self._guard(self.restore_checkpoint)); layout.addWidget(self.restore_button)

    def is_current(self):
        return (self.owner.project is self.project and self.owner._close_coordinator.generation == self.generation
                and self.owner._close_coordinator.state == "open" and self._invalid is None)

    def _guard(self, function):
        try: return function()
        except Exception as exc:
            self.message.setText(f"{getattr(exc, 'code', type(exc).__name__)}: {system_error(exc)}")
            self.owner._error(exc, operation="scope and label history")

    def _operation_failed(self, message):
        if self._waiting: self._waiting = False; self.message.setText(message)

    def _changed(self, *_args):
        if not self._loading: self._dirty = True

    def _clear_confirmations(self):
        self._loading = True
        for check in self.checks.values(): check.setChecked(False)
        self.restore_confirm.setChecked(False)
        self._loading = False; self._restore_ticket = None

    def _scope_changed(self, *_args):
        if not self._loading:
            self._changed(); self._clear_confirmations(); self._buttons()

    def _scope_payload(self):
        _require(self.scope_mode.currentData() in {"all", "local"}, "SCOPE_REQUIRED", "Explicitly choose the whole task or a local subset")
        _require(self.level.currentData() in {"family", "leaf"}, "TARGET_LEVEL", "Explicitly choose the target level")
        ids = ([row["item_id"] for row in self._view["items"]] if self.scope_mode.currentData() == "all" else
               [self.scope_items.item(index, 1).text() for index in range(self.scope_items.rowCount())
                if self.scope_items.item(index, 0).checkState() == Qt.CheckState.Checked])
        return {"scope_ids": ids, "target_level": self.level.currentData(),
                **{key: edit.toPlainText() for key, edit in self.scope_texts.items()}}

    def _scope_matches(self):
        saved = (self._view or {}).get("scope")
        if saved is None: return False
        try: value = self._scope_payload()
        except FinalReviewError: return False
        return set(value["scope_ids"]) == set(saved["scope_ids"]) and all(value[key] == saved.get(key) for key in ("target_level", *SCOPE_TEXTS))

    def _buttons(self, *_args):
        if not hasattr(self, "complete_button") or self._loading: return
        available = self.is_current() and not self.owner._busy and not self._waiting
        self.scope_note.setText("Whole task: all frozen task members will be declared; local checkboxes are ignored." if self.scope_mode.currentData() == "all" else
                                "Local subset: explicitly check the stable IDs to include." if self.scope_mode.currentData() == "local" else "Choose the whole task or an explicit local subset before declaring scope.")
        self.tabs.setEnabled(available)
        if hasattr(self, "save_button"):
            self.save_button.setEnabled(available); self.restore_draft_button.setEnabled(available)
        self.scope_button.setEnabled(available)
        self.complete_button.setEnabled(available and bool((self._view or {}).get("completion", {}).get("can_submit"))
            and self._scope_matches() and all(check.isChecked() for check in self.checks.values()))
        self.checkpoint_button.setEnabled(available)
        self.restore_button.setEnabled(available and self._selected_checkpoint() is not None
            and self.restore_confirm.isChecked() and self._restore_ticket is not None)

    def _selected_checkpoint(self):
        row = self.checkpoints.currentRow()
        return self.checkpoints.item(row, 0).text() if row >= 0 and self.checkpoints.item(row, 0) else None

    def _checkpoint_selected(self):
        if self._loading: return
        self.restore_confirm.setChecked(False)
        self._restore_ticket = None; self._buttons()

    def _restore_acknowledged(self, checked):
        if self._loading: return
        if checked:
            self._restore_ticket = copy.deepcopy(self._view["ticket"])
        else: self._restore_ticket = None
        self._buttons()

    def load_current_scope(self):
        if self._dirty: self.save_draft()
        self._loading = True
        saved = (self._view or {}).get("scope")
        self.scope_mode.setCurrentIndex(0 if saved is None else self.scope_mode.findData("all" if set(saved["scope_ids"]) == {row["item_id"] for row in self._view["items"]} else "local"))
        self.level.setCurrentIndex(0 if saved is None else self.level.findData(saved["target_level"]))
        for key, edit in self.scope_texts.items(): edit.setPlainText((saved or {}).get(key, ""))
        for index in range(self.scope_items.rowCount()):
            self.scope_items.item(index, 0).setCheckState(Qt.CheckState.Checked if saved and self.scope_items.item(index, 1).text() in saved["scope_ids"] else Qt.CheckState.Unchecked)
        self._loading = False; self._dirty = False; self._clear_confirmations(); self._buttons()

    def refresh(self, force=False):
        if self.owner._busy and not force: self._buttons(); return
        self._waiting = False
        if not self.is_current(): self.invalidate("Project changed or is closing; this review form cannot write another owner."); return
        state = self.owner.state if getattr(self.owner, "state", None) is not None else self.project.snapshot()
        if force or self._revision != state["revision"]:
            self._revision = state["revision"]; self._view = self.controller.snapshot(state)
            self.controller.rebase(state); self._clear_confirmations()
            report, scope = self._view["completion"], self._view["scope"]
            counts = self._view["display_categories"]
            self.status.setText(f"Scoped completion: {'recorded' if self._view['current_completion_id'] else 'not confirmed'} · "
                + (f"Scope {len(report.get('scope_ids', []))}/{len(self._view['items'])}" if self._view["scope_declared"] else "Scope not declared")
                + f" · Gaps {len(self._view['gaps'])}\nUnfinished {counts['unfinished']} · Reviewed unresolved {counts['reviewed_unresolved']} · "
                f"Family known / form unresolved {counts['family_known_form_unresolved']} · Excluded {counts['excluded']}")
            self.status.setToolTip(str(self._view["current_completion_id"] or "No current completion record") + "\nService processing counts: " + str(report.get("counts")))
            self.saved_scope.setText(("No scope is declared. The service's provisional all-task check is not a saved declaration." if scope is None else
                f"Saved scope {scope['scope_id']} · {len(scope['scope_ids'])}/{len(self._view['items'])} task members · {scope['target_level']} · Purpose: {scope['purpose']}")
                + ("\nRecord completeness permits an explicit confirmation." if report.get("can_submit") else "\nCurrent gaps block completion."))
            selected = {self.scope_items.item(i, 1).text() for i in range(self.scope_items.rowCount()) if self.scope_items.item(i, 0).checkState() == Qt.CheckState.Checked}
            self._loading = True
            self.scope_items.setRowCount(len(self._view["items"]))
            for index, row in enumerate(self._view["items"]):
                include = QTableWidgetItem(); include.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
                include.setCheckState(Qt.CheckState.Checked if row["item_id"] in selected else Qt.CheckState.Unchecked)
                self.scope_items.setItem(index, 0, include)
                for column, value in enumerate((row["item_id"], row["processing_state"], ", ".join(row["family_ids"]),
                                                str(row["form_status"]) + " / " + str(row["disposition"])), 1):
                    self.scope_items.setItem(index, column, QTableWidgetItem(value))
            self.gaps.clear()
            for gap in self._view["gaps"]:
                parent = QTreeWidgetItem([gap["code"], gap["message"]]); parent.setData(0, Qt.ItemDataRole.UserRole, {"code": gap["code"]})
                self.gaps.addTopLevelItem(parent)
                for target in gap["targets"]:
                    child = QTreeWidgetItem([target["kind"] + " · " + target["id"], "Open this actual target"])
                    child.setData(0, Qt.ItemDataRole.UserRole, target); parent.addChild(child)
            checkpoint_id = self._selected_checkpoint()
            self.checkpoints.setRowCount(len(self._view["checkpoints"]))
            for index, row in enumerate(self._view["checkpoints"]):
                for column, key in enumerate(("checkpoint_id", "reason", "created_at")):
                    self.checkpoints.setItem(index, column, QTableWidgetItem(str(row.get(key, ""))))
                if row["checkpoint_id"] == checkpoint_id: self.checkpoints.selectRow(index)
            self._loading = False
            self.history_summary.setText("Later histories and the latest explicit source mapping are retained by label restore.\n" + json.dumps(system_fields(self._view["history"]), ensure_ascii=False))
            self.report_history.setPlainText(json.dumps(system_fields({"existing_report_applicability": self._view["reports"],
                "current_completion_id": self._view["current_completion_id"], "active_tasks": self._view["active_tasks"]}), ensure_ascii=False, indent=2))
        self._buttons()

    def _open_location(self, item):
        _require(self.is_current() and not self.owner._busy, "FINAL_CONTEXT_CHANGED", "Wait for the current project context")
        data = item.data(0, Qt.ItemDataRole.UserRole) or {}
        if data.get("code") == "SCOPE_REQUIRED": self.tabs.setCurrentIndex(0); return
        _require("kind" in data and "id" in data, "FINAL_LOCATION_SCOPE", "Select a concrete missing item, type, group or review")
        target = self.controller.resolve_location(data["kind"], data["id"])
        {"item": self.itemRequested, "type": self.typeRequested, "group": self.groupRequested, "review": self.reviewRequested}[target["kind"]].emit(target["id"])

    def _run(self, label, function):
        _require(self.is_current() and not self.owner._busy, "FINAL_CONTEXT_CHANGED", "The current project is not accepting another final-review command")
        self._waiting = True; self._buttons()
        def done(result):
            self._waiting = False; self.owner.state = self.project.snapshot(); self.refresh(force=True)
            self.message.setText("Original receipt replayed; no duplicate command was applied." if result.get("replayed") else
                "Saved through the shared service. Current completion and report applicability were reread; form drafts remain separate.")
            self.changed.emit(result)
        try: self.owner._run(label, lambda _progress, _cancelled: function(), done)
        except Exception: self._waiting = False; self._buttons(); raise

    def declare_scope(self):
        payload, ticket = self._scope_payload(), copy.deepcopy(self._view["ticket"])
        self.save_draft(); self._run("Declare review scope", lambda: self.controller.declare_scope(payload, ticket=ticket))

    def complete(self):
        _require(self._scope_matches(), "SCOPE_CHANGED", "Submit or load the current saved scope before confirming completion")
        ticket = copy.deepcopy(self._view["ticket"])
        payload = {"scope_id": ticket["scope_id"], "checks": {key: check.isChecked() for key, check in self.checks.items()},
                   **{key: edit.toPlainText() for key, edit in self.completion_texts.items()}}
        self.save_draft(); self._run("Confirm scoped completion", lambda: self.controller.complete(payload, ticket=ticket))

    def save_checkpoint(self):
        reason, ticket = self.checkpoint_reason.text(), copy.deepcopy(self._view["ticket"])
        self.save_draft(); self._run("Save label checkpoint", lambda: self.controller.save_checkpoint(reason, ticket=ticket))

    def restore_checkpoint(self):
        ident, ticket, confirmed = self._selected_checkpoint(), copy.deepcopy(self._restore_ticket), self.restore_confirm.isChecked()
        _require(confirmed and ticket is not None, "CHECKPOINT_CONFIRMATION", "Review and explicitly confirm the selected checkpoint")
        self.save_draft()
        hook = getattr(self.owner, "before_checkpoint_restore", None)
        if hook is not None: hook()
        self._run("Restore label checkpoint", lambda: self.controller.restore_checkpoint(ident, confirmed=confirmed, ticket=ticket))

    def save_draft(self):
        # Called after the owner enters draining as well: only local unsubmitted bytes are saved.
        if self._dirty:
            selected = [self.scope_items.item(i, 1).text() for i in range(self.scope_items.rowCount()) if self.scope_items.item(i, 0).checkState() == Qt.CheckState.Checked]
            form = {"scope_mode": self.scope_mode.currentData(), "scope_ids": selected, "target_level": self.level.currentData(),
                "scope_texts": {key: edit.toPlainText() for key, edit in self.scope_texts.items()},
                "completion_texts": {key: edit.toPlainText() for key, edit in self.completion_texts.items()},
                "checkpoint_reason": self.checkpoint_reason.text(), "selected_checkpoint_id": self._selected_checkpoint()}
            self.store.save(form, (self._view or {}).get("ticket", self.controller.base_ticket)); self._dirty = False
        return True

    def restore_draft(self):
        _require(self.is_current() and not self.owner._busy, "FINAL_CONTEXT_CHANGED", "Restore this form only in its current open project")
        value = self.store.load(); _require(value is not None, "FINAL_DRAFT_MISSING", self.store.warning or "No final form draft is stored")
        form = value["form"]; self._loading = True
        self.scope_mode.setCurrentIndex(max(0, self.scope_mode.findData(form.get("scope_mode", CHOOSE))))
        self.level.setCurrentIndex(max(0, self.level.findData(form.get("target_level", CHOOSE))))
        for key, edit in self.scope_texts.items(): edit.setPlainText(form.get("scope_texts", {}).get(key, ""))
        for key, edit in self.completion_texts.items(): edit.setPlainText(form.get("completion_texts", {}).get(key, ""))
        for index in range(self.scope_items.rowCount()): self.scope_items.item(index, 0).setCheckState(Qt.CheckState.Checked if self.scope_items.item(index, 1).text() in form.get("scope_ids", []) else Qt.CheckState.Unchecked)
        self.checkpoint_reason.setText(form.get("checkpoint_reason", ""))
        self._loading = False; self._clear_confirmations(); self._dirty = True; self._buttons()
        self.message.setText("Unsubmitted form restored; every completion/restore confirmation is cleared. Current saved scope and label state remain authoritative.")

    def invalidate(self, reason):
        self._invalid = reason; self.timer.stop(); self._clear_confirmations(); self._buttons(); self.message.setText(reason)

    def stop(self): self.timer.stop()
    def history_visible(self):
        return self.tabs.currentWidget() is self.history_tab

    def show_history(self):
        self.tabs.setCurrentWidget(self.history_tab)
        self.show()
        self.raise_()

    def _layout_footer(self):
        columns = 2 if self.width() < 760 else 4
        if columns == self._footer_columns: return
        for button in self.footer_buttons: self.footer_layout.removeWidget(button)
        for column in range(4): self.footer_layout.setColumnStretch(column, 1 if column < columns else 0)
        for index, button in enumerate(self.footer_buttons): self.footer_layout.addWidget(button, index // columns, index % columns)
        self._footer_columns = columns

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "footer_buttons"): self._layout_footer()

    def reject(self):
        try: self.save_draft()
        except Exception as exc: self._guard(lambda: (_ for _ in ()).throw(exc)); return
        self._clear_confirmations(); super().reject()

    def closeEvent(self, event):
        try: self.save_draft()
        except Exception as exc: event.ignore(); self._guard(lambda: (_ for _ in ()).throw(exc)); return
        self.timer.stop(); self._clear_confirmations(); super().closeEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        if self.is_current(): self.refresh(force=True); self.timer.start()
