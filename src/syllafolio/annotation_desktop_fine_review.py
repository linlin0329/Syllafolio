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
    QHBoxLayout, QLabel, QLineEdit, QMenu, QStackedWidget, QPlainTextEdit, QPushButton, QScrollArea, QVBoxLayout, QWidget)

from .annotation_assistant_contracts import canonical_hash
from .annotation_batch_review import item_version, family_definition_fingerprint
from .annotation_manual_protocol import REVIEW_CHECKS
from .annotation_form_status import projected_fine_form


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

    def submit(self, payload, *, audition_confirmed, idempotency_key=None, saved_draft=None):
        _require(audition_confirmed is True, "FINE_AUDITION_REQUIRED", "Explicitly confirm this target's current listening and spectrogram inspection")
        _require(self.invalid_reason is None, "FINE_CONTEXT_CHANGED", self.invalid_reason or "Reload and review the current context")
        value = copy.deepcopy(payload)
        _require(value.get("item_id", self.item_id) == self.item_id, "FINE_ITEM_SCOPE", "Fine input belongs to a different target")
        value["item_id"] = self.item_id
        draft_payload = copy.deepcopy(value if saved_draft is None else saved_draft)
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


CHOICE_LABELS = {
    "diagnostic_region": {
        "assessable": "Clear enough to assess", "unassessable": "Obscured or incomplete",
        "not_assessed": "Not examined", "not_applicable": "Not applicable"},
    "component_status": {
        "present": "Present in the target", "not_observed_when_assessable": "Not observed in a clear region",
        "undetermined": "Cannot determine", "not_assessed": "Not examined", "not_applicable": "Not applicable"},
    "form_status": {
        "main": "Main form", "variant": "Variant", "candidate_form": "Candidate new form",
        "unresolved": "Unresolved after review", "not_assessed": "Not assessed", "not_applicable": "Not applicable"},
    "boundary_status": {"no_issue_reported": "No boundary issue", "needs_review": "Boundary needs review"},
    "processing_state": {
        "decided": "Decision reached", "reviewed_unresolved": "Reviewed, uncertainty remains",
        "unfinished": "Further inspection needed"},
}
TEXT_LABELS = {
    "observation_text": "Observation", "question": "Review question", "reason": "Basis for the judgment",
    "answer": "Conclusion", "comparison_note": "Comparisons made", "affected_relations_note": "Effect on type relationships",
    "pending_steps": "Remaining steps", "unresolved_level": "Level still unresolved", "missing_information": "Missing evidence",
}
WORDING_FIELDS = ("question", "answer", "affected_relations_note", "unresolved_level")
CHECK_LABELS = {
    "comparisons_completed": "I completed the necessary comparisons.",
    "affected_relations_checked": "I checked the affected type relationships.",
    "no_required_steps_pending": "All required review steps are complete.",
}


