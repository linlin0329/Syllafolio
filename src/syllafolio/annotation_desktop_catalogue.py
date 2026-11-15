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
            self._write(drafts, original)

    def discard(self, key, expected):
        with self.lock:
            drafts, original = self._load()
            if expected is not None and drafts.get(key) == expected:
                del drafts[key]
                self._write(drafts, original)

    def _write(self, drafts, original):
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
                "card_id": (card or {}).get("card_id"),
                "title": (card or {}).get("title"),
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
            _require(family is not None and family["parent_type_id"] is None, "BATCH_FAMILY_EMPTY", "Choose a current human family.")
            review = self._active(state, family_id)
            continuing = bool(review and review.get("directed_issue_id") and not review.get("closed")
                and not review.get("archived") and not review_stale_reason(state, review) and review["status"] in ACTIVE | {"ready"})
            if continuing:
                _require(self._matches_question(review, context), "DIRECTED_REVIEW_IN_PROGRESS",
                         "This family already has an open directed question. Continue that round before starting a different question.")
            else:
                _require(family["member_ids"], "BATCH_FAMILY_EMPTY", "Choose a family with current members.")
            targets = list(review["item_ids"]) if continuing else sorted(family["member_ids"])
            stub = {"family_id": family_id, "item_ids": targets, "family_member_fingerprint": family_member_fingerprint(state, family_id)}
            derived = copy.deepcopy(review["directed_context"]) if continuing else prepare_directed_context(state, stub, context)
            projected_review = project.service.batch.snapshot(state)["reviews"].get(review["review_id"], {}) if review else {}
            scope = {"family_id": family_id, "target_ids": targets, "current_member_ids": sorted(family["member_ids"]),
                "member_fingerprint": family_member_fingerprint(state, family_id), "definition_fingerprint": family_definition_fingerprint(state, family_id),
                "route": review["route"] if continuing else "direct" if len(targets) <= 30 else "cluster", "continuing": continuing,
                "active_review_id": review["review_id"] if review else None, "requires_reopen_reason": bool(review and (review.get("closed") or review_stale_reason(state, review))),
                "routine_in_progress": bool(review and not review.get("directed_issue_id") and not review.get("closed")
                    and not review_stale_reason(state, review) and review["status"] in ACTIVE | {"ready"}),
                "card_revision": (state.get("type_cards", {}).get(family_id) or {}).get("revision"),
                "direct_exception": copy.deepcopy(projected_review.get("direct_exception", {"allowed": False})),
                "derived_context": derived, "delivery_scope": copy.deepcopy(state.get("catalogue_scope")),
                "coverage": self.coverage(project, context["affected_type_ids"], state, context=context)}
            value = {"context": context, "scope": scope, "binding": self._binding(project, state, family_id, generation)}
        with self._lock:
            self._directed[family_id] = value
        return copy.deepcopy(value)

    @staticmethod
    def _matches_question(review, context):
        original = review["directed_context"]
        for field in ("question", "basis", "reason"):
            if context.get(field) != original.get(field):
                return False
        for field in ("affected_type_ids", "observation_ids"):
            values = context.get(field, [])
            if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
                return False
            if sorted(values) != sorted(original.get(field, [])):
                return False
        parent = context.get("parent_issue_id")
        return parent is None or parent == original.get("parent_issue_id")

    def continue_directed(self, project, family_id, *, generation, expected_review_id=None):
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
            _require((expected_review_id is None or review["review_id"] == expected_review_id)
                     and self._matches_question(review, selected["context"]), "DIRECTED_REVIEW_SCOPE",
                     "The current question changed. Inspect the open round again.")
            return {"review_id": review["review_id"], "continued": True, "waiting": review["status"] in ACTIVE,
                    "target_ids": list(review["item_ids"]), "return_view": "final"}

    def prepare_directed(self, project, family_id, context, *, generation, idempotency_key, purpose="", reopen_reason="",
                         gap_seconds=.4, normalization="per_item", use_direct_exception=False, exception_reason="", page_layout=None):
        _require(isinstance(idempotency_key, str) and 0 < len(idempotency_key) <= 160, "IDEMPOTENCY_REQUIRED", "Use an explicit directed request key.")
        context = self._context(context)
        intent = {"context": context, "purpose": purpose, "reopen_reason": reopen_reason, "gap_seconds": gap_seconds,
                  "normalization": normalization, "use_direct_exception": use_direct_exception, "exception_reason": exception_reason,
                  "page_layout": page_layout}
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
                    _require(self._matches_question(review, context),
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
                if page_layout is not None:
                    payload["page_layout"] = page_layout
                if reopen_reason:
                    payload["reopen_reason"] = reopen_reason
                if use_direct_exception:
                    payload.update(exception_reason=exception_reason, exception_source_review_id=scope["direct_exception"]["source_review_id"])
                self._requests[key] = {"intent": copy.deepcopy(intent), "payload": copy.deepcopy(payload), "revision": state["revision"], "identity": _identity(project)}
                result = project.execute("batch_prepare_family", payload, expected_revision=state["revision"], idempotency_key=idempotency_key)
                return {**result, "review_id": result["record"]["review"]["review_id"], "continued": False, "waiting": True, "scope": copy.deepcopy(scope)}


from PySide6.QtCore import Qt, QTimer, QSize, Signal
from PySide6.QtGui import QIcon, QImage, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMenu, QPlainTextEdit,
    QPushButton, QScrollArea, QStackedWidget, QTableWidget, QTableWidgetItem,
    QTabWidget, QVBoxLayout, QWidget,
)
from .annotation_desktop_directed import DirectedWorkflow
from .annotation_manual_protocol import definition_content


