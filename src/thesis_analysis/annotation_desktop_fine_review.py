"""单条精细判断：人工七轴表单、未提交草稿和共同服务事务入口。"""
from __future__ import annotations
from .annotation_english import system_error

import copy
import hashlib
import json
from pathlib import Path
import threading
import uuid

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFormLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton, QScrollArea, QVBoxLayout, QWidget)

from .annotation_assistant_contracts import canonical_hash
from .annotation_batch_review import item_version, family_definition_fingerprint
from .annotation_manual_protocol import REVIEW_CHECKS


SCHEMA = "syllafolio-desktop-fine-drafts-v1"
FILENAME = "desktop_fine_drafts.json"
CHOOSE = "__fine_choose__"
NEW_LEAF = "__fine_new_leaf__"
AXIS_OPTIONS = {
    "family_status": ("assigned", "unresolved"),
    "diagnostic_region": ("assessable", "unassessable", "not_assessed", "not_applicable"),
    "component_status": ("present", "not_observed_when_assessable", "undetermined", "not_assessed", "not_applicable"),
    "form_status": ("main", "variant", "candidate_form", "unresolved", "not_assessed", "not_applicable"),
    "boundary_status": ("no_issue_reported", "needs_review"),
}
AXES = ("family_status", "family_id", "diagnostic_region", "component_status", "form_status", "leaf_id", "boundary_status")
TEXTS = ("observation_text", "question", "reason", "answer", "comparison_note", "affected_relations_note",
         "pending_steps", "unresolved_level", "missing_information")


class FineReviewError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(f"{code}: {message}")


def _require(value, code, message):
    if not value:
        raise FineReviewError(code, message)


def _read(raw):
    def pairs(values):
        result = {}
        for key, value in values:
            _require(key not in result, "FINE_DRAFT_JSON", "Duplicate draft key")
            result[key] = value
        return result
    def invalid(value):
        raise FineReviewError("FINE_DRAFT_JSON", "Nonfinite draft value: " + value)
    return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=invalid)


