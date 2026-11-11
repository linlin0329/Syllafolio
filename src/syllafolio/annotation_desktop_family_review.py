"""人工family入口：完整当前成员、显式范围和共同准备事务；不启动定向复核。"""
from __future__ import annotations
from .annotation_english import system_fields, system_text, system_error

import copy
import json
import threading
import uuid


ACTIVE = {"queued", "running", "cancel_requested"}
DEFAULT_PURPOSE = "Review all current members against this human-defined family's current diagnostic criteria."


class FamilyReviewError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(f"{code}: {message}")


def _require(condition, code, message):
    if not condition:
        raise FamilyReviewError(code, message)


def project_view(project, snapshot=None, *, family_id=None, reference_ids=(), visible_item_ids=None):
    """Project one supplied owner snapshot; filters never reduce preparation scope."""
    state = project.snapshot() if snapshot is None else snapshot
    batch = state.get("batch_review", {})
    families = []
    for family in batch.get("families", []):
        row = copy.deepcopy(family)
        card = state.get("type_cards", {}).get(row["family_id"], {})
        review = batch.get("reviews", {}).get(row.get("active_review_id"))
        row.update(definition_status=card.get("definition_status", "not_recorded"),
                   diagnostic_body=card.get("diagnostic_body", row.get("diagnostic_body", "")),
                   definition_card=copy.deepcopy(card), active_review=copy.deepcopy(review))
        families.append(row)
    scope = None
    if family_id is not None:
        family = next((row for row in families if row["family_id"] == family_id), None)
        _require(family is not None, "FAMILY_NOT_FOUND", "Select a current human-defined family, not a machine group or leaf.")
        current = sorted(set(family.get("member_ids", [])))
        review = family.get("active_review") or {}
        current_review = bool(review and not review.get("read_only") and not review.get("archived"))
        directed = bool(current_review and review.get("directed_issue_id") and not review.get("closed"))
        continuing = bool(current_review and not directed and not review.get("closed") and not review.get("scope_stale")
                          and review.get("status") in ACTIVE | {"ready"})
        targets = list(review["item_ids"]) if continuing else current
        route = review.get("route") if continuing else "direct" if len(current) <= 30 else "cluster"
        all_ids = {row["item_id"] for row in state.get("items", [])}
        references = sorted(set(reference_ids))
        _require(all(isinstance(item, str) for item in references) and set(references) <= all_ids,
                 "REFERENCE_SCOPE", "References must be stable IDs from this project.")
        overlap = sorted(set(references) & set(targets))
        readonly = sorted(set(references) - set(targets))
        visible = targets if visible_item_ids is None else sorted(set(visible_item_ids))
        _require(set(visible) <= set(targets), "FAMILY_VIEW_SCOPE", "A view filter may only hide current targets; it cannot add targets.")
        exception = copy.deepcopy(review.get("direct_exception", {"allowed": False, "code": "NO_CLUSTER_FAILURE",
            "message": "A matching real clustering failure for the current complete scope is required."}))
        scope = {"family_id": family_id, "title": family.get("title", family_id),
            "all_current_member_ids": current, "target_ids": targets,
            "preparation_item_ids": targets, "fit_ids": targets if route == "cluster" else [],
            "read_only_reference_ids": readonly, "reference_target_overlap_ids": overlap,
            "eligible_change_ids": targets, "visible_item_ids": visible,
            "view_filter_affects_fit": False, "route": route, "purpose": DEFAULT_PURPOSE,
            "definition_status": family["definition_status"], "diagnostic_body": family["diagnostic_body"],
            "definition_card": family["definition_card"],
            "member_fingerprint": family["member_fingerprint"], "definition_fingerprint": family["definition_fingerprint"],
            "active_review": copy.deepcopy(review) if review else None,
            "can_continue": continuing, "waiting": continuing and review.get("status") in ACTIVE,
            "requires_reopen_reason": bool(review and (review.get("closed") or review.get("scope_stale") or review.get("decisions"))),
            "direct_exception": exception, "directed_review_managed_elsewhere": directed,
            "empty": not current and not continuing,
            "reference_note": "Overlapping reference IDs remain real targets; only external reference IDs are read-only and excluded from fitting and saving."}
    return {"project_id": project.metadata["project_id"], "run_id": project.service.run_id,
            "revision": state["revision"], "families": families, "scope": scope}


