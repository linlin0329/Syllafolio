"""D3固定内部评估的桌面投影；报告和标签权威均保留在共同服务。"""
from __future__ import annotations
from .annotation_english import system_error, system_fields, system_text

import copy
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import threading
import uuid

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog,
    QFileDialog, QFormLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPlainTextEdit,
    QPushButton, QScrollArea, QTabWidget, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget)

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
        assessment = state.get("final_review", {}).get("assessment", {})
        registry = copy.deepcopy(state.get("source_registry", {}))
        sources = {row["item_id"]: row for row in registry.get("mappings", [])}
        scope = set((state.get("catalogue_scope") or {}).get("scope_ids", []))
        families = {row["local_type_id"]: row for row in state.get("memberships", []) if row["parent_type_id"] is None}
        assignments = state.get("batch_review", {}).get("assignments", {})
        membership = {item: fid for fid, family in families.items() for item in family["member_ids"]}
        names = {row["family_id"]: row.get("title") or row["family_id"]
                 for row in state.get("batch_review", {}).get("families", [])}
        names.update({fid: card["title"] for fid, card in state.get("type_cards", {}).items()
                      if fid in families and card.get("title")})
        items = []
        for row in state.get("items", []):
            ident = row["item_id"]
            family = membership.get(ident)
            disposition = row.get("batch_disposition", assignments.get(ident, {}).get("disposition"))
            status = ("excluded" if disposition == "discard" else "confirmed" if family is not None
                      and row.get("processing_state") in {"decided", "reviewed_unresolved"}
                      and not row.get("closure_confirmation_required") else "unresolved")
            items.append({"item_id": ident, "in_scope": ident in scope, "family_id": family,
                          "label_status": status, "processing_state": row.get("processing_state"),
                          "source_group_id": sources.get(ident, {}).get("raw_recording_id"),
                          "source_clip_id": sources.get(ident, {}).get("source_clip_id")})
        provisional = None
        if not state.get("catalogue_scope"):
            from .annotation_label_assessment import summarize_support
            provisional = summarize_support([dict(row, in_scope=True) for row in items], sorted(families))
        return {**assessment, "identity": _identity(self.project), "ticket": self.ticket(state),
                "provisional_support": provisional,
                "revision": state["revision"], "source_registry": registry, "items": items,
                "current_family_ids": sorted(families), "family_names": names, "scope": copy.deepcopy(state.get("catalogue_scope")),
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

    def report_reader(self, job_id, state=None):
        """Bind a registered report now so the I/O worker needs no project owner."""
        job = self.job(job_id, state)
        _require(job["status"] in REPORT_STATES and job.get("report") is not None,
                 "ASSESSMENT_NO_REPORT", "This run has no complete report; an older success is not substituted.")
        url, digest = job.get("report_url"), job.get("report_sha256")
        _require(isinstance(url, str) and re.fullmatch(r"/api/media/asset-[a-f0-9]{24}", url)
                 and isinstance(digest, str) and re.fullmatch(r"[a-f0-9]{64}", digest),
                 "ASSESSMENT_REPORT_ASSET", "The selected report lacks a registered, hashed project asset.")
        # DesktopProject performs the same-owner asset allow-list/path validation.
        path = self.project.media_path(url)
        snapshot_id, status = job["snapshot_fingerprint"], job["status"]
        input_id = self.project.dataset.input_fingerprint

        def read():
            raw = path.read_bytes()
            _require(hashlib.sha256(raw).hexdigest() == digest, "ASSESSMENT_REPORT_HASH", "The registered report bytes changed.")
            document = json.loads(raw)
            _require(document.get("schema_version") == "annotation-label-assessment-report-v1"
                     and document.get("job_id") == job_id
                     and document.get("snapshot_fingerprint") == snapshot_id
                     and document.get("frozen_snapshot", {}).get("input_fingerprint") == input_id
                     and document.get("result", {}).get("status") == status,
                     "ASSESSMENT_REPORT_IDENTITY", "The report does not match this selected run and input.")
            return raw

        return read

    def report_bytes(self, job_id):
        return self.report_reader(job_id)()

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


def _label(text="", *, heading=False):
    widget = QLabel(text)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setWordWrap(True)
    if heading:
        font = widget.font()
        font.setBold(True)
        widget.setFont(font)
    return widget


def _table(headers, lines=5, *, stretch=(0,)):
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.verticalHeader().hide()
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    table.setMinimumWidth(0)
    table.setProperty("visibleRows", lines)
    header = table.horizontalHeader()
    header.setStretchLastSection(False)
    header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    for column in stretch:
        header.setSectionResizeMode(column, QHeaderView.ResizeMode.Stretch)
    return table


def _rows(table, rows):
    table.setRowCount(len(rows))
    for i, row in enumerate(rows):
        for j, value in enumerate(row):
            item = QTableWidgetItem(str(value))
            item.setToolTip(str(value))
            table.setItem(i, j, item)
    height = table.horizontalHeader().sizeHint().height() + 2 * table.frameWidth()
    height += max(1, min(table.rowCount(), table.property("visibleRows"))) * table.verticalHeader().defaultSectionSize()
    # Scroll bars take space on narrow/high-DPI displays.
    height += table.horizontalScrollBar().sizeHint().height() if table.horizontalScrollBar().maximum() else 0
    table.setFixedHeight(height)


def _timestamp(iso):
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is not None:
            return dt.astimezone().strftime("%Y-%m-%d %H:%M %z")
    except ValueError:
        pass
    return str(iso)


def _percent(value):
    return "—" if value is None else f"{value * 100:.1f}%"


def _coverage_text(c):
    return (f"{c.get('n_eligible', 0)} of {c.get('n_scope', 0)} syllables in scope eligible"
            f"  ·  {c.get('n_eligible_families', 0)} of {c.get('n_families', 0)} catalogue families eligible")


def _reason(row):
    return REASON_TEXT.get(row.get("code"), system_text(row.get("message") or row.get("code", "Unknown reason")))


def _status(job):
    return {"completed": "Scored", "not_estimable": "Support report", "running": "Running",
            "queued": "Queued", "failed": "Failed", "interrupted": "Interrupted",
            "cancelled": "Cancelled"}.get(job["status"], job["status"])


def _family_names(view):
    names = view["family_names"]
    counts = {}
    for name in names.values():
        counts[name] = counts.get(name, 0) + 1
    return {fid: name if counts[name] == 1 or name == fid else f"{name} ({fid})"
            for fid, name in names.items()}


def _support_rows(rows, names):
    result = []
    for row in rows:
        fid = row["family_id"]
        eligible = "Eligible" if row["eligible"] else "Outside scope" if not row["n_in_scope"] else "Ineligible"
        reasons = []
        if row["n_in_scope"]:
            for item in row["reasons"]:
                reasons.append({"INSUFFICIENT_ITEMS": "Needs 10 confirmed syllables with a known source",
                                "INSUFFICIENT_SOURCE_GROUPS": "Needs 3 original recordings"}.get(item["code"], _reason(item)))
        result.append([names.get(fid, fid), row["n_known_source"], row["n_source_groups"],
                       eligible, "; ".join(reasons) or "—"])
    return result


def _foldout(parent, text, *, when_opened=None):
    button = QPushButton(text)
    button.setCheckable(True)
    button.setAutoDefault(False)
    parent.addWidget(button, 0, Qt.AlignmentFlag.AlignLeft)
    panel = QWidget()
    inner = QVBoxLayout(panel)
    inner.setContentsMargins(0, 0, 0, 0)
    parent.addWidget(panel)
    panel.hide()
    button.toggled.connect(panel.setVisible)
    if when_opened is not None:
        button.toggled.connect(lambda opened: when_opened() if opened else None)
    return inner


class AssessmentDialog(QDialog):
    sourceRequested = Signal()
    finalChecksRequested = Signal()
    catalogueRequested = Signal(str)
    directedRequested = Signal(object)
    assessmentChanged = Signal(object)
    reportSaved = Signal(object)

    def __init__(self, owner):
        super().__init__(owner)
        self.owner, self.project = owner, owner.project
        self.generation = owner._close_coordinator.generation
        self.controller = AssessmentController(self.project)
        self.store = AssessmentDraftStore(self.project)
        self._view = None
        self._invalid = None
        self._waiting = self._dirty = self._loading = False
        self._callback_generation = 0
        self._selected_job = self._render_key = self._view_key = None
        self._follow_latest = True
        self.pair_dialog = self._pair_key = None
        self.setWindowTitle("Internal assessment")
        screen = owner.screen()
        area = screen.availableGeometry() if screen else None
        self.resize(min(930, area.width() - 36) if area else 930, min(700, area.height() - 72) if area else 700)
        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        self.tabs.setObjectName("assessmentPages")
        layout.addWidget(self.tabs, 1)
        self._build_support()
        self._build_results()
        self.message = _label()
        layout.addWidget(self.message)
        footer = QHBoxLayout()
        self.restore_button = self._button("Restore form draft…", "assessmentRestore", self.restore_draft)
        self.restore_button.setVisible(False)
        self.tabs.currentChanged.connect(lambda _: self._buttons())
        footer.addWidget(self.restore_button)
        footer.addStretch()
        self.close_button = QPushButton("Close")
        self.close_button.clicked.connect(self.reject)
        footer.addWidget(self.close_button)
        layout.addLayout(footer)
        self.timer = QTimer(self)
        self.timer.setInterval(400)
        self.timer.timeout.connect(lambda: self._guard(self.refresh))
        self.finished.connect(lambda _: self.stop())
        owner.operationFailed.connect(self._operation_failed)
        self.refresh(force=True)

    def _button(self, text, name, callback):
        button = QPushButton(text)
        button.setAutoDefault(False)
        button.setObjectName(name)
        button.clicked.connect(lambda _: self._guard(callback))
        return button

    def _page(self, title):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        inner = QVBoxLayout(body)
        scroll.setWidget(body)
        self.tabs.addTab(scroll, title)
        return inner

    def _build_support(self):
        layout = self._page("Preparation")
        self.status = _label(heading=True)
        self.status.setObjectName("assessmentReadiness")
        layout.addWidget(self.status)
        self.scope_text = _label()
        layout.addWidget(self.scope_text)
        self.coverage = _label()
        layout.addWidget(self.coverage)
        self.blockers = _label()
        layout.addWidget(self.blockers)
        actions = QHBoxLayout()
        self.scope_button = self._button("Open final checks…", "assessmentFinalChecks", self.request_final)
        self.source_button = self._button("Recording sources…", "assessmentSources", self.request_sources)
        actions.addWidget(self.scope_button)
        actions.addWidget(self.source_button)
        actions.addStretch()
        layout.addLayout(actions)
        layout.addWidget(_label("Family eligibility", heading=True))
        self.support = _table(["Family", "Known-source N", "Recordings", "Eligibility", "Reason"], lines=6, stretch=(0, 4))
        self.support.setObjectName("assessmentSupport")
        self.support.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.support.itemSelectionChanged.connect(self._support_selected)
        layout.addWidget(self.support)
        self.source_summary = _label()
        layout.addWidget(self.source_summary)
        self.run_button = self._button("Run assessment…", "assessmentRun", self._open_confirmation)
        layout.addWidget(self.run_button, 0, Qt.AlignmentFlag.AlignRight)
        self.run_panel = QWidget()
        confirm = QVBoxLayout(self.run_panel)
        confirm.setContentsMargins(0, 0, 0, 0)
        confirm.addWidget(_label("Confirm this run", heading=True))
        confirm.addWidget(_label("Five repeats of three-fold cross-validation keep each original recording together."))
        self.reason = QPlainTextEdit()
        self.reason.setObjectName("assessmentReason")
        self.reason.setPlaceholderText("Purpose of this assessment")
        self.reason.setMaximumHeight(58)
        self.reason.textChanged.connect(self._reason_changed)
        confirm.addWidget(self.reason)
        self.start_confirm = QCheckBox("I reviewed the scope, recording sources and eligibility for this run.")
        self.start_confirm.setObjectName("assessmentConfirm")
        self.start_confirm.toggled.connect(self._buttons)
        confirm.addWidget(self.start_confirm)
        row = QHBoxLayout()
        row.addStretch()
        self.dismiss_run_button = self._button("Back", "assessmentBack", self._hide_confirmation)
        row.addWidget(self.dismiss_run_button)
        self.start_button = self._button("Run assessment", "assessmentStart", self.start_assessment)
        row.addWidget(self.start_button)
        confirm.addLayout(row)
        layout.addWidget(self.run_panel)
        self.run_panel.hide()
        details = _foldout(layout, "Method and recording details…", when_opened=self._render_item_table)
        self.method_panel = details.parentWidget()
        details.addWidget(_label("Eligible families need 10 confirmed syllables with known sources across 3 original "
                                 "recordings. Scoring needs at least 2 eligible families and 5 feasible three-fold "
                                 "partitions. Separate recordings may contain the same bird."))
        details.addWidget(_label("The fixed preset fits scaling and PCA on each training fold, combines acoustic "
                                 "and log-mel features, then predicts held-out family labels with logistic regression."))
        self.items = _table(["Syllable", "Scope", "Human family", "Review outcome", "Original recording"], lines=5, stretch=(0, 2, 4))
        details.addWidget(self.items)
        layout.addStretch()

    def _build_results(self):
        layout = self._page("Results")
        choose = QHBoxLayout()
        choose.addWidget(_label("Assessment run"))
        self.report_selector = QComboBox()
        self.report_selector.setObjectName("assessmentRunHistory")
        self.report_selector.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.report_selector.currentIndexChanged.connect(self._job_selected)
        choose.addWidget(self.report_selector, 1)
        self.export_button = self._button("Export assessment…", "exportAssessment", self.export_selected)
        layout.addLayout(choose)
        exports = QHBoxLayout()
        exports.addWidget(self.export_button)
        self.catalogue_button = self._button("Export catalogue with this report…", "assessmentExportCatalogue",
                                           self.request_catalogue)
        exports.addWidget(self.catalogue_button)
        exports.addStretch()
        layout.addLayout(exports)
        self.report_status = _label(heading=True)
        self.report_status.setObjectName("assessmentReportStatus")
        layout.addWidget(self.report_status)
        self.result_coverage = _label()
        layout.addWidget(self.result_coverage)
        self.result_reason = _label()
        layout.addWidget(self.result_reason)
        self.cancel_panel = QWidget()
        row = QHBoxLayout(self.cancel_panel)
        row.setContentsMargins(0, 0, 0, 0)
        self.cancel_reason = QLineEdit()
        self.cancel_reason.setPlaceholderText("Reason for cancellation")
        self.cancel_reason.textChanged.connect(self._changed)
        row.addWidget(self.cancel_reason, 1)
        self.cancel_button = self._button("Cancel run", "assessmentCancel", self.cancel_selected)
        row.addWidget(self.cancel_button)
        layout.addWidget(self.cancel_panel)
        self.scored_panel = QWidget()
        scored = QVBoxLayout(self.scored_panel)
        scored.setContentsMargins(0, 0, 0, 0)
        self.metrics = _label()
        self.metrics.setObjectName("assessmentMetrics")
        scored.addWidget(self.metrics)
        self.pair_heading = _label("Where families were confused", heading=True)
        scored.addWidget(self.pair_heading)
        self.pair_hint = _label()
        scored.addWidget(self.pair_hint)
        self.pairs = _table(["Human family", "Predicted family", "Mean confusion rate", "SD (pp)"], lines=5, stretch=(0, 1))
        self.pairs.setObjectName("assessmentConfusionPairs")
        self.pairs.itemSelectionChanged.connect(self._pair_selected)
        self.pairs.cellClicked.connect(lambda row, _: self._guard(lambda: self._listen_pair(row)))
        self.pairs.cellActivated.connect(lambda row, _: self._guard(lambda: self._listen_pair(row)))
        scored.addWidget(self.pairs)
        actions = QHBoxLayout()
        self.listen_button = self._button("Inspect examples…", "assessmentListen", self.inspect_selected)
        self.question_button = self._button("Create review question…", "assessmentQuestion", self._open_question)
        actions.addWidget(self.listen_button)
        actions.addWidget(self.question_button)
        actions.addStretch()
        scored.addLayout(actions)
        self.question_panel = QWidget()
        question = QFormLayout(self.question_panel)
        self.family = QComboBox()
        question.addRow("Family to review", self.family)
        self.question = QPlainTextEdit()
        self.question.setPlaceholderText("What would you like to check while listening?")
        self.question.setMaximumHeight(60)
        self.question.textChanged.connect(self._changed)
        question.addRow("Question", self.question)
        self.diagnostic_confirm = QCheckBox("I reviewed this report and the selected families.")
        self.diagnostic_confirm.toggled.connect(self._buttons)
        self.family.currentIndexChanged.connect(lambda _: self.diagnostic_confirm.setChecked(False))
        self.question.textChanged.connect(lambda: self.diagnostic_confirm.setChecked(False))
        question.addRow(self.diagnostic_confirm)
        self.directed_button = self._button("Open question in Type definitions", "assessmentSendQuestion", self.request_directed)
        question.addRow(self.directed_button)
        scored.addWidget(self.question_panel)
        self.question_panel.hide()
        scored.addWidget(_label("Recall by family", heading=True))
        self.recalls = _table(["Human family", "Scored syllables", "Mean recall", "SD (pp)"], lines=4)
        scored.addWidget(self.recalls)
        matrix_layout = _foldout(scored, "Full confusion matrix…")
        matrix_layout.addWidget(_label("Rows are human families. Columns are predictions. Each row sums to 100% before rounding."))
        self.matrix = _table(["Human / predicted"], stretch=())
        matrix_layout.addWidget(self.matrix)
        layout.addWidget(self.scored_panel)
        self.report_support_heading = _label("Support in this report", heading=True)
        self.report_support = _table(["Family", "Known-source N", "Recordings", "Eligibility", "Reason"], stretch=(0, 4))
        layout.addWidget(self.report_support_heading)
        layout.addWidget(self.report_support)
        self.run_details = _label()
        details = _foldout(layout, "Run details…")
        details.addWidget(self.run_details)
        self.save_json_button = self._button("Save full assessment JSON…", "assessmentRawJSON", self.save_json)
        details.addWidget(self.save_json_button, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addStretch()
    def is_current(self):
        return (self.owner.project is self.project and self.owner._close_coordinator.generation == self.generation
                and self.owner._close_coordinator.state == "open" and self._invalid is None)

    def _guard(self, callback):
        try:
            return callback()
        except Exception as exc:
            self.message.setText(REASON_TEXT.get(getattr(exc, "code", None), system_error(exc)))
            self.owner._error(exc, operation="internal assessment")
            self._buttons()

    def _changed(self, *_):
        if not self._loading:
            self._dirty = True
        self._buttons()

    def _reason_changed(self):
        self.start_confirm.setChecked(False)
        self._changed()

    def _operation_failed(self, message):
        if self._waiting and self.is_current():
            self._waiting = False
            self.message.setText(system_text(message))
            self._buttons()

    def _selected(self):
        jobs = (self._view or {}).get("jobs", [])
        return next((job for job in jobs if job["job_id"] == self._selected_job), None)

    def _pairs(self):
        report = (self._selected() or {}).get("report") or {}
        return (report.get("confusion") or {}).get("top_pairs", [])

    def _buttons(self, *_):
        if not self._view:
            return
        available = self.is_current() and not self.owner._busy and not self._waiting
        self.tabs.setEnabled(available)
        self.restore_button.setEnabled(available)
        self.restore_button.setVisible(self.store.path.exists() and
            (self.run_panel.isVisible() or self.question_panel.isVisible() or self.cancel_panel.isVisible()))
        can_start = bool(self._view.get("can_start"))
        self.run_button.setEnabled(can_start)
        self.start_button.setEnabled(can_start and self.start_confirm.isChecked()
                                     and bool(self.reason.toPlainText().strip()))
        job = self._selected()
        ready = bool(job and job["status"] in REPORT_STATES and job.get("report_url"))
        self.export_button.setEnabled(ready)
        self.catalogue_button.setEnabled(ready and not job.get("stale_for_current"))
        self.catalogue_button.setToolTip("Use this report in a catalogue export." if ready and not job.get("stale_for_current")
                                        else "Select a completed report matching the current scope.")
        self.save_json_button.setEnabled(ready)
        selected = 0 <= self.pairs.currentRow() < len(self._pairs())
        self.listen_button.setEnabled(selected and bool(job) and not job.get("stale_for_current"))
        self.question_button.setEnabled(selected and self.family.count() > 1)
        self.directed_button.setEnabled(self.diagnostic_confirm.isChecked() and selected
                                       and self.family.currentData() is not None and bool(self.question.toPlainText().strip()))
        self.cancel_button.setEnabled(bool(job and job["status"] in ACTIVE and not job.get("cancel_requested"))
                                      and bool(self.cancel_reason.text().strip()))

    def _hide_confirmation(self):
        self.run_panel.hide()
        self.run_button.show()
        self.start_confirm.setChecked(False)

    def _open_confirmation(self):
        self.refresh()
        _require(self._view.get("can_start"), "ASSESSMENT_NOT_READY", "Review the current eligibility first.")
        self.start_confirm.setChecked(False)
        self.run_panel.show()
        self.run_button.hide()
        self.reason.setFocus()
        self.tabs.widget(0).ensureWidgetVisible(self.reason)

    def refresh(self, force=False):
        if not self.is_current():
            self.invalidate("This project changed or is closing.")
            return
        if self.owner._busy and not force:
            self._buttons()
            return
        state = self.owner.state if self.owner.state is not None else self.project.snapshot()
        assessment = state.get("final_review", {}).get("assessment", {})
        jobs = assessment.get("jobs", [])
        # Progress is live between database revisions. Redraw only the selected status
        # during those ticks; preserve table selection and dialogs while listening.
        key = (state["revision"], assessment.get("current_fingerprint"),
               tuple((j["job_id"], j["status"], j.get("stale_for_current"), j.get("report_sha256"),
                      j.get("cancel_requested")) for j in jobs))
        if not force and key == self._view_key:
            self._update_progress(next((j for j in jobs if j["job_id"] == self._selected_job), None))
            self._buttons()
            return

        previous = self._view
        self._view_key = key
        self._view = self.controller.view(state)
        self.controller.rebase(state)
        context_changed = previous and (previous["current_fingerprint"] != self._view.get("current_fingerprint")
                                        or previous["revision"] != self._view["revision"])
        if context_changed:
            self.start_confirm.setChecked(False)
            self.diagnostic_confirm.setChecked(False)
        if self._follow_latest:
            self._selected_job = self._view.get("latest_job_id")
        self._loading = True
        try:
            self._render_preparation()
            self.report_selector.clear()
            for i, job in reversed(list(enumerate(jobs, 1))):
                when = _timestamp(job.get("created_at"))
                stale = " · Historical" if job.get("stale_for_current") else ""
                self.report_selector.addItem(f"Run {i} · {_status(job)} · {when}{stale}", job["job_id"])
            self.report_selector.setCurrentIndex(self.report_selector.findData(self._selected_job))
        finally:
            self._loading = False
        self._render_results()
        selected = self._selected()
        new_report = (previous is not None and self._follow_latest and selected
                      and selected["status"] in REPORT_STATES
                      and previous.get("current_job_id") == selected["job_id"])
        if (previous is None or new_report) and selected and not selected.get("stale_for_current"):
            self.tabs.setCurrentIndex(1)
        self._buttons()

    def _render_preparation(self):
        view = self._view
        support_view = view.get("provisional_support") or view
        coverage = support_view.get("coverage", {})
        names = _family_names(view)
        scope = view.get("scope")
        n = coverage.get("n_scope", 0)
        self.scope_text.setText(f"Declared scope · {n} of {coverage.get('n_total', n)} syllables · Family-level assessment"
                                if scope else "Provisional preview · all project syllables. Declare a scope in Final checks.")
        self.coverage.setText(_coverage_text(coverage).replace("in scope", "in this preview") if not scope else _coverage_text(coverage))
        blocks = view.get("blocking_reasons", [])
        support_only = coverage.get("n_eligible_families", 0) < 2 or bool(view.get("numeric_input_reasons"))
        if view.get("current_job_id"):
            heading = "Assessment in progress"
        elif not view.get("can_start"):
            heading = "Assessment prerequisites"
        elif support_only:
            heading = "Ready to generate a support report"
        else:
            heading = "Ready to run the assessment"
        self.status.setText(heading)
        reasons = list(dict.fromkeys(_reason(row) for row in blocks))
        if scope and support_only:
            if coverage.get("n_eligible_families", 0) < 2:
                reasons.append("Scoring requires at least two eligible families.")
            reasons.extend(_reason(row) for row in view.get("numeric_input_reasons", []))
        self.blockers.setText("\n".join(reasons))
        self.blockers.setVisible(bool(reasons))
        self.scope_button.setVisible(not scope or not view["current_completion_id"]
                                     or any(row.get("code") not in {"DEADLINE_LOCKED", "ASSESSMENT_BUSY"} for row in blocks))
        self.run_button.setText("Generate support report…" if support_only else "Run assessment…")
        self.start_button.setText("Generate support report" if support_only else "Run assessment")
        if not view.get("can_start"):
            self._hide_confirmation()
        self.source_button.setText("Review recording sources…" if coverage.get("n_scope_unknown_source") else "Recording sources…")
        current_fid = self.support.item(self.support.currentRow(), 0)
        selected_fid = current_fid.data(Qt.ItemDataRole.UserRole) if current_fid else None
        rows = support_view.get("support", [])
        _rows(self.support, _support_rows(rows, names))
        self.support.clearSelection()
        self.support.setCurrentCell(-1, -1)
        for i, row in enumerate(rows):
            fid = row["family_id"]
            item = self.support.item(i, 0)
            item.setData(Qt.ItemDataRole.UserRole, fid)
            item.setToolTip(fid)
            if fid == selected_fid:
                self.support.selectRow(i)
        self._render_source_detail()
        if not self.method_panel.isHidden():
            self._render_item_table()

    def _render_item_table(self):
        view = self._view
        if not view:
            return
        names = _family_names(view)
        _rows(self.items, [(r["item_id"], "Provisional" if not view["scope"] else "Included" if r["in_scope"] else "Outside",
                            names.get(r["family_id"], r["family_id"] or "—"), r["processing_state"] or "—",
                            r["source_group_id"] or "Unknown") for r in view["items"]])

    def _support_selected(self):
        if not self._loading and self._view is not None:
            self._render_source_detail()

    def _render_source_detail(self):
        rows = (self._view.get("provisional_support") or self._view).get("support", [])
        i = self.support.currentRow()
        if not 0 <= i < len(rows):
            self.source_summary.setText("Eligibility requires 10 confirmed, known-source syllables from at least 3 original recordings per family.")
            return
        row = rows[i]
        self.source_summary.setText(f"{row['n_in_scope']} in scope · {row['n_confirmed']} confirmed · "
                                    f"{row['n_unknown_source']} confirmed with unknown source · "
                                    f"Largest recording share of known-source syllables: {_percent(row.get('max_source_share'))}")

    def _job_selected(self, *_):
        if self._loading or not self._view:
            return
        self._follow_latest = False
        self._selected_job = self.report_selector.currentData()
        self._render_results()
        self._changed()

    def open_report(self, job_id):
        """Select exactly the run requested by catalogue delivery, including historical runs."""
        self.refresh(force=True)
        index = self.report_selector.findData(job_id)
        _require(index >= 0, "ASSESSMENT_NOT_FOUND", "This assessment run is unavailable in the current project.")
        self._follow_latest = False
        self._selected_job = job_id
        self.report_selector.setCurrentIndex(index)
        self._render_results()
        self.tabs.setCurrentIndex(1)
        self._buttons()

    def _update_progress(self, job):
        if not job or job["status"] not in ACTIVE:
            return
        progress = job.get("progress") or {}
        text = system_text(str(progress.get("message") or progress.get("stage") or _status(job)))
        if job.get("cancel_requested"):
            text += " · Cancellation requested"
        self.result_reason.setText(text)

    def _render_results(self):
        job = self._selected()
        key = (self._selected_job, (job or {}).get("status"), (job or {}).get("stale_for_current"),
               tuple((job or {}).get("stale_reasons", [])), (job or {}).get("report_sha256"))
        self._update_progress(job)
        if self._render_key == key:
            return
        self._render_key = key
        self._close_comparison()
        self._loading = True
        self.diagnostic_confirm.setChecked(False)
        self.cancel_reason.clear()
        self.question_panel.hide()
        self.pairs.setRowCount(0)
        self.family.clear()
        self._loading = False
        report = (job or {}).get("report") or {}
        state = (job or {}).get("status")
        scored = state == "completed" and bool(report)
        self.cancel_panel.setVisible(state in ACTIVE)
        self.scored_panel.setVisible(scored)
        self.report_support_heading.setVisible(state == "not_estimable")
        self.report_support.setVisible(state == "not_estimable")
        self.result_coverage.setText("")
        self.result_reason.setText("")
        if not job:
            self.report_status.setText("No assessment run selected")
            self.result_reason.setText("Use Preparation to check recording support and run an assessment.")
            self.run_details.clear()
            self._buttons()
            return
        stale = bool(job.get("stale_for_current"))
        names = {} if stale else _family_names(self._view)
        self.report_status.setText(_status(job) + (" · Historical report" if stale else " · Current scope"))
        self.run_details.setText(f"Run ID: {job['job_id']}\nRequested: {job.get('reason', '')}\n"
                                 f"Started: {_timestamp(job.get('created_at'))}\nFinished: {_timestamp(job.get('ended_at'))}"
                                 + ("\nChanged since this run: " + ", ".join(job.get("stale_reasons", [])) if stale else ""))
        if report:
            c = report.get("coverage", {})
            if scored:
                self.result_coverage.setText(f"Scored {c.get('n_eligible', 0)} of {c.get('n_scope', 0)} syllables in scope · "
                    f"{c.get('n_eligible_families', 0)} of {c.get('n_families', 0)} catalogue families\n"
                    f"Unresolved: {c.get('n_unresolved', 0)} · Excluded: {c.get('n_excluded', 0)} · "
                    f"Confirmed, source unknown: {c.get('n_unknown_source', 0)}")
                self.result_reason.setText("These scores describe how acoustic features predict the saved human families across held-out recordings.")
            else:
                self.result_coverage.setText(_coverage_text(c))
                reasons = [_reason(r) for r in report.get("reasons", [])]
                self.result_reason.setText("No score could be estimated. " + " ".join(reasons))
                _rows(self.report_support, _support_rows(report.get("support", []), names))
            if stale:
                self.result_reason.setText("Historical snapshot. Current catalogue delivery requires a matching assessment.\n" + self.result_reason.text())
        elif state in ACTIVE:
            self._update_progress(job)
        else:
            error = job.get("error") or {}
            self.result_reason.setText(_reason(error) if isinstance(error, dict) and error else
                                       str(error or "This run ended without a complete report."))
        if scored:
            metrics = report.get("metrics") or {}
            ba = metrics.get("balanced_accuracy", {})
            f1 = metrics.get("macro_f1", {})
            self.metrics.setText(f"Balanced accuracy: {_percent(ba.get('mean'))}   ·   Macro-F1: {_percent(f1.get('mean'))}\n"
                                 f"SD across five repeats (percentage points): balanced accuracy {ba.get('sd', 0) * 100:.1f}, "
                                 f"macro-F1 {f1.get('sd', 0) * 100:.1f}.")
            _rows(self.recalls, [(names.get(r['family_id'], r['family_id']), r["n"], _percent(r["recall_mean"]),
                                  f"{r['recall_sd'] * 100:.1f}") for r in report.get("per_family", [])])
            confusion = report.get("confusion") or {}
            ids = confusion.get("family_ids", [])
            self.matrix.setColumnCount(1 + len(ids))
            self.matrix.setHorizontalHeaderLabels(["Human / predicted", *[names.get(i, i) for i in ids]])
            _rows(self.matrix, [(names.get(fid, fid), *[_percent(v) for v in values])
                                for fid, values in zip(ids, confusion.get("matrix_mean_row_normalized", []))])
            pairs = self._pairs()
            self._loading = True
            _rows(self.pairs, [(names.get(r["actual_family_id"], r["actual_family_id"]),
                                names.get(r["predicted_family_id"], r["predicted_family_id"]),
                                _percent(r["rate_mean"]), f"{r['rate_sd'] * 100:.1f}") for r in pairs])
            self.pairs.clearSelection()
            self.pairs.setCurrentCell(-1, -1)
            self._loading = False
            self.pair_hint.setText("Historical family IDs are shown. Open a current report to audition its examples." if stale
                                   else "Select a direction to compare its syllables with the family's representative examples."
                                   if pairs else "No off-diagonal confusions were observed.")
        self._buttons()

    def _pair_selected(self):
        if self._loading:
            return
        self.family.clear()
        self.family.addItem("Choose family…", None)
        self.question_panel.hide()
        self.diagnostic_confirm.setChecked(False)
        index = self.pairs.currentRow()
        pairs = self._pairs()
        if 0 <= index < len(pairs):
            for fid in (pairs[index]["actual_family_id"], pairs[index]["predicted_family_id"]):
                if fid in self._view["current_family_ids"]:
                    self.family.addItem(self._view["family_names"].get(fid, fid), fid)
        self._buttons()

    def _listen_pair(self, row):
        job = self._selected()
        if not job or job.get("stale_for_current") or not 0 <= row < len(self._pairs()):
            return
        pair = self._pairs()[row]
        key = (job["job_id"], pair["actual_family_id"], pair["predicted_family_id"])
        if self.pair_dialog and self._pair_key == key and self.pair_dialog.isVisible():
            self.pair_dialog.raise_()
            return
        self._close_comparison()
        from .annotation_desktop_assessment_listen import PairAuditionDialog
        self.pair_dialog = PairAuditionDialog(self, self.controller, *key)
        self._pair_key = key
        self.pair_dialog.show()
        self.pair_dialog.raise_()

    def inspect_selected(self):
        self._listen_pair(self.pairs.currentRow())

    def _close_comparison(self):
        if self.pair_dialog is not None:
            self.pair_dialog.close()
            self.pair_dialog.deleteLater()
            self.pair_dialog = None
            self._pair_key = None

    def _open_question(self):
        if self.family.count() > 1:
            self.question_panel.show()
            self.diagnostic_confirm.setChecked(False)
            self.question.setFocus()
            self.tabs.widget(1).ensureWidgetVisible(self.question)
    def _run(self, label, function, *, export=False):
        _require(self.is_current() and not self.owner._busy and not self._waiting,
                 "ASSESSMENT_CONTEXT_CHANGED", "Wait for the current project.")
        self._waiting = True
        self._buttons()
        generation = self._callback_generation
        def done(result):
            if not self.is_current() or generation != self._callback_generation:
                return
            self._waiting = False
            self.owner.state = self.project.snapshot()
            self.refresh(force=True)
            self.start_confirm.setChecked(False)
            self.diagnostic_confirm.setChecked(False)
            if export:
                self.message.setText("Saved " + str(result["path"]))
                self.reportSaved.emit(result)
            else:
                ident = (result.get("record") or {}).get("job_id")
                if ident:
                    self.open_report(ident)
                    self._follow_latest = True
                self.assessmentChanged.emit(result)
        try:
            self.owner._run(label, lambda _progress, _cancelled: function(), done)
        except Exception:
            self._waiting = False
            self._buttons()
            raise

    def start_assessment(self):
        _require(self.run_panel.isVisible() and self.start_confirm.isChecked(), "ASSESSMENT_CONFIRMATION",
                 "Confirm this run for the current scope.")
        ticket = copy.deepcopy(self._view["ticket"])
        reason = self.reason.toPlainText().strip()
        self.save_draft()
        self._hide_confirmation()
        self._follow_latest = True
        self._run("Run internal assessment", lambda: self.controller.start(reason, confirmed=True, ticket=ticket))

    def cancel_selected(self):
        job = self._selected()
        _require(job is not None and job["status"] in ACTIVE, "ASSESSMENT_NOT_ACTIVE", "Choose an active assessment.")
        reason, ticket = self.cancel_reason.text(), copy.deepcopy(self._view["ticket"])
        self.save_draft()
        self._run("Cancel assessment", lambda: self.controller.cancel(job["job_id"], reason, ticket=ticket))

    def _export(self, *, raw_json):
        _require(self.is_current() and not self.owner._busy, "ASSESSMENT_CONTEXT_CHANGED", "Wait for the current project.")
        job = self._selected()
        _require(job and job["status"] in REPORT_STATES, "ASSESSMENT_NO_REPORT", "Select a complete assessment report.")
        ident = job["job_id"]
        suffix, label = (".json", "Full JSON") if raw_json else (".zip", "Assessment package")
        path, _ = QFileDialog.getSaveFileName(self, "Export assessment", ident + suffix, f"{label} (*{suffix})")
        if not path:
            return
        if raw_json:
            self._run("Save assessment JSON", lambda: self.controller.save_report(ident, path, overwrite=True), export=True)
        else:
            from .annotation_assessment_export import export_assessment
            self._run("Export assessment package", lambda: export_assessment(self.controller, ident, path, overwrite=True), export=True)

    def export_selected(self):
        self._export(raw_json=False)

    def request_catalogue(self):
        _require(self.is_current() and not self.owner._busy and not self._waiting,
                 "ASSESSMENT_CONTEXT_CHANGED", "Wait for the current project.")
        job_id = self._selected_job
        view = self.controller.view(self.project.snapshot())
        job = next((r for r in view["jobs"] if r["job_id"] == job_id), None)
        _require(job and job["status"] in REPORT_STATES and job.get("report_url") and not job.get("stale_for_current"),
                 "ASSESSMENT_NO_REPORT", "Select a completed report matching the current scope.")
        self.save_draft()
        self.catalogueRequested.emit(job_id)

    def save_json(self):
        self._export(raw_json=True)

    def request_sources(self):
        self.save_draft()
        self.sourceRequested.emit()

    def request_final(self):
        self.save_draft()
        self.finalChecksRequested.emit()

    def request_directed(self):
        value = self.controller.directed_selection(self._selected_job, self.pairs.currentRow(), self.family.currentData(),
            self.question.toPlainText(), confirmed=self.diagnostic_confirm.isChecked())
        self.save_draft()
        self.diagnostic_confirm.setChecked(False)
        self.directedRequested.emit(value)

    def save_draft(self):
        if self._dirty:
            self.store.save(dict(reason=self.reason.toPlainText(), cancel_reason=self.cancel_reason.text(),
                                 question=self.question.toPlainText(), selected_job_id=self._selected_job or ""),
                            (self._view or {}).get("current_fingerprint"))
            self._dirty = False
            self._buttons()
        return True

    def restore_draft(self):
        _require(self.is_current() and not self.owner._busy, "ASSESSMENT_CONTEXT_CHANGED", "Open the current project first.")
        value = self.store.load()
        _require(value, "ASSESSMENT_DRAFT_MISSING", "No saved assessment form is available.")
        form = value["form"]
        if form["selected_job_id"] and self.report_selector.findData(form["selected_job_id"]) >= 0:
            self.open_report(form["selected_job_id"])
        self._loading = True
        self.reason.setPlainText(form["reason"])
        self.cancel_reason.setText(form["cancel_reason"])
        self.question.setPlainText(form["question"])
        self._loading = False
        self.start_confirm.setChecked(False)
        self.diagnostic_confirm.setChecked(False)
        self.family.setCurrentIndex(0)
        self._dirty = False
        self._buttons()
        self.message.setText("Form text restored. Review the current scope and confirm before starting a run.")

    def invalidate(self, reason):
        self._invalid = reason
        self._callback_generation += 1
        self._hide_confirmation()
        self.diagnostic_confirm.setChecked(False)
        self.stop()
        self.message.setText(reason)
        self._buttons()

    def stop(self):
        self.timer.stop()
        self._close_comparison()

    def showEvent(self, event):
        super().showEvent(event)
        if hasattr(self, "timer") and self.is_current():
            self.refresh(force=not self.owner._busy)
            self.timer.start()

    def _leaving(self):
        self.save_draft()
        self._callback_generation += 1
        self._waiting = False
        self._hide_confirmation()
        self.diagnostic_confirm.setChecked(False)
        self.stop()

    def reject(self):
        try:
            self._leaving()
        except Exception as exc:
            self._guard(lambda: (_ for _ in ()).throw(exc))
            return
        super().reject()

    def closeEvent(self, event):
        try:
            self._leaving()
        except Exception as exc:
            event.ignore()
            self._guard(lambda: (_ for _ in ()).throw(exc))
            return
        super().closeEvent(event)
