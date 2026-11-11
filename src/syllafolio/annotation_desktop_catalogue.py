"""D2人工类型卡与逐family定向重开；共同服务仍是唯一标签/版本权威。"""
from __future__ import annotations
from .annotation_english import system_fields, system_text, system_error

import copy
import hashlib
import json
import threading
import uuid

from .annotation_assistant_contracts import canonical_hash
from .annotation_manual_protocol import REOPEN_BASES, definition_fingerprint, membership_fingerprint
from .annotation_batch_review import family_member_fingerprint, family_definition_fingerprint, review_stale_reason
from .annotation_final_review import prepare_directed_context, directed_return_current
from .annotation_desktop_review_media import _checked_path

FILENAME = "desktop_catalogue_drafts.json"
SCHEMA = "syllafolio-desktop-catalogue-drafts-v1"
TEXT_FIELDS = ("title", "diagnostic_body", "main_variant_difference", "uncertainties", "reason", "definition_status")
EXAMPLE_FIELDS = ("representative_ids", "atypical_ids", "counterexample_ids", "competing_type_ids")
CARD_FIELDS = {"local_type_id", *TEXT_FIELDS, *EXAMPLE_FIELDS}
ACTIVE = {"queued", "running", "cancel_requested"}


class CatalogueReviewError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(f"{code}: {message}")


def _require(condition, code, message):
    if not condition:
        raise CatalogueReviewError(code, message)


def _identity(project):
    return {"project_id": project.metadata["project_id"], "run_id": project.service.run_id,
            "input_fingerprint": project.dataset.input_fingerprint}


class CatalogueDraftStore:
    """独立于Fine的未提交sidecar；坏原件拒绝覆写，失败保留内存输入。"""
    def __init__(self, project):
        self.project = project
        self.path = project.project_dir / FILENAME
        self.identity = _identity(project)
        with project.service._lock:
            if not hasattr(project.service, "_desktop_catalogue_draft_lock"):
                project.service._desktop_catalogue_draft_lock = threading.RLock()
        self.lock = project.service._desktop_catalogue_draft_lock

    def _load(self):
        _checked_path(self.project, self.path)
        if not self.path.exists():
            return {}, None
        raw = self.path.read_bytes()
        def pairs(rows):
            value = {}
            for key, item in rows:
                _require(key not in value, "CATALOGUE_DRAFT_INVALID", "Duplicate catalogue draft key.")
                value[key] = item
            return value
        try:
            value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs)
            _require(isinstance(value, dict) and value.get("schema_version") == SCHEMA
                     and all(value.get(key) == item for key, item in self.identity.items())
                     and isinstance(value.get("drafts"), dict), "CATALOGUE_DRAFT_IDENTITY", "Catalogue draft input/run identity does not match.")
            _require(all(isinstance(row, dict) and isinstance(row.get("payload"), dict)
                     and isinstance(row.get("binding"), dict) and row.get("submission_state") == "unsubmitted"
                     and row.get("review_confirmation") is False
                     and all(row["binding"].get(field) == expected for field, expected in self.identity.items())
                     and isinstance(row["binding"].get("type_id"), str)
                     for row in value["drafts"].values()), "CATALOGUE_DRAFT_INVALID", "Invalid unsubmitted catalogue draft.")
        except (UnicodeError, json.JSONDecodeError, TypeError) as exc:
            raise CatalogueReviewError("CATALOGUE_DRAFT_INVALID", "Unreadable catalogue drafts retained unchanged.") from exc
        return value["drafts"], hashlib.sha256(raw).hexdigest()

    def put(self, key, binding, payload):
        with self.lock:
            drafts, original = self._load()
            drafts[key] = {"binding": copy.deepcopy(binding), "payload": copy.deepcopy(payload),
                           "submission_state": "unsubmitted", "review_confirmation": False}
            value = {"schema_version": SCHEMA, **self.identity, "drafts": drafts}
            temporary = self.path.with_name(".catalogue-draft-" + uuid.uuid4().hex + ".json")
            try:
                temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
                _checked_path(self.project, self.path)
                current = hashlib.sha256(self.path.read_bytes()).hexdigest() if self.path.exists() else None
                _require(current == original, "CATALOGUE_DRAFT_CHANGED", "Draft bytes changed; no original was overwritten.")
                temporary.replace(self.path)
            finally:
                temporary.unlink(missing_ok=True)

    def get(self, key):
        with self.lock:
            return copy.deepcopy(self._load()[0].get(key))