def _scroll_page(tabs, title):
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    content = QWidget()
    layout = QVBoxLayout(content)
    scroll.setWidget(content)
    tabs.addTab(scroll, title)
    return layout


def _list():
    widget = QListWidget()
    widget.setMaximumHeight(150)
    return widget


def _checked(widget):
    return [widget.item(index).data(Qt.ItemDataRole.UserRole) for index in range(widget.count())
            if widget.item(index).checkState() == Qt.CheckState.Checked]


def _choices(widget, ids, checked=(), *, allowed=None):
    widget.clear()
    selected = set(checked)
    for ident in dict.fromkeys([*ids, *checked]):
        valid = allowed is None or ident in allowed
        item = QListWidgetItem(ident + (' · outside current allowed scope' if not valid else ''))
        item.setData(Qt.ItemDataRole.UserRole, ident)
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(Qt.CheckState.Checked if ident in selected else Qt.CheckState.Unchecked)
        if not valid and ident not in selected:
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
        widget.addItem(item)


def _name(row):
    return (row.get('card') or {}).get('title') or row.get('title') or row['local_type_id']


def _status(row):
    return {"missing": "Not defined", "stale": "Needs review", "draft": "Draft",
            "reviewed": "Reviewed"}.get(row["definition_state"], row["definition_state"])


def _label(text=''):
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return label


def _heading(text):
    label = _label(text)
    font = label.font()
    font.setBold(True)
    label.setFont(font)
    return label