class FineDraftStore:
    """只保存未提交输入；损坏原件先留副本，再允许写新草稿。"""
    def __init__(self, project):
        self.project = project
        self.path = project.project_dir / FILENAME
        self.identity = {"project_id": project.metadata["project_id"], "run_id": project.service.run_id,
                         "input_fingerprint": project.dataset.input_fingerprint}
        if not hasattr(project.service, "_desktop_fine_draft_lock"):
            project.service._desktop_fine_draft_lock = threading.RLock()
        self.lock = project.service._desktop_fine_draft_lock
        self.warning = None
        self._source_sha256 = None
        with self.lock:
            self._load()

    def _load(self):
        if not self.path.exists() and not self.path.is_symlink():
            self._source_sha256 = None
            return {}
        _require(not self.path.is_symlink() and self.path.resolve().is_relative_to(self.project.project_dir),
                 "FINE_DRAFT_PATH", "Fine drafts must remain inside this project")
        raw = self.path.read_bytes()
        self._source_sha256 = hashlib.sha256(raw).hexdigest()
        try:
            value = _read(raw)
            _require(isinstance(value, dict) and value.get("schema_version") == SCHEMA
                     and all(value.get(key) == expected for key, expected in self.identity.items())
                     and isinstance(value.get("drafts"), dict)
                     and all(isinstance(key, str) and isinstance(row, dict)
                             and isinstance(row.get("binding"), dict) and isinstance(row.get("payload"), dict)
                             and row.get("submission_state") == "unsubmitted" and row.get("audition_confirmed") is False
                             and all(row["binding"].get(field) == expected for field, expected in self.identity.items())
                             and isinstance(row["binding"].get("item_id"), str) and bool(row["binding"]["item_id"])
                             and row["payload"].get("item_id") == row["binding"].get("item_id")
                             and isinstance(row["payload"].get("axes"), dict)
                             for key, row in value["drafts"].items()),
                     "FINE_DRAFT_IDENTITY", "Fine draft schema or project/input identity does not match")
            return value["drafts"]
        except (UnicodeError, ValueError, TypeError) as exc:
            digest = hashlib.sha256(raw).hexdigest()
            backup = self.path.with_name("desktop_fine_drafts.recovery-" + digest[:16] + ".json")
            if backup.exists():
                _require(backup.read_bytes() == raw, "FINE_DRAFT_BACKUP", "Recovery filename contains different bytes")
            else:
                with backup.open("xb") as stream:
                    stream.write(raw)
            self.warning = "Unreadable or foreign draft retained in " + backup.name + ": " + system_error(exc)
            return {}

    def get(self, key):
        with self.lock:
            return copy.deepcopy(self._load().get(key))

    def _write(self, drafts):
        value = {"schema_version": SCHEMA, **self.identity, "submission_state": "unsubmitted", "drafts": drafts}
        temporary = self.path.with_name(".fine-draft-" + uuid.uuid4().hex + ".tmp")
        try:
            with temporary.open("x", encoding="utf-8") as stream:
                stream.write(json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + "\n")
            _require(not self.path.is_symlink(), "FINE_DRAFT_PATH", "Fine draft target changed to a link")
            current = hashlib.sha256(self.path.read_bytes()).hexdigest() if self.path.exists() else None
            _require(current == self._source_sha256, "FINE_DRAFT_CHANGED", "Draft bytes changed during saving; no original was overwritten")
            temporary.replace(self.path)
            self._source_sha256 = hashlib.sha256(self.path.read_bytes()).hexdigest()
        finally:
            temporary.unlink(missing_ok=True)

    def put(self, key, record):
        with self.lock:
            drafts = self._load()
            drafts[key] = copy.deepcopy(record)
            self._write(drafts)

    def remove(self, key, *, expected_binding=None, expected_payload=None):
        with self.lock:
            drafts = self._load()
            if key in drafts:
                current = drafts[key]
                if (expected_binding is not None and current.get("binding") != expected_binding
                        or expected_payload is not None and current.get("payload") != expected_payload):
                    return False
                drafts.pop(key)
                self._write(drafts)
            return True