class FamilyReviewController:
    project_view = staticmethod(project_view)

    def __init__(self):
        self._lock = threading.RLock()
        self._selected = None
        self._requests = {}
        self._invalid_reason = None

    @staticmethod
    def _binding(project, state, generation, scope):
        return {"project_id": project.metadata["project_id"], "run_id": project.service.run_id,
                "generation": generation, "revision": state["revision"],
                "input_fingerprint": state.get("input_fingerprint"), "family_id": scope["family_id"],
                "member_fingerprint": scope["member_fingerprint"], "definition_fingerprint": scope["definition_fingerprint"]}

    def select(self, project, family_id, *, snapshot=None, generation=0, reference_ids=(), visible_item_ids=None):
        try:
            state = project.snapshot() if snapshot is None else snapshot
            reference_ids = list(reference_ids)
            view = project_view(project, state, family_id=family_id, reference_ids=reference_ids, visible_item_ids=visible_item_ids)
        except Exception:
            self.clear("The requested family/reference scope is invalid; select a valid complete scope before preparing.")
            raise
        with self._lock:
            self._selected = {**view, "binding": self._binding(project, state, generation, view["scope"]),
                              "requested_reference_ids": list(reference_ids)}
            self._invalid_reason = None
            return self.snapshot()

    def clear(self, reason="Select a complete current family scope first."):
        with self._lock:
            self._selected = None
            self._invalid_reason = reason

    def snapshot(self):
        with self._lock:
            result = copy.deepcopy(self._selected) if self._selected is not None else {"scope": None, "binding": None, "families": []}
            result["invalid_reason"] = self._invalid_reason
            return result

    def observe_context(self, state, *, project_id, generation):
        with self._lock:
            if self._selected is None:
                return False
            binding = self._selected["binding"]
            family = next((row for row in state.get("batch_review", {}).get("families", []) if row["family_id"] == binding["family_id"]), {})
            changed = any(binding.get(key) != value for key, value in {
                "project_id": project_id, "run_id": state.get("run_id"), "generation": generation,
                "revision": state.get("revision"), "input_fingerprint": state.get("input_fingerprint"),
                "member_fingerprint": family.get("member_fingerprint"), "definition_fingerprint": family.get("definition_fingerprint")}.items())
            if changed:
                self._invalid_reason = "The scope or project version changed. Explicitly refresh the complete family scope before preparing a new round."
            return changed

    def prepare(self, project, *, generation, idempotency_key, purpose="", reopen_reason="",
                use_direct_exception=False, exception_reason="", gap_seconds=.4, normalization="per_item",
                cluster_for_audition=False):
        _require(isinstance(idempotency_key, str) and 0 < len(idempotency_key) <= 160,
                 "IDEMPOTENCY_REQUIRED", "Preparation needs an explicit request key.")
        _require(all(isinstance(text, str) for text in (purpose, reopen_reason, exception_reason)),
                 "FAMILY_REASON", "Review purpose and retry reasons must be text.")
        _require(type(use_direct_exception) is bool, "FAMILY_EXCEPTION", "Direct-exception selection must be explicit.")
        intent = {"purpose": purpose.strip() or DEFAULT_PURPOSE, "reopen_reason": reopen_reason.strip(),
                  "use_direct_exception": use_direct_exception, "exception_reason": exception_reason.strip(),
                  "gap_seconds": gap_seconds, "normalization": normalization, "cluster_for_audition": cluster_for_audition}
        with project.service._lock:
            with self._lock:
                _require(self._selected is not None, "FAMILY_SCOPE_REQUIRED", "Select and inspect the complete current family scope first.")
                selected = self._selected
                prior = self._requests.get(idempotency_key)
                if prior is not None:
                    _require(prior["intent"] == intent and prior["project_id"] == project.metadata["project_id"]
                             and prior["run_id"] == project.service.run_id and prior["generation"] == generation
                             and prior["family_id"] == selected["scope"]["family_id"],
                             "IDEMPOTENCY_CONFLICT", "The preparation key belongs to a different original request.")
                    response = project.execute("batch_prepare_family", copy.deepcopy(prior["payload"]),
                                               expected_revision=prior["revision"], idempotency_key=idempotency_key)
                    return {**response, "review_id": response["record"]["review"]["review_id"], "continued": False, "waiting": True,
                            "scope": copy.deepcopy(selected["scope"])}
                binding = selected["binding"]
                _require(binding["project_id"] == project.metadata["project_id"] and binding["run_id"] == project.service.run_id
                         and binding["generation"] == generation, "FAMILY_PROJECT_CHANGED", "Reopen this family entry for the current project.")
                state = project.snapshot()
                current = project_view(project, state, family_id=selected["scope"]["family_id"],
                                       reference_ids=selected["requested_reference_ids"])["scope"]
                _require(not current["directed_review_managed_elsewhere"], "DIRECTED_REVIEW_MANAGED",
                         "An existing directed review round cannot be replaced from the ordinary family entry.")
                if use_direct_exception:
                    _require(current["direct_exception"].get("allowed") is True, "FAMILY_EXCEPTION_NOT_ALLOWED",
                             current["direct_exception"].get("message", "No matching clustering failure exists."))
                    _require(bool(intent["exception_reason"]), "FAMILY_EXCEPTION_REASON", "Give a separate actual reason for this direct-audition exception.")
                if current["can_continue"] and not use_direct_exception:
                    review = current["active_review"]
                    return {"review_id": review["review_id"], "continued": True,
                            "waiting": review["status"] in ACTIVE, "scope": current}
                _require(self._binding(project, state, generation, current) == binding,
                         "FAMILY_SCOPE_CHANGED", "Members, definitions or the state version changed; explicitly refresh and inspect the complete scope.")
                _require(not current["empty"], "BATCH_FAMILY_EMPTY", "This family has no current members; no empty round will be created.")
                _require(not current["requires_reopen_reason"] or bool(intent["reopen_reason"]),
                         "FAMILY_REOPEN_REASON", "State why the changed or previously reviewed current scope needs a new round.")
                if cluster_for_audition and not use_direct_exception:
                    current = dict(current, route="family_cluster",
                                   fit_ids=current["target_ids"] if len(current["target_ids"]) > 1 else [])
                payload = {"family_id": current["family_id"], "expected_member_fingerprint": current["member_fingerprint"],
                           "expected_definition_fingerprint": current["definition_fingerprint"], "reason": intent["purpose"],
                           "route": "direct_exception" if use_direct_exception else current["route"],
                           "gap_seconds": gap_seconds, "normalization": normalization, "review_origin": "family_recheck"}
                if intent["reopen_reason"]:
                    payload["reopen_reason"] = intent["reopen_reason"]
                if use_direct_exception:
                    payload.update(exception_reason=intent["exception_reason"],
                                   exception_source_review_id=current["direct_exception"]["source_review_id"])
                    current = dict(current, route="direct_exception", fit_ids=[])
                self._requests[idempotency_key] = {"intent": intent, "payload": copy.deepcopy(payload),
                    "revision": binding["revision"], "project_id": binding["project_id"], "run_id": binding["run_id"],
                    "generation": generation, "family_id": current["family_id"]}
                response = project.execute("batch_prepare_family", payload, expected_revision=binding["revision"], idempotency_key=idempotency_key)
                return {**response, "review_id": response["record"]["review"]["review_id"], "continued": False, "waiting": True, "scope": current}


