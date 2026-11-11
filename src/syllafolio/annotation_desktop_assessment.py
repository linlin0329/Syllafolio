"""D3固定内部评估的桌面投影；报告和标签权威均保留在共同服务。"""
from __future__ import annotations
from .annotation_english import system_error, system_fields, system_text

import copy
import hashlib
import json
from pathlib import Path
import re
import threading
import uuid

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog,
    QFileDialog, QFormLayout, QGridLayout, QLabel, QLineEdit, QPlainTextEdit,
    QPushButton, QScrollArea, QTabWidget, QTableWidget, QTableWidgetItem,
    QSizePolicy, QVBoxLayout, QWidget)

from .annotation_assistant_contracts import canonical_hash
from .annotation_desktop_review_media import _checked_path

ACTIVE = {"queued", "running"}
REPORT_STATES = {"completed", "not_estimable"}
FILENAME = "desktop_assessment_draft.json"
SCHEMA = "syllafolio-desktop-assessment-draft-v1"
SOURCE_LIMITATION = ("Recording groups are operator declarations. The service checks known same-source and exact-PCM constraints; "
                    "it does not verify recording identity, independent birds or sessions.")
SERVICE_LIMITATIONS = [SOURCE_LIMITATION,
    "Only internal acoustic separability of the current human family labels within the declared recording groups is assessed; this does not establish label truth, individual independence or generalization.",
    "A person must choose a type-review question. No class changes, review reopening or parameter search occur automatically."]
REASON_TEXT = {
    "HUMAN_COMPLETION_REQUIRED": "Confirm the current scope and type-definition review before starting an assessment.",
    "ASSESSMENT_NOT_READY": "Resolve the displayed scope, definition and completion requirements before starting an assessment.",
    "ASSESSMENT_BUSY": "An assessment is already queued or running in this project.",
    "DEADLINE_LOCKED": "The task deadline is locked; new assessments are not accepted.",
    "RAW_FEATURES_UNAVAILABLE": "The fixed preset requires the raw 2048-dimensional log-mel input; a support report remains available.",
    "INSUFFICIENT_ITEMS": "Fewer than 10 confirmed items have a known recording source.",
    "INSUFFICIENT_SOURCE_GROUPS": "Fewer than three declared original recording groups support this family.",
    "INSUFFICIENT_FAMILIES": "Fewer than two families meet the fixed eligibility requirements.",
    "REVISION_CONFLICT": "The project changed. Refresh and explicitly review the current context before submitting again.",
    "PROJECT_DRAINING": "The project is closing. Unsubmitted text can still be saved as a local draft.",
    "PROCESS_INTERRUPTED": "The prior process ended without a complete report. Start again only by an explicit new request.",
    "CANCELLED": "Assessment cancelled; no incomplete score is published.",
}


def _display(value):
    return system_fields(value)



class AssessmentError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(f"{code}: {message}")


def _require(value, code, message):
    if not value: raise AssessmentError(code, message)


def _identity(project):
    return {"project_id": project.metadata["project_id"], "run_id": project.service.run_id,
            "input_fingerprint": project.dataset.input_fingerprint}


