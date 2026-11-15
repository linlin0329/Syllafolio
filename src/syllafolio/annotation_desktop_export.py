"""D4原生成果表单；服务冻结版本，桌面不重造目录、数值或完成记录。"""
from __future__ import annotations
from .annotation_english import system_error, system_fields, system_text

from concurrent.futures import TimeoutError as FutureTimeout
from datetime import datetime
from pathlib import Path
import copy
import hashlib
import json
import threading
import uuid

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFileDialog,
    QFormLayout, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton,
    QScrollArea, QStackedWidget, QVBoxLayout, QWidget)

from .annotation_assistant_contracts import canonical_hash
from .annotation_desktop_review_media import _checked_path

CHOOSE = "__export_choose__"
ACTIVE = {"queued", "running"}
FILENAME = "desktop_export_draft.json"
SCHEMA = "syllafolio-desktop-export-draft-v1"
ERROR_TEXT = {
    "EXPORT_HUMAN_INCOMPLETE": "Current scope and type-review completion is missing. Draft delivery remains available.",
    "CURRENT_ASSESSMENT_REQUIRED": "Complete delivery needs an explicitly selected complete, current assessment report. A not-estimable support report may retain its limitations.",
    "EXPORT_ASSESSMENT_REQUIRED": "The selected assessment run is missing, incomplete or historical. Select a current completed or not-estimable report.",
    "EXPORT_BUSY": "Another delivery is queued or running in this project.",
    "EXPORT_INPUT_CHANGED": "Frozen input bytes changed; existing labels cannot be exported with different input.",
    "EXPORT_NOT_READY": "This delivery is not completed and has no published package.",
    "EXPORT_CHANGED": "Published package bytes or their manifest identity changed.",
    "EXPORT_CLEANUP_FAILED": "The delivery failed and cleanup needs attention; no partial package is offered.",
    "REVISION_CONFLICT": "The project changed. Refresh and explicitly confirm its current scope, assessment selection and delivery again.",
    "PROJECT_DRAINING": "The project is closing. Unsubmitted text can still be saved locally.",
    "PROCESS_INTERRUPTED": "The previous process ended without a complete package. A new explicit request is required.",
    "CANCELLED": "Delivery cancelled; no partial package is published.",
}


class ExportError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(f"{code}: {message}")


def _require(value, code, message):
    if not value: raise ExportError(code, message)


def _identity(project):
    return {"project_id": project.metadata["project_id"], "run_id": project.service.run_id,
            "input_fingerprint": project.dataset.input_fingerprint}


def _display(value):
    return system_fields(value)