class CatalogueReviewController:
    def __init__(self):
        self._lock = threading.RLock()
        self._selection = None
        self._directed = {}
        self._requests = {}
        self.invalid_reason = None

    @staticmethod
    def view(project, snapshot=None):
        state = project.snapshot() if snapshot is None else snapshot
        memberships = {row["local_type_id"]: row for row in state["memberships"]}
        rows = []
        for row in state.get("final_review", {}).get("types", []):
            item = copy.deepcopy(row)
            item["member_ids"] = sorted(memberships[row["local_type_id"]]["member_ids"])
            item["family_id"] = row["parent_type_id"] or row["local_type_id"]
            item["definition_review_required_ids"] = state.get("completion", {}).get("definition_review_gaps", {}).get(row["local_type_id"], [])
            rows.append(item)
        return {"types": rows, "items": copy.deepcopy(state.get("items", [])), "revision": state["revision"],
                "scope": copy.deepcopy(state.get("catalogue_scope")), "directed_reviews": copy.deepcopy(state.get("final_review", {}).get("directed_reviews", []))}

    @staticmethod
    def _binding(project, state, type_id, generation):
        member = next((row for row in state["memberships"] if row["local_type_id"] == type_id), None)
        _require(member is not None, "TYPE_NOT_FOUND", "Select an existing human family or leaf.")
        card = state.get("type_cards", {}).get(type_id)
        return {**_identity(project), "type_id": type_id, "generation": generation, "revision": state["revision"],
                "membership_fingerprint": membership_fingerprint(member),
                "definition_fingerprint": definition_fingerprint(card) if card else None}

    def select(self, project, type_id, *, generation=0, snapshot=None):
        state = project.snapshot() if snapshot is None else snapshot
        view = self.view(project, state)
        selected = next((row for row in view["types"] if row["local_type_id"] == type_id), None)
        _require(selected is not None, "TYPE_NOT_FOUND", "Select an existing family or leaf.")
        with self._lock:
            self._selection = {**view, "selection": selected, "binding": self._binding(project, state, type_id, generation)}
            self.invalid_reason = None
            return self.snapshot()

    def snapshot(self):
        with self._lock:
            return {**copy.deepcopy(self._selection or {"selection": None, "binding": None, "types": []}), "invalid_reason": self.invalid_reason}

    def invalidate(self, reason):
        with self._lock:
            self.invalid_reason = reason
            self._directed.clear()

    def observe_context(self, state, *, project_id, generation):
        with self._lock:
            if self._selection is None:
                return False
            binding = self._selection["binding"]
            changed = any(binding[key] != value for key, value in {"project_id": project_id, "run_id": state.get("run_id"),
                "generation": generation, "revision": state.get("revision")}.items())
            if changed:
                self.invalid_reason = "The catalogue context changed. Keep the draft, refresh this type's current members/definition and explicitly review before saving."
            return changed

    def save_draft(self, project, payload, *, kind="card", type_id=None, generation=0, binding=None):
        selected = self.snapshot()
        type_id = type_id or payload.get("local_type_id")
        _require(isinstance(type_id, str) and bool(type_id), "TYPE_NOT_FOUND", "Bind a draft to an existing type/family first.")
        binding = binding or (selected["binding"] if kind == "card" and selected["binding"] and selected["binding"]["type_id"] == type_id
                              else self._binding(project, project.snapshot(), type_id, generation))
        CatalogueDraftStore(project).put(kind + ":" + type_id, binding, payload)
        return True

    def restore_draft(self, project, type_id, *, kind="card", generation=0):
        record = CatalogueDraftStore(project).get(kind + ":" + type_id)
        _require(record is not None, "CATALOGUE_DRAFT_MISSING", "No matching unsubmitted draft exists.")
        current = self._binding(project, project.snapshot(), type_id, generation)
        record["stale_for_current_context"] = any(record["binding"].get(key) != value for key, value in current.items() if key != "revision")
        record["review_confirmation"] = False
        return record

    def save_card(self, project, payload, *, generation, idempotency_key, reviewed_confirmed=False):
        _require(isinstance(idempotency_key, str) and 0 < len(idempotency_key) <= 160, "IDEMPOTENCY_REQUIRED", "Use an explicit card request key.")
        _require(isinstance(payload, dict) and set(payload) == CARD_FIELDS, "CATALOGUE_CARD_FIELDS", "Submit the explicit card fields and selected stable IDs.")
        _require(payload.get("definition_status") != "reviewed" or reviewed_confirmed is True,
                 "CATALOGUE_REVIEW_CONFIRMATION", "Explicitly confirm the definition and chosen examples before marking reviewed.")
        key = ("card", idempotency_key)
        with project.service._lock:
            with self._lock:
                prior = self._requests.get(key)
                if prior:
                    _require(prior["payload"] == payload and prior["identity"] == _identity(project),
                             "IDEMPOTENCY_CONFLICT", "The original card request changed.")
                    return project.execute("type_card", copy.deepcopy(payload), expected_revision=prior["revision"], idempotency_key=idempotency_key)
                _require(self._selection is not None and self.invalid_reason is None, "CATALOGUE_CONTEXT_CHANGED", self.invalid_reason or "Select and review the current type first.")
                state = project.snapshot()
                binding = self._binding(project, state, payload.get("local_type_id"), generation)
                _require(binding == self._selection["binding"], "CATALOGUE_CONTEXT_CHANGED", "The type, members or version changed. The unsubmitted input is retained.")
                self._requests[key] = {"payload": copy.deepcopy(payload), "revision": state["revision"], "identity": _identity(project)}
                return project.execute("type_card", copy.deepcopy(payload), expected_revision=state["revision"], idempotency_key=idempotency_key)

    @staticmethod
    def _context(context):
        _require(isinstance(context, dict) and set(context) <= {"question", "basis", "reason", "affected_type_ids", "parent_issue_id", "observation_ids"},
                 "DIRECTED_CONTEXT", "Use only the existing directed-context contract fields.")
        return copy.deepcopy(context)

    @staticmethod
    def _active(state, family_id):
        batch = state.get("batch_review", {})
        return batch.get("reviews", {}).get(batch.get("active_family_reviews", {}).get(family_id))

    def coverage(self, project, affected_type_ids, snapshot=None, *, context=None):
        _require(isinstance(affected_type_ids, list) and all(isinstance(key, str) for key in affected_type_ids)
                 and len(affected_type_ids) == len(set(affected_type_ids)), "DIRECTED_AFFECTED_TYPES", "Use distinct existing affected type IDs.")
        state = project.service.store.snapshot(project.service.run_id) if snapshot is None else snapshot
        types = {row["local_type_id"]: row for row in state["memberships"]}
        _require(all(key in types for key in affected_type_ids), "DIRECTED_AFFECTED_TYPES", "Affected types must exist in this project.")
        families = sorted({types[key]["parent_type_id"] or key for key in affected_type_ids})
        rows = []
        for family in families:
            review = self._active(state, family)
            directed = bool(review and review.get("directed_issue_id"))
            matching = bool(directed and set(affected_type_ids) <= set(review["directed_context"]["affected_type_ids"]))
            if matching and context is not None:
                matching = all(review["directed_context"].get(key) == value for key, value in context.items()
                               if key not in {"affected_type_ids", "parent_issue_id"} or key == "parent_issue_id" and value is not None)
            current = bool(matching and not review.get("archived") and not review_stale_reason(state, review))
            closed_current = bool(current and directed_return_current(state, review))
            if review and review.get("closed") and not closed_current:
                current = False
            rows.append({"family_id": family, "current_member_ids": sorted(types[family]["member_ids"]),
                "review_id": review["review_id"] if review else None, "prepared_for_this_question": matching,
                "status": review.get("status") if review else "not_prepared", "closed": bool(review and review.get("closed")),
                "closed_current": closed_current, "historical_only": bool(review and not current),
                "frozen_target_ids": list(review.get("item_ids", [])) if directed else [],
                "verification_level": review.get("directed_context", {}).get("verification_level") if directed else None})
        return {"affected_type_ids": list(affected_type_ids), "required_family_ids": families, "families": rows,
                "all_required_families_closed": bool(rows) and all(row["closed_current"] for row in rows),
                "claim": "Each family requires its own complete bound round; affected-type IDs alone do not cover other families."}

    def preview_directed(self, project, family_id, context, *, generation):
        context = self._context(context)
        with project.service._lock:
            state = project.service.store.snapshot(project.service.run_id)
            types = {row["local_type_id"]: row for row in state["memberships"]}
            family = types.get(family_id)
            _require(family is not None and family["parent_type_id"] is None and family["member_ids"], "BATCH_FAMILY_EMPTY", "Select a nonempty current family.")
            review = self._active(state, family_id)
            continuing = bool(review and review.get("directed_issue_id") and not review.get("closed")
                and not review.get("archived") and not review_stale_reason(state, review) and review["status"] in ACTIVE | {"ready"})
            targets = list(review["item_ids"]) if continuing else sorted(family["member_ids"])
            stub = {"family_id": family_id, "item_ids": targets, "family_member_fingerprint": family_member_fingerprint(state, family_id)}
            derived = prepare_directed_context(state, stub, context)
            projected_review = project.service.batch.snapshot(state)["reviews"].get(review["review_id"], {}) if review else {}
            scope = {"family_id": family_id, "target_ids": targets, "current_member_ids": sorted(family["member_ids"]),
                "member_fingerprint": family_member_fingerprint(state, family_id), "definition_fingerprint": family_definition_fingerprint(state, family_id),
                "route": "direct" if len(targets) <= 30 else "cluster", "continuing": continuing,
                "active_review_id": review["review_id"] if review else None, "requires_reopen_reason": bool(review and (review.get("closed") or review_stale_reason(state, review))),
                "direct_exception": copy.deepcopy(projected_review.get("direct_exception", {"allowed": False})),
                "derived_context": derived, "delivery_scope": copy.deepcopy(state.get("catalogue_scope")),
                "coverage": self.coverage(project, context["affected_type_ids"], state, context=context)}
            value = {"context": context, "scope": scope, "binding": self._binding(project, state, family_id, generation)}
        with self._lock:
            self._directed[family_id] = value
        return copy.deepcopy(value)

    def continue_directed(self, project, family_id, *, generation):
        with project.service._lock:
            selected = self._directed.get(family_id)
            _require(selected is not None and selected["binding"]["generation"] == generation
                     and all(selected["binding"][key] == value for key, value in _identity(project).items()),
                     "CATALOGUE_CONTEXT_CHANGED", "Inspect this current project's directed scope first.")
            _require(not getattr(project.service, "is_draining", False), "PROJECT_DRAINING", "Wait or return to the workspace before continuing a round.")
            state = project.service.store.snapshot(project.service.run_id)
            review = self._active(state, family_id)
            _require(review is not None and review.get("directed_issue_id") and not review.get("closed")
                     and not review.get("archived") and not review_stale_reason(state, review)
                     and review["status"] in ACTIVE | {"ready"}, "DIRECTED_REVIEW_SCOPE", "Only the current valid unclosed directed round can continue.")
            return {"review_id": review["review_id"], "continued": True, "waiting": review["status"] in ACTIVE,
                    "target_ids": list(review["item_ids"]), "return_view": "final"}

    def prepare_directed(self, project, family_id, context, *, generation, idempotency_key, purpose="", reopen_reason="",
                         gap_seconds=.4, normalization="per_item", use_direct_exception=False, exception_reason=""):
        _require(isinstance(idempotency_key, str) and 0 < len(idempotency_key) <= 160, "IDEMPOTENCY_REQUIRED", "Use an explicit directed request key.")
        context = self._context(context)
        intent = {"context": context, "purpose": purpose, "reopen_reason": reopen_reason, "gap_seconds": gap_seconds,
                  "normalization": normalization, "use_direct_exception": use_direct_exception, "exception_reason": exception_reason}
        _require(all(isinstance(value, str) for value in (purpose, reopen_reason, exception_reason)) and type(use_direct_exception) is bool,
                 "DIRECTED_CONTEXT", "Review purpose, reopen/exception reasons and exception selection must be explicit.")
        key = ("directed", idempotency_key)
        with project.service._lock:
            with self._lock:
                previous = self._requests.get(key)
                if previous:
                    _require(previous["intent"] == intent and previous["identity"] == _identity(project), "IDEMPOTENCY_CONFLICT", "The original directed request changed.")
                    result = project.execute("batch_prepare_family", copy.deepcopy(previous["payload"]), expected_revision=previous["revision"], idempotency_key=idempotency_key)
                    return {**result, "review_id": result["record"]["review"]["review_id"], "continued": False, "waiting": True}
                selected = self._directed.get(family_id)
                _require(selected is not None and selected["context"] == context, "CATALOGUE_DIRECTED_SCOPE_REQUIRED", "Inspect this exact question and complete family scope first.")
                state = project.service.store.snapshot(project.service.run_id)
                _require(selected["binding"]["generation"] == generation and all(selected["binding"][key] == value for key, value in _identity(project).items()),
                         "CATALOGUE_CONTEXT_CHANGED", "The project or window generation changed.")
                review = self._active(state, family_id)
                if review and review.get("directed_issue_id") and not review.get("closed") and not review_stale_reason(state, review) and review["status"] in ACTIVE | {"ready"}:
                    _require(all(review["directed_context"].get(field) == value for field, value in context.items()
                                 if field != "parent_issue_id" or value is not None),
                             "DIRECTED_REVIEW_IN_PROGRESS", "Continue the existing current question or finish it before requesting a different one.")
                    return self.continue_directed(project, family_id, generation=generation)
                _require(self._binding(project, state, family_id, generation) == selected["binding"], "CATALOGUE_CONTEXT_CHANGED", "Members, definitions or state changed; inspect the complete scope again.")
                scope = selected["scope"]
                _require(not scope["requires_reopen_reason"] or isinstance(reopen_reason, str) and bool(reopen_reason.strip()),
                         "FAMILY_REOPEN_REASON", "Give the actual reason for replacing a closed or stale round.")
                _require(not use_direct_exception or scope["direct_exception"].get("allowed") is True and isinstance(exception_reason, str) and bool(exception_reason.strip()),
                         "FAMILY_EXCEPTION_NOT_ALLOWED", "A direct exception needs current bound clustering evidence and a separate reason.")
                payload = {"family_id": family_id, "expected_member_fingerprint": scope["member_fingerprint"],
                    "expected_definition_fingerprint": scope["definition_fingerprint"], "reason": purpose.strip() or context["reason"],
                    "route": "direct_exception" if use_direct_exception else scope["route"], "gap_seconds": gap_seconds,
                    "normalization": normalization, "review_origin": "directed_recheck", "directed_context": context}
                if reopen_reason:
                    payload["reopen_reason"] = reopen_reason
                if use_direct_exception:
                    payload.update(exception_reason=exception_reason, exception_source_review_id=scope["direct_exception"]["source_review_id"])
                self._requests[key] = {"intent": copy.deepcopy(intent), "payload": copy.deepcopy(payload), "revision": state["revision"], "identity": _identity(project)}
                result = project.execute("batch_prepare_family", payload, expected_revision=state["revision"], idempotency_key=idempotency_key)
                return {**result, "review_id": result["record"]["review"]["review_id"], "continued": False, "waiting": True, "scope": copy.deepcopy(scope)}