class AssessmentController:
    """只读投影、精确重试和注册资产导出；不接受数值参数或前端完成标志。"""
    def __init__(self, project):
        self.project = project
        self.requests, self.pending = {}, {}
        self.base_ticket = self.ticket()

    def ticket(self, state=None):
        state = self.project.snapshot() if state is None else state
        return {**_identity(self.project), "revision": state["revision"]}

    def view(self, state=None):
        state = self.project.snapshot() if state is None else state
        assessment = copy.deepcopy(state.get("final_review", {}).get("assessment", {}))
        registry = copy.deepcopy(state.get("source_registry", {}))
        sources = {row["item_id"]: row for row in registry.get("mappings", [])}
        scope = set((state.get("catalogue_scope") or {}).get("scope_ids", []))
        families = {row["local_type_id"]: row for row in state.get("memberships", []) if row["parent_type_id"] is None}
        assignments = state.get("batch_review", {}).get("assignments", {})
        items = []
        for row in state.get("items", []):
            ident = row["item_id"]
            family = next((fid for fid, member in families.items() if ident in member["member_ids"]), None)
            disposition = row.get("batch_disposition", assignments.get(ident, {}).get("disposition"))
            status = ("excluded" if disposition == "discard" else "confirmed" if family is not None
                      and row.get("processing_state") in {"decided", "reviewed_unresolved"}
                      and not row.get("closure_confirmation_required") else "unresolved")
            items.append({"item_id": ident, "in_scope": ident in scope, "family_id": family,
                          "label_status": status, "processing_state": row.get("processing_state"),
                          "source_group_id": sources.get(ident, {}).get("raw_recording_id"),
                          "source_clip_id": sources.get(ident, {}).get("source_clip_id")})
        return {**assessment, "identity": _identity(self.project), "ticket": self.ticket(state),
                "revision": state["revision"], "source_registry": registry, "items": items,
                "current_family_ids": sorted(families), "scope": copy.deepcopy(state.get("catalogue_scope")),
                "current_completion_id": state.get("current_completion_id"),
                "scientific_validity_certified": False}

    def rebase(self, state=None):
        self.base_ticket = self.ticket(state)

    def _execute(self, action, payload, *, ticket=None, idempotency_key=None):
        ticket = copy.deepcopy(self.base_ticket if ticket is None else ticket)
        _require(all(ticket.get(key) == value for key, value in _identity(self.project).items()),
                 "ASSESSMENT_CONTEXT_CHANGED", "The command belongs to another project/run/input.")
        _require(idempotency_key is None or isinstance(idempotency_key, str) and 0 < len(idempotency_key) <= 160,
                 "IDEMPOTENCY_REQUIRED", "Use a nonempty exact request key.")
        request = {"action": action, "payload": copy.deepcopy(payload), "ticket": ticket}
        signature = canonical_hash(request)
        key = idempotency_key or self.pending.get(signature) or "desktop-assessment-" + uuid.uuid4().hex
        if key in self.requests:
            _require(self.requests[key] == request, "IDEMPOTENCY_CONFLICT", "Retry the original exact assessment request.")
        else:
            self.requests[key] = request; self.pending[signature] = key
        return self.project.execute(action, copy.deepcopy(payload), expected_revision=ticket["revision"], idempotency_key=key)

    def start(self, reason, *, confirmed, **kwargs):
        _require(confirmed is True, "ASSESSMENT_CONFIRMATION", "Explicitly confirm this fixed internal assessment.")
        _require(isinstance(reason, str) and bool(reason.strip()), "ASSESSMENT_REASON", "Enter the actual reason for this run.")
        return self._execute("assessment_start", {"reason": reason}, **kwargs)

    def cancel(self, job_id, reason, **kwargs):
        _require(isinstance(reason, str) and bool(reason.strip()), "ASSESSMENT_REASON", "Enter the actual cancellation reason.")
        _require(isinstance(job_id, str) and bool(job_id), "ASSESSMENT_NOT_FOUND", "Select the exact assessment job.")
        return self._execute("assessment_cancel", {"job_id": job_id, "reason": reason}, **kwargs)

    def job(self, job_id, state=None):
        found = next((row for row in self.view(state).get("jobs", []) if row["job_id"] == job_id), None)
        _require(found is not None, "ASSESSMENT_NOT_FOUND", "Select a persisted assessment start from this project.")
        return found

    def report_bytes(self, job_id):
        job = self.job(job_id)
        _require(job["status"] in REPORT_STATES and job.get("report") is not None,
                 "ASSESSMENT_NO_REPORT", "This run has no complete report; an older success is not substituted.")
        url, digest = job.get("report_url"), job.get("report_sha256")
        _require(isinstance(url, str) and re.fullmatch(r"/api/media/asset-[a-f0-9]{24}", url)
                 and isinstance(digest, str) and re.fullmatch(r"[a-f0-9]{64}", digest),
                 "ASSESSMENT_REPORT_ASSET", "The selected report lacks a registered, hashed project asset.")
        # DesktopProject performs the same-owner asset allow-list/path validation.
        path = self.project.media_path(url)
        raw = path.read_bytes()
        _require(hashlib.sha256(raw).hexdigest() == digest, "ASSESSMENT_REPORT_HASH", "The registered report bytes changed.")
        document = json.loads(raw.decode("utf-8"))
        _require(document.get("schema_version") == "annotation-label-assessment-report-v1"
                 and document.get("job_id") == job_id
                 and document.get("snapshot_fingerprint") == job.get("snapshot_fingerprint")
                 and document.get("frozen_snapshot", {}).get("input_fingerprint") == self.project.dataset.input_fingerprint
                 and document.get("result", {}).get("status") == job["status"],
                 "ASSESSMENT_REPORT_IDENTITY", "The report does not match this selected run and input.")
        return raw

    def save_report(self, job_id, destination, *, overwrite=False):
        raw = self.report_bytes(job_id)
        destination = Path(destination)
        _require(destination.suffix.lower() == ".json" and destination.parent.is_dir(),
                 "ASSESSMENT_EXPORT_PATH", "Choose a JSON file in an existing export directory.")
        _require(not destination.is_symlink() and not destination.resolve().is_relative_to(self.project.project_dir.resolve()),
                 "ASSESSMENT_EXPORT_PATH", "Export outside the project so frozen inputs and owned assets remain protected.")
        _require(overwrite or not destination.exists(), "ASSESSMENT_EXPORT_EXISTS", "Explicitly confirm replacing the selected export file.")
        temporary = destination.with_name(".assessment-export-" + uuid.uuid4().hex + ".tmp")
        try:
            with temporary.open("xb") as stream: stream.write(raw)
            _require(not destination.is_symlink() and (overwrite or not destination.exists()),
                     "ASSESSMENT_EXPORT_CHANGED", "The export destination changed before saving.")
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
        return {"job_id": job_id, "path": str(destination.resolve()), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}

    def directed_selection(self, job_id, pair_index, family_id, question, *, confirmed=False, state=None):
        view = self.view(state); job = self.job(job_id, state)
        pairs = (job.get("report", {}).get("confusion") or {}).get("top_pairs", [])
        _require(job["status"] == "completed" and type(pair_index) is int and 0 <= pair_index < len(pairs),
                 "ASSESSMENT_CONFUSION_SELECTION", "Select a real confusion row in a complete report.")
        pair = pairs[pair_index]; affected = [pair["actual_family_id"], pair["predicted_family_id"]]
        _require(family_id in affected and family_id in view["current_family_ids"]
                 and all(ident in view["current_family_ids"] for ident in affected),
                 "ASSESSMENT_FAMILY_SELECTION", "Explicitly choose a still-existing affected family; inspect changed historical types first.")
        _require(isinstance(question, str) and bool(question.strip()), "ASSESSMENT_QUESTION", "Write the actual human review question.")
        _require(confirmed is True, "ASSESSMENT_DIAGNOSTIC_CONFIRMATION", "Review this report's current or historical applicability before prefilling a type-review question.")
        return {"family_id": family_id, "affected_type_ids": affected, "question": question,
                "assessment_job_id": job_id,
                "reason": "Human-selected assessment diagnostic from " + job_id + (" (historical/stale: " + ", ".join(job.get("stale_reasons", [])) + ")" if job.get("stale_for_current") else " (current frozen report)")}