class ExportController:
    """所有权威动作使用同owner的CAS和精确请求；用途并入既有reason字段。"""
    def __init__(self, project):
        self.project, self.requests, self.pending = project, {}, {}
        self.base_ticket = self.ticket()
        self.generated_job = None

    def ticket(self, state=None):
        state = self.project.snapshot() if state is None else state
        return {**_identity(self.project), "revision": state["revision"],
                "export_fingerprint": state.get("final_review", {}).get("publication_report", {}).get("current_fingerprint")}

    def rebase(self, state=None): self.base_ticket = self.ticket(state)

    def view(self, state=None):
        state = self.project.snapshot() if state is None else state
        section = copy.deepcopy(state.get("final_review", {}).get("publication_report", {}))
        for job in section.get("jobs", []):
            # Older system receipts used `mode` for the project contract. Never
            # display that value as a draft/complete delivery choice.
            job["delivery_mode"] = next((value for value in (job.get("delivery_mode"),
                job.get("metadata", {}).get("mode"), job.get("mode")) if isinstance(value, str) and value in {"draft", "complete"}), None)
        return {**section, "identity": _identity(self.project), "revision": state["revision"], "ticket": self.ticket(state),
                "declared_scope": copy.deepcopy(state.get("catalogue_scope")),
                "completion_missing": copy.deepcopy(state.get("completion", {}).get("missing", [])),
                "current_completion_id": state.get("current_completion_id"),
                "scientific_validity_certified": False}

    def _execute(self, action, payload, *, ticket=None, idempotency_key=None):
        ticket = copy.deepcopy(self.base_ticket if ticket is None else ticket)
        _require(all(ticket.get(key) == value for key, value in _identity(self.project).items()),
                 "EXPORT_CONTEXT_CHANGED", "This request belongs to another project/run/input.")
        _require(idempotency_key is None or isinstance(idempotency_key, str) and 0 < len(idempotency_key) <= 160,
                 "IDEMPOTENCY_REQUIRED", "Use a nonempty original request key.")
        request = {"action": action, "payload": copy.deepcopy(payload), "ticket": ticket}
        signature = canonical_hash(request)
        key = idempotency_key or self.pending.get(signature) or "desktop-export-" + uuid.uuid4().hex
        if key in self.requests:
            _require(self.requests[key] == request, "IDEMPOTENCY_CONFLICT", "Retry the original exact delivery request.")
        else:
            self.requests[key] = request; self.pending[signature] = key
        return self.project.execute(action, copy.deepcopy(payload), expected_revision=ticket["revision"], idempotency_key=key)

    def start(self, mode, assessment_job_id, purpose, reason, *, confirmed, scope_confirmed, **kwargs):
        _require(mode in {"draft", "complete"}, "EXPORT_MODE", "Explicitly choose draft or scoped complete delivery.")
        _require(confirmed is True and scope_confirmed is True, "EXPORT_CONFIRMATION", "Explicitly confirm the displayed scope/counts and this delivery request.")
        _require(isinstance(purpose, str) and bool(purpose.strip()) and isinstance(reason, str) and bool(reason.strip()),
                 "EXPORT_REASON", "Enter the actual delivery purpose and its reason.")
        _require(assessment_job_id is None or isinstance(assessment_job_id, str) and assessment_job_id not in {"", CHOOSE},
                 "EXPORT_ASSESSMENT_SELECTION", "Choose an assessment run explicitly, or explicitly choose no report for a draft.")
        _require(mode != "complete" or assessment_job_id is not None, "EXPORT_ASSESSMENT_REQUIRED", "Complete delivery requires an explicitly selected current assessment report.")
        combined = "Purpose: " + purpose + "\nReason: " + reason
        _require(len(combined.strip()) <= 4000, "EXPORT_REASON", "Combined purpose and reason must fit the service's 4000-character limit.")
        return self._execute("catalogue_export_start", {"mode": mode, "assessment_job_id": assessment_job_id, "reason": combined}, **kwargs)

    def export_to(self, destination, mode, assessment_id, purpose, reason, *, ticket, confirmed,
                  progress=None, cancelled=None):
        """Freeze one reviewed request, then deliver the registered bytes for its exact job."""
        from .annotation_desktop_export_files import ExporterAdapter
        files = ExporterAdapter(self.project)
        path = files.check_zip_destination(destination)
        if cancelled and cancelled():
            raise ExportError("CANCELLED", ERROR_TEXT["CANCELLED"])
        response = self.start(mode, assessment_id, purpose, reason, ticket=ticket,
                              confirmed=confirmed, scope_confirmed=confirmed)
        job_id = response["record"]["job_id"]
        if progress:
            progress({"stage": "Generating catalogue"})
        service = self.project.service
        with service._lock:
            future = service.catalogue_export.futures.get(job_id)
        cancel_sent = False
        while future is not None:
            try:
                future.result(timeout=.25)
                break
            except FutureTimeout:
                if future.done():
                    future.result()
                    break
                if cancelled and cancelled() and not cancel_sent:
                    cancel_sent = True
                    job = self.job(job_id)
                    if job["status"] in ACTIVE:
                        self.cancel(job_id, "Cancelled during catalogue export.", ticket=ticket)
        job = self.job(job_id)
        if job["status"] != "completed":
            error = job.get("error") or {}
            raise ExportError(error.get("code", "EXPORT_NOT_READY"),
                              error.get("message") or "The catalogue could not be generated.")
        # Keep this version available when saving fails, including a destination race.
        self.generated_job = job_id
        return files.save_zip(job_id, path, cancelled=cancelled, progress=progress)

    def cancel(self, job_id, reason, **kwargs):
        _require(isinstance(job_id, str) and bool(job_id), "EXPORT_NOT_FOUND", "Select the exact persisted delivery job.")
        _require(isinstance(reason, str) and bool(reason.strip()), "EXPORT_REASON", "Enter the actual cancellation reason.")
        return self._execute("catalogue_export_cancel", {"job_id": job_id, "reason": reason}, **kwargs)

    def job(self, job_id, state=None):
        job = next((row for row in self.view(state).get("jobs", []) if row["job_id"] == job_id), None)
        _require(job is not None, "EXPORT_NOT_FOUND", "Select a delivery start from this project.")
        return job