def _reference_ids(project):
    try:
        from .annotation_desktop_review_media import ReferenceStore
    except ModuleNotFoundError as exc:
        if exc.name != f"{__package__}.annotation_desktop_review_media":
            raise
        return []
    return ReferenceStore(project).ids()


def _table_rows(table, rows):
    from PySide6.QtWidgets import QTableWidgetItem
    table.setRowCount(len(rows))
    for index, values in enumerate(rows):
        for column, value in enumerate(values):
            item = QTableWidgetItem("Not recorded" if value is None else str(value))
            item.setToolTip(item.text())
            table.setItem(index, column, item)
    table.resizeColumnsToContents()


# Qt is confined to the native dialog; the controller has no widget dependency.
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QDoubleSpinBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit,
    QPushButton, QScrollArea, QTableWidget, QVBoxLayout, QWidget)


class FamilyReviewDialog(QDialog):
    def __init__(self, owner, on_review_selected):
        super().__init__(owner)
        self.owner, self.project = owner, owner.project
        self.generation = owner._close_coordinator.generation
        self.on_review_selected = on_review_selected
        self.controller = FamilyReviewController()
        self._family_id, self._pending_review_id = None, None
        self._waiting, self._rendering, self._dialog_active = False, False, False
        self.setWindowTitle("Review a human-defined family")
        available = self.screen().availableGeometry()
        self.resize(min(1050, available.width() - 24), min(760, available.height() - 24))
        outer = QVBoxLayout(self)
        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        content = QWidget(); layout = QVBoxLayout(content)
        scroll.setWidget(content); outer.addWidget(scroll, 1)
        self.filter = QLineEdit(); self.filter.setPlaceholderText("Filter the family list; this never changes the fitting scope")
        self.filter.textChanged.connect(lambda _text: self.refresh())
        layout.addWidget(self.filter)
        self.families = QTableWidget(0, 5)
        self.families.setObjectName("familyReviewTable")
        self.families.setHorizontalHeaderLabels(["Family", "Title", "Current members", "Definition", "Current round"])
        self.families.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.families.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.families.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.families.itemSelectionChanged.connect(lambda: self._guard(self._select_family))
        layout.addWidget(self.families, 2)
        self.refresh_scope = QPushButton("Refresh and inspect the complete selected scope")
        self.refresh_scope.setObjectName("refreshFamilyScope")
        self.refresh_scope.clicked.connect(lambda: self._guard(self._select_family))
        layout.addWidget(self.refresh_scope)
        self.scope = QPlainTextEdit(); self.scope.setReadOnly(True)
        self.scope.setObjectName("familyReviewScope")
        layout.addWidget(self.scope, 2)
        form = QFormLayout()
        self.purpose = QLineEdit(); self.purpose.setPlaceholderText("Optional specific review purpose; the complete-family review instruction is used otherwise")
        self.reopen_reason = QLineEdit(); self.reopen_reason.setPlaceholderText("Required for changed, closed, or already-decided rounds")
        self.gap = QDoubleSpinBox(); self.gap.setRange(.05, 2); self.gap.setDecimals(2); self.gap.setValue(.4)
        self.normalization = QComboBox()
        self.normalization.addItem("Per syllable", "per_item"); self.normalization.addItem("Whole page", "whole_page")
        self.direct_exception = QCheckBox("Use the current backend-authorized direct-audition exception")
        self.direct_exception.setObjectName("directFamilyException")
        self.direct_exception.toggled.connect(lambda _checked: self._render_scope(self.controller.snapshot()))
        self.exception_reason = QLineEdit(); self.exception_reason.setPlaceholderText("Separate actual exception reason; cancellation is not clustering failure")
        self.exception_reason.setObjectName("familyExceptionReason")
        for label, widget in (("Purpose", self.purpose), ("New-round reason", self.reopen_reason), ("Silence gap (s)", self.gap),
                              ("Display normalization", self.normalization), ("Direct exception", self.direct_exception), ("Exception reason", self.exception_reason)):
            form.addRow(label, widget)
        layout.addLayout(form)
        self.message = QLabel("Select a human family to inspect all current members and criteria. References are read-only and do not reduce targets.")
        self.message.setWordWrap(True); layout.addWidget(self.message)
        actions = QHBoxLayout()
        self.prepare_button = QPushButton("Prepare or continue selected family")
        self.prepare_button.setObjectName("prepareFamilyReview")
        self.prepare_button.clicked.connect(lambda: self._guard(self.prepare))
        self.cancel_button = QPushButton("Cancel this preparation")
        self.cancel_button.clicked.connect(lambda: self._guard(self.cancel_preparation))
        actions.addWidget(self.prepare_button); actions.addWidget(self.cancel_button); outer.addLayout(actions)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject); outer.addWidget(buttons)
        self.timer = QTimer(self); self.timer.setInterval(350); self.timer.timeout.connect(lambda: self._guard(self.refresh))
        self.finished.connect(self._finished)
        owner.operationFailed.connect(self._operation_failed)

    def _current(self):
        return self.owner.project is self.project and self.owner._close_coordinator.generation == self.generation and self.owner._close_coordinator.state == "open"

    def _guard(self, function):
        try:
            return function()
        except Exception as exc:
            self.owner._error(exc, operation="family review scope")
            self._render_scope(self.controller.snapshot())
            self.message.setText(f"{getattr(exc, 'code', type(exc).__name__)}: {system_error(exc)}")

    def _operation_failed(self, message):
        if self._waiting:
            self.message.setText(message)

    def _select_family(self):
        if self._rendering or not self._current():
            return
        row = self.families.currentRow()
        if row < 0 or self.families.item(row, 0) is None:
            return
        self._family_id = self.families.item(row, 0).text()
        state = self.owner.state or self.project.snapshot()
        try:
            selected = self.controller.select(self.project, self._family_id, snapshot=state,
                generation=self.generation, reference_ids=_reference_ids(self.project))
        except Exception:
            self.controller.clear("The selected reference or family scope cannot be loaded; the previous selection is not active.")
            raise
        self._pending_review_id = None
        self._render_scope(selected)

    def _render_scope(self, selected):
        scope = selected.get("scope")
        if scope and self.direct_exception.isChecked() and scope["direct_exception"].get("allowed"):
            scope = dict(scope, route="direct_exception", fit_ids=[])
        text = json.dumps(system_fields(scope), ensure_ascii=False, indent=2, allow_nan=False) if scope else "Select a family."
        if self.scope.toPlainText() != text:
            self.scope.setPlainText(text)
        allowed = bool(scope and scope["direct_exception"].get("allowed"))
        self.direct_exception.setEnabled(allowed and not self._waiting)
        if not allowed:
            self.direct_exception.setChecked(False)
        self.prepare_button.setEnabled(bool(scope and not scope["empty"] and not scope["directed_review_managed_elsewhere"]
            and (not selected.get("invalid_reason") or scope["can_continue"])
            and self._current() and not self.owner._busy and not self._waiting))
        if selected.get("invalid_reason"):
            self.message.setText(selected["invalid_reason"])
        elif scope:
            self.message.setText(f"{len(scope['target_ids'])} complete targets · {len(scope['fit_ids'])} fitted · "
                f"{len(scope['read_only_reference_ids'])} external read-only references · route {scope['route']}. " +
                ("Continue the existing valid frozen round; opening it does not recompute." if scope["can_continue"] else
                 system_text(scope["direct_exception"].get("message", "Inspect this complete scope before preparing."))))

    def refresh(self):
        if not self._current():
            self.prepare_button.setEnabled(False); self.cancel_button.setEnabled(False)
            return
        state = self.owner.state or self.project.snapshot()
        view = project_view(self.project, state)
        text = self.filter.text().strip().lower()
        rows = [family for family in view["families"] if not text or text in (family["family_id"] + " " + family.get("title", "")).lower()]
        self._rendering = True
        _table_rows(self.families, [[f["family_id"], f.get("title"), f["member_count"], f["definition_status"],
            (f.get("active_review") or {}).get("status", "None")] for f in rows])
        selected_row = next((index for index, family in enumerate(rows) if family["family_id"] == self._family_id), -1)
        if selected_row >= 0:
            self.families.selectRow(selected_row)
        self._rendering = False
        if self._waiting and not self.owner._busy:
            self._waiting = False
        self.controller.observe_context(state, project_id=self.project.metadata["project_id"], generation=self.generation)
        selected = self.controller.snapshot()
        self._render_scope(selected)
        review = state.get("batch_review", {}).get("reviews", {}).get(self._pending_review_id, {})
        self.cancel_button.setEnabled(bool(review.get("status") in ACTIVE and not self.owner._busy))
        if review.get("status") == "ready" and not self.owner._busy and self._dialog_active:
            if not review.get("scope_stale") and not review.get("read_only"):
                ident, self._pending_review_id = self._pending_review_id, None
                self.on_review_selected(ident)
                self.accept()
        elif review.get("status") in {"failed", "cancelled"}:
            self.message.setText(f"{review['status']}: {review.get('error')}. Refresh the scope; retry uses the required route unless a current matching exception is authorized.")

    def prepare(self):
        _require(self._current(), "FAMILY_PROJECT_CHANGED", "Reopen this entry for the current project.")
        self._waiting = True
        arguments = {"generation": self.generation, "idempotency_key": "desktop-family-" + uuid.uuid4().hex,
            "purpose": self.purpose.text(), "reopen_reason": self.reopen_reason.text(),
            "use_direct_exception": self.direct_exception.isChecked(), "exception_reason": self.exception_reason.text(),
            "gap_seconds": self.gap.value(), "normalization": self.normalization.currentData()}
        self._render_scope(self.controller.snapshot())
        def done(result):
            self._pending_review_id = result["review_id"]
            self.owner.state = self.project.snapshot()
            self.refresh()
        self.owner._run("Prepare current human family", lambda _progress, _cancelled:
            self.controller.prepare(self.project, **arguments), done)

    def cancel_preparation(self):
        _require(self._current() and self._pending_review_id is not None, "REVIEW_NOT_FOUND", "No selected preparation is active.")
        ident = self._pending_review_id
        def cancel(_progress, _cancelled):
            state = self.project.snapshot()
            return self.project.execute("batch_cancel", {"review_id": ident}, expected_revision=state["revision"],
                                        idempotency_key="desktop-family-cancel-" + uuid.uuid4().hex)
        self.owner._run("Cancel current family preparation", cancel, lambda _result: None)

    def showEvent(self, event):
        super().showEvent(event)
        self._dialog_active = True
        self.refresh(); self.timer.start()

    def _finished(self, _result):
        self._dialog_active = False
        self.timer.stop()

    def closeEvent(self, event):
        self.timer.stop()
        super().closeEvent(event)