class AssessmentDraftStore:
    """只有未提交文字；恢复永不恢复人工勾选或启动/取消命令。"""
    def __init__(self, project):
        self.project, self.path = project, project.project_dir / FILENAME
        if not hasattr(project.service, "_desktop_assessment_draft_lock"):
            project.service._desktop_assessment_draft_lock = threading.RLock()
        self.lock = project.service._desktop_assessment_draft_lock
        self.warning = None

    def load(self):
        with self.lock:
            _checked_path(self.project, self.path)
            if not self.path.exists(): return None
            raw = self.path.read_bytes()
            try:
                def pairs(rows):
                    value = {}
                    for key, item in rows:
                        _require(key not in value, "ASSESSMENT_DRAFT_INVALID", "Duplicate draft key.")
                        value[key] = item
                    return value
                value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs)
                _require(isinstance(value, dict) and value.get("schema_version") == SCHEMA
                         and all(value.get(key) == item for key, item in _identity(self.project).items())
                         and value.get("submission_state") == "unsubmitted"
                         and value.get("start_confirmed") is False and value.get("diagnostic_confirmed") is False
                         and isinstance(value.get("form"), dict) and set(value["form"]) == {"reason", "cancel_reason", "question", "selected_job_id"}
                         and all(isinstance(item, str) for item in value["form"].values()),
                         "ASSESSMENT_DRAFT_INVALID", "Draft schema, fields or project/run/input identity does not match.")
                return value
            except (ValueError, UnicodeError, TypeError) as exc:
                digest = hashlib.sha256(raw).hexdigest()
                backup = self.path.with_name("desktop_assessment_draft.recovery-" + digest[:16] + ".json")
                _checked_path(self.project, backup)
                if backup.exists(): _require(backup.read_bytes() == raw, "ASSESSMENT_DRAFT_BACKUP", "Recovery copy differs.")
                else:
                    with backup.open("xb") as stream: stream.write(raw)
                self.warning = "Unreadable draft retained unchanged in " + backup.name + ": " + system_error(exc)
                raise AssessmentError("ASSESSMENT_DRAFT_INVALID", self.warning) from exc

    def save(self, form, fingerprint):
        with self.lock:
            _checked_path(self.project, self.path)
            self.load()  # Preserve malformed originals; never replace them with a fabricated clean draft.
            before = hashlib.sha256(self.path.read_bytes()).hexdigest() if self.path.exists() else None
            value = {"schema_version": SCHEMA, **_identity(self.project), "submission_state": "unsubmitted",
                     "start_confirmed": False, "diagnostic_confirmed": False,
                     "context_fingerprint": fingerprint, "form": copy.deepcopy(form)}
            temporary = self.path.with_name(".assessment-draft-" + uuid.uuid4().hex + ".tmp")
            try:
                temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
                _checked_path(self.project, self.path)
                current = hashlib.sha256(self.path.read_bytes()).hexdigest() if self.path.exists() else None
                _require(current == before, "ASSESSMENT_DRAFT_CHANGED", "External draft changes were retained.")
                temporary.replace(self.path)
            finally: temporary.unlink(missing_ok=True)