class ExportDraftStore:
    def __init__(self, project):
        self.project, self.path = project, project.project_dir / FILENAME
        if not hasattr(project.service, "_desktop_export_draft_lock"):
            project.service._desktop_export_draft_lock = threading.RLock()
        self.lock = project.service._desktop_export_draft_lock

    def load(self):
        with self.lock:
            _checked_path(self.project, self.path)
            if not self.path.exists(): return None
            raw = self.path.read_bytes()
            try:
                def pairs(rows):
                    value = {}
                    for key, item in rows:
                        _require(key not in value, "EXPORT_DRAFT_INVALID", "Duplicate draft field.")
                        value[key] = item
                    return value
                value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs)
                form = value.get("form") if isinstance(value, dict) else None
                _require(isinstance(value, dict) and value.get("schema_version") == SCHEMA
                         and all(value.get(key) == item for key, item in _identity(self.project).items())
                         and value.get("submission_state") == "unsubmitted" and value.get("confirmed") is False
                         and value.get("scope_confirmed") is False and isinstance(form, dict)
                         and set(form) == {"mode", "assessment_job_id", "purpose", "reason", "cancel_reason", "selected_job_id"}
                         and form["mode"] in {CHOOSE, "draft", "complete"}
                         and (form["assessment_job_id"] is None or isinstance(form["assessment_job_id"], str))
                         and all(isinstance(form[key], str) for key in ("purpose", "reason", "cancel_reason", "selected_job_id")),
                         "EXPORT_DRAFT_INVALID", "Draft fields or project/run/input identity do not match.")
                return value
            except (ValueError, UnicodeError, TypeError) as exc:
                backup = self.path.with_name("desktop_export_draft.recovery-" + hashlib.sha256(raw).hexdigest()[:16] + ".json")
                _checked_path(self.project, backup)
                if backup.exists(): _require(backup.read_bytes() == raw, "EXPORT_DRAFT_BACKUP", "Recovery bytes differ.")
                else:
                    with backup.open("xb") as stream: stream.write(raw)
                raise ExportError("EXPORT_DRAFT_INVALID", "Unreadable original retained unchanged: " + backup.name) from exc

    def save(self, form, fingerprint):
        with self.lock:
            _checked_path(self.project, self.path); self.load()
            before = hashlib.sha256(self.path.read_bytes()).hexdigest() if self.path.exists() else None
            value = {"schema_version": SCHEMA, **_identity(self.project), "form": copy.deepcopy(form),
                     "context_fingerprint": fingerprint, "submission_state": "unsubmitted", "confirmed": False, "scope_confirmed": False}
            temporary = self.path.with_name(".export-draft-" + uuid.uuid4().hex + ".tmp")
            try:
                temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
                _checked_path(self.project, self.path)
                current = hashlib.sha256(self.path.read_bytes()).hexdigest() if self.path.exists() else None
                _require(current == before, "EXPORT_DRAFT_CHANGED", "External draft changes were retained.")
                temporary.replace(self.path)
            finally: temporary.unlink(missing_ok=True)



def _label(text="", *, heading=False):
    widget = QLabel(text)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setWordWrap(True)
    if heading:
        font = widget.font()
        font.setBold(True)
        widget.setFont(font)
    return widget


def _date(value):
    try:
        date = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return date.astimezone().strftime("%Y-%m-%d %H:%M %z")
    except (AttributeError, TypeError, ValueError):
        return str(value or "")


def _report_name(option):
    status = {"completed": "Scored", "not_estimable": "Support report"}.get(option["status"], option["status"].capitalize())
    match = "Historical" if option["stale_for_current"] else "Current"
    return f"{_date(option['created_at'])} · {status} · {match}"


def _short_id(job_id):
    return job_id.rsplit("-", 1)[-1][:8]


def _inventory_details(inventory):
    sections = []
    for title, field in (("Declared or provisional scope", "scope_item_ids"),
                         ("Outside this scope, included in the full index", "outside_scope_item_ids"),
                         ("Examples with audio and spectrograms", "example_item_ids"),
                         ("Examples outside this scope", "outside_scope_example_ids")):
        ids = inventory[field]
        sections.append(f"{title} ({len(ids)})\n" + (", ".join(ids) or "None"))
    return "\n\n".join(sections)


def _inventory_summary(inventory):
    total = len(inventory["all_item_ids"])
    outside = len(inventory["outside_scope_item_ids"])
    examples = len(inventory["example_item_ids"])
    external = len(inventory["outside_scope_example_ids"])
    return (f"The full index and audit cover all {total} syllables ({outside} outside this scope). "
            f"Example media: {examples} distinct syllables, including {external} outside the scope.")