class CatalogueReviewDialog(DirectedWorkflow, QDialog):
    auditionRequested = Signal(str, bool)
    fineRequested = Signal(str)
    stopRequested = Signal()
    cardSaved = Signal(object)
    reviewPrepared = Signal(str)

    def __init__(self, owner, on_review_selected=None):
        super().__init__(owner)
        self.owner, self.project = (owner, owner.project)
        self.generation = owner._close_coordinator.generation
        self.controller = CatalogueReviewController()
        self.on_review_selected = on_review_selected
        self.type_id = None
        self._dirty = self._directed_dirty = self._loading = self._waiting = False
        self._directed_family_id = self._pending_review_id = self._directed_draft_binding = None
        self._row = self._card_binding = self._prior_draft = self._revision = None
        self._directory_rows = []
        self._audition_id = None
        self._store = CatalogueDraftStore(self.project)
        self.setWindowTitle('Type definitions')
        screen = self.screen().availableGeometry()
        self.resize(min(900, screen.width() - 32), min(690, screen.height() - 60))
        outer = QVBoxLayout(self)
        self.tabs = QTabWidget()
        self.tabs.tabBar().hide()
        outer.addWidget(self.tabs, 1)
        self._build_card()
        self._build_directed()
        self.message = _label()
        outer.addWidget(self.message)
        footer = QHBoxLayout()
        self.directory_button = self._button('All definitions', 'allDefinitions', lambda: self.show_card())
        footer.addWidget(self.directory_button)
        self.more_button = QPushButton('More…')
        menu = QMenu(self.more_button)
        self.draft_action = menu.addAction("Save draft definition…", lambda: self._guard(lambda: self._ask_save("draft")))
        menu.addAction('Definition history…', lambda: self._guard(self._show_history))
        self.more_button.setMenu(menu)
        footer.addWidget(self.more_button)
        footer.addStretch()
        self.save_card_button = self._button("Confirm definition…", "confirmDefinition", lambda: self._ask_save("reviewed"))
        footer.addWidget(self.save_card_button)
        close = QPushButton('Close')
        close.clicked.connect(self.reject)
        footer.addWidget(close)
        outer.addLayout(footer)
        self.tabs.currentChanged.connect(lambda _i: self._set_enabled())
        self.timer = QTimer(self)
        self.timer.setInterval(350)
        self.timer.timeout.connect(lambda: self._guard(self.refresh))
        self.finished.connect(lambda _result: self.timer.stop())
        self.owner.operationFailed.connect(self._operation_failed)
        self.refresh()

    def _button(self, title, name, action):
        button = QPushButton(title)
        button.setObjectName(name)
        button.setAutoDefault(False)
        button.clicked.connect(lambda _checked=False: self._guard(action))
        return button

    def _build_card(self):
        self.card_pages = QStackedWidget()
        self.tabs.addTab(self.card_pages, 'Definitions')
        self.directory = QWidget()
        self.card_pages.addWidget(self.directory)
        layout = QVBoxLayout(self.directory)
        layout.addWidget(_heading("Type definitions"))
        bar = QHBoxLayout()
        self.filter = QLineEdit()
        self.filter.setPlaceholderText('Find a family or fine type…')
        self.filter.setAccessibleName('Find type')
        self.filter.textChanged.connect(self._filter_directory)
        bar.addWidget(self.filter, 1)
        self.attention_filter = QComboBox()
        self.attention_filter.addItems(['All types', 'Needs attention', 'Reviewed'])
        self.attention_filter.currentIndexChanged.connect(self._filter_directory)
        bar.addWidget(self.attention_filter)
        layout.addLayout(bar)
        self.types = QTableWidget(0, 4)
        self.types.setObjectName('typeDirectory')
        self.types.setHorizontalHeaderLabels(['Type', 'Level', 'Members', 'Definition'])
        self.types.verticalHeader().hide()
        self.types.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col in (1, 2, 3):
            self.types.horizontalHeader().setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        self.types.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.types.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.types.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.types.cellClicked.connect(self._open_row)
        self.types.cellActivated.connect(self._open_row)
        layout.addWidget(self.types, 1)
        self.empty_label = _label()
        layout.addWidget(self.empty_label)

        self.editor = QScrollArea()
        self.editor.setWidgetResizable(True)
        body = QWidget()
        edit = QVBoxLayout(body)
        self.editor.setWidget(body)
        self.card_pages.addWidget(self.editor)
        header = QHBoxLayout()
        self.card_title = _heading('')
        self.card_title.setObjectName('cardTitle')
        header.addWidget(self.card_title, 1)
        self.directed_button = self._button("Start directed review…","startDirectedReview",
                                            lambda: self.show_directed(self._row["family_id"]))
        header.addWidget(self.directed_button)
        edit.addLayout(header)
        self.type_details = _label()
        edit.addWidget(self.type_details)
        self.texts = {}
        form = QFormLayout()
        edit.addLayout(form)
        self.name_edit = QLineEdit()
        self.name_edit.setObjectName("card_title")
        self.name_edit.textChanged.connect(self._changed)
        form.addRow("Name", self.name_edit)
        for key, label, height in [("diagnostic_body", "Diagnostic criteria", 65),
                                   ("main_variant_difference", "Fine-form difference", 48)]:
            field = QPlainTextEdit()
            field.setFixedHeight(height)
            field.setObjectName('card_' + key)
            field.textChanged.connect(self._changed)
            form.addRow(label, field)
            self.texts[key] = field
        self.main_variant_label = form.labelForField(self.texts['main_variant_difference'])
        self.texts['diagnostic_body'].setPlaceholderText('What can you hear or see that distinguishes this type?')
        self.texts['main_variant_difference'].setPlaceholderText('How does this form differ from another form in the family?')
        edit.addWidget(_heading("Representative syllables"))
        self.representatives = QListWidget()
        self.representatives.setObjectName('cardRepresentatives')
        self.representatives.setMaximumHeight(123)
        self.representatives.setIconSize(QSize(58, 32))
        self.representatives.itemDoubleClicked.connect(lambda item: self._audition(item.data(Qt.ItemDataRole.UserRole)))
        self.representatives.itemSelectionChanged.connect(self._set_enabled)
        edit.addWidget(self.representatives)
        repbar = QHBoxLayout()
        self.member_combo = QComboBox()
        self.member_combo.setAccessibleName('Current member')
        self.member_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.member_combo.setMinimumContentsLength(15)
        repbar.addWidget(self.member_combo, 1)
        self.add_member_button=self._button("Add member", "cardAddMember",
                                            lambda: self.add_representatives(self.type_id,[self.member_combo.currentData()]))
        repbar.addWidget(self.add_member_button)
        chosen_actions = QHBoxLayout()
        self.listen_rep_button = self._button('Listen to selected', 'cardListen', self._listen_rep)
        chosen_actions.addWidget(self.listen_rep_button)
        self.remove_rep_button = self._button('Remove selected', 'cardRemoveRepresentative', self._remove_rep)
        chosen_actions.addWidget(self.remove_rep_button)
        chosen_actions.addStretch()
        edit.addLayout(chosen_actions)
        self.members_tools = QPushButton('Inspect member…')
        tools = QMenu(self.members_tools)
        tools.addAction('Segment and spectrogram', lambda: self._inspect_member(lambda i: self._audition(i)))
        tools.addAction('Verified parent recording', lambda: self._inspect_member(lambda i: self._audition(i, True)))
        tools.addAction('Fine review…', lambda: self._inspect_member(self.fineRequested.emit))
        self.members_tools.setMenu(tools)
        repbar.addWidget(self.members_tools)
        edit.addLayout(repbar)
        self.extra_button = QPushButton('Additional evidence')
        self.extra_button.setCheckable(True)
        self.extra_button.setObjectName('additionalEvidence')
        optbar = QHBoxLayout()
        optbar.addWidget(self.extra_button)
        optbar.addStretch()
        edit.addLayout(optbar)
        self.extra = QWidget()
        extra = QVBoxLayout(self.extra)
        extra.setContentsMargins(0, 0, 0, 0)
        edit.addWidget(self.extra)
        self.extra_button.toggled.connect(self.extra.setVisible)
        self.extra_button.toggled.connect(self._load_evidence)
        self.extra.hide()
        self.examples = {}
        for key,title in (("atypical_ids","Atypical members"),("counterexample_ids","Counterexamples outside this type"),
                          ("competing_type_ids","Confusable types")):
            widget = _list()
            widget.itemChanged.connect(self._changed)
            self.examples[key] = widget
            if key != 'competing_type_ids':
                widget.itemDoubleClicked.connect(lambda item: self._audition(item.data(Qt.ItemDataRole.UserRole)))
            extra.addWidget(_heading(title))
            extra.addWidget(widget)
        field = QPlainTextEdit()
        field.setFixedHeight(52)
        field.textChanged.connect(self._changed)
        self.texts['uncertainties'] = field
        extra.addWidget(_label('Uncertainties'))
        extra.addWidget(field)
        self.refresh_context_button = self._button('Review changed context…', 'refreshDefinition', self._refresh_context)
        self.restore_card_button = self._button('Use earlier draft…', 'restoreDefinitionDraft', self._restore_card)
        notices = QHBoxLayout()
        notices.addWidget(self.refresh_context_button)
        notices.addWidget(self.restore_card_button)
        notices.addStretch()
        edit.addLayout(notices)
        edit.addStretch()

    def _current(self):
        return self.owner.project is self.project and self.owner._close_coordinator.accepts_callback(self.generation)

    def _guard(self, call):
        try:
            return call()
        except Exception as exc:
            self.message.setText(system_error(exc))
            self.owner._error(exc, operation='type definitions and review')
            self._set_enabled()

    def _operation_failed(self, message):
        if self._waiting:
            self._waiting = False
            self.message.setText(message)
            self._set_enabled()

    def _changed(self, *_args):
        if self._loading:
            return
        self._dirty = True
        self._update_card_summary()
        self._set_enabled()

    def _open_row(self, row, _column):
        item = self.types.item(row, 0)
        if item is not None:
            self._guard(lambda: self.show_card(item.data(Qt.ItemDataRole.UserRole)))

    def _filter_directory(self, *_args):
        q = self.filter.text().strip().casefold()
        mode = self.attention_filter.currentIndex()
        rows_by_id = {r['local_type_id']: r for r in self._directory_rows}
        def attention(r):
            return r['definition_state'] != 'reviewed' or bool(r.get('definition_review_required_ids'))
        rows = sorted(self._directory_rows, key=lambda r: (not attention(r), _name(r).casefold(), r['local_type_id']))
        if mode:
            rows = [row for row in rows if attention(row) == (mode == 1)]
        if q:
            rows = [row for row in rows if q in (_name(row) + " " + row["local_type_id"]).casefold()]
        self.types.setRowCount(len(rows))
        for i, r in enumerate(rows):
            key = r['local_type_id']
            parent = r['parent_type_id']
            level = 'Family' if parent is None else 'Fine form · ' + _name(rows_by_id[parent])
            state = 'Review pending' if r.get('definition_review_required_ids') else _status(r)
            for col, val in enumerate((_name(r), level, str(r['member_count']), state)):
                item = QTableWidgetItem(val)
                item.setData(Qt.ItemDataRole.UserRole, key)
                item.setToolTip(key)
                self.types.setItem(i, col, item)
        self.empty_label.setText("No matching types." if self._directory_rows else
                                 "Create a human family in the review workspace to define it here.")
        self.empty_label.setVisible(not rows)

    def show_card(self, type_id=None):
        if type_id is None:
            self.save_draft()
            self.card_pages.setCurrentWidget(self.directory)
            self.message.clear()
        else:
            self.select_type(type_id)
            self.card_pages.setCurrentWidget(self.editor)
        self.tabs.setCurrentIndex(0)
        self._set_enabled()

    def select_type(self, type_id):
        _require(self._current() and (not self._waiting), 'CATALOGUE_BUSY', 'Wait for the current card operation.')
        self.save_draft()
        state = self.project.snapshot()
        selected = self.controller.select(self.project, type_id, generation=self.generation, snapshot=state)
        self.owner.state = state
        self.type_id = type_id
        self._row = selected['selection']
        self._card_binding = selected['binding']
        self._directory_rows = selected['types']
        self._prior_draft = None
        payload = self._row.get('card') or {}
        record = self._store.get('card:' + type_id)
        notice = ''
        if record is not None:
            fields = ('membership_fingerprint', 'definition_fingerprint', 'card_id', 'title')
            if all((record['binding'][k] == self._card_binding[k] for k in fields if k in record['binding'])):
                payload = record['payload']
                notice = 'Unsubmitted edits restored.'
            else:
                self._prior_draft = record
                notice = 'Members or the saved definition changed. Choose which inputs to use before editing.'
        self._fill_card(payload)
        self.message.setText(notice)
        self._set_enabled()
        return selected

    def _fill_card(self, payload):
        self._loading = True
        try:
            self.name_edit.setText(payload.get("title", _name(self._row)))
            for key, field in self.texts.items():
                field.setPlainText(payload.get(key, ""))
            self._reason = payload.get('reason', '') if 'local_type_id' in payload and 'card_id' not in payload else ''
            self._draft_status = payload.get('definition_status', 'draft')
            self._members = set(self._row['member_ids'])
            self.member_combo.clear()
            for ident in self._row['member_ids']:
                self.member_combo.addItem(ident, ident)
            self.representatives.clear()
            for ident in payload.get('representative_ids', []):
                self._append_rep(ident)
            self._optional_values = {key: list(payload.get(key, [])) for key in self.examples}
            self._optional_loaded = False
            for widget in self.examples.values():
                widget.clear()
            leaf = self._row['parent_type_id'] is not None
            show_difference = leaf or bool(payload.get("main_variant_difference"))
            self.main_variant_label.setVisible(show_difference)
            self.texts["main_variant_difference"].setVisible(show_difference)
            self.card_title.setText(_name(self._row))
            self.type_details.setToolTip(self.type_id)
            self.extra_button.setChecked(False)
            self.editor.verticalScrollBar().setValue(0)
            self._dirty = False
        finally:
            self._loading = False
        self._update_card_summary()

    def _rep_ids(self):
        return [self.representatives.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.representatives.count())]

    def _append_rep(self, ident):
        item = QListWidgetItem(ident if ident in self._members else ident + ' · outside current members')
        item.setData(Qt.ItemDataRole.UserRole, ident)
        self.representatives.addItem(item)
        # Use the frozen overview patch. The exact time/frequency view is available via Listen.
        dataset = self.project.dataset
        if ident in self._members and dataset.logmel.shape[1:] == (2048,):
            import numpy as np
            patch = dataset.logmel[dataset._items[ident]['feature_row_index']].reshape(64, 32)
            lo, hi = (float(patch.min()), float(patch.max()))
            pixels = np.ascontiguousarray(255 - 255 * (patch[::-1] - lo) / max(hi - lo, 1e-12), dtype=np.uint8)
            image = QImage(pixels.data, 32, 64, 32, QImage.Format.Format_Grayscale8).copy()
            thumb = image.scaled(58, 32, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
            item.setIcon(QIcon(QPixmap.fromImage(thumb)))
            item.setToolTip("Log-mel thumbnail · double-click for the full spectrogram and audio.")

    def _remove_rep(self):
        i = self.representatives.currentRow()
        if i >= 0:
            self.representatives.takeItem(i)
            self._changed()

    def _audition(self, ident, context=False):
        if ident:
            self._audition_id = ident
            self.auditionRequested.emit(ident, context)

    def _listen_rep(self):
        item = self.representatives.currentItem()
        if item:
            self._audition(item.data(Qt.ItemDataRole.UserRole))

    def _inspect_member(self, action):
        ident = self.member_combo.currentData()
        if ident:
            self._guard(lambda: action(ident))

    def _load_evidence(self, expanding):
        if not expanding or self._optional_loaded:
            return
        self._loading = True
        try:
            types = {r['local_type_id']: _name(r) for r in self._directory_rows if r['local_type_id'] != self.type_id}
            options = {"competing_type_ids": sorted(types, key=lambda k: types[k].casefold()),
                       "atypical_ids": self._row["member_ids"],
                       "counterexample_ids": [i for i in self.project.dataset.item_ids if i not in self._members]}
            for key, widget in self.examples.items():
                _choices(widget, options[key], self._optional_values[key], allowed=set(options[key]))
                if key == "competing_type_ids":
                    for j in range(widget.count()):
                        item = widget.item(j)
                        ident = item.data(Qt.ItemDataRole.UserRole)
                        if ident in types:
                            item.setText(types[ident])
                            item.setToolTip(ident)
            self._optional_loaded = True
        finally:
            self._loading = False

    def _update_card_summary(self):
        value = self.payload()
        card = self._row.get("card") or {}
        changed = (any(value[k] != card.get(k, "") for k in self.texts)
                   or any(sorted(value[k]) != sorted(card.get(k, [])) for k in EXAMPLE_FIELDS)
                   or value["title"] != card.get("title", _name(self._row)) or bool(self._reason))
        status = _status(self._row)
        if changed:
            status = f"Unsubmitted edits · Saved definition: {status}"
        level = "Fine form" if self._row["parent_type_id"] else "Family"
        self.type_details.setText(f"{status} · {len(self._members)} members · {level}")
        count = sum(len(value[k]) for k in self.examples) + bool(value["uncertainties"].strip())
        self.extra_button.setText(f'Additional evidence ({count})' if count else 'Additional evidence')

    def add_representatives(self, type_id, ids):
        ids = list(dict.fromkeys(ids))
        _require(bool(ids),"TYPE_EXAMPLE_SCOPE","Select at least one saved member to add.")
        state = self.project.snapshot()
        row = next((r for r in state['memberships'] if r['local_type_id'] == type_id), None)
        _require(row is not None and set(ids)<=set(row["member_ids"]),"TYPE_EXAMPLE_SCOPE",
                 "Each selected syllable must be a saved member of this type.")
        self.show_card(type_id)
        _require(self._prior_draft is None, 'CATALOGUE_DRAFT_CHANGED', 'Resolve the earlier draft before adding representatives.')
        known = set(self._rep_ids())
        new = [i for i in ids if i not in known]
        for i in new:
            self._append_rep(i)
        for j in range(self.representatives.count()):
            if self.representatives.item(j).data(Qt.ItemDataRole.UserRole) == ids[0]:
                self.representatives.setCurrentRow(j)
                break
        if new:
            self._changed()
            self.save_draft()
        self.message.setText(f"Added to card draft: {len(new)}." if new else "Already included in this card.")
        return self._rep_ids()

    def payload(self):
        evidence = ({k: _checked(w) for k, w in self.examples.items()} if self._optional_loaded
                    else copy.deepcopy(self._optional_values))
        return {"local_type_id": self.type_id, "title": self.name_edit.text(),
                **{k: w.toPlainText() for k, w in self.texts.items()}, **evidence,
                "representative_ids": self._rep_ids(), "definition_status": self._draft_status, "reason": self._reason}

    def save_draft(self):
        if self._dirty and self.type_id:
            self.controller.save_draft(self.project, self.payload(), binding=self._card_binding)
            self._dirty = False
        self._save_directed_draft()
        return True

    def _refresh_context(self):
        if self._prior_draft:
            self._store.discard('card:' + self.type_id, self._prior_draft)
            self._prior_draft = None
        self.select_type(self.type_id)

    def _restore_card(self):
        record = self._prior_draft
        if record:
            self._prior_draft = None
            self._fill_card(record['payload'])
            self._dirty = True
            self.save_draft()
            self._set_enabled()
            self.message.setText('Earlier inputs restored. Review the current members before confirming.')

    def _before_change(self):
        self.save_draft()
        if hasattr(self.owner, 'before_catalogue_change'):
            self.owner.before_catalogue_change()

    def _show_history(self):
        state = self.project.service.store.snapshot(self.project.service.run_id)
        rows=sorted((r for r in state.get("type_card_history",{}).values() if r["local_type_id"]==self.type_id),
                    key=lambda r:r["revision"],reverse=True)
        box = QDialog(self)
        box.setWindowTitle('Definition history')
        layout = QVBoxLayout(box)
        text = QPlainTextEdit()
        text.setReadOnly(True)
        text.setPlainText("\n\n".join(f"Revision {r['revision']} · {r['title']} · {r['definition_status']}\n{r['diagnostic_body']}\n{r['reason']}" for r in rows) or "No definition has been saved.")
        layout.addWidget(text)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(box.reject)
        layout.addWidget(buttons)
        box.resize(520, 330)
        box.exec()

    def _ask_save(self, status):
        _require(self._current() and (not self.owner._busy) and (not self.controller.invalid_reason), 'CATALOGUE_BUSY', 'Refresh the current definition before saving.')
        payload = dict(self.payload(), definition_status=status)
        box = QDialog(self)
        box.setWindowTitle('Confirm definition' if status == 'reviewed' else 'Save draft definition')
        layout = QVBoxLayout(box)
        layout.addWidget(_heading(_name(self._row)))
        representatives = payload['representative_ids']
        layout.addWidget(_label(f'{len(self._members)} members · {len(representatives)} representative syllables'))
        raw = self.project.service.store.snapshot(self.project.service.run_id)
        prior = raw.get('type_cards', {}).get(self.type_id)
        while prior and prior['definition_status'] != 'reviewed':
            prior = raw.get('type_card_history', {}).get(prior.get('previous_card_id'))
        if prior and definition_content(prior) != definition_content(payload):
            related = {self.type_id, *prior.get('competing_type_ids', []), *payload['competing_type_ids']}
            if self._row['parent_type_id']:
                related.add(self._row['parent_type_id'])
            affected = set().union(*(set(m['member_ids']) for m in raw['memberships'] if m['local_type_id'] in related))
            layout.addWidget(_label(f"The revised definition requires a new review of {len(affected)} affected syllables."))
        layout.addWidget(_label('Basis for this definition or revision'))
        reason = QPlainTextEdit(self._reason)
        reason.setObjectName('definitionRevisionBasis')
        reason.setMaximumHeight(78)
        layout.addWidget(reason)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        submit = buttons.addButton('Confirm reviewed definition' if status == 'reviewed' else 'Save draft definition', QDialogButtonBox.ButtonRole.AcceptRole)
        submit.setEnabled(bool(self._reason.strip()))
        reason.textChanged.connect(lambda:submit.setEnabled(bool(reason.toPlainText().strip())))
        buttons.accepted.connect(box.accept)
        buttons.rejected.connect(box.reject)
        layout.addWidget(buttons)
        box.resize(min(self.width(), 460), box.sizeHint().height())
        if box.exec() != QDialog.DialogCode.Accepted:
            return
        self._reason = reason.toPlainText().strip()
        self._draft_status = status
        self._dirty = True
        self._before_change()
        payload = self.payload()
        draft = self._store.get('card:' + self.type_id)
        type_id = self.type_id
        self._waiting = True
        self._set_enabled()
        def done(result):
            self._store.discard('card:' + type_id, draft)
            self._waiting = False
            self.owner.state = self.project.snapshot()
            self.select_type(type_id)
            self.cardSaved.emit(result)
            self.message.setText('Definition confirmed.' if status == 'reviewed' else 'Draft definition saved.')
        request_key = "desktop-type-card-" + uuid.uuid4().hex

        def submit_card(_progress, _cancelled):
            return self.controller.save_card(self.project, payload, generation=self.generation,
                                             idempotency_key=request_key, reviewed_confirmed=status == "reviewed")

        self.owner._run("Save type definition", submit_card, done)

    def _set_enabled(self, *_args):
        if not hasattr(self, 'save_card_button') or self._loading:
            return
        editing = self.tabs.currentIndex() == 0 and self.card_pages.currentWidget() is self.editor and (self.type_id is not None)
        available = self._current() and (not self.owner._busy) and (not self._waiting)
        self.directory_button.setVisible(editing)
        self.more_button.setVisible(editing)
        self.save_card_button.setVisible(editing)
        self.types.setEnabled(available)
        if hasattr(self, '_row') and self._row:
            blocked = self.controller.invalid_reason is not None or self._prior_draft is not None
            valid = available and (not blocked)
            self.refresh_context_button.setVisible(blocked)
            self.refresh_context_button.setText('Use current definition' if self._prior_draft else 'Refresh current members')
            self.restore_card_button.setVisible(self._prior_draft is not None)
            for widget in [self.name_edit, *self.texts.values(), self.representatives, self.member_combo, self.extra, self.extra_button]:
                widget.setEnabled(valid)
            self.add_member_button.setEnabled(valid and bool(self._members))
            self.members_tools.setEnabled(valid and bool(self._members))
            chosen = self.representatives.currentItem() is not None
            self.listen_rep_button.setEnabled(valid and chosen)
            self.remove_rep_button.setEnabled(valid and chosen)
            self.draft_action.setEnabled(valid and bool(self.name_edit.text().strip()))
            criteria = bool(self.texts["diagnostic_body"].toPlainText().strip())
            fine_difference = not self._row["parent_type_id"] or bool(self.texts["main_variant_difference"].toPlainText().strip())
            self.save_card_button.setEnabled(self.draft_action.isEnabled() and self.representatives.count() > 0
                                             and criteria and fine_difference)
            self.directed_button.setEnabled(valid)
            active = self._active_directed(self._row['family_id']) if editing else None
            self.directed_button.setText('Continue directed review…' if active else 'Start directed review…')
        self._directed_controls(available)

    def refresh(self):
        if not self._current():
            self.invalidate('Project changed. Reopen Type definitions.')
            return
        state = self.owner.state or self.project.snapshot()
        revision = state['revision']
        if revision != self._revision:
            self._revision = revision
            view = self.controller.view(self.project, state)
            self._directory_rows = view['types']
            self._filter_directory()
            self.controller.observe_context(state, project_id=self.project.metadata['project_id'], generation=self.generation)
            if self.controller.invalid_reason:
                self.message.setText(self.controller.invalid_reason)
            self._render_directed(view)
        self.refresh_pending_directed(state)
        self._set_enabled()

    def invalidate(self, reason):
        self.controller.invalidate(reason)
        self.timer.stop()
        self._set_enabled()
        self.message.setText(reason)

    def stop(self):
        media = getattr(self.owner, '_review_media_dialog', None)
        if self._audition_id and media and (media.target_id == self._audition_id):
            media.stop()
        self._audition_id = None

    def reject(self):
        try:
            self.save_draft()
        except Exception as exc:
            self.owner._error(exc, operation="save definition draft")
            return
        self.stop()
        super().reject()

    def closeEvent(self, event):
        try:
            self.save_draft()
        except Exception as exc:
            event.ignore()
            self.owner._error(exc, operation="save definition draft")
            return
        self.stop()
        self.timer.stop()
        super().closeEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()
        self.timer.start()