def _table(headers):
    result = QTableWidget(0, len(headers)); result.setHorizontalHeaderLabels(headers)
    result.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    result.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    result.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    result.setMinimumWidth(0)
    return result


def _rows(table, rows):
    table.setRowCount(len(rows))
    for i, row in enumerate(rows):
        for j, value in enumerate(row): table.setItem(i, j, QTableWidgetItem(str(value if value is not None else "Unknown")))


class AssessmentDialog(QDialog):
    sourceRequested = Signal()
    directedRequested = Signal(object)
    assessmentChanged = Signal(object)
    reportSaved = Signal(object)

    def __init__(self, owner):
        super().__init__(owner)
        self.owner, self.project = owner, owner.project
        self.generation = owner._close_coordinator.generation
        self.controller, self.store = AssessmentController(self.project), AssessmentDraftStore(self.project)
        self._view, self._waiting, self._invalid, self._dirty, self._loading = None, False, None, False, False
        self._callback_generation = 0
        self._selected_job, self._render_key, self._footer_columns = None, None, None
        self.setWindowTitle("Source support and internal assessment")
        screen = owner.screen(); area = screen.availableGeometry() if screen else None
        self.resize(min(1080, area.width() - 32) if area else 1080, min(780, area.height() - 64) if area else 780)
        layout = QVBoxLayout(self)
        self.status = QLabel(); self.status.setWordWrap(True); layout.addWidget(self.status)
        self.tabs = QTabWidget(); self.tabs.setUsesScrollButtons(True); self.tabs.setElideMode(Qt.TextElideMode.ElideRight)
        self.tabs.tabBar().setMinimumWidth(0); layout.addWidget(self.tabs, 1)
        self._build_support(); self._build_jobs(); self._build_diagnostics()
        self.message = QLabel("Internal acoustic separability of current human family labels is not annotation accuracy or biological truth.")
        self.message.setWordWrap(True); layout.addWidget(self.message)
        self.footer = QGridLayout(); layout.addLayout(self.footer)
        self.save_button = QPushButton("Save draft"); self.save_button.clicked.connect(lambda: self._guard(self.save_draft))
        self.restore_button = QPushButton("Restore draft"); self.restore_button.clicked.connect(lambda: self._guard(self.restore_draft))
        self.refresh_button = QPushButton("Refresh"); self.refresh_button.clicked.connect(lambda: self._guard(lambda: self.refresh(force=True)))
        self.close_button = QPushButton("Close"); self.close_button.clicked.connect(self.reject)
        self.footer_buttons = (self.save_button, self.restore_button, self.refresh_button, self.close_button)
        # Do not let the previous four-column minimum prevent the resize event
        # that must switch a narrow window to two columns.
        for button in self.footer_buttons:
            button.setMinimumWidth(0)
            button.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self._layout_footer()
        self.timer = QTimer(self); self.timer.setInterval(350); self.timer.timeout.connect(lambda: self._guard(self.refresh))
        self.finished.connect(lambda _result: self.stop())
        if hasattr(getattr(owner, "operationFailed", None), "connect"): owner.operationFailed.connect(self._operation_failed)
        self.refresh(force=True)

    def _page(self, title):
        page = QWidget(); layout = QVBoxLayout(page)
        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        body = QWidget(); inner = QVBoxLayout(body); scroll.setWidget(body); layout.addWidget(scroll, 1)
        self.tabs.addTab(page, title)
        return layout, inner

    def _build_support(self):
        layout, inner = self._page("Support & eligibility")
        self.source_summary = QLabel(); self.source_summary.setWordWrap(True); inner.addWidget(self.source_summary)
        self.source_button = QPushButton("Open source declarations")
        self.source_button.clicked.connect(lambda: self._guard(self.request_sources)); inner.addWidget(self.source_button)
        self.coverage = _table(["Coverage / denominator", "Value"]); self.coverage.setMinimumHeight(150); inner.addWidget(self.coverage)
        self.support = _table(["Family", "Scope", "Confirmed", "Known source", "Unknown source", "Recordings", "Largest share", "Eligible", "Reasons / source counts"])
        self.support.setMinimumHeight(160); inner.addWidget(self.support)
        self.items = _table(["Stable item ID", "In scope", "Human family", "Label status", "Processing", "Recording declaration", "Parent clip"])
        self.items.setMinimumHeight(160); inner.addWidget(self.items)
        self.limits = QPlainTextEdit(); self.limits.setReadOnly(True); self.limits.setMinimumHeight(150); inner.addWidget(self.limits)
        form = QFormLayout(); inner.addLayout(form)
        self.reason = QPlainTextEdit(); self.reason.setMaximumHeight(80); self.reason.textChanged.connect(self._changed)
        form.addRow("Actual reason for this fixed assessment", self.reason)
        self.start_confirm = QCheckBox("I confirm the displayed scope, support, source limitations and fixed 15-fold preset")
        self.start_confirm.toggled.connect(self._buttons); inner.addWidget(self.start_confirm)
        self.start_button = QPushButton("Start fixed assessment")
        self.start_button.clicked.connect(lambda: self._guard(self.start_assessment)); layout.addWidget(self.start_button)

    def _build_jobs(self):
        layout, inner = self._page("Started runs & reports")
        self.jobs = _table(["Job ID (start order)", "Started / queued at", "State", "Applicability", "Progress", "Actual reason / error"])
        self.jobs.setMinimumHeight(160); self.jobs.itemSelectionChanged.connect(self._job_selected); inner.addWidget(self.jobs)
        self.report_status = QLabel(); self.report_status.setWordWrap(True); inner.addWidget(self.report_status)
        self.metrics = _table(["Fixed metric", "Mean", "SD"]); inner.addWidget(self.metrics)
        self.recalls = _table(["Human family", "Eligible N", "Recall mean", "Recall SD"]); inner.addWidget(self.recalls)
        self.matrix = _table(["True human family / predicted →"]); inner.addWidget(self.matrix)
        self.report_details = QPlainTextEdit(); self.report_details.setReadOnly(True); self.report_details.setMinimumHeight(180); inner.addWidget(self.report_details)
        self.cancel_reason = QLineEdit(); self.cancel_reason.setPlaceholderText("Actual cancellation reason for the selected job")
        self.cancel_reason.textChanged.connect(self._changed); inner.addWidget(self.cancel_reason)
        self.cancel_button = QPushButton("Cancel selected active run"); self.cancel_button.clicked.connect(lambda: self._guard(self.cancel_selected))
        self.export_button = QPushButton("Save selected complete JSON…"); self.export_button.clicked.connect(lambda: self._guard(self.export_selected))
        layout.addWidget(self.cancel_button); layout.addWidget(self.export_button)

    def _build_diagnostics(self):
        layout, inner = self._page("Type-review question")
        text = QLabel("Choose an actual confusion row and an affected family, then write a review question. This only prefills an unsubmitted type-review form. No review is reopened, no label changes and no assessment reruns automatically. Historical reports retain their stale reasons.")
        text.setWordWrap(True); inner.addWidget(text)
        self.pairs = _table(["Actual human family", "Predicted family", "Confusion mean", "SD"])
        self.pairs.setMinimumHeight(180); self.pairs.itemSelectionChanged.connect(self._pair_selected); inner.addWidget(self.pairs)
        self.family = QComboBox(); inner.addWidget(self.family)
        self.question = QPlainTextEdit(); self.question.setPlaceholderText("Actual human review question"); self.question.setMaximumHeight(120)
        self.question.textChanged.connect(self._changed); inner.addWidget(self.question)
        self.diagnostic_confirm = QCheckBox("I reviewed this selected current or historical report and chose these affected families")
        self.diagnostic_confirm.toggled.connect(self._buttons); inner.addWidget(self.diagnostic_confirm)
        self.family.currentIndexChanged.connect(lambda _index: self.diagnostic_confirm.setChecked(False))
        self.family.currentIndexChanged.connect(self._buttons)
        self.question.textChanged.connect(lambda: self.diagnostic_confirm.setChecked(False))
        self.directed_button = QPushButton("Prefill review question")
        self.directed_button.clicked.connect(lambda: self._guard(self.request_directed)); layout.addWidget(self.directed_button)

    def is_current(self):
        return (self.owner.project is self.project and self.owner._close_coordinator.generation == self.generation
                and self.owner._close_coordinator.state == "open" and self._invalid is None)

    def _guard(self, function):
        try: return function()
        except Exception as exc:
            text = REASON_TEXT.get(getattr(exc, "code", None), system_error(exc))
            self.message.setText(str(getattr(exc, "code", type(exc).__name__)) + ": " + text)
            self.owner._error(exc, operation="internal assessment")

    def _changed(self, *_args):
        if not self._loading: self._dirty = True
        self._buttons()

    def _operation_failed(self, message):
        if self._waiting and self.is_current():
            self._waiting = False
            self.message.setText(system_text(message))
            self._buttons()

    def _buttons(self, *_args):
        if not hasattr(self, "directed_button"): return
        available = self.is_current() and not self.owner._busy and not self._waiting
        self.tabs.setEnabled(available)
        self.save_button.setEnabled(available) if hasattr(self, "save_button") else None
        self.restore_button.setEnabled(available) if hasattr(self, "restore_button") else None
        self.source_button.setEnabled(available)
        self.start_button.setEnabled(available and bool((self._view or {}).get("can_start"))
                                     and self.start_confirm.isChecked() and bool(self.reason.toPlainText().strip()))
        job = self._selected()
        self.cancel_button.setEnabled(available and job is not None and job["status"] in ACTIVE
                                      and not job.get("cancel_requested") and bool(self.cancel_reason.text().strip()))
        self.export_button.setEnabled(available and job is not None and job["status"] in REPORT_STATES and bool(job.get("report_url")))
        self.directed_button.setEnabled(available and self.pairs.currentRow() >= 0 and self.family.currentData() is not None
                                        and self.diagnostic_confirm.isChecked() and bool(self.question.toPlainText().strip()))

    def _selected(self):
        return next((job for job in (self._view or {}).get("jobs", []) if job["job_id"] == self._selected_job), None)

    def _job_selected(self):
        if self._loading: return
        row = self.jobs.currentRow()
        self._selected_job = self.jobs.item(row, 0).text() if row >= 0 and self.jobs.item(row, 0) else None
        self._render_report(); self._dirty = True; self._buttons()

    def _pair_selected(self):
        if self._loading: return
        self.family.clear(); self.family.addItem("Choose affected family explicitly…", None)
        job = self._selected(); index = self.pairs.currentRow()
        pairs = ((job or {}).get("report", {}).get("confusion") or {}).get("top_pairs", [])
        if 0 <= index < len(pairs):
            for fid in (pairs[index]["actual_family_id"], pairs[index]["predicted_family_id"]):
                if fid in self._view["current_family_ids"]: self.family.addItem(fid, fid)
        self.diagnostic_confirm.setChecked(False); self._buttons()

    def refresh(self, force=False):
        if not self.is_current(): self.invalidate("Project changed or is closing; this assessment view cannot write another owner."); return
        if self.owner._busy and not force: self._buttons(); return
        state = self.owner.state if getattr(self.owner, "state", None) is not None else self.project.snapshot()
        old = self._view; self._view = self.controller.view(state); self.controller.rebase(state)
        if old is None or old["revision"] != self._view["revision"] or old.get("current_fingerprint") != self._view.get("current_fingerprint"):
            self.start_confirm.setChecked(False)
            if old is not None: self.diagnostic_confirm.setChecked(False)
        coverage, source = self._view.get("coverage", {}), self._view["source_registry"]
        self.status.setText(f"Current scoped completion: {self._view['current_completion_id'] or 'not confirmed'} · Eligible families {coverage.get('n_eligible_families', 0)} · Eligible items {coverage.get('n_eligible', 0)}\nFixed preset only: 3 source-grouped folds × 5 repeats. BA and macro-F1 are internal separability, not label accuracy.")
        self.source_summary.setText(f"Source declarations v{source.get('version', 0)} · {source.get('counts', {})}\n" + SOURCE_LIMITATION)
        self._loading = True
        _rows(self.coverage, [(key, value) for key, value in coverage.items()])
        _rows(self.support, [(row.get("family_id"), row.get("n_in_scope"), row.get("n_confirmed"), row.get("n_known_source"),
            row.get("n_unknown_source"), row.get("n_source_groups"), row.get("max_source_share"), row.get("eligible"),
            json.dumps(_display({"reasons": row.get("reasons"), "source_counts": row.get("source_counts"), "share_denominator": row.get("max_source_share_denominator")}), ensure_ascii=False))
            for row in self._view.get("support", [])])
        _rows(self.items, [(row["item_id"], row["in_scope"], row["family_id"], row["label_status"], row["processing_state"], row["source_group_id"], row["source_clip_id"]) for row in self._view["items"]])
        self.limits.setPlainText(json.dumps(_display({"fixed_preset": self._view.get("preset"), "blocking_reasons": self._view.get("blocking_reasons"),
            "numeric_input_reasons": self._view.get("numeric_input_reasons"), "limitations": SERVICE_LIMITATIONS}), ensure_ascii=False, indent=2))
        jobs = self._view.get("jobs", [])
        if old is None: self._selected_job = self._view.get("latest_job_id")
        _rows(self.jobs, [(row["job_id"], row.get("created_at"), row["status"] + (" / cancellation requested" if row.get("cancel_requested") else ""),
            "Historical / stale: " + ", ".join(row.get("stale_reasons", [])) if row.get("stale_for_current") else "Current frozen snapshot",
            json.dumps(_display(row.get("progress", {})), ensure_ascii=False), _display(row.get("error")) or row.get("reason")) for row in jobs])
        for i, row in enumerate(jobs):
            if row["job_id"] == self._selected_job: self.jobs.selectRow(i)
        self._loading = False
        # Always render progress, even when the state revision has not changed.
        self._render_report(); self._buttons()

    def _render_report(self):
        job = self._selected(); report = (job or {}).get("report")
        key = canonical_hash({"id": self._selected_job, "job": {key: value for key, value in (job or {}).items() if key != "progress"}})
        if job is None: self.report_status.setText("Select a persisted start. No previous report is substituted.")
        else:
            self.report_status.setText(f"Selected {job['job_id']} · {job['status']} · "
                + ("Historical / STALE: " + ", ".join(job.get("stale_reasons", [])) if job.get("stale_for_current") else "Current frozen snapshot")
                + "\nProgress: " + json.dumps(_display(job.get("progress", {})), ensure_ascii=False)
                + ("\nCancellation requested; waiting for a worker terminal state." if job.get("cancel_requested") and job["status"] in ACTIVE else "")
                + ("\nNo complete report for this run. Older successes remain separately selectable history." if report is None else ""))
        if key == self._render_key: return
        self._render_key = key
        metrics = (report or {}).get("metrics") or {}
        _rows(self.metrics, [(name, value.get("mean"), value.get("sd")) for name, value in metrics.items() if isinstance(value, dict)])
        _rows(self.recalls, [(row["family_id"], row["n"], row["recall_mean"], row["recall_sd"]) for row in (report or {}).get("per_family", [])])
        confusion = (report or {}).get("confusion") or {}; ids = confusion.get("family_ids", [])
        self.matrix.setColumnCount(1 + len(ids)); self.matrix.setHorizontalHeaderLabels(["True human family / predicted →", *ids])
        _rows(self.matrix, [(fid, *values) for fid, values in zip(ids, confusion.get("matrix_mean_row_normalized", []))])
        self._loading = True
        _rows(self.pairs, [(row["actual_family_id"], row["predicted_family_id"], row["rate_mean"], row["rate_sd"]) for row in confusion.get("top_pairs", [])])
        self.pairs.clearSelection(); self.pairs.setCurrentCell(-1, -1)
        self._loading = False; self._pair_selected()
        self.report_details.setPlainText(json.dumps(_display({"selected_job": {key: value for key, value in (job or {}).items() if key != "report"},
            "report": report, "full_fold_audit": "Save the registered complete JSON asset; polling view omits full fold preprocessing arrays.",
            "labels_unchanged": True, "scientific_validity_certified": False}), ensure_ascii=False, indent=2))

    def _run(self, label, function, *, saved_report=False):
        _require(self.is_current() and not self.owner._busy and not self._waiting,
                 "ASSESSMENT_CONTEXT_CHANGED", "Wait for the current open owner before another action.")
        self._waiting = True; self._buttons()
        callback_generation = self._callback_generation
        def done(result):
            if not self.is_current() or callback_generation != self._callback_generation: return
            self._waiting = False
            self.owner.state = self.project.snapshot()
            if not saved_report and (result.get("record") or {}).get("job_id"):
                self._selected_job = result["record"]["job_id"]
            self.refresh(force=True)
            self.start_confirm.setChecked(False); self.diagnostic_confirm.setChecked(False)
            self.message.setText("Saved selected complete JSON bytes and hash." if saved_report else
                "Original exact request replayed; no new run was created." if result.get("replayed") else
                "Shared service receipt saved. A running job is not a complete report.")
            (self.reportSaved if saved_report else self.assessmentChanged).emit(result)
        try: self.owner._run(label, lambda _progress, _cancelled: function(), done)
        except Exception: self._waiting = False; self._buttons(); raise

    def start_assessment(self):
        _require(self.start_confirm.isChecked(), "ASSESSMENT_CONFIRMATION", "Explicitly confirm the current support and fixed preset.")
        reason, ticket = self.reason.toPlainText(), copy.deepcopy(self._view["ticket"])
        self.save_draft()
        self._run("Start fixed assessment", lambda: self.controller.start(reason, confirmed=True, ticket=ticket))

    def cancel_selected(self):
        job, reason, ticket = self._selected(), self.cancel_reason.text(), copy.deepcopy(self._view["ticket"])
        _require(job is not None and job["status"] in ACTIVE, "ASSESSMENT_NOT_ACTIVE", "Select the exact active run to cancel.")
        self.save_draft(); self._run("Cancel selected assessment", lambda: self.controller.cancel(job["job_id"], reason, ticket=ticket))

    def export_selected(self):
        _require(self.is_current() and not self.owner._busy, "ASSESSMENT_CONTEXT_CHANGED", "Wait for the current open project.")
        job = self._selected(); _require(job is not None and job["status"] in REPORT_STATES, "ASSESSMENT_NO_REPORT", "Select a complete report.")
        path, _filter = QFileDialog.getSaveFileName(self, "Save selected full assessment JSON", job["job_id"] + ".json", "JSON report (*.json)")
        if path: self._run("Save selected assessment JSON", lambda: self.controller.save_report(job["job_id"], path, overwrite=True), saved_report=True)

    def request_sources(self):
        _require(self.is_current() and not self.owner._busy, "ASSESSMENT_CONTEXT_CHANGED", "Wait for the current open project.")
        self.save_draft(); self.sourceRequested.emit()

    def request_directed(self):
        _require(self.is_current() and not self.owner._busy, "ASSESSMENT_CONTEXT_CHANGED", "Wait for the current open project.")
        value = self.controller.directed_selection(self._selected_job, self.pairs.currentRow(), self.family.currentData(),
            self.question.toPlainText(), confirmed=self.diagnostic_confirm.isChecked())
        self.save_draft(); self.directedRequested.emit(value); self.diagnostic_confirm.setChecked(False)
        self.message.setText("Only an unsubmitted type-review question was requested. No review, label or assessment changed.")

    def save_draft(self):
        if self._dirty:
            self.store.save({"reason": self.reason.toPlainText(), "cancel_reason": self.cancel_reason.text(),
                "question": self.question.toPlainText(), "selected_job_id": self._selected_job or ""},
                (self._view or {}).get("current_fingerprint"))
            self._dirty = False
        return True

    def restore_draft(self):
        _require(self.is_current() and not self.owner._busy, "ASSESSMENT_CONTEXT_CHANGED", "Restore only in the current open owner.")
        value = self.store.load(); _require(value is not None, "ASSESSMENT_DRAFT_MISSING", "No unsubmitted assessment form is stored.")
        form = value["form"]; self._loading = True
        self.reason.setPlainText(form["reason"]); self.cancel_reason.setText(form["cancel_reason"]); self.question.setPlainText(form["question"])
        self._selected_job = form["selected_job_id"] or None; self._loading = False; self._render_key = None
        self.refresh(force=True); self.start_confirm.setChecked(False); self.diagnostic_confirm.setChecked(False)
        self._pair_selected(); self._dirty = False
        self.message.setText("Unsubmitted text restored; all confirmations and affected-family choices require new human review."
            + (" The saved catalogue/source context changed." if value.get("context_fingerprint") != self._view.get("current_fingerprint") else ""))

    def invalidate(self, reason):
        self._invalid = reason; self._callback_generation += 1
        self.start_confirm.setChecked(False); self.diagnostic_confirm.setChecked(False)
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
            self._waiting = False; self._render_key = None
            self.refresh(force=not self.owner._busy); self.timer.start()

    def reject(self):
        try: self.save_draft()
        except Exception as exc: self._guard(lambda: (_ for _ in ()).throw(exc)); return
        self._callback_generation += 1; self._waiting = False
        self.stop(); super().reject()

    def closeEvent(self, event):
        try: self.save_draft()
        except Exception as exc:
            self._guard(lambda: (_ for _ in ()).throw(exc)); event.ignore(); return
        self._callback_generation += 1; self._waiting = False
        self.stop(); super().closeEvent(event)