class ExportDialog(QDialog):
    changed = Signal(object)
    saved = Signal(object)
    previewOpened = Signal(object)
    assessmentRequested = Signal(object)
    scopeRequested = Signal()

    def __init__(self, owner):
        super().__init__(owner)
        self.owner, self.project = owner, owner.project
        self.generation = owner._close_coordinator.generation
        self.controller = ExportController(self.project)
        self.store = ExportDraftStore(self.project)
        self._view = self._selected_job = self._retry_job = self._saved = self._invalid = None
        self._historic_inventory_id = self._historic_inventory = None
        self._dirty = self._loading = self._waiting = False
        self._epoch = 0
        self.setWindowTitle("Export catalogue")
        area = self.screen().availableGeometry()
        self.resize(min(800, area.width()-32), min(690, area.height()-60))
        outer = QVBoxLayout(self)
        self.pages = QStackedWidget()
        outer.addWidget(self.pages, 1)
        self._build_form()
        self._build_history()
        self.message = _label()
        outer.addWidget(self.message)
        buttons = QHBoxLayout()
        self.history_button = QPushButton("Previous exports…")
        self.history_button.clicked.connect(self._toggle_history)
        buttons.addWidget(self.history_button)
        buttons.addStretch()
        self.start_button = QPushButton("Export catalogue…")
        self.start_button.setObjectName("exportCatalogue")
        self.start_button.clicked.connect(lambda: self._guard(self.start_export))
        buttons.addWidget(self.start_button)
        self.close_button = QPushButton("Close")
        self.close_button.clicked.connect(self.reject)
        buttons.addWidget(self.close_button)
        outer.addLayout(buttons)
        self.timer = QTimer(self)
        self.timer.setInterval(350)
        self.timer.timeout.connect(lambda: self._guard(self.refresh))
        self.finished.connect(lambda _: self.timer.stop())
        self.owner.operationFailed.connect(self._operation_failed)
        self.refresh(force=True)
        self._restore_form()

    def _page(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        layout = QVBoxLayout(body)
        scroll.setWidget(body)
        self.pages.addWidget(scroll)
        return scroll, layout

    def _button(self, text, callback):
        button = QPushButton(text)
        button.setAutoDefault(False)
        button.clicked.connect(lambda: self._guard(callback))
        return button

    def _build_form(self):
        self.form_page, layout = self._page()
        top = QHBoxLayout()
        top.addWidget(_label("Catalogue contents", heading=True))
        top.addStretch()
        self.scope_button = self._button("Final checks…", lambda: self._request_workspace(self.scopeRequested))
        top.addWidget(self.scope_button)
        layout.addLayout(top)
        self.scope_text = _label()
        self.counts_text = _label()
        self.inventory_text = _label()
        for label in (self.scope_text, self.counts_text, self.inventory_text):
            layout.addWidget(label)
        self.members_button = QPushButton("Show included syllables…")
        self.members_button.setCheckable(True)
        self.members_button.toggled.connect(self._inventory_toggled)
        layout.addWidget(self.members_button, 0, Qt.AlignmentFlag.AlignLeft)
        self.inventory_detail = QPlainTextEdit()
        self.inventory_detail.setReadOnly(True)
        self.inventory_detail.setMaximumHeight(130)
        self.inventory_detail.hide()
        layout.addWidget(self.inventory_detail)
        layout.addSpacing(6)
        layout.addWidget(_label("Export options", heading=True))
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.mode = QComboBox()
        self.mode.setObjectName("catalogueMode")
        self.mode.addItem("Choose a mode…", CHOOSE)
        self.mode.addItem("Draft catalogue", "draft")
        self.mode.addItem("Complete catalogue", "complete")
        self.assessment = QComboBox()
        self.assessment.setObjectName("catalogueAssessment")
        self.assessment.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.assessment_button = self._button("Internal assessment…", self._request_assessment)
        report_row = QHBoxLayout()
        report_row.addWidget(self.assessment, 1)
        report_row.addWidget(self.assessment_button)
        form.addRow("Mode", self.mode)
        form.addRow("Assessment", report_row)
        self.purpose = QLineEdit()
        self.purpose.setObjectName("cataloguePurpose")
        self.reason = QLineEdit()
        self.reason.setObjectName("catalogueReason")
        self.reason.setPlaceholderText("Why are you exporting this version?")
        form.addRow("Purpose", self.purpose)
        form.addRow("Export reason", self.reason)
        layout.addLayout(form)
        self.hint = _label()
        layout.addWidget(self.hint)
        self.confirmation = QCheckBox("I confirm the scope, included data, export mode and assessment shown above.")
        self.confirmation.setObjectName("catalogueConfirmation")
        layout.addWidget(self.confirmation)
        self.mode.currentIndexChanged.connect(self._input_changed)
        self.assessment.currentIndexChanged.connect(self._input_changed)
        self.purpose.textChanged.connect(self._input_changed)
        self.reason.textChanged.connect(self._input_changed)
        self.confirmation.toggled.connect(self._buttons)
        layout.addStretch()
        self.result_box = QWidget()
        self.retry_summary = _label()
        self.retry_summary.hide()
        layout.addWidget(self.retry_summary)
        row = QHBoxLayout(self.result_box)
        row.setContentsMargins(0, 0, 0, 0)
        self.retry_button = self._button("Save generated ZIP…", self.retry_save)
        self.retry_button.setObjectName("retryCatalogueSave")
        self.new_button = self._button("Start a different export", self._new_request)
        self.preview_saved_button = self._button("Open report", self._preview_saved)
        self.folder_button = self._button("Open folder", self._open_folder)
        for button in (self.retry_button, self.new_button, self.preview_saved_button, self.folder_button):
            row.addWidget(button)
        row.addStretch()
        self.result_box.hide()
        layout.addWidget(self.result_box)

    def _build_history(self):
        self.history_page, layout = self._page()
        layout.addWidget(_label("Previous exports", heading=True))
        self.jobs = QComboBox()
        self.jobs.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.jobs.currentIndexChanged.connect(self._history_selected)
        layout.addWidget(self.jobs)
        self.selected_status = _label()
        layout.addWidget(self.selected_status)
        self.history_members_button = QPushButton("Show included syllables…")
        self.history_members_button.setCheckable(True)
        layout.addWidget(self.history_members_button, 0, Qt.AlignmentFlag.AlignLeft)
        self.history_detail = QPlainTextEdit()
        self.history_detail.setReadOnly(True)
        self.history_detail.setMaximumHeight(160)
        self.history_detail.hide()
        self.history_members_button.toggled.connect(self.history_detail.setVisible)
        layout.addWidget(self.history_detail)
        row = QHBoxLayout()
        self.save_package_button = self._button("Save this ZIP…", self.save_package)
        self.preview_button = self._button("Open report", self.preview_selected)
        row.addWidget(self.save_package_button)
        row.addWidget(self.preview_button)
        row.addStretch()
        layout.addLayout(row)
        self.cancel_reason = QLineEdit()
        self.cancel_reason.setPlaceholderText("Reason for cancelling this export")
        self.cancel_reason.textChanged.connect(self._input_changed)
        self.cancel_button = self._button("Cancel active export", self.cancel_selected)
        layout.addWidget(self.cancel_reason)
        layout.addWidget(self.cancel_button, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addStretch()

    def _inventory_toggled(self, checked):
        self.inventory_detail.setVisible(checked)
        self.members_button.setText("Hide included syllables" if checked else "Show included syllables…")
        if checked and self._view:
            self._render_inventory()

    def _render_inventory(self):
        inventory = self._view.get("inventory") or {}
        counts = self._view.get("counts", {})
        scope = self._view.get("scope") or {}
        n, total = counts.get("n_scope", 0), counts.get("n_task", 0)
        level = {"family": "Family level", "leaf": "Fine-form level"}.get(scope.get("target_level"), "")
        self.scope_text.setText(
            f"Declared scope: {n} of {total} syllables · {level}" if scope.get("declared") else
            f"No declared scope. A draft covers all {total} task syllables.")
        self.counts_text.setText("   ·   ".join(f"{title}: {counts.get(field, 0)}" for field, title in
            (("assigned", "Assigned"), ("unresolved", "Family unresolved"), ("excluded", "Excluded"), ("unreviewed", "Unreviewed"))))
        self.inventory_text.setText(_inventory_summary(inventory) if inventory else "Package contents unavailable.")
        if self.members_button.isChecked() and inventory:
            self.inventory_detail.setPlainText(_inventory_details(inventory))

    def is_current(self):
        return (self.owner.project is self.project and self.owner._close_coordinator.generation == self.generation
                and self._invalid is None)

    def _guard(self, function):
        try:
            return function()
        except Exception as exc:
            self.message.setText(system_error(exc))
            self.owner._error(exc, operation="catalogue export")

    def _operation_failed(self, error):
        if self._waiting:
            self._waiting = False
            generated = self.controller.generated_job
            if generated:
                self._retry_job = generated
                self._selected_job = generated
                inventory = self._adapter().frozen_inventory(generated)
                self.retry_summary.setText(
                    f"Generated version {_short_id(generated)}: {len(inventory['scope_item_ids'])} syllables in scope. "
                    + _inventory_summary(inventory))
                self.message.setText(f"Version {_short_id(generated)} was generated. Saving failed: {str(error)[:200]} "
                                     "Choose another location to save the same ZIP.")
            else:
                self.message.setText(str(error)[:260])
            self.controller.generated_job = None
            self._buttons()

    def _input_changed(self, *_args):
        if self._loading:
            return
        self._dirty = True
        self.confirmation.setChecked(False)
        self._buttons()

    def _selected(self):
        return next((row for row in (self._view or {}).get("jobs", []) if row["job_id"] == self._selected_job), None)

    def _option(self):
        selected = self.assessment.currentData()
        return next((row for row in (self._view or {}).get("assessment_options", [])
                     if row["job_id"] == selected), None)

    def _buttons(self, *_args):
        if not self._view or self._loading:
            return
        available = self.is_current() and not self.owner._busy and not self._waiting
        self.pages.setEnabled(available)
        self.history_button.setEnabled(available)
        mode = self.mode.currentData()
        option = self._option()
        view = self._view
        if mode == "draft":
            eligible = view.get("can_export_draft", False) and (option is not None or self.assessment.currentData() is None)
            message = ("A draft retains unfinished items and records their status." if not option else
                       "The draft will label the selected assessment as historical." if option.get("stale_for_current") else
                       "The draft will include the selected assessment status.")
            title = "Export draft catalogue…"
        elif mode == "complete":
            eligible = view.get("can_export_complete", False) and option and option.get("eligible_for_complete")
            messages = []
            if not view.get("human_completion_current"):
                messages.append("Complete Final checks for the current scope and definitions.")
            if not option or not option["eligible_for_complete"]:
                messages.append("Choose a matching scored or support report." if
                    any(r["eligible_for_complete"] for r in view.get("assessment_options", [])) else
                    "Run an internal assessment for the current scope, labels and sources.")
            message = " ".join(messages) or "The selected report matches the confirmed scope."
            title = "Export complete catalogue…"
        else:
            eligible = False
            title = "Export catalogue…"
            message = "Choose Draft to share work in progress, or Complete to deliver the confirmed scope."
        if view.get("current_job_id"):
            message = "An export is in progress."
        self.hint.setText(message)
        self.start_button.setText(title)
        on_form = self.pages.currentWidget() is self.form_page
        self.start_button.setVisible(on_form and not self._retry_job)
        self.start_button.setEnabled(bool(available and view.get("inventory") and eligible and self.confirmation.isChecked()
                                         and self.purpose.text().strip() and self.reason.text().strip()))
        self.assessment_button.setText("View assessment…" if option else "Internal assessment…")
        self.result_box.setVisible(bool(self._retry_job or self._saved))
        self.retry_summary.setVisible(bool(self._retry_job))
        self.retry_button.setVisible(bool(self._retry_job))
        self.new_button.setVisible(bool(self._retry_job))
        self.preview_saved_button.setVisible(bool(self._saved) and not self._retry_job)
        self.folder_button.setVisible(bool(self._saved) and not self._retry_job)
        for field in (self.mode, self.assessment, self.purpose, self.reason, self.confirmation):
            field.setEnabled(self._retry_job is None)
        job = self._selected()
        complete = bool(job and job["status"] == "completed")
        active = bool(job and job["status"] in ACTIVE)
        self.save_package_button.setEnabled(available and complete)
        self.preview_button.setEnabled(available and complete)
        self.cancel_reason.setVisible(active)
        self.cancel_button.setVisible(active)
        self.cancel_button.setEnabled(bool(available and active and not job.get("cancel_requested") and self.cancel_reason.text().strip()))

    def select_assessment(self, job_id):
        self.refresh(force=True)
        index = self.assessment.findData(job_id)
        _require(index >= 0 and job_id not in (None, CHOOSE), "ASSESSMENT_NOT_FOUND",
                 "The selected assessment is unavailable in the current project.")
        self._new_request()
        self.pages.setCurrentWidget(self.form_page)
        self.history_button.setText("Previous exports…")
        self.assessment.setCurrentIndex(index)
        option = self._option()
        if self.mode.currentData() == CHOOSE and option and option["eligible_for_complete"]:
            self.mode.setCurrentIndex(self.mode.findData("complete"))
        self.confirmation.setChecked(False)
        self._buttons()

    def _toggle_history(self):
        history = self.pages.currentWidget() is self.form_page
        self.pages.setCurrentWidget(self.history_page if history else self.form_page)
        self.history_button.setText("Back to export" if history else "Previous exports…")
        if history:
            self._render_history()
        self._buttons()

    def _history_selected(self, *_args):
        if not self._loading:
            self._selected_job = self.jobs.currentData()
            self._render_history()
            self._buttons()

    def _render_history(self):
        job = self._selected()
        if self.pages.currentWidget() is not self.history_page:
            return
        complete = bool(job and job["status"] == "completed")
        self.history_members_button.setVisible(complete)
        self.history_detail.setVisible(complete and self.history_members_button.isChecked())
        if not job:
            self.selected_status.setText("No previous export.")
            return
        info = [f"Version {job['job_id']}", "Historical version" if job.get("stale_for_current") else "Matches current project data",
                "Assessment: " + (job.get("assessment_job_id") or "None")]
        if complete:
            if self._historic_inventory_id != job["job_id"]:
                self._historic_inventory = self._adapter().frozen_inventory(job["job_id"])
                self._historic_inventory_id = job["job_id"]
                self.history_detail.setPlainText(_inventory_details(self._historic_inventory))
            info.append(f"Frozen delivery scope: {len(self._historic_inventory['scope_item_ids'])} syllables. "
                        + _inventory_summary(self._historic_inventory))
        if job.get("error"):
            info.append(system_error(job["error"]))
        self.selected_status.setText("\n".join(info))

    def refresh(self, force=False):
        if not self.is_current():
            self.invalidate("Project changed. Open Export catalogue for the current project.")
            return
        if self.owner._busy and not force:
            self._buttons()
            return
        state = self.owner.state if self.owner.state is not None else self.project.snapshot()
        if not force and self._view and self._view["revision"] == state["revision"]:
            self._buttons()
            return
        old = self._view
        self._view = self.controller.view(state)
        self.controller.rebase(state)
        self._loading = True
        try:
            self._render_inventory()
            options = self._view.get("assessment_options", [])
            previous = self.assessment.currentData()
            self.assessment.clear()
            self.assessment.addItem("Choose a report…", CHOOSE)
            self.assessment.addItem("No assessment report (draft only)", None)
            for option in options:
                self.assessment.addItem(_report_name(option) + " · " + _short_id(option["job_id"]), option["job_id"])
            if old is None:
                eligible = [o for o in options if o["eligible_for_complete"]]
                previous = eligible[0]["job_id"] if len(eligible) == 1 else CHOOSE
            idx = self.assessment.findData(previous)
            self.assessment.setCurrentIndex(max(0, idx))
            prior_purpose = ((old or {}).get("declared_scope") or {}).get("purpose", "")
            if not self.purpose.text().strip() or self.purpose.text() == prior_purpose:
                self.purpose.setText((self._view.get("declared_scope") or {}).get("purpose", ""))
            if old is None or old["ticket"] != self._view["ticket"]:
                self.confirmation.setChecked(False)
            self.jobs.clear()
            for row in reversed(self._view.get("jobs", [])):
                label = f"{_date(row['created_at'])} · {(row['delivery_mode'] or 'Export').capitalize()} · {row['status']} · {_short_id(row['job_id'])}"
                self.jobs.addItem(label, row["job_id"])
            self.jobs.setCurrentIndex(max(0, self.jobs.findData(self._selected_job)))
            self._selected_job = self.jobs.currentData()
            self._render_history()
        finally:
            self._loading = False
        self._buttons()

    def _new_request(self):
        self._saved = self._retry_job = None
        self.confirmation.setChecked(False)
        self.message.clear()
        self._buttons()

    def _pick_path(self, suggested):
        path, _ = QFileDialog.getSaveFileName(self, "Save catalogue ZIP to a new file", suggested + ".zip", "ZIP archive (*.zip)")
        return path

    def _run(self, label, worker, done):
        _require(self.is_current() and not self.owner._busy and not self._waiting,
                 "EXPORT_CONTEXT_CHANGED", "Wait for the current project operation.")
        epoch = self._epoch
        self._waiting = True
        self._buttons()
        def finish(result):
            if not self.is_current() or epoch != self._epoch:
                return
            self._waiting = False
            self.controller.generated_job = None
            self.owner.state = self.project.snapshot()
            self.refresh(force=True)
            done(result)
            self._buttons()
        try:
            self.owner._run(label, worker, finish)
        except Exception:
            self._waiting = False
            self._buttons()
            raise

    def _saved_zip(self, result):
        self._saved = result
        self._retry_job = None
        self._selected_job = result["job_id"]
        self.confirmation.setChecked(False)
        self.message.setText("Saved " + result["zip_path"])
        self.saved.emit(result)
        self.changed.emit(result)

    def start_export(self):
        _require(self.start_button.isEnabled() and self.is_current(), "EXPORT_CONFIRMATION",
                 "Review the scope and selected assessment, then confirm this export.")
        ticket = copy.deepcopy(self._view["ticket"])
        args = self.mode.currentData(), self.assessment.currentData(), self.purpose.text().strip(), self.reason.text().strip()
        confirmed = self.confirmation.isChecked()
        path = self._pick_path(f"Syllafolio-{args[0]}-catalogue")
        if not path:
            return
        _require(self.is_current() and self.controller.ticket() == ticket and self.confirmation.isChecked(),
                 "REVISION_CONFLICT", ERROR_TEXT["REVISION_CONFLICT"])
        self.save_draft()
        self.controller.generated_job = None
        self.confirmation.setChecked(False)
        self.message.setText("Generating the catalogue and saving the verified ZIP…")
        self._run("Export catalogue", lambda progress, cancelled: self.controller.export_to(
            path, *args, ticket=ticket, confirmed=confirmed, cancelled=cancelled, progress=progress), self._saved_zip)

    def _save_version(self, job_id):
        _require(job_id and self.controller.job(job_id)["status"] == "completed", "EXPORT_NOT_READY", ERROR_TEXT["EXPORT_NOT_READY"])
        path = self._pick_path(job_id)
        if not path:
            return
        self.controller.generated_job = job_id
        self._run("Save catalogue ZIP", lambda progress, cancelled: self._adapter().save_zip(
            job_id, path, cancelled=cancelled, progress=progress), self._saved_zip)

    def retry_save(self):
        self._save_version(self._retry_job)

    def save_package(self):
        self._save_version(self._selected_job)

    def cancel_selected(self):
        job = self._selected()
        _require(job and job["status"] in ACTIVE and self.cancel_reason.text().strip(),
                 "EXPORT_NOT_ACTIVE", "Choose an active export and enter a cancellation reason.")
        job_id, reason = job["job_id"], self.cancel_reason.text().strip()
        self._run("Cancel catalogue export", lambda _progress, _cancelled: self.controller.cancel(job_id, reason),
                  lambda receipt: self.changed.emit(receipt))

    def _adapter(self):
        from .annotation_desktop_export_files import ExporterAdapter
        return ExporterAdapter(self.project)

    def _preview(self, job_id):
        def done(result):
            _require(QDesktopServices.openUrl(QUrl.fromLocalFile(result["html_path"])), "EXPORT_PREVIEW_OPEN",
                     "The offline report could not be opened. Open report.html from the extracted ZIP.")
            self.previewOpened.emit(result)
        self._run("Open offline report", lambda progress, cancelled:
                  self._adapter().prepare_preview(job_id, cancelled=cancelled, progress=progress), done)

    def preview_selected(self):
        self._preview(self._selected_job)

    def _preview_saved(self):
        self._preview(self._saved["job_id"])

    def _open_folder(self):
        _require(self._saved and QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(self._saved["zip_path"]).parent))),
                 "EXPORT_DIRECTORY", "The output folder could not be opened.")

    def _request_assessment(self):
        job_id = self.assessment.currentData()
        self._request_workspace(self.assessmentRequested, job_id if job_id != CHOOSE else None)

    def _request_workspace(self, signal, *args):
        _require(self.is_current() and not self.owner._busy, "EXPORT_CONTEXT_CHANGED", "Wait for the current project.")
        self.save_draft()
        signal.emit(*args)

    def save_draft(self):
        if self._dirty:
            self.store.save({"mode": self.mode.currentData(), "assessment_job_id": self.assessment.currentData(),
                            "purpose": self.purpose.text(), "reason": self.reason.text(), "cancel_reason": self.cancel_reason.text(),
                            "selected_job_id": self._selected_job or ""}, (self._view or {}).get("current_fingerprint"))
            self._dirty = False
        return True

    def _restore_form(self):
        try:
            value = self.store.load()
        except ExportError as exc:
            self.message.setText(system_error(exc))
            return
        if not value:
            return
        form = value["form"]
        self._loading = True
        try:
            self.purpose.setText(form["purpose"])
            self.reason.setText(form["reason"])
            # A changed project needs fresh export choices. Earlier notes remain editable.
            if value.get("context_fingerprint") == self._view["current_fingerprint"]:
                self.mode.setCurrentIndex(max(0, self.mode.findData(form["mode"])))
                self.assessment.setCurrentIndex(max(0, self.assessment.findData(form["assessment_job_id"])))
            self._dirty = False
        finally:
            self._loading = False
        self._buttons()

    def invalidate(self, reason):
        self._invalid = reason
        self._epoch += 1
        self._waiting = False
        self.confirmation.setChecked(False)
        self.stop()
        self.message.setText(reason)
        self._buttons()

    def stop(self):
        self.timer.stop()

    def showEvent(self, event):
        super().showEvent(event)
        if hasattr(self, "timer") and self.is_current():
            self.refresh(force=True)
            self.timer.start()

    def reject(self):
        try:
            self.save_draft()
        except Exception as exc:
            self._guard(lambda: (_ for _ in ()).throw(exc))
            return
        self.confirmation.setChecked(False)
        self.stop()
        super().reject()

    def closeEvent(self, event):
        try:
            self.save_draft()
        except Exception as exc:
            event.ignore()
            self._guard(lambda: (_ for _ in ()).throw(exc))
            return
        self.confirmation.setChecked(False)
        self.stop()
        super().closeEvent(event)