def _plain_label(text="", *, bold=False):
    label = QLabel()
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    label.setText(text)
    if bold:
        font = label.font()
        font.setBold(True)
        label.setFont(font)
    return label


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
        self._dirty = self._loading = self._waiting = self._stale = False
        self._draft_available = False
        self._new_leaf_id = None
        self._suggested_wording = {}
        self._context = None
        self._revision = None
        self.axes, self.texts, self.checks, self.new_leaf_fields = {}, {}, {}, {}
        self.setWindowTitle("Fine review · " + item_id)
        screen = owner.screen()
        available = screen.availableGeometry() if screen else None
        self.resize(min(860, available.width() - 32) if available else 860,
                    min(735, available.height() - 64) if available else 735)
        layout = QVBoxLayout(self)
        self.context_label = _plain_label(bold=True)
        self.saved_label = _plain_label()
        self.saved_label.setObjectName("fineSavedOutcome")
        self.criteria_label = _plain_label()
        layout.addWidget(self.context_label)
        layout.addWidget(self.saved_label)
        criterion_row = QHBoxLayout()
        self.criteria_button = QPushButton("Family criteria")
        self.criteria_button.setCheckable(True)
        self.criteria_button.toggled.connect(self.criteria_label.setVisible)
        criterion_row.addWidget(self.criteria_button)
        self.inspect_button = self._button("Inspect spectrogram", "fineInspectTarget",
                                          lambda: self.inspectRequested.emit(self.item_id, False))
        self.play_target = self._button("Play target", "finePlayTarget",
                                       lambda: self.auditionRequested.emit(self.item_id, False))
        self.play_context = self._button("Play source context", "finePlayContext",
                                        lambda: self.auditionRequested.emit(self.item_id, True))
        self.stop_button = self._button("Stop", "fineStop", self.stop)
        for button in (self.inspect_button, self.play_target, self.play_context, self.stop_button):
            criterion_row.addWidget(button)
        criterion_row.addStretch()
        layout.addLayout(criterion_row)
        layout.addWidget(self.criteria_label)
        self.criteria_label.hide()
        self.pages = QStackedWidget()
        # The main window uses the same context when switching targets or closing.
        self.form_body = self.pages
        layout.addWidget(self.pages, 1)
        self._build_judgment()
        self._build_evidence()
        self.summary = _plain_label()
        self.summary.setObjectName("fineSummary")
        layout.addWidget(self.summary)
        self.message = _plain_label()
        layout.addWidget(self.message)
        footer = QHBoxLayout()
        self.more_button = QPushButton("Draft options")
        menu = QMenu(self.more_button)
        self.more_button.setMenu(menu)
        self.save_button = menu.addAction("Save unsubmitted draft", lambda: self._guard(self.save_draft))
        self.restore_button = menu.addAction("Restore draft", lambda: self._guard(self.restore_draft))
        self.reload_button = menu.addAction("Reload saved context and reset choices", lambda: self._guard(self.reload_context))
        footer.addWidget(self.more_button)
        self.back_button = self._button("Back to judgment", "fineBack", self._back)
        footer.addWidget(self.back_button)
        footer.addStretch()
        self.reinspect_button = self._button("Review again", "fineReviewAgain", lambda: self.reload_context(clear_text=True))
        self.next_button = self._button("Review evidence…", "fineReviewEvidence", self._next)
        self.submit_button = self._button("Save fine decision", "fineSubmitDecision", self.submit)
        footer.addWidget(self.reinspect_button)
        footer.addWidget(self.next_button)
        footer.addWidget(self.submit_button)
        footer.addWidget(self._button("Close", "fineClose", self.reject))
        layout.addLayout(footer)
        self.timer = QTimer(self)
        self.timer.setInterval(350)
        self.timer.timeout.connect(lambda: self._guard(self.refresh))
        self.finished.connect(lambda _: self.timer.stop())
        if hasattr(owner, "operationFailed"):
            owner.operationFailed.connect(self._operation_failed)
        self.reload_context(clear_text=False)
        if self.controller.store.warning:
            self.message.setText(self.controller.store.warning)

    def _button(self, text, name, callback):
        button = QPushButton(text)
        button.setObjectName(name)
        button.setAutoDefault(False)
        button.clicked.connect(lambda _=False: self._guard(callback))
        return button

    def _page(self, name):
        scroll = QScrollArea()
        scroll.setObjectName(name)
        scroll.setWidgetResizable(True)
        self.pages.addWidget(scroll)
        body = QWidget()
        inner = QVBoxLayout(body)
        scroll.setWidget(body)
        return scroll, inner

    def _combo(self, key, form, label, choices=()):
        combo = QComboBox()
        combo.setObjectName("fine_" + key)
        combo.addItem("Choose…", CHOOSE)
        for value in choices:
            combo.addItem(CHOICE_LABELS[key][value], value)
        form.addRow(label, combo)
        return combo

    def _build_judgment(self):
        self.judgment_page, layout = self._page("fineJudgment")
        layout.addWidget(_plain_label("Inspect the sound", bold=True))
        self.judgment = QFormLayout()
        self.judgment.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        layout.addLayout(self.judgment)
        self.axes["family_id"] = self._combo("family_id", self.judgment, "Family for this decision")
        for key, label in (("diagnostic_region", "Can the diagnostic region be assessed?"),
                          ("component_status", "What can you tell about the component?"),
                          ("form_status", "What fine form does the sound support?")):
            self.axes[key] = self._combo(key, self.judgment, label, AXIS_OPTIONS[key])
        self.axes["leaf_id"] = self._combo("leaf_id", self.judgment, "Fine type")
        self.new_leaf_box = QGroupBox("New fine type · draft definition")
        new_form = QFormLayout(self.new_leaf_box)
        for key, label in (("title", "Name"),("main_variant_difference", "Distinguishing features"),
                          ("reason", "Reason for creating this type")):
            edit = QLineEdit()
            edit.setObjectName("fine_new_leaf_" + key)
            edit.textChanged.connect(self._edited)
            new_form.addRow(label, edit)
            self.new_leaf_fields[key] = edit
        # Older drafts may have a separate diagnostic body. Keep it editable.
        self.leaf_detail = self._button("Additional diagnostic criteria…", "fineLeafDetails", self._toggle_leaf_details)
        new_form.addRow(self.leaf_detail)
        body = QLineEdit()
        body.setObjectName("fine_new_leaf_diagnostic_body")
        body.textChanged.connect(self._edited)
        self.new_leaf_fields["diagnostic_body"] = body
        new_form.addRow("Diagnostic criteria", body)
        new_form.setRowVisible(body, False)
        self._leaf_form = new_form
        layout.addWidget(self.new_leaf_box)
        status_form = QFormLayout()
        self.axes["boundary_status"] = self._combo("boundary_status", status_form, "Syllable boundaries", AXIS_OPTIONS["boundary_status"])
        self.processing = self._combo("processing_state", status_form, "Review progress", ("decided", "reviewed_unresolved", "unfinished"))
        layout.addLayout(status_form)
        self.guidance = _plain_label()
        layout.addWidget(self.guidance)
        layout.addStretch()
        self.axes["family_id"].currentIndexChanged.connect(self._family_changed)
        self.axes["form_status"].currentIndexChanged.connect(self._form_changed)
        for key in ("diagnostic_region", "component_status", "boundary_status", "leaf_id"):
            self.axes[key].currentIndexChanged.connect(self._edited)
        self.processing.currentIndexChanged.connect(self._edited)

    def _build_evidence(self):
        self.evidence_page, layout = self._page("fineEvidence")
        layout.addWidget(_plain_label("Evidence and decision record", bold=True))
        self.wording_button = self._button("Use decision wording", "fineUseDecisionWording", self._use_decision_wording)
        self.wording_button.setToolTip("Fill the question, conclusion and type relationships from your choices. You can edit the wording.")
        wording_actions = QHBoxLayout()
        wording_actions.addWidget(self.wording_button)
        self.show_wording = QCheckBox("Show decision wording")
        self.show_wording.setChecked(True)
        self.show_wording.toggled.connect(self._update_controls)
        wording_actions.addWidget(self.show_wording)
        wording_actions.addStretch()
        layout.addLayout(wording_actions)
        self.evidence_form = QFormLayout()
        self.evidence_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        layout.addLayout(self.evidence_form)
        for key in TEXTS:
            edit = QPlainTextEdit()
            edit.setObjectName("fine_" + key)
            edit.setFixedHeight(44)
            edit.textChanged.connect(self._edited)
            self.evidence_form.addRow(TEXT_LABELS[key], edit)
            self.texts[key] = edit
        for key in sorted(REVIEW_CHECKS):
            check = QCheckBox(CHECK_LABELS[key])
            check.toggled.connect(self._update_controls)
            self.checks[key] = check
            layout.addWidget(check)
        self.audition = QCheckBox("I listened to this syllable and inspected its spectrogram.")
        self.audition.setObjectName("fineAuditionConfirmed")
        self.audition.toggled.connect(self._update_controls)
        layout.addWidget(self.audition)
        layout.addStretch()

    def _toggle_leaf_details(self):
        field = self.new_leaf_fields["diagnostic_body"]
        self._leaf_form.setRowVisible(field, not self._leaf_form.isRowVisible(field))

    def _use_decision_wording(self):
        """Reuse the operator's selections only; observations and comparisons remain human inputs."""
        family = self.axes["family_id"].currentData()
        family_name = self._names.get(family, family) if family else "unresolved"
        form = CHOICE_LABELS["form_status"][self.axes["form_status"].currentData()]
        leaf = self.axes["leaf_id"].currentData()
        if leaf == NEW_LEAF:
            relation = f"Create a draft fine type within {family_name}: {self.new_leaf_fields['title'].text().strip()}."
        elif leaf:
            relation = f"Family: {family_name}. Fine type: {self._names.get(leaf, leaf)}."
        else:
            relation = f"Family: {family_name}. No fine type assigned."
        suggestions = {
            "question": "What level of fine-form assignment does this syllable support?",
            "answer": f"Family: {family_name}. Fine form: {form}.",
            "affected_relations_note": relation,
            "unresolved_level": "Fine form" if family else "Family",
        }
        for key in self._text_keys():
            edit = self.texts[key]
            if key in suggestions and not edit.toPlainText().strip():
                edit.setPlainText(suggestions[key])
                self._suggested_wording[key] = suggestions[key]
        self.show_wording.setChecked(False)

    def _next(self):
        if self._problem():
            self.guidance.setText(self._problem())
            return
        self._clear_confirmations()
        self.pages.setCurrentWidget(self.evidence_page)
        self._update_controls()

    def _back(self):
        self._clear_confirmations()
        self.pages.setCurrentWidget(self.judgment_page)
        self._update_controls()
    def is_current(self):
        return (self.owner.project is self.project and self.owner._close_coordinator.generation == self.generation
                and self.owner._close_coordinator.state == "open")

    def _guard(self, function):
        try:
            return function()
        except Exception as exc:
            self.message.setText(system_error(exc))
            self.owner._error(exc, operation="Fine review " + self.item_id)

    def _operation_failed(self, message):
        if self._waiting:
            self._waiting = False
            self.message.setText(message)
            self._update_controls()

    def _family_changed(self):
        if self._loading:
            return
        self._loading = True
        for key in ("diagnostic_region", "component_status", "form_status", "boundary_status"):
            self.axes[key].setCurrentIndex(0)
        self.processing.setCurrentIndex(0)
        self._clear_leaf_draft()
        self._populate_leaves()
        self._loading = False
        self._edited()

    def _form_changed(self):
        if self._loading:
            return
        self._loading = True
        self._populate_leaves()
        self._loading = False
        self._edited()

    def _clear_leaf_draft(self):
        self._new_leaf_id = None
        for edit in self.new_leaf_fields.values():
            edit.clear()
        self._leaf_form.setRowVisible(self.new_leaf_fields["diagnostic_body"], False)

    def _populate_leaves(self):
        family = self.axes["family_id"].currentData()
        form = self.axes["form_status"].currentData()
        combo = self.axes["leaf_id"]
        combo.clear()
        if form not in ("main", "variant", "candidate_form") or family in (None, CHOOSE):
            combo.addItem("No fine type assigned", None)
            self.judgment.setRowVisible(combo, False)
            return
        self.judgment.setRowVisible(combo, True)
        if form == "candidate_form":
            combo.addItem("Propose the form only", None)
        else:
            combo.addItem("Choose a fine type…", CHOOSE)
        for row in self._context["leaves"]:
            if row["parent_type_id"] == family:
                key = row["local_type_id"]
                combo.addItem(self._names.get(key, key), key)
                combo.setItemData(combo.count() - 1, key, Qt.ItemDataRole.ToolTipRole)
        combo.addItem("Create new fine type…", NEW_LEAF)

    def _edited(self, *_):
        if self._loading:
            return
        if self.pages.currentWidget() is self.evidence_page and self.sender() in (
                self.texts[k] for k in WORDING_FIELDS):
            self.show_wording.setChecked(True)
        if self.pages.currentWidget() is self.judgment_page and self._suggested_wording:
            # Discard untouched suggested prose when its underlying choices change.
            self._loading = True
            for key, previous in self._suggested_wording.items():
                if self.texts[key].toPlainText() == previous:
                    self.texts[key].clear()
            self._suggested_wording.clear()
            self._loading = False
        if self.axes["leaf_id"].currentData() == NEW_LEAF and not self._new_leaf_id:
            self._new_leaf_id = "L-" + uuid.uuid4().hex
        self._dirty = True
        self._clear_confirmations()
        self._update_controls()

    def _text_keys(self):
        state = self.processing.currentData()
        keys = ["question", "observation_text", "reason"]
        if state == "unfinished":
            keys.append("pending_steps")
        elif state in ("reviewed_unresolved", "decided"):
            keys.extend(("answer", "comparison_note", "affected_relations_note"))
            if state == "reviewed_unresolved":
                keys.extend(("unresolved_level", "missing_information"))
        return keys

    def payload(self):
        axes = {k: w.currentData() for k, w in self.axes.items() if w.currentData() != CHOOSE}
        if "family_id" in axes:
            axes["family_status"] = "assigned" if axes["family_id"] is not None else "unresolved"
        value = {"item_id": self.item_id, "axes": axes,
                 **{key: self.texts[key].toPlainText() for key in self._text_keys()},
                 "checks": {k: w.isChecked() for k, w in self.checks.items()}}
        if self.processing.currentData() != CHOOSE:
            value["processing_state"] = self.processing.currentData()
        if axes.get("leaf_id") == NEW_LEAF:
            axes["leaf_id"] = self._new_leaf_id
            value["new_leaf"] = {"local_type_id": self._new_leaf_id,
                                 **{k: w.text() for k, w in self.new_leaf_fields.items()}}
            value["draft_new_leaf_parent"] = axes["family_id"]
        return value

    def _problem(self):
        f = self.axes["family_id"].currentData()
        if f == CHOOSE:
            return "Choose the family for this decision."
        for key, msg in (("diagnostic_region", "Choose whether the diagnostic region can be assessed."),
                         ("component_status", "Record what you can tell about the diagnostic component."),
                         ("form_status", "Choose the finest form judgment supported by the evidence.")):
            if self.axes[key].currentData() == CHOOSE:
                return msg
        region = self.axes["diagnostic_region"].currentData()
        component = self.axes["component_status"].currentData()
        form = self.axes["form_status"].currentData()
        leaf = self.axes["leaf_id"].currentData()
        if component in ("present", "not_observed_when_assessable") and region != "assessable":
            return "Presence or absence requires an assessable diagnostic region."
        if form == "candidate_form" and f is None:
            return "Choose the family whose fine form you are proposing."
        if form in ("main", "variant"):
            if f is None:
                return "A main form or variant needs a supported family."
            expected = "present" if form == "variant" else "not_observed_when_assessable"
            if component != expected:
                return ("A variant requires the diagnostic component to be present." if form == "variant"
                        else "A main form requires an assessable region with the diagnostic component absent.")
            if leaf in (None, CHOOSE):
                return "Choose an existing fine type or create a draft definition."
        if leaf == NEW_LEAF:
            if any(not self.new_leaf_fields[key].text().strip() for key in ("title", "reason")):
                return "Enter a name and a creation reason for the new fine type."
            if not any(self.new_leaf_fields[k].text().strip() for k in ("main_variant_difference", "diagnostic_body")):
                return "Describe the features that distinguish the proposed fine type."
        boundary = self.axes["boundary_status"].currentData()
        processing = self.processing.currentData()
        if boundary == CHOOSE or processing == CHOOSE:
            return "Choose the boundary status and review progress."
        if processing != "unfinished" and boundary == "needs_review":
            return "Keep the review open while a boundary still needs inspection."
        if processing == "reviewed_unresolved" and form not in ("unresolved", "candidate_form", "not_applicable"):
            return "For remaining uncertainty, choose an unresolved or candidate judgment."
        if leaf == NEW_LEAF and processing != "decided":
            return "To record a new type draft, finish this decision or propose the form without creating a type."
        if form in ("main", "variant") and boundary == "needs_review":
            return "Resolve the boundary before assigning a main form or variant."
        return ""

    def _summary(self):
        axes = self.payload()["axes"]
        family = axes.get("family_id")
        leaf = axes.get("leaf_id")
        family_name = self._names.get(family, family) if family else "Unresolved"
        form = CHOICE_LABELS["form_status"].get(axes.get("form_status"), "Not selected")
        leaf_name = (self.new_leaf_fields["title"].text().strip() if self.axes["leaf_id"].currentData() == NEW_LEAF
                     else self._names.get(leaf, leaf))
        suffix = f" · {leaf_name}" if leaf_name else ""
        action = ("Create draft definition" if self.axes["leaf_id"].currentData() == NEW_LEAF
                  else "Use existing definition")
        region = CHOICE_LABELS["diagnostic_region"].get(axes.get("diagnostic_region"), "Not selected")
        part = CHOICE_LABELS["component_status"].get(axes.get("component_status"), "Not selected")
        progress = CHOICE_LABELS["processing_state"].get(self.processing.currentData(), "Not selected")
        return (f"{self.item_id} · Family: {family_name} · {form}{suffix}\n"
                f"Region: {region} · Component: {part} · {progress}" + (f" · {action}" if leaf_name else ""))

    def _update_controls(self, *_):
        if not self._context or self._loading:
            return
        available = (self.is_current() and not self.owner._busy and not self._waiting
                     and not self.controller.invalid_reason and not self.controller.submitted and not self._stale)
        self.form_body.setEnabled(available)
        self.new_leaf_box.setVisible(self.axes["leaf_id"].currentData() == NEW_LEAF)
        complete = self.processing.currentData() != "unfinished"
        active_texts = self._text_keys()
        for key, edit in self.texts.items():
            visible = self.show_wording.isChecked() or key not in WORDING_FIELDS or not edit.toPlainText().strip()
            self.evidence_form.setRowVisible(edit, key in active_texts and visible)
        for check in self.checks.values():
            check.setVisible(complete)
        on_target = self.pages.currentWidget() is self.judgment_page
        self.back_button.setVisible(not on_target)
        self.reinspect_button.setVisible(self.controller.submitted)
        self.reinspect_button.setEnabled(self.is_current() and not self.owner._busy and not self._waiting)
        self.next_button.setVisible(on_target and not self.controller.submitted)
        self.submit_button.setVisible(not on_target and not self.controller.submitted)
        self.summary.setVisible(not on_target)
        problem = self._problem()
        self.guidance.setText(problem)
        self.summary.setText(self._summary())
        self.next_button.setEnabled(available and not problem)
        self.submit_button.setEnabled(available and not on_target and not problem and not self._context["blockers"]
                                      and all(self.texts[k].toPlainText().strip() for k in active_texts)
                                      and (not complete or all(c.isChecked() for c in self.checks.values()))
                                      and self.audition.isChecked())
        self.restore_button.setEnabled(available and self._draft_available)
        self.save_button.setEnabled(available and self._dirty)
        self.reload_button.setEnabled(self.is_current() and not self._waiting and not self.owner._busy)
        self.back_button.setEnabled(available)
    def save_draft(self):
        if self._dirty:
            self.controller.save_draft(self._draft_payload())
            self._dirty = False
            self._draft_available = True
            self.message.setText("Fine review draft saved.")
            self._update_controls()
        return True

    def _draft_payload(self):
        return {**self.payload(), **{key: edit.toPlainText() for key, edit in self.texts.items()}}

    def _fill(self, value):
        axes = value.get("axes", {})
        self._suggested_wording.clear()
        self._loading = True
        try:
            family = self.axes["family_id"]
            current = self._context["binding"]["families"]
            default = current[0] if len(current) == 1 and not self._context["directed"] else CHOOSE
            family.setCurrentIndex(max(0, family.findData(axes.get("family_id", default))))
            for key in AXIS_OPTIONS:
                if key in self.axes:
                    combo = self.axes[key]
                    combo.setCurrentIndex(max(0, combo.findData(axes.get(key, CHOOSE))))
            self._populate_leaves()
            self._clear_leaf_draft()
            leaf = value.get("new_leaf")
            if leaf:
                _require(value.get("draft_new_leaf_parent") == family.currentData(),
                         "FINE_DRAFT_STALE", "The draft fine type belongs to a different family.")
                self._new_leaf_id = leaf["local_type_id"]
                for key, edit in self.new_leaf_fields.items():
                    edit.setText(leaf.get(key, ""))
                self._leaf_form.setRowVisible(self.new_leaf_fields["diagnostic_body"], bool(leaf.get("diagnostic_body")))
            combo = self.axes["leaf_id"]
            combo.setCurrentIndex(max(0, combo.findData(NEW_LEAF if leaf else axes.get("leaf_id", CHOOSE))))
            for key, edit in self.texts.items():
                edit.setPlainText(value.get(key, ""))
            self.processing.setCurrentIndex(max(0, self.processing.findData(value.get("processing_state", CHOOSE))))
        finally:
            self._loading = False
        self._dirty = True
        self._back()

    def restore_draft(self):
        self._fill(self.controller.restore_draft())
        self.message.setText("Restored unsubmitted choices. Inspect this target before confirming.")

    def _set_header(self, context):
        state = context["state"]
        names = {key: card.get("title") or key for key, card in state.get("type_cards", {}).items()}
        for row in state.get("batch_review", {}).get("families", []):
            names.setdefault(row["family_id"], row.get("title") or row["family_id"])
        self._names = names
        ids = context["binding"]["families"]
        family = ids[0] if len(ids) == 1 else None
        leaf = next((r["local_type_id"] for r in context["leaves"] if self.item_id in r["member_ids"]), None)
        item = next(row for row in state["items"] if row["item_id"] == self.item_id)
        saved = state.get("batch_review", {}).get("assignments", {}).get(self.item_id, {})
        decisions = state["decisions"]
        if isinstance(decisions, dict):
            decisions = decisions.values()
        fine = projected_fine_form(self.item_id, item.get("current_issue_ids", []), saved, family, leaf,
                                   decisions, set(state.get("effective_decision_ids", [])))
        label = CHOICE_LABELS["form_status"].get(fine["form_status"], "Not recorded")
        self.context_label.setText(self.item_id + " · " + (names.get(family, family) if family else "No assigned family"))
        self.saved_label.setText("Saved fine form: " + label + (" · " + names.get(leaf, leaf) if leaf else ""))
        card = state.get("type_cards", {}).get(family, {})
        self.criteria_label.setText(card.get("diagnostic_body") or "No saved diagnostic criteria for this family.")
        self.play_context.setEnabled(self.project.dataset._items[self.item_id].get("context_kind") == "source_parent")
        if self._stale or self.controller.invalid_reason:
            self.message.setText(self.controller.invalid_reason or "The saved context changed. Reload and inspect this syllable again.")
        elif context["blockers"]:
            self.message.setText("Finish the open review before submitting this decision." if "FINE_REVIEW_SCOPE_STALE" not in context["blockers"]
                                 else "This saved round is historical. Open the syllable in the current scope to submit a decision.")

    def reload_context(self, *, clear_text=False):
        self.controller.rebase()
        self._suggested_wording.clear()
        self._context = self.controller.context()
        self._revision = self._context["state"]["revision"]
        self._stale = False
        self._loading = True
        try:
            self._set_header(self._context)
            self._clear_leaf_draft()
            family_combo = self.axes["family_id"]
            family_combo.clear()
            family_combo.addItem("Choose a family…", CHOOSE)
            family_combo.addItem("Family unresolved", None)
            current = self._context["binding"]["families"]
            directed = self._context["directed"]
            for row in self._context["families"]:
                key = row["local_type_id"]
                if directed or key in current:
                    family_combo.addItem(self._names.get(key, key), key)
                    family_combo.setItemData(family_combo.count()-1, key, Qt.ItemDataRole.ToolTipRole)
            retained = not directed and len(current) == 1
            if retained:
                family_combo.setCurrentIndex(family_combo.findData(current[0]))
            self.judgment.setRowVisible(family_combo, not retained)
            for key in AXIS_OPTIONS:
                if key in self.axes:
                    self.axes[key].setCurrentIndex(0)
            self._populate_leaves()
            self.processing.setCurrentIndex(0)
            if clear_text:
                for edit in self.texts.values():
                    edit.clear()
            if directed and not self.texts["question"].toPlainText():
                review = self._context["state"]["batch_review"]["reviews"][self.review_id]
                self.texts["question"].setPlainText(review.get("directed_context", {}).get("question", ""))
            self._draft_available = self.controller.store.get(self.controller.key) is not None
        finally:
            self._loading = False
        self._dirty = False
        self._back()
        if self._draft_available and not self._context["blockers"]:
            self.message.setText("A saved fine review draft is available in Draft options.")
        elif not self._context["blockers"]:
            self.message.clear()
        if self.isVisible() and self.is_current():
            self.timer.start()

    def refresh(self, force=False):
        if not self.is_current():
            self.invalidate("The project changed or is closing. Reopen Fine review for the current syllable.")
            return
        if not self.owner._busy:
            state = getattr(self.owner, "state", None)
            if force or state is None or state.get("revision") != self._revision:
                context = self.controller.context()
                self._revision = context["state"]["revision"]
                self._stale = context["binding"] != self.controller.base
                self._context = context
                if self._stale:
                    self._clear_confirmations()
                self._set_header(context)
        self._update_controls()

    def submit(self):
        _require(self.is_current() and self.pages.currentWidget() is self.evidence_page and self.submit_button.isEnabled(),
                 "FINE_CONTEXT_CHANGED", "Review the judgment, evidence and explicit confirmations before saving.")
        value = self.payload()
        draft = self._draft_payload()
        heard = self.audition.isChecked()
        self.save_draft()
        self._waiting = True
        self._update_controls()
        def completed(result):
            self._waiting = False
            self._draft_available = bool(result.get("draft_retained") or result.get("draft_cleanup_error"))
            self.submitted.emit(result)
            self.refresh(force=True)
            self.message.setText("Fine decision saved." +
                (" A newer unsubmitted draft was retained." if result.get("draft_retained") else "") +
                (" Draft cleanup needs attention: " + result["draft_cleanup_error"] if result.get("draft_cleanup_error") else ""))
        def run(_progress, _cancelled):
            return self.controller.submit(value, audition_confirmed=heard, saved_draft=draft)
        try:
            self.owner._run("Submit fine decision for " + self.item_id, run, completed)
        except Exception:
            self._waiting = False
            self._update_controls()
            raise

    def invalidate(self, reason):
        self.controller.invalidate(reason)
        self.timer.stop()
        self._clear_confirmations()
        self._update_controls()
        self.message.setText(reason)

    def stop(self):
        self.stopRequested.emit(self.item_id)

    def _clear_confirmations(self):
        for check in (*self.checks.values(), self.audition):
            check.blockSignals(True)
            check.setChecked(False)
            check.blockSignals(False)

    def reject(self):
        try:
            self.save_draft()
        except Exception as exc:
            self.message.setText(system_error(exc))
            self.owner._error(exc, operation="Save fine review draft")
            return
        self._clear_confirmations()
        self.stop()
        super().reject()

    def closeEvent(self, event):
        try:
            self.save_draft()
        except Exception as exc:
            event.ignore()
            self.message.setText(system_error(exc))
            self.owner._error(exc, operation="Save fine review draft")
            return
        self.timer.stop()
        self._clear_confirmations()
        self.stop()
        super().closeEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh(force=True)
        if self.is_current():
            self.timer.start()