class FineReviewController:
    """冻结具体目标/范围，精确重试，不重写SQL或expected token。"""
    def __init__(self, project, item_id, review_id=None):
        self.project, self.item_id, self.review_id = project, item_id, review_id
        self.store = FineDraftStore(project)
        self.key = item_id + "/" + (review_id or "ordinary")
        self.invalid_reason, self.pending, self.submitted = None, None, False
        self.base = self.context()["binding"]

    def context(self):
        state = self.project.snapshot()
        _require(self.item_id in {row["item_id"] for row in state["items"]}, "FINE_ITEM_SCOPE", "The item is not in this project")
        families = sorted(row["local_type_id"] for row in state["memberships"]
                          if row["parent_type_id"] is None and self.item_id in row["member_ids"])
        reviews = state.get("batch_review", {}).get("reviews", {})
        review = reviews.get(self.review_id) if self.review_id else None
        if self.review_id:
            _require(review is not None and self.item_id in review.get("item_ids", []), "FINE_REVIEW_SCOPE", "The item is not in the selected frozen review")
        directed = bool(review and review.get("directed_issue_id"))
        allowed_families = ({row["local_type_id"] for row in state["memberships"] if row["parent_type_id"] is None}
                            if directed else set(families))
        relevant_types = [row["local_type_id"] for row in state["memberships"]
                          if row["local_type_id"] in allowed_families or row["parent_type_id"] in allowed_families]
        decisions = state["decisions"].values() if isinstance(state["decisions"], dict) else state["decisions"]
        effective = set(state.get("effective_decision_ids", []))
        effective_item_decisions = sorted(row["decision_id"] for row in decisions
            if self.item_id in row.get("affected_ids", []) and row["decision_id"] in effective)
        scope = ({key: copy.deepcopy(review.get(key)) for key in ("review_id", "organization_id", "stage", "family_id", "item_ids", "directed_context")}
                 if review else None)
        binding = {"project_id": self.project.metadata["project_id"], "run_id": self.project.service.run_id,
                   "input_fingerprint": state["input_fingerprint"], "item_id": self.item_id,
                   "families": families, "item_version": item_version(state, self.item_id),
                   "effective_item_decisions": effective_item_decisions,
                   "family_definitions": {family: family_definition_fingerprint(state, family) for family in families},
                   "type_definitions": {key: family_definition_fingerprint(state, key) for key in relevant_types},
                   "organization_id": (state.get("batch_review", {}).get("organization") or {}).get("organization_id"),
                   "source_registry_version": state.get("source_registry", {}).get("version"),
                   "review_scope": canonical_hash(scope), "review_id": self.review_id, "directed": directed}
        blockers = []
        if review and any(review.get(key) for key in ("scope_stale", "read_only", "archived", "superseded_by_protocol_upgrade")):
            blockers.append("FINE_REVIEW_SCOPE_STALE")
        if directed:
            if review.get("closed") or review.get("status") != "ready" or review.get("scope_stale") or review.get("read_only"):
                blockers.append("DIRECTED_REVIEW_SCOPE")
        else:
            blockers.extend(state.get("fine_submission", {}).get("blocked_items", {}).get(self.item_id, []))
        return {"state": state, "binding": binding, "directed": directed, "blockers": blockers,
                "families": [row for row in state["memberships"] if row["parent_type_id"] is None],
                "leaves": [row for row in state["memberships"] if row["parent_type_id"] is not None]}

    def invalidate(self, reason):
        self.invalid_reason = reason

    def rebase(self):
        self.base = self.context()["binding"]
        self.pending, self.submitted, self.invalid_reason = None, False, None
        return copy.deepcopy(self.base)

    def save_draft(self, payload):
        value = copy.deepcopy(payload)
        value["item_id"] = self.item_id
        value["checks"] = dict.fromkeys(REVIEW_CHECKS, False)
        self.store.put(self.key, {"binding": copy.deepcopy(self.base), "payload": value,
            "audition_confirmed": False, "submission_state": "unsubmitted"})
        return True

    def restore_draft(self):
        record = self.store.get(self.key)
        _require(record is not None and isinstance(record.get("payload"), dict), "FINE_DRAFT_MISSING", "No matching fine draft is stored")
        _require(record.get("binding") == self.context()["binding"], "FINE_DRAFT_STALE",
                 "The item, family, definition or frozen scope changed; retain the old draft and review the current context")
        self.base = copy.deepcopy(record["binding"])
        value = copy.deepcopy(record["payload"])
        value["checks"] = dict.fromkeys(REVIEW_CHECKS, False)
        return value

    def submit(self, payload, *, audition_confirmed, idempotency_key=None):
        _require(audition_confirmed is True, "FINE_AUDITION_REQUIRED", "Explicitly confirm this target's current listening and spectrogram inspection")
        _require(self.invalid_reason is None, "FINE_CONTEXT_CHANGED", self.invalid_reason or "Reload and review the current context")
        value = copy.deepcopy(payload)
        _require(value.get("item_id", self.item_id) == self.item_id, "FINE_ITEM_SCOPE", "Fine input belongs to a different target")
        value["item_id"] = self.item_id
        draft_payload = copy.deepcopy(value)
        draft_payload["checks"] = dict.fromkeys(REVIEW_CHECKS, False)
        parent = value.pop("draft_new_leaf_parent", None)
        if value.get("new_leaf"):
            _require(parent == value.get("axes", {}).get("family_id") and bool(parent),
                     "FINAL_NEW_LEAF", "The explicit new leaf parent must match the selected family")
        _require(isinstance(value.get("axes"), dict) and set(value["axes"]) == set(AXES), "FINAL_REVIEW_AXES", "Choose all seven axes explicitly, including none where appropriate")
        if self.base["directed"]:
            _require(value.get("directed_review_id", self.review_id) == self.review_id,
                     "FINE_REVIEW_SCOPE", "The explicit directed review ID does not match this frozen scope")
            value["directed_review_id"] = self.review_id
        else:
            _require(not value.get("directed_review_id"), "FINE_REVIEW_SCOPE", "An ordinary target cannot borrow a directed review ID")
        signature = canonical_hash(value)
        if self.pending is None or self.pending["signature"] != signature:
            context = self.context()
            _require(context["binding"] == self.base, "FINE_CONTEXT_CHANGED", "The target context changed; reload and inspect it before another decision")
            _require(not context["blockers"], "FINE_REVIEW_PENDING", "Close the current ordinary review first; a stale/read-only scope must be replaced by a current target scope")
            if context["directed"]:
                value["directed_review_id"] = self.review_id
            else:
                _require(not value.get("directed_review_id"), "FINE_REVIEW_SCOPE", "An ordinary target cannot borrow a directed review ID")
            self.pending = {"signature": signature, "payload": value, "revision": context["state"]["revision"],
                            "key": idempotency_key or "desktop-fine-" + uuid.uuid4().hex}
        elif idempotency_key is not None:
            _require(idempotency_key == self.pending["key"], "IDEMPOTENCY_CONFLICT", "Retry the original exact request key")
        pending = self.pending
        result = self.project.execute("final_item_review", copy.deepcopy(pending["payload"]),
            expected_revision=pending["revision"], idempotency_key=pending["key"])
        self.submitted = True
        try:
            removed = self.store.remove(self.key, expected_binding=self.base, expected_payload=draft_payload)
            if not removed:
                result = dict(result, draft_retained=True)
        except Exception as exc:
            result = dict(result, draft_cleanup_error=system_error(exc))
        return result


