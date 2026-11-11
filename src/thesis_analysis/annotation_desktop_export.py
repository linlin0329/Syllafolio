"""D4原生成果表单；服务冻结版本，桌面不重造目录、数值或完成记录。"""
from __future__ import annotations
from .annotation_english import system_error, system_fields, system_text

import copy
import hashlib
import json
import threading
import uuid

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFileDialog,
    QFormLayout, QGridLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton,
    QScrollArea, QSizePolicy, QTabWidget, QVBoxLayout, QWidget)

from .annotation_assistant_contracts import canonical_hash
from .annotation_desktop_assessment import _table, _rows
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


class ExportDialog(QDialog):
    changed = Signal(object)
    saved = Signal(object)
    previewOpened = Signal(object)
    assessmentRequested = Signal()
    scopeRequested = Signal()

    def __init__(self, owner):
        super().__init__(owner)
        self.owner, self.project = owner, owner.project
        self.generation = owner._close_coordinator.generation
        self.controller, self.store = ExportController(self.project), ExportDraftStore(self.project)
        self._view, self._selected_job, self._loading, self._dirty = None, None, False, False
        self._waiting, self._invalid, self._epoch, self._footer_columns = False, None, 0, None
        self.setWindowTitle("Scoped catalogue delivery")
        screen = owner.screen(); area = screen.availableGeometry() if screen else None
        self.resize(min(1080, area.width() - 32) if area else 1080, min(780, area.height() - 64) if area else 780)
        layout = QVBoxLayout(self)
        self.status = QLabel(); self.status.setWordWrap(True); layout.addWidget(self.status)
        self.tabs = QTabWidget(); self.tabs.setUsesScrollButtons(True); self.tabs.setElideMode(Qt.TextElideMode.ElideRight)
        self.tabs.tabBar().setMinimumWidth(0); layout.addWidget(self.tabs, 1)
        self._build_request(); self._build_history()
        self.message = QLabel("A draft may retain unfinished entries. Scoped complete delivery is an engineering completeness claim, not annotation accuracy or biological truth.")
        self.message.setWordWrap(True); layout.addWidget(self.message)
        self.footer = QGridLayout(); layout.addLayout(self.footer)
        self.save_draft_button = QPushButton("Save form draft"); self.save_draft_button.clicked.connect(lambda: self._guard(self.save_draft))
        self.restore_draft_button = QPushButton("Restore form draft"); self.restore_draft_button.clicked.connect(lambda: self._guard(self.restore_draft))
        self.refresh_button = QPushButton("Refresh"); self.refresh_button.clicked.connect(lambda: self._guard(lambda: self.refresh(force=True)))
        self.close_button = QPushButton("Close"); self.close_button.clicked.connect(self.reject)
        self.footer_buttons = (self.save_draft_button, self.restore_draft_button, self.refresh_button, self.close_button)
        for button in self.footer_buttons:
            button.setMinimumWidth(0); button.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self._layout_footer()
        self.timer = QTimer(self); self.timer.setInterval(350); self.timer.timeout.connect(lambda: self._guard(self.refresh))
        self.finished.connect(lambda _result: self.stop())
        if hasattr(getattr(owner, "operationFailed", None), "connect"): owner.operationFailed.connect(self._operation_failed)
        self.refresh(force=True)

    def _page(self, title):
        page = QWidget(); layout = QVBoxLayout(page)
        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        body = QWidget(); inner = QVBoxLayout(body); scroll.setWidget(body)
        layout.addWidget(scroll, 1); self.tabs.addTab(page, title)
        return layout, inner

    def _build_request(self):
        layout, inner = self._page("Scope & request")
        self.scope_text = QLabel(); self.scope_text.setWordWrap(True); inner.addWidget(self.scope_text)
        self.counts = _table(["Current scope / task count", "Value"]); self.counts.setMinimumHeight(150); inner.addWidget(self.counts)
        self.blockers = QPlainTextEdit(); self.blockers.setReadOnly(True); self.blockers.setMinimumHeight(120); inner.addWidget(self.blockers)
        self.scope_button = QPushButton("Open scope and completion")
        self.scope_button.clicked.connect(lambda: self._guard(lambda: self._request_workspace(self.scopeRequested))); inner.addWidget(self.scope_button)
        form = QFormLayout(); inner.addLayout(form)
        self.mode = QComboBox(); self.mode.addItem("Choose delivery mode explicitly…", CHOOSE)
        self.mode.addItem("Draft: retain unfinished entries and limits", "draft")
        self.mode.addItem("Complete: only the declared current scope", "complete")
        self.mode.currentIndexChanged.connect(self._input_changed); form.addRow("Mode", self.mode)
        self.assessment = QComboBox(); self.assessment.currentIndexChanged.connect(self._input_changed)
        self.assessment.currentIndexChanged.connect(self._assessment_choice_changed)
        form.addRow("Assessment report", self.assessment)
        self.assessments = _table(["Assessment start ID", "Created at", "Status", "Eligible for complete", "Current / historical reasons"])
        self.assessments.itemSelectionChanged.connect(self._assessment_row_selected)
        self.assessments.setMinimumHeight(140); inner.addWidget(self.assessments)
        self.assessment_button = QPushButton("Open source support and reports")
        self.assessment_button.clicked.connect(lambda: self._guard(lambda: self._request_workspace(self.assessmentRequested))); inner.addWidget(self.assessment_button)
        self.purpose, self.reason = QPlainTextEdit(), QPlainTextEdit()
        for field in (self.purpose, self.reason): field.setMaximumHeight(80); field.textChanged.connect(self._input_changed)
        form.addRow("Actual delivery purpose", self.purpose); form.addRow("Actual reason / basis", self.reason)
        note = QLabel("Purpose and reason are saved verbatim as two labelled sections in the existing service reason field (combined maximum 4000 characters). This form does not edit the saved delivery scope. Packages contain chosen examples, not every parent recording or a full project backup.")
        note.setWordWrap(True); inner.addWidget(note)
        self.scope_confirm = QCheckBox("I reviewed the displayed scope, counts, unreviewed, unresolved and excluded entries")
        self.confirmation = QCheckBox("I explicitly request this delivery mode, assessment report, purpose and stated limitations")
        self.scope_confirm.toggled.connect(self._buttons); self.confirmation.toggled.connect(self._buttons)
        inner.addWidget(self.scope_confirm); inner.addWidget(self.confirmation)
        self.start_button = QPushButton("Generate delivery")
        self.start_button.clicked.connect(lambda: self._guard(self.start_export)); layout.addWidget(self.start_button)

    def _build_history(self):
        layout, inner = self._page("Versions & files")
        self.jobs = _table(["Delivery ID (start order)", "Created at", "Mode", "Status", "Assessment selected", "Applicability / error"])
        self.jobs.setMinimumHeight(160); self.jobs.itemSelectionChanged.connect(self._job_selected); inner.addWidget(self.jobs)
        self.selected_status = QLabel(); self.selected_status.setWordWrap(True); inner.addWidget(self.selected_status)
        self.details = QPlainTextEdit(); self.details.setReadOnly(True); self.details.setMinimumHeight(180); inner.addWidget(self.details)
        self.cancel_reason = QLineEdit(); self.cancel_reason.setPlaceholderText("Actual reason to cancel the selected active delivery")
        self.cancel_reason.textChanged.connect(self._changed); inner.addWidget(self.cancel_reason)
        self.cancel_button = QPushButton("Cancel selected delivery")
        self.cancel_button.clicked.connect(lambda: self._guard(self.cancel_selected)); layout.addWidget(self.cancel_button)
        self.save_package_button = QPushButton("Save ZIP + manifest…")
        self.save_package_button.setToolTip("Save both verified original files atomically to a new output folder.")
        self.save_package_button.clicked.connect(lambda: self._guard(self.save_package)); layout.addWidget(self.save_package_button)
        self.preview_button = QPushButton("Open offline HTML")
        self.preview_button.setToolTip("Verify and open this package's local HTML, styles and chosen example audio.")
        self.preview_button.clicked.connect(lambda: self._guard(self.preview_selected)); layout.addWidget(self.preview_button)

    def is_current(self):
        return (self.owner.project is self.project and self.owner._close_coordinator.generation == self.generation
                and self.owner._close_coordinator.state == "open" and self._invalid is None)

    def _guard(self, function):
        try: return function()
        except Exception as exc:
            text = ERROR_TEXT.get(getattr(exc, "code", None), system_error(exc))
            self.message.setText(str(getattr(exc, "code", type(exc).__name__)) + ": " + text)
            self.owner._error(exc, operation="catalogue delivery")

    def _operation_failed(self, message):
        if self._waiting and self.is_current():
            self._waiting = False
            self.message.setText(system_text(message))
            self._buttons()

    def _changed(self, *_args):
        if not self._loading: self._dirty = True
        self._buttons()

    def _input_changed(self, *_args):
        if not self._loading:
            self.confirmation.setChecked(False); self._dirty = True
        self._buttons()

    def _selected(self):
        return next((job for job in (self._view or {}).get("jobs", []) if job["job_id"] == self._selected_job), None)

    def _assessment_row_selected(self):
        if self._loading: return
        row = self.assessments.currentRow()
        if row >= 0 and self.assessments.item(row, 0):
            self.assessment.setCurrentIndex(max(0, self.assessment.findData(self.assessments.item(row, 0).text())))

    def _assessment_choice_changed(self, *_args):
        if self._loading or not hasattr(self, "assessments"): return
        selected = self.assessment.currentData(); self._loading = True
        try:
            row = next((i for i in range(self.assessments.rowCount()) if self.assessments.item(i, 0).text() == selected), None)
            if row is None:
                self.assessments.clearSelection(); self.assessments.setCurrentCell(-1, -1)
            else: self.assessments.selectRow(row)
        finally: self._loading = False

    def _job_selected(self):
        if self._loading: return
        row = self.jobs.currentRow()
        self._selected_job = self.jobs.item(row, 0).text() if row >= 0 and self.jobs.item(row, 0) else None
        self._dirty = True; self._render_selected(); self._buttons()

    def _buttons(self, *_args):
        if not hasattr(self, "preview_button"): return
        available = self.is_current() and not self.owner._busy and not self._waiting
        self.tabs.setEnabled(available)
        if hasattr(self, "save_draft_button"):
            self.save_draft_button.setEnabled(available); self.restore_draft_button.setEnabled(available)
        mode, assessment = self.mode.currentData(), self.assessment.currentData()
        option = next((row for row in (self._view or {}).get("assessment_options", []) if row["job_id"] == assessment), None)
        eligible = ((mode == "draft" and bool((self._view or {}).get("can_export_draft"))) or
                    (mode == "complete" and bool((self._view or {}).get("can_export_complete")) and option is not None and option["eligible_for_complete"]))
        self.start_button.setEnabled(available and eligible and assessment != CHOOSE and self.scope_confirm.isChecked()
            and self.confirmation.isChecked() and bool(self.purpose.toPlainText().strip()) and bool(self.reason.toPlainText().strip()))
        job = self._selected()
        self.cancel_button.setEnabled(available and job is not None and job["status"] in ACTIVE
            and not job.get("cancel_requested") and bool(self.cancel_reason.text().strip()))
        published = available and job is not None and job["status"] == "completed"
        self.save_package_button.setEnabled(published); self.preview_button.setEnabled(published)

    def refresh(self, force=False):
        if not self.is_current(): self.invalidate("Project changed or is closing. Reopen catalogue delivery and explicitly review restored input."); return
        if self.owner._busy and not force: self._buttons(); return
        state = self.owner.state if getattr(self.owner, "state", None) is not None else self.project.snapshot()
        old = self._view; self._view = self.controller.view(state); self.controller.rebase(state)
        if old is None or old["revision"] != self._view["revision"] or old.get("current_fingerprint") != self._view.get("current_fingerprint"):
            self.scope_confirm.setChecked(False); self.confirmation.setChecked(False)
        scope, counts = self._view.get("scope", {}), self._view.get("counts", {})
        self.status.setText(f"Current scope completion: {self._view.get('completion_id') or 'not confirmed'} · Current delivery jobs: {len(self._view.get('jobs', []))}\nDraft and scoped complete delivery remain distinct; only completed jobs offer files.")
        self.scope_text.setText(("Saved delivery scope: " + str(scope.get("scope_id")) + " · target " + str(scope.get("target_level")) + " · purpose: " + str(scope.get("purpose"))
            if scope.get("declared") else "No delivery scope is declared. A draft uses the service's provisional all-task scope; this is not human scoped completion.")
            + f"\nEffective scope {len(scope.get('scope_ids', []))}/{scope.get('total_task_items', 0)} members. Outside-scope entries remain separately identified in the audit.")
        self.blockers.setPlainText(json.dumps(_display({"complete_delivery_blockers": self._view.get("blocking_reasons", []),
            "human_completion_missing": self._view.get("completion_missing", []),
            "note": "Drafts retain missing entries. Complete delivery requires current human completion and your explicit eligible assessment report; not-estimable reports retain limitations."}), ensure_ascii=False, indent=2))
        self._loading = True; _rows(self.counts, list(counts.items()))
        choice = self.assessment.currentData() if self.assessment.count() else CHOOSE
        self.assessment.clear(); self.assessment.addItem("Choose an assessment report…", CHOOSE)
        self.assessment.addItem("No assessment report (draft only)", None)
        options = self._view.get("assessment_options", [])
        for row in options:
            self.assessment.addItem(row["job_id"] + " · " + row["status"] + (" · historical / stale" if row["stale_for_current"] else " · current"), row["job_id"])
        self.assessment.setCurrentIndex(max(0, self.assessment.findData(choice)))
        _rows(self.assessments, [(row["job_id"], row["created_at"], row["status"], row["eligible_for_complete"],
            ", ".join(row.get("stale_reasons", [])) if row["stale_for_current"] else "Current complete report" if row["eligible_for_complete"] else "No complete assessment report") for row in options])
        jobs = self._view.get("jobs", [])
        if old is None: self._selected_job = self._view.get("latest_job_id")
        _rows(self.jobs, [(row["job_id"], row["created_at"], row.get("delivery_mode") or "Not recorded", row["status"] + (" / cancellation requested" if row.get("cancel_requested") else ""),
            row.get("assessment_job_id"), ("Historical / stale: " + ", ".join(row.get("stale_reasons", [])) if row.get("stale_for_current") else "Current frozen version")
            + (" · " + json.dumps(_display(row["error"]), ensure_ascii=False) if row.get("error") else "")) for row in jobs])
        for i, row in enumerate(jobs):
            if row["job_id"] == self._selected_job: self.jobs.selectRow(i)
        self._loading = False; self._assessment_choice_changed(); self._render_selected(); self._buttons()

    def _render_selected(self):
        job = self._selected()
        if job is None:
            self.selected_status.setText("Select a persisted delivery start. No older success is substituted.")
            self.details.setPlainText("No delivery selected."); return
        self.selected_status.setText(job["job_id"] + " · " + (job.get("delivery_mode") or "Delivery mode not recorded") + " · " + job["status"] + " · "
            + ("Historical / stale: " + ", ".join(job.get("stale_reasons", [])) if job.get("stale_for_current") else "Current frozen version")
            + "\nProgress: " + json.dumps(_display(job.get("progress", {})), ensure_ascii=False)
            + ("\nNo complete package for this run. Older completed versions remain separately selectable history." if job["status"] != "completed" else
               "\nSave verifies registered ZIP and manifest. Offline preview verifies and extracts this same published version; it does not use the HTTP report URL."))
        self.details.setPlainText(json.dumps(_display({"selected_delivery": job,
            "interpretation": "The selected frozen version is historical if current scope, evidence, source or definitions changed. A successful draft is still a draft. Chosen examples are not a full source-data or project backup.",
            "scientific_validity_certified": False}), ensure_ascii=False, indent=2))

    def _run(self, label, function, *, result_kind="command"):
        _require(self.is_current() and not self.owner._busy and not self._waiting, "EXPORT_CONTEXT_CHANGED", "Wait for the current open owner before another delivery action.")
        epoch = self._epoch; self._waiting = True; self._buttons()
        def done(result):
            if not self.is_current() or epoch != self._epoch: return
            self._waiting = False; self.owner.state = self.project.snapshot()
            if result_kind == "command" and (result.get("record") or {}).get("job_id"):
                self._selected_job = result["record"]["job_id"]
            self.refresh(force=True); self.scope_confirm.setChecked(False); self.confirmation.setChecked(False)
            if result_kind == "preview":
                opened = QDesktopServices.openUrl(QUrl.fromLocalFile(str(result["html_path"])))
                _require(opened, "EXPORT_PREVIEW_OPEN", "Verified offline files are ready, but the desktop could not open the HTML application.")
                self.previewOpened.emit(result); self.message.setText("Opened the verified offline package version and its local examples.")
            elif result_kind == "save":
                self.saved.emit(result); self.message.setText("ZIP and manifest saved together to the new verified output folder.")
            else:
                self.changed.emit(result); self.message.setText("Original exact request replayed; no duplicate delivery was started." if result.get("replayed") else
                    "Shared service receipt saved. Queued or running jobs do not yet offer a complete package.")
        try: self.owner._run(label, function, done)
        except Exception: self._waiting = False; self._buttons(); raise

    def start_export(self):
        mode, selected = self.mode.currentData(), self.assessment.currentData()
        _require(selected != CHOOSE, "EXPORT_ASSESSMENT_SELECTION", "Choose an assessment run or explicitly choose no report for a draft.")
        purpose, reason = self.purpose.toPlainText(), self.reason.toPlainText()
        ticket = copy.deepcopy(self._view["ticket"])
        confirmed, scope_confirmed = self.confirmation.isChecked(), self.scope_confirm.isChecked()
        self.save_draft()
        self._run("Generate catalogue delivery", lambda _progress, _cancelled:
            self.controller.start(mode, selected, purpose, reason, confirmed=confirmed, scope_confirmed=scope_confirmed, ticket=ticket))

    def cancel_selected(self):
        job = self._selected(); _require(job is not None and job["status"] in ACTIVE, "EXPORT_NOT_ACTIVE", "Select the exact active delivery to cancel.")
        reason, ticket = self.cancel_reason.text(), copy.deepcopy(self._view["ticket"])
        self.save_draft(); self._run("Cancel selected delivery", lambda _progress, _cancelled:
            self.controller.cancel(job["job_id"], reason, ticket=ticket))

    def _adapter(self):
        from .annotation_desktop_export_files import ExporterAdapter
        return ExporterAdapter(self.project)

    def save_package(self):
        _require(self.is_current() and not self.owner._busy, "EXPORT_CONTEXT_CHANGED", "Wait for the current open project.")
        job = self._selected(); _require(job is not None and job["status"] == "completed", "EXPORT_NOT_READY", "Select a completed package, including an explicitly selected historical version.")
        path, _filter = QFileDialog.getSaveFileName(self, "Choose a new folder for ZIP and manifest", job["job_id"], "New delivery folder (*)")
        if path:
            self.save_draft(); self._run("Save verified ZIP and manifest", lambda progress, cancelled:
                self._adapter().save_package(job["job_id"], path, cancelled=cancelled, progress=progress), result_kind="save")

    def preview_selected(self):
        _require(self.is_current() and not self.owner._busy, "EXPORT_CONTEXT_CHANGED", "Wait for the current open project.")
        job = self._selected(); _require(job is not None and job["status"] == "completed", "EXPORT_NOT_READY", "Select a completed version before opening its offline report.")
        self.save_draft(); self._run("Prepare verified offline report", lambda progress, cancelled:
            self._adapter().prepare_preview(job["job_id"], cancelled=cancelled, progress=progress), result_kind="preview")

    def _request_workspace(self, signal):
        _require(self.is_current() and not self.owner._busy, "EXPORT_CONTEXT_CHANGED", "Wait for the current open project.")
        self.save_draft(); signal.emit()

    def save_draft(self):
        if self._dirty:
            self.store.save({"mode": self.mode.currentData(), "assessment_job_id": self.assessment.currentData(),
                "purpose": self.purpose.toPlainText(), "reason": self.reason.toPlainText(), "cancel_reason": self.cancel_reason.text(),
                "selected_job_id": self._selected_job or ""}, (self._view or {}).get("current_fingerprint"))
            self._dirty = False
        return True

    def restore_draft(self):
        _require(self.is_current() and not self.owner._busy, "EXPORT_CONTEXT_CHANGED", "Restore only in the current open project.")
        value = self.store.load(); _require(value is not None, "EXPORT_DRAFT_MISSING", "No unsubmitted delivery form is stored.")
        form = value["form"]; self._loading = True
        self.mode.setCurrentIndex(max(0, self.mode.findData(form["mode"])))
        self.assessment.setCurrentIndex(max(0, self.assessment.findData(form["assessment_job_id"])))
        self.purpose.setPlainText(form["purpose"]); self.reason.setPlainText(form["reason"]); self.cancel_reason.setText(form["cancel_reason"])
        self._selected_job = form["selected_job_id"] or None; self._loading = False
        self.refresh(force=True); self.scope_confirm.setChecked(False); self.confirmation.setChecked(False); self._dirty = False
        self.message.setText("Unsubmitted delivery input restored, not a package or command. Review the scope and assessment selection again; all confirmations are cleared."
            + (" The saved scope/source/catalogue context changed." if value.get("context_fingerprint") != self._view.get("current_fingerprint") else ""))

    def invalidate(self, reason):
        self._invalid = reason; self._epoch += 1; self.scope_confirm.setChecked(False); self.confirmation.setChecked(False)
        self.stop(); self.message.setText(reason); self._buttons()

    def stop(self): self.timer.stop()

    def _layout_footer(self):
        columns = 2 if self.width() < 760 else 4
        if columns == self._footer_columns: return
        self._footer_columns = columns
        for button in self.footer_buttons: self.footer.removeWidget(button)
        for i, button in enumerate(self.footer_buttons): self.footer.addWidget(button, i // columns, i % columns)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "footer_buttons"): self._layout_footer()

    def showEvent(self, event):
        super().showEvent(event)
        if hasattr(self, "timer") and self.is_current():
            self._waiting = False; self.refresh(force=not self.owner._busy); self.timer.start()

    def reject(self):
        try: self.save_draft()
        except Exception as exc: self._guard(lambda: (_ for _ in ()).throw(exc)); return
        self._epoch += 1; self._waiting = False; self.stop(); super().reject()

    def closeEvent(self, event):
        try: self.save_draft()
        except Exception as exc: self._guard(lambda: (_ for _ in ()).throw(exc)); event.ignore(); return
        self._epoch += 1; self._waiting = False; self.stop(); super().closeEvent(event)