from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QPlainTextEdit, QPushButton, QScrollArea,
    QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)


def _scroll_page(tabs, title):
    scroll = QScrollArea(); scroll.setWidgetResizable(True)
    content = QWidget(); layout = QVBoxLayout(content); scroll.setWidget(content); tabs.addTab(scroll, title)
    return layout


def _list():
    widget = QListWidget(); widget.setMaximumHeight(150); return widget


def _checked(widget):
    return [widget.item(index).data(Qt.ItemDataRole.UserRole) for index in range(widget.count())
            if widget.item(index).checkState() == Qt.CheckState.Checked]


def _choices(widget, ids, checked=(), *, allowed=None):
    widget.clear()
    selected = set(checked)
    for ident in dict.fromkeys([*ids, *checked]):
        valid = allowed is None or ident in allowed
        item = QListWidgetItem(ident + (" · outside current allowed scope" if not valid else ""))
        item.setData(Qt.ItemDataRole.UserRole, ident)
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(Qt.CheckState.Checked if ident in selected else Qt.CheckState.Unchecked)
        if not valid and ident not in selected:
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
        widget.addItem(item)


class CatalogueReviewDialog(QDialog):
    auditionRequested = Signal(str, bool)
    fineRequested = Signal(str)
    stopRequested = Signal()
    cardSaved = Signal(object)
    reviewPrepared = Signal(str)

    def __init__(self, owner, on_review_selected=None):
        super().__init__(owner)
        self.owner, self.project = owner, owner.project
        self.generation = owner._close_coordinator.generation
        self.controller = CatalogueReviewController()
        self.on_review_selected = on_review_selected
        self.type_id = None; self._dirty = False; self._directed_dirty = False; self._loading = False
        self._directed_family_id = None
        self._waiting = False; self._pending_review_id = None; self._directed_draft_binding = None
        self.setWindowTitle("Type definitions and directed review")
        screen = self.screen().availableGeometry()
        self.resize(min(1100, int(screen.width() * .94)), min(850, int(screen.height() * .9)))
        outer = QVBoxLayout(self)
        self.tabs = QTabWidget(); outer.addWidget(self.tabs, 1)
        self._build_card(); self._build_directed()
        self.message = QLabel("Select a human family or leaf. Examples must be explicitly selected; no labels or completion are inferred.")
        self.message.setWordWrap(True); outer.addWidget(self.message)
        footer = QHBoxLayout()
        self.save_draft_button = QPushButton("Save draft")
        self.save_draft_button.setToolTip("Save unsubmitted card and directed inputs; no authoritative decision is written.")
        self.save_card_button = QPushButton("Save card")
        self.save_card_button.setToolTip("Save this explicit type card and its explicitly selected definition status.")
        self.prepare_button = QPushButton("Prepare / continue")
        self.prepare_button.setToolTip("Prepare or continue only the inspected complete directed family scope.")
        stop, close = QPushButton("Stop playback"), QPushButton("Close")
        for button in (self.save_draft_button, self.save_card_button, self.prepare_button, stop, close): footer.addWidget(button)
        outer.addLayout(footer)
        self.save_draft_button.clicked.connect(lambda: self._guard(self.save_draft))
        self.save_card_button.clicked.connect(lambda: self._guard(self._save_card))
        self.prepare_button.clicked.connect(lambda: self._guard(self._prepare))
        stop.clicked.connect(self.stop); close.clicked.connect(self.reject)
        self.timer = QTimer(self); self.timer.setInterval(350); self.timer.timeout.connect(lambda: self._guard(self.refresh))
        self.finished.connect(lambda _result: self.timer.stop())
        self.owner.operationFailed.connect(self._operation_failed)

    def _build_card(self):
        layout = _scroll_page(self.tabs, "Type cards and human examples")
        self.types = QTableWidget(0, 5); self.types.setMaximumHeight(170)
        self.types.setHorizontalHeaderLabels(["Type", "Parent family", "Members", "Definition state", "Pending relation review"])
        self.types.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.types.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.types.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.types.itemSelectionChanged.connect(self._select_row); layout.addWidget(self.types)
        self.type_details = QLabel("No type selected."); self.type_details.setWordWrap(True); layout.addWidget(self.type_details)
        form = QFormLayout(); self.texts = {}
        for key in ("title", "diagnostic_body", "main_variant_difference", "uncertainties", "reason"):
            field = QPlainTextEdit(); field.setMaximumHeight(85); field.textChanged.connect(self._changed)
            self.texts[key] = field; form.addRow(key, field)
        self.status = QComboBox(); self.status.addItem("Draft — not reviewed", "draft"); self.status.addItem("Reviewed explicitly by operator", "reviewed")
        self.status.currentIndexChanged.connect(self._changed); form.addRow("Definition status", self.status); layout.addLayout(form)
        self.members = _list(); layout.addWidget(QLabel("All current members (stable IDs; listening never confirms a card automatically)")); layout.addWidget(self.members)
        self.members.itemDoubleClicked.connect(lambda item: self.auditionRequested.emit(item.data(Qt.ItemDataRole.UserRole), False))
        listen = QHBoxLayout()
        for text, action in (("Inspect selected segment", lambda ident: self.auditionRequested.emit(ident, False)),
                             ("Inspect verified parent", lambda ident: self.auditionRequested.emit(ident, True)),
                             ("Open selected Fine review", self.fineRequested.emit)):
            button = QPushButton(text); button.clicked.connect(lambda _checked=False, action=action: self._inspect_member(action)); listen.addWidget(button)
        layout.addLayout(listen)
        self.examples = {}
        for key in EXAMPLE_FIELDS:
            widget = _list(); widget.itemChanged.connect(self._changed)
            if key != "competing_type_ids":
                widget.itemDoubleClicked.connect(lambda item: self.auditionRequested.emit(item.data(Qt.ItemDataRole.UserRole), False))
            self.examples[key] = widget; layout.addWidget(QLabel(key + " — operator-selected stable IDs")); layout.addWidget(widget)
        self.review_confirmation = QCheckBox("I explicitly reviewed the current definition and the selected examples / relationships.")
        self.review_confirmation.toggled.connect(lambda _checked: self._set_enabled()); layout.addWidget(self.review_confirmation)
        actions = QHBoxLayout()
        self.refresh_context_button = QPushButton("Refresh current context; keep typed draft")
        self.restore_card_button = QPushButton("Restore unsubmitted card inputs")
        actions.addWidget(self.refresh_context_button); actions.addWidget(self.restore_card_button); layout.addLayout(actions)
        self.refresh_context_button.clicked.connect(lambda: self._guard(self._refresh_context))
        self.restore_card_button.clicked.connect(lambda: self._guard(self._restore_card))

    def _build_directed(self):
        layout = _scroll_page(self.tabs, "One complete family per directed round")
        form = QFormLayout()
        self.directed_family = QComboBox(); self.directed_family.setMinimumWidth(0)
        self.directed_family.addItem("Choose a family", None); self.directed_family.currentIndexChanged.connect(self._directed_family_changed)
        self.directed_texts = {}
        for key in ("question", "reason", "purpose", "reopen_reason", "observation_ids", "parent_issue_id", "exception_reason"):
            field = QLineEdit(); field.textChanged.connect(self._directed_changed); self.directed_texts[key] = field
        self.basis = QComboBox(); self.basis.addItem("Choose actual reopening basis", None)
        for basis in sorted(REOPEN_BASES): self.basis.addItem(basis, basis)
        self.basis.currentIndexChanged.connect(self._directed_changed)
        form.addRow("Complete family", self.directed_family); form.addRow("Actual basis", self.basis)
        for key, field in self.directed_texts.items(): form.addRow(key, field)
        layout.addLayout(form)
        self.affected = _list(); self.affected.itemChanged.connect(self._directed_changed)
        layout.addWidget(QLabel("Explicitly affected types — include this family; each other family still needs a separate complete round")); layout.addWidget(self.affected)
        self.direct_exception = QCheckBox("Use only the current bound numerical-failure exception")
        self.direct_exception.toggled.connect(self._directed_changed); layout.addWidget(self.direct_exception)
        self.delivery = QLabel("Delivery level and unresolved policy come from the saved scope.")
        self.delivery.setWordWrap(True); layout.addWidget(self.delivery)
        buttons = QHBoxLayout(); self.inspect_scope_button = QPushButton("Inspect this exact question / complete scope")
        self.restore_directed_button = QPushButton("Restore unsubmitted directed inputs")
        buttons.addWidget(self.inspect_scope_button); buttons.addWidget(self.restore_directed_button); layout.addLayout(buttons)
        self.inspect_scope_button.clicked.connect(lambda: self._guard(self._inspect_directed))
        self.restore_directed_button.clicked.connect(lambda: self._guard(self._restore_directed))
        self.directed_scope = QPlainTextEdit(); self.directed_scope.setReadOnly(True); self.directed_scope.setMaximumHeight(210); layout.addWidget(self.directed_scope)
        self.coverage_table = QTableWidget(0, 5); self.coverage_table.setHorizontalHeaderLabels(["Required family", "Current members", "Prepared round", "Status", "Closed/current"])
        self.coverage_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers); self.coverage_table.setMaximumHeight(180); layout.addWidget(self.coverage_table)
        self.history = QTableWidget(0, 5); self.history.setHorizontalHeaderLabels(["Review", "Family", "Question", "State", "Current / historical"])
        self.history.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers); self.history.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.history.setMaximumHeight(170); layout.addWidget(self.history)
        self.continue_button = QPushButton("Continue selected current unclosed directed round")
        self.continue_button.clicked.connect(lambda: self._guard(self._continue)); layout.addWidget(self.continue_button)

    def _current(self):
        return self.owner.project is self.project and self.owner._close_coordinator.generation == self.generation and self.owner._close_coordinator.state == "open"

    def _guard(self, call):
        try: return call()
        except Exception as exc:
            self.message.setText(system_error(exc)); self.owner._error(exc, operation="type definitions and review"); self._set_enabled()

    def _operation_failed(self, message):
        if self._waiting: self.message.setText(message)

    def _changed(self, *_args):
        if not self._loading:
            self._dirty = True; self.review_confirmation.setChecked(False)
        if hasattr(self, "save_card_button"): self._set_enabled()

    def _directed_changed(self, *_args):
        if not self._loading:
            self._directed_dirty = True; self.controller._directed.clear(); self.directed_scope.clear()
        if hasattr(self, "prepare_button"): self._set_enabled()

    def _select_row(self):
        if self._loading or not self._current(): return
        row = self.types.currentRow()
        if row < 0: return
        previous = self.type_id
        try: self.select_type(self.types.item(row, 0).text())
        except Exception as exc:
            self._loading = True
            for index in range(self.types.rowCount()):
                if self.types.item(index, 0).text() == previous: self.types.selectRow(index); break
            self._loading = False; self._guard(lambda: (_ for _ in ()).throw(exc))

    def select_type(self, type_id):
        if self._dirty or self._directed_dirty: self.save_draft()
        state = self.project.snapshot()
        selected = self.controller.select(self.project, type_id, generation=self.generation, snapshot=state)
        self.owner.state = state; self.type_id = type_id
        self._fill_card(selected["selection"].get("card") or {})
        self._render_directory(selected)
        family = selected["selection"]["family_id"]
        self._loading = True; self.directed_family.setCurrentIndex(self.directed_family.findData(family)); self._loading = False
        self._directed_family_changed()
        self._set_enabled()
        return selected

    def _render_directory(self, view):
        self._loading = True
        self.types.setRowCount(len(view["types"]))
        for index, row in enumerate(view["types"]):
            for column, value in enumerate((row["local_type_id"], row["parent_type_id"] or "family", row["member_count"], row["definition_state"], len(row.get("definition_review_required_ids", [])))):
                self.types.setItem(index, column, QTableWidgetItem(str(value)))
            if row["local_type_id"] == self.type_id: self.types.selectRow(index)
        current_family = self.directed_family.currentData()
        self.directed_family.clear(); self.directed_family.addItem("Choose a family", None)
        for row in view["types"]:
            if row["parent_type_id"] is None: self.directed_family.addItem(row["local_type_id"], row["local_type_id"])
        self.directed_family.setCurrentIndex(max(0, self.directed_family.findData(current_family)))
        checked = _checked(self.affected)
        _choices(self.affected, [row["local_type_id"] for row in view["types"]], checked)
        self.history.setRowCount(len(view.get("directed_reviews", [])))
        for index, row in enumerate(view.get("directed_reviews", [])):
            for column, value in enumerate((row["review_id"], row["family_id"], row.get("question"), "closed" if row["closed"] else "open", "history only" if row["historical_only"] or row["scope_stale"] else "current")):
                self.history.setItem(index, column, QTableWidgetItem(str(value)))
        self._loading = False

    def _fill_card(self, payload):
        self._loading = True
        for key, field in self.texts.items(): field.setPlainText(payload.get(key, "") if key != "reason" else "")
        self.status.setCurrentIndex(max(0, self.status.findData(payload.get("definition_status", "draft"))))
        selected = self.controller.snapshot()["selection"]
        all_ids = list(self.project.dataset.item_ids); members = set(selected["member_ids"])
        self.members.clear()
        for ident in selected["member_ids"]:
            item = QListWidgetItem(ident); item.setData(Qt.ItemDataRole.UserRole, ident); self.members.addItem(item)
        types = [row["local_type_id"] for row in self.controller.snapshot()["types"] if row["local_type_id"] != self.type_id]
        for key, widget in self.examples.items():
            allowed = set(types) if key == "competing_type_ids" else set(all_ids) - members if key == "counterexample_ids" else members
            _choices(widget, types if key == "competing_type_ids" else all_ids, payload.get(key, []), allowed=allowed)
        self.review_confirmation.setChecked(False)
        self.type_details.setText(f"{self.type_id} · {selected['definition_state']} · Parent: {selected['parent_type_id']} · {len(members)} current members. "
                                 "Draft/local restoration is not a reviewed definition or a completed directed round.")
        self._loading = False; self._dirty = False

    def payload(self):
        return {"local_type_id": self.type_id, **{key: field.toPlainText() for key, field in self.texts.items()},
                "definition_status": self.status.currentData(), **{key: _checked(widget) for key, widget in self.examples.items()}}

    def _directed_payload(self):
        values = {key: field.text() for key, field in self.directed_texts.items()}
        context = {"question": values["question"], "reason": values["reason"], "basis": self.basis.currentData(),
                   "affected_type_ids": _checked(self.affected), "observation_ids": [value.strip() for value in values["observation_ids"].split(",") if value.strip()],
                   "parent_issue_id": values["parent_issue_id"].strip() or None}
        return {"context": context, "family_id": self.directed_family.currentData(), "purpose": values["purpose"],
                "reopen_reason": values["reopen_reason"], "use_direct_exception": self.direct_exception.isChecked(), "exception_reason": values["exception_reason"]}

    def prefill_directed_question(self, value):
        """Carry an explicit D3 question into an unsubmitted form, without preparing B."""
        _require(self._current() and not self.owner._busy, "CATALOGUE_BUSY", "Reopen type definitions after the current operation.")
        _require(isinstance(value, dict), "CATALOGUE_QUESTION", "Select one human question first.")
        state = self.project.snapshot()
        types = {row["local_type_id"]: row for row in state.get("memberships", [])}
        family = value.get("family_id")
        affected = value.get("affected_type_ids")
        question = value.get("question")
        _require(family in types and types[family]["parent_type_id"] is None
                 and isinstance(affected, list) and family in affected and set(affected) <= set(types)
                 and isinstance(question, str) and bool(question.strip()),
                 "CATALOGUE_QUESTION", "Select a current family, affected types and a specific question.")
        self.select_type(family)  # Saves any previous input before replacing the form.
        self._loading = True
        try:
            for field in self.directed_texts.values():
                field.clear()
            self.directed_texts["question"].setText(question.strip())
            self.directed_texts["reason"].setText(value.get("reason", ""))
            self.basis.setCurrentIndex(0)
            _choices(self.affected, list(types), affected)
            self.direct_exception.setChecked(False)
        finally:
            self._loading = False
        self._directed_changed()
        self.tabs.setCurrentIndex(1)
        self.message.setText("Assessment question copied as unsubmitted input. Choose the actual basis and purpose, inspect current scope, then explicitly prepare a review. Keeping labels and reporting limitations is also valid.")
        self._set_enabled()
        return self._directed_payload()

    def save_draft(self):
        if self._dirty and self.type_id:
            self.controller.save_draft(self.project, self.payload()); self._dirty = False
        if self._directed_dirty and self._directed_family_id:
            value = self._directed_payload(); value["family_id"] = self._directed_family_id
            self.controller.save_draft(self.project, value, kind="directed", type_id=self._directed_family_id,
                generation=self.generation, binding=self._directed_draft_binding); self._directed_dirty = False
        return True

    def _refresh_context(self):
        payload = self.payload(); self.save_draft()
        self.controller.select(self.project, self.type_id, generation=self.generation)
        self._fill_card(payload); self.texts["reason"].setPlainText(payload["reason"])
        self._dirty = True; self.message.setText("Current members and definition refreshed. Typed inputs remain unsubmitted; explicitly review again.")

    def _restore_card(self):
        value = self.controller.restore_draft(self.project, self.type_id, generation=self.generation)
        self._fill_card(value["payload"]); self.texts["reason"].setPlainText(value["payload"].get("reason", "")); self._dirty = True
        self.message.setText("Unsubmitted inputs restored; confirmation cleared." + (" Context changed: refresh and inspect before saving." if value["stale_for_current_context"] else ""))

    def _inspect_member(self, action):
        item = self.members.currentItem()
        _require(item is not None, "MEDIA_IDS", "Select the exact stable member ID to inspect.")
        action(item.data(Qt.ItemDataRole.UserRole))

    def _before_change(self):
        self.save_draft()
        if hasattr(self.owner, "before_catalogue_change"): self.owner.before_catalogue_change()

    def _save_card(self):
        _require(self._current() and not self.owner._busy, "CATALOGUE_BUSY", "Wait or return to the current open workspace before saving.")
        self._before_change(); payload = self.payload(); confirmation = self.review_confirmation.isChecked()
        self._waiting = True; self._set_enabled()
        def done(result):
            self.owner.state = self.project.snapshot(); self.controller.select(self.project, self.type_id, generation=self.generation, snapshot=self.owner.state)
            self._fill_card(self.controller.snapshot()["selection"]["card"]); self.cardSaved.emit(result)
            self.message.setText("Explicit type card saved. Related item review and final scoped completion remain separate.")
        self.owner._run("Save explicit human type card", lambda _progress, _cancelled: self.controller.save_card(self.project, payload,
            generation=self.generation, idempotency_key="desktop-type-card-" + uuid.uuid4().hex, reviewed_confirmed=confirmation), done)

    def _directed_family_changed(self, *_args):
        if self._loading or not self._current(): return
        if self._directed_dirty: self.save_draft()
        family = self.directed_family.currentData()
        self._directed_family_id = family
        self._directed_draft_binding = self.controller._binding(self.project, self.project.snapshot(), family, self.generation) if family else None
        self.controller._directed.clear(); self.directed_scope.clear(); self._set_enabled()

    def _inspect_directed(self):
        value = self._directed_payload()
        inspected = self.controller.preview_directed(self.project, value["family_id"], value["context"], generation=self.generation)
        self._directed_draft_binding = inspected["binding"]
        self.directed_scope.setPlainText(json.dumps(system_fields(inspected["scope"]), ensure_ascii=False, indent=2))
        self._render_coverage(inspected["scope"]["coverage"])
        scope = inspected["scope"]["delivery_scope"] or {}
        derived = inspected["scope"]["derived_context"]
        self.delivery.setText(f"Saved delivery target: {derived['delivery_target_level']} · Required verification: {derived['verification_level']} · "
                              f"Unresolved policy: {scope.get('unresolved_policy', 'not declared in the saved scope')} · Exact Fine-required IDs: {derived['fine_required_ids']}")
        self.direct_exception.setEnabled(inspected["scope"]["direct_exception"].get("allowed") is True)
        self._set_enabled()

    def _render_coverage(self, coverage):
        self.coverage_table.setRowCount(len(coverage["families"]))
        for index, row in enumerate(coverage["families"]):
            for column, value in enumerate((row["family_id"], len(row["current_member_ids"]), row["review_id"] or "not prepared", row["status"], row["closed_current"])):
                self.coverage_table.setItem(index, column, QTableWidgetItem(str(value)))

    def _restore_directed(self):
        family = self.directed_family.currentData()
        value = self.controller.restore_draft(self.project, family, kind="directed", generation=self.generation)["payload"]
        self._loading = True
        for key, field in self.directed_texts.items():
            field.setText(", ".join(value["context"].get("observation_ids", [])) if key == "observation_ids" else
                          value["context"].get(key, "") or "" if key in {"question", "reason", "parent_issue_id"} else value.get(key, ""))
        self.basis.setCurrentIndex(max(0, self.basis.findData(value["context"].get("basis"))))
        _choices(self.affected, [row["local_type_id"] for row in self.controller.view(self.project)["types"]], value["context"]["affected_type_ids"])
        self.direct_exception.setChecked(False); self._loading = False; self._directed_dirty = True
        self.controller._directed.clear(); self.directed_scope.clear(); self.message.setText("Directed inputs restored, not prepared. Inspect the exact current family and question again.")

    def _prepare(self):
        _require(self._current() and not self.owner._busy, "CATALOGUE_BUSY", "Wait before requesting a directed round.")
        self._before_change(); value = self._directed_payload()
        self._waiting = True; self._set_enabled()
        def done(result): self._pending_review_id = result["review_id"]; self.owner.state = self.project.snapshot()
        self.owner._run("Prepare one explicitly directed human family", lambda _progress, _cancelled: self.controller.prepare_directed(self.project,
            value.pop("family_id"), value.pop("context"), generation=self.generation, idempotency_key="desktop-directed-" + uuid.uuid4().hex, **value), done)

    def _continue(self):
        row = self.history.currentRow(); _require(row >= 0, "DIRECTED_REVIEW_SCOPE", "Select a current directed history row.")
        rid = self.history.item(row, 0).text(); state = self.project.service.store.snapshot(self.project.service.run_id)
        review = state.get("batch_review", {}).get("reviews", {}).get(rid)
        _require(review and review.get("directed_issue_id") and not review.get("closed") and self.controller._active(state, review["family_id"]) is review,
                 "DIRECTED_REVIEW_SCOPE", "Historical or closed rounds are read-only.")
        context = {key: copy.deepcopy(review["directed_context"].get(key)) for key in ("question", "basis", "reason", "affected_type_ids", "parent_issue_id", "observation_ids")}
        self.controller.preview_directed(self.project, review["family_id"], context, generation=self.generation)
        self._pending_review_id = self.controller.continue_directed(self.project, review["family_id"], generation=self.generation)["review_id"]
        self.refresh()

    def _set_enabled(self):
        if not hasattr(self, "save_card_button"): return
        available = self._current() and not self.owner._busy and not self._waiting
        self.save_draft_button.setEnabled(bool(self.type_id))
        self.save_card_button.setEnabled(available and self.type_id is not None and self.controller.invalid_reason is None
            and (self.status.currentData() != "reviewed" or self.review_confirmation.isChecked()))
        self.prepare_button.setEnabled(available and self.directed_family.currentData() in self.controller._directed)
        self.inspect_scope_button.setEnabled(available and self.directed_family.currentData() is not None)
        self.continue_button.setEnabled(available)
        self.types.setEnabled(available); self.refresh_context_button.setEnabled(available and self.type_id is not None)
        for widget in [*self.texts.values(), *self.examples.values(), self.status, self.review_confirmation,
                       self.directed_family, *self.directed_texts.values(), self.basis, self.affected,
                       self.restore_card_button, self.restore_directed_button, self.members]:
            widget.setEnabled(available)

    def refresh(self):
        if not self._current():
            self.invalidate("Project or window changed; unsubmitted inputs are retained."); return
        state = self.owner.state or self.project.snapshot()
        if not self.owner._busy: self._waiting = False
        view = self.controller.view(self.project, state)
        self._render_directory(view)
        self.controller.observe_context(state, project_id=self.project.metadata["project_id"], generation=self.generation)
        if self.controller.invalid_reason: self.message.setText(self.controller.invalid_reason)
        if self._pending_review_id and not self.owner._busy:
            review = state.get("batch_review", {}).get("reviews", {}).get(self._pending_review_id, {})
            if review.get("status") == "ready":
                rid, self._pending_review_id = self._pending_review_id, None
                _require(not review.get("scope_stale") and not review.get("read_only"), "DIRECTED_REVIEW_SCOPE", "Prepared directed round is stale.")
                self.reviewPrepared.emit(rid)
                if self.on_review_selected: self.on_review_selected(rid)
            elif review.get("status") in {"failed", "cancelled"}:
                self.message.setText(str(system_fields(review.get("error", review["status"]))))
        self._set_enabled()

    def invalidate(self, reason):
        self.controller.invalidate(reason); self.timer.stop(); self.review_confirmation.setChecked(False); self._set_enabled()

    def stop(self):
        if hasattr(self.owner, "_stop_review_audio"): self.owner._stop_review_audio()
        else: self.stopRequested.emit()

    def reject(self):
        try: self.save_draft()
        except Exception as exc: self._guard(lambda: (_ for _ in ()).throw(exc)); return
        self.review_confirmation.setChecked(False); self.stop(); super().reject()

    def closeEvent(self, event):
        try: self.save_draft()
        except Exception as exc: event.ignore(); self._guard(lambda: (_ for _ in ()).throw(exc)); return
        self.review_confirmation.setChecked(False); self.stop(); self.timer.stop(); super().closeEvent(event)

    def showEvent(self, event):
        super().showEvent(event); self.refresh(); self.timer.start()