class FineReviewDialog(QDialog):
    auditionRequested = Signal(str, bool)
    inspectRequested = Signal(str, bool)
    stopRequested = Signal(str)
    submitted = Signal(object)

    def __init__(self, owner, item_id, review_id=None):
        super().__init__(owner)
        self.owner, self.project, self.item_id, self.review_id = owner, owner.project, item_id, review_id
        self.generation = owner._close_coordinator.generation
        self.controller = FineReviewController(self.project, item_id, review_id)
        self._dirty, self._loading, self._waiting = False, False, False
        self.setWindowTitle("Fine review · " + item_id)
        screen = owner.screen()
        available = screen.availableGeometry() if screen else None
        self.resize(min(920, available.width() - 32) if available else 920,
                    min(760, available.height() - 64) if available else 760)
        if available:
            self.move(max(available.left(), min(owner.frameGeometry().center().x() - self.width() // 2, available.right() - self.width())),
                      max(available.top(), min(owner.frameGeometry().center().y() - self.height() // 2, available.bottom() - self.height())))
        layout = QVBoxLayout(self)
        self.context_label = QLabel(); self.context_label.setWordWrap(True)
        layout.addWidget(self.context_label)
        actions = QHBoxLayout()
        for label, context in (("Play target", False), ("Play source context", True)):
            button = QPushButton(label)
            button.setObjectName("finePlayContext" if context else "finePlayTarget")
            if context: self.play_context = button
            else: self.play_target = button
            button.clicked.connect(lambda _checked=False, target=item_id, ctx=context: self.auditionRequested.emit(target, ctx))
            actions.addWidget(button)
        inspect = QPushButton("Inspect this target")
        inspect.setObjectName("fineInspectTarget")
        inspect.clicked.connect(lambda: self.inspectRequested.emit(self.item_id, False)); actions.addWidget(inspect)
        stop = QPushButton("Stop"); stop.clicked.connect(self.stop); actions.addWidget(stop)
        self.inspect_button, self.stop_button = inspect, stop
        layout.addLayout(actions)
        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        body = QWidget(); self.form_body = body; form = QFormLayout(body)
        self.axes = {}
        for key in AXES:
            combo = QComboBox(); combo.setObjectName("fine_" + key)
            combo.addItem("Choose explicitly…", CHOOSE)
            if key in AXIS_OPTIONS:
                for value in AXIS_OPTIONS[key]:
                    combo.addItem(value.replace("_", " "), value)
            else:
                combo.addItem("None (explicit)", None)
            self.axes[key] = combo
            combo.currentIndexChanged.connect(self._changed)
            form.addRow(key.replace("_", " ").title(), combo)
        self.axes["family_id"].currentIndexChanged.connect(self._family_changed)
        self.axes["leaf_id"].currentIndexChanged.connect(self._new_leaf_changed)
        self.new_leaf_box = QGroupBox("Explicit new leaf draft (does not confirm its definition)")
        new_form = QFormLayout(self.new_leaf_box)
        self.new_leaf_fields = {}
        for key in ("local_type_id", "parent_family_id", "title", "reason", "diagnostic_body", "main_variant_difference"):
            edit = QLineEdit(); edit.setObjectName("fine_new_leaf_" + key)
            edit.textChanged.connect(self._changed); self.new_leaf_fields[key] = edit
            new_form.addRow(key.replace("_", " "), edit)
        form.addRow(self.new_leaf_box)
        self.processing = QComboBox(); self.processing.addItem("Choose explicitly…", CHOOSE)
        for value in ("unfinished", "reviewed_unresolved", "decided"):
            self.processing.addItem(value.replace("_", " "), value)
        self.processing.currentIndexChanged.connect(self._changed)
        form.addRow("Processing state", self.processing)
        self.texts = {}
        for key in TEXTS:
            edit = QPlainTextEdit(); edit.setMaximumHeight(84); edit.setObjectName("fine_" + key)
            edit.textChanged.connect(self._changed); self.texts[key] = edit
            form.addRow(key.replace("_", " ").title(), edit)
        self.checks = {}
        for key in sorted(REVIEW_CHECKS):
            check = QCheckBox(key.replace("_", " ")); check.toggled.connect(self._changed)
            self.checks[key] = check; form.addRow(check)
        self.audition = QCheckBox("I listened to and inspected this exact current target; restore does not carry this confirmation.")
        self.audition.setObjectName("fineAuditionConfirmed"); self.audition.toggled.connect(self._changed)
        form.addRow(self.audition)
        scroll.setWidget(body); layout.addWidget(scroll, 1)
        self.message = QLabel(self.controller.store.warning or "All fields are unsubmitted. Choose every axis and confirm actual inspection yourself.")
        self.message.setWordWrap(True); layout.addWidget(self.message)
        footer = QHBoxLayout()
        self.save_button = QPushButton("Save draft"); self.save_button.clicked.connect(lambda: self._guard(self.save_draft))
        self.restore_button = QPushButton("Restore draft"); self.restore_button.clicked.connect(lambda: self._guard(self.restore_draft))
        self.reload_button = QPushButton("Reload scope / clear axes"); self.reload_button.clicked.connect(lambda: self._guard(self.reload_context))
        self.submit_button = QPushButton("Submit fine decision"); self.submit_button.clicked.connect(lambda: self._guard(self.submit))
        for button in (self.save_button, self.restore_button, self.reload_button, self.submit_button):
            footer.addWidget(button)
        layout.addLayout(footer)
        self.timer = QTimer(self); self.timer.setInterval(350)
        self.timer.timeout.connect(lambda: self._guard(self.refresh))
        self.finished.connect(lambda _result: self.timer.stop())
        if hasattr(getattr(owner, "operationFailed", None), "connect"):
            owner.operationFailed.connect(self._operation_failed)
        self.reload_context(clear_text=False)

    def is_current(self):
        return self.owner.project is self.project and self.owner._close_coordinator.generation == self.generation and self.owner._close_coordinator.state == "open"

    def _guard(self, function):
        try:
            return function()
        except Exception as exc:
            self.message.setText(f"{getattr(exc, 'code', type(exc).__name__)}: {system_error(exc)}")
            self.owner._error(exc, operation="Fine review " + self.item_id)
            return None

    def _operation_failed(self, message):
        if self._waiting:
            self._waiting = False
            self.message.setText(message)

    def _changed(self, *_args):
        if not self._loading:
            self._dirty = True

    def _family_changed(self, *_args):
        if self._loading:
            return
        self._loading = True
        self._populate_leaves()
        for key in AXIS_OPTIONS:
            self.axes[key].setCurrentIndex(0)
        for check in self.checks.values():
            check.setChecked(False)
        self.audition.setChecked(False)
        for edit in self.new_leaf_fields.values():
            edit.clear()
        self._loading = False
        self._new_leaf_changed(); self._changed()

    def _populate_leaves(self):
        family = self.axes["family_id"].currentData()
        combo = self.axes["leaf_id"]
        combo.clear(); combo.addItem("Choose explicitly…", CHOOSE); combo.addItem("None (explicit)", None)
        for row in self._context["leaves"]:
            if row["parent_type_id"] == family:
                combo.addItem(row["local_type_id"], row["local_type_id"])
        combo.addItem("Create explicit new leaf…", NEW_LEAF)

    def _new_leaf_changed(self, *_args):
        self.new_leaf_box.setEnabled(self.axes["leaf_id"].currentData() == NEW_LEAF)

    def payload(self):
        axes = {key: combo.currentData() for key, combo in self.axes.items() if combo.currentData() != CHOOSE}
        value = {"item_id": self.item_id, "axes": axes,
                 **{key: edit.toPlainText() for key, edit in self.texts.items()},
                 "checks": {key: check.isChecked() for key, check in self.checks.items()}}
        if self.processing.currentData() != CHOOSE:
            value["processing_state"] = self.processing.currentData()
        if axes.get("leaf_id") == NEW_LEAF:
            leaf = {key: edit.text() for key, edit in self.new_leaf_fields.items()}
            value["draft_new_leaf_parent"] = leaf.pop("parent_family_id")
            axes["leaf_id"] = leaf["local_type_id"]
            value["new_leaf"] = leaf
        return value

    def save_draft(self):
        if self._dirty:
            self.controller.save_draft(self.payload())
            self._dirty = False
            self.message.setText("Unsubmitted draft saved. No decision or listening confirmation was submitted.")
        return True

    def _fill(self, value):
        self._loading = True
        for key, combo in self.axes.items():
            selected = value.get("axes", {}).get(key, CHOOSE)
            if key == "leaf_id" and value.get("new_leaf"):
                selected = NEW_LEAF
            combo.setCurrentIndex(max(0, combo.findData(selected)))
        selected_leaf = value.get("axes", {}).get("leaf_id", CHOOSE)
        self._populate_leaves()
        self.axes["leaf_id"].setCurrentIndex(max(0, self.axes["leaf_id"].findData(NEW_LEAF if value.get("new_leaf") else selected_leaf)))
        for key, edit in self.new_leaf_fields.items():
            edit.setText(value.get("draft_new_leaf_parent", "") if key == "parent_family_id" else value.get("new_leaf", {}).get(key, ""))
        for key, edit in self.texts.items():
            edit.setPlainText(value.get(key, ""))
        self.processing.setCurrentIndex(max(0, self.processing.findData(value.get("processing_state", CHOOSE))))
        for check in self.checks.values():
            check.setChecked(False)
        self.audition.setChecked(False)
        self._loading = False
        self._new_leaf_changed(); self._dirty = True

    def restore_draft(self):
        self._fill(self.controller.restore_draft())
        self.message.setText("Draft restored as unsubmitted. All inspection and completion confirmations are cleared.")

    def reload_context(self, *, clear_text=False):
        self.controller.rebase()
        self._context = self.controller.context()
        self._loading = True
        family_combo = self.axes["family_id"]
        family_combo.clear(); family_combo.addItem("Choose explicitly…", CHOOSE); family_combo.addItem("None (explicit)", None)
        current = set(self._context["binding"]["families"])
        for row in self._context["families"]:
            if self._context["directed"] or row["local_type_id"] in current:
                family_combo.addItem(row["local_type_id"], row["local_type_id"])
        for combo in self.axes.values():
            combo.setCurrentIndex(0)
        self.processing.setCurrentIndex(0)
        for edit in self.new_leaf_fields.values(): edit.clear()
        for check in self.checks.values(): check.setChecked(False)
        self.audition.setChecked(False)
        if clear_text:
            for edit in self.texts.values(): edit.clear()
        self._loading = False
        self._family_changed()
        self._dirty = False
        self.refresh()
        if self.isVisible() and self.is_current():
            self.timer.start()

    def refresh(self):
        if self.owner._busy:
            self.form_body.setEnabled(False)
            self.submit_button.setEnabled(False)
            for button in (self.save_button, self.restore_button, self.reload_button): button.setEnabled(False)
            return
        self._waiting = False
        for button in (self.save_button, self.restore_button, self.reload_button): button.setEnabled(self.is_current())
        self.form_body.setEnabled(self.is_current() and self.controller.invalid_reason is None and not self.controller.submitted)
        if not self.is_current():
            self.invalidate("Project changed or is closing. This fine form cannot submit to another owner.")
            return
        context = self.controller.context()
        stale = context["binding"] != self.controller.base
        self.context_label.setText(f"Target {self.item_id} · Current family {', '.join(context['binding']['families']) or 'unresolved'} · "
            + ("Bound directed review: " + str(self.review_id) if context["directed"] else "Ordinary: close the current review before submission")
            + (" · Scope changed: reload and inspect again" if stale else "")
            + (" · Selected scope is stale/read-only; choose the current target scope" if "FINE_REVIEW_SCOPE_STALE" in context["blockers"] else
               " · Draft only until the ordinary round closes" if context["blockers"] else ""))
        self.submit_button.setEnabled(not stale and not context["blockers"] and not self.controller.invalid_reason and not self.controller.submitted)

    def submit(self):
        _require(self.is_current(), "FINE_CONTEXT_CHANGED", "The current project changed")
        value = self.payload()
        heard = self.audition.isChecked()
        self.save_draft()
        self._waiting = True
        self.submit_button.setEnabled(False)
        self.form_body.setEnabled(False)
        for button in (self.save_button, self.restore_button, self.reload_button): button.setEnabled(False)
        def completed(result):
            self._waiting = False
            self.message.setText("Fine decision saved through the shared service." +
                (" Draft cleanup needs attention: " + result["draft_cleanup_error"] if result.get("draft_cleanup_error") else "") +
                (" A newer unsubmitted draft was retained." if result.get("draft_retained") else ""))
            self.submitted.emit(result)
            self.refresh()
        def run(_progress, _cancelled):
            return self.controller.submit(value, audition_confirmed=heard)
        try:
            self.owner._run("Submit fine decision for " + self.item_id, run, completed)
        except Exception:
            self._waiting = False
            raise

    def invalidate(self, reason):
        self.controller.invalidate(reason)
        self.timer.stop()
        self._loading = True
        self.audition.setChecked(False)
        for check in self.checks.values(): check.setChecked(False)
        self._loading = False
        self.form_body.setEnabled(False)
        self.submit_button.setEnabled(False)
        self.message.setText(reason)

    def stop(self):
        self.stopRequested.emit(self.item_id)

    def _clear_confirmations(self):
        self._loading = True
        self.audition.setChecked(False)
        for check in self.checks.values(): check.setChecked(False)
        self._loading = False

    def reject(self):
        try:
            self.save_draft()
        except Exception as exc:
            self._guard(lambda: (_ for _ in ()).throw(exc))
            return
        self._clear_confirmations()
        self.stop()
        super().reject()

    def closeEvent(self, event):
        try:
            self.save_draft()
        except Exception as exc:
            event.ignore(); self._guard(lambda: (_ for _ in ()).throw(exc)); return
        self._clear_confirmations()
        self.stop(); self.timer.stop(); super().closeEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh(); self.timer.start()
