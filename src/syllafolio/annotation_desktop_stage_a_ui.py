"""原生候选工作区：只读输入/来源/配方与显式预览、确认、应用。"""
from __future__ import annotations
from .annotation_english import system_fields, system_text, system_error

import copy
import json
import uuid

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QFormLayout, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton,
    QScrollArea, QSpinBox, QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

from .annotation_desktop_organization import OrganizationController


def _json(value):
    return json.dumps(system_fields(value), ensure_ascii=False, indent=2, allow_nan=False)


def _table(headers):
    result = QTableWidget(0, len(headers))
    result.setHorizontalHeaderLabels(headers)
    result.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    result.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    result.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    result.verticalHeader().hide()
    return result


def _rows(table, rows):
    table.setRowCount(len(rows))
    for index, values in enumerate(rows):
        for column, value in enumerate(values):
            item = QTableWidgetItem("Not recorded" if value is None else str(value))
            item.setToolTip(item.text())
            table.setItem(index, column, item)
    table.resizeColumnsToContents()


def _readonly():
    edit = QPlainTextEdit()
    edit.setReadOnly(True)
    return edit


def _seconds(value, status=None):
    if status == "not_applicable":
        return "N/A"
    return f"{value:.3f}" if type(value) in {int, float} else "Not recorded"


def _summary_text(view):
    summary, sources = view.get("summary", {}), view.get("sources", {})
    counts = sources.get("counts", {})
    sidecar = summary.get("has_sidecar")
    return (f"{summary.get('item_count')} syllables · {summary.get('sample_rate_hz')} Hz · "
        f"{_seconds(summary.get('total_duration_s'))} s of audio\n"
        f"Parent context: {summary.get('context_available_count')} · Segment only: {summary.get('segment_only_count')} · "
        f"Original segments.json: {'present' if sidecar is True else 'absent' if sidecar is False else 'not recorded'}\n"
        f"Original-recording declarations: {counts.get('declared')} known / {counts.get('unknown')} unknown. "
        "A parent clip or syllable file is not automatically an independent recording.")


class BandpassControls(QWidget):
    """Shared import-time audio processing choices; display settings are separate."""
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        self.enable_checkbox = QCheckBox("Band-pass filter")
        self.enable_checkbox.setObjectName("bandpassEnabled")
        self.enable_checkbox.setToolTip("Filter the imported PCM before feature extraction. Original input files remain unchanged.")
        self.low_hz = QDoubleSpinBox(); self.low_hz.setObjectName("bandpassLowHz")
        self.high_hz = QDoubleSpinBox(); self.high_hz.setObjectName("bandpassHighHz")
        for widget, prefix, value in ((self.low_hz, "Low ", 500), (self.high_hz, "High ", 15000)):
            widget.setDecimals(1); widget.setRange(.1, 1_000_000)
            widget.setSingleStep(100); widget.setPrefix(prefix); widget.setSuffix(" Hz"); widget.setValue(value)
        self.order = QSpinBox(); self.order.setObjectName("bandpassOrder")
        self.order.setRange(1, 8); self.order.setValue(4); self.order.setPrefix("Order ")
        row.addWidget(self.enable_checkbox)
        for widget in (self.low_hz, self.high_hz, self.order):
            row.addWidget(widget); widget.setEnabled(False)
            widget.valueChanged.connect(lambda _value: self.changed.emit())
        row.addStretch(1)
        self.enable_checkbox.toggled.connect(self._toggle)

    def _toggle(self, enabled):
        for widget in (self.low_hz, self.high_hz, self.order): widget.setEnabled(enabled)
        self.changed.emit()

    def settings(self, rate=None):
        if not self.enable_checkbox.isChecked():
            return None
        value = {"low_hz": self.low_hz.value(), "high_hz": self.high_hz.value(), "order": self.order.value()}
        if value["low_hz"] >= value["high_hz"]:
            raise ValueError("The low cutoff must be below the high cutoff.")
        if rate is not None:
            from .acoustics.bandpass import validate_bandpass
            return validate_bandpass(value, rate)
        return value


class InputInspectionDialog(QDialog):
    """审阅已有首错检查结果；接受只是导入意图，不创建项目或人工记录。"""
    def __init__(self, view, parent=None):
        super().__init__(parent)
        self.view = copy.deepcopy(view)
        self.setWindowTitle("Review input before import")
        self.resize(920, 620)
        layout = QVBoxLayout(self)
        summary = QLabel(_summary_text(view))
        summary.setWordWrap(True)
        layout.addWidget(summary)
        skipped = view.get("summary", {}).get("skipped_audio_files", [])
        if skipped:
            notice = QLabel(f"Skipped {len(skipped)} unsupported audio file(s). See the file list in the details below.")
            notice.setWordWrap(True)
            layout.addWidget(notice)
        self.items = _table(["Stable ID", "Input file", "Context", "Duration (s)"])
        _rows(self.items, [[row.get("item_id"), row.get("filename"), row.get("context_kind"), row.get("duration_s")]
                          for row in view.get("items", [])])
        layout.addWidget(self.items, 1)
        self.details = _readonly()
        self.details.setPlainText(_json({key: view.get(key) for key in
            ("summary", "sources", "duplicate_pcm_groups", "input_requirements", "limits")}))
        layout.addWidget(self.details, 1)
        note = QLabel("Supported input: WAV, FLAC, MP3, Ogg Vorbis or AIFF (.aif/.aiff). "
                      "Files must be mono, share a sample rate of at least 32000 Hz, and contain segments no longer than 30 seconds. "
                      "This inspection is read-only. Import freezes this reviewed input identity and runs the fixed recipe. "
                      "No human labels are loaded. The first invalid input stops inspection.")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.bandpass = BandpassControls(self)
        layout.addWidget(self.bandpass)
        self.problem = QLabel(); self.problem.setWordWrap(True)
        self.problem.setTextFormat(Qt.TextFormat.PlainText)
        self.problem.setStyleSheet("color:#a34838;")
        layout.addWidget(self.problem)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Import this input")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

    def bandpass_settings(self):
        return self.bandpass.settings(self.view.get("summary", {}).get("sample_rate_hz"))

    def accept(self):
        try:
            self.bandpass_settings()
        except (ValueError, RuntimeError) as error:
            self.problem.setText(system_error(error))
            return
        super().accept()


class StageAWorkspaceDialog(QDialog):
    """同一窗口/owner上的候选读模型与命令入口，不创建第二个服务。"""
    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.project = owner.project
        self.generation = owner._close_coordinator.generation
        self.controller = OrganizationController()
        self._waiting = False
        self._rendering = False
        self._revision = None
        self._items = []
        self.setWindowTitle("Candidate workspace")
        self.resize(1100, 740)
        self.setModal(False)
        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)
        self._build_input()
        self._build_recipe()
        self._build_organization()
        self.message = QLabel("Read-only input and source summaries. Preview an organization before applying it.")
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        self.close_buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self.close_buttons.rejected.connect(self.reject)
        layout.addWidget(self.close_buttons)
        self.timer = QTimer(self)
        self.timer.setInterval(350)
        self.timer.timeout.connect(lambda: self._guard(self.refresh))
        self.finished.connect(lambda _result: self.timer.stop())
        self.owner.operationFailed.connect(self._operation_error)

    def _build_input(self):
        page = QWidget(); layout = QVBoxLayout(page)
        self.input_summary = QLabel(); self.input_summary.setWordWrap(True)
        layout.addWidget(self.input_summary)
        self.input_items = _table(["Stable ID", "Original ID / file", "Context", "Parent / frames", "Original recording", "Pool"])
        self.input_items.itemSelectionChanged.connect(self._select_input)
        layout.addWidget(self.input_items, 2)
        self.item_details = _readonly()
        self.item_details.setPlaceholderText("Select an input row to inspect exact frame, PCM, original-file and source evidence.")
        layout.addWidget(self.item_details, 1)
        self.source_summary = QLabel(); self.source_summary.setWordWrap(True)
        layout.addWidget(self.source_summary)
        self.tabs.addTab(page, "Input & sources")

    def _build_recipe(self):
        page = QWidget(); layout = QVBoxLayout(page)
        note = QLabel("Current fixed global recipe: 105D → RobustScaler → UMAP 10D → HDBSCAN. "
            "Candidate organisation uses 94D coordinate-median microcluster prototypes, one equal-weight point per microcluster, and Ward. "
            "The log-mel view is not concatenated into the global 105D fit. Small batches may explicitly skip embedding; "
            "the frozen fit status is shown below. No feature or parameter search is available here.")
        note.setWordWrap(True); layout.addWidget(note)
        self.timing_summary = QLabel(); self.timing_summary.setWordWrap(True)
        layout.addWidget(self.timing_summary)
        self.timing_table = _table(["Observed phase", "Seconds", "Status"])
        layout.addWidget(self.timing_table)
        self.recipe_details = _readonly(); layout.addWidget(self.recipe_details, 2)
        self.tabs.addTab(page, "Recipe & actual time")

    def _build_organization(self):
        page = QWidget(); layout = QVBoxLayout(page)
        form = QFormLayout()
        self.mode = QComboBox()
        for label, value in [("Automatic recommendation", "auto"), ("Keep original microclusters", "microclusters"),
                             ("Advanced: manual Ward merge height", "ward")]:
            self.mode.addItem(label, value)
        self.height = QDoubleSpinBox(); self.height.setDecimals(9); self.height.setRange(0, 1e100)
        self.height.setValue(0)
        form.addRow("Organization", self.mode); form.addRow("Ward merge height h", self.height)
        layout.addLayout(form)
        note = QLabel("Merge height belongs to this batch's Ward tree; it is not a similarity percentage or a maximum pairwise distance. "
                      "Machine groups are listening organization, not human families. Unclustered items stay in the global pool.")
        note.setWordWrap(True); layout.addWidget(note)
        actions = QHBoxLayout()
        self.preview_button = QPushButton("Preview selected organization")
        self.preview_button.setObjectName("previewOrganization")
        self.apply_button = QPushButton("Apply reviewed organization")
        self.apply_button.setObjectName("applyReviewedOrganization")
        actions.addWidget(self.preview_button); actions.addWidget(self.apply_button)
        layout.addLayout(actions)
        self.applied_summary = QLabel("No organization has been applied.")
        self.applied_summary.setWordWrap(True); layout.addWidget(self.applied_summary)
        self.preview_summary = QLabel("No current preview."); self.preview_summary.setWordWrap(True)
        layout.addWidget(self.preview_summary)
        self.groups = _table(["Candidate group", "Items", "Source microclusters", "Stable IDs"])
        layout.addWidget(self.groups, 2)
        self.impact_details = _readonly(); layout.addWidget(self.impact_details, 1)
        self.acknowledgement = QCheckBox("I reviewed these exact affected materials, decisions and completion records.")
        self.acknowledgement.setObjectName("acknowledgeOrganizationImpact")
        layout.addWidget(self.acknowledgement)
        self.note = QLineEdit(); self.note.setMaxLength(4000)
        self.note.setPlaceholderText("Optional operator note; not a substitute for impact confirmation")
        layout.addWidget(self.note)
        self.mode.currentIndexChanged.connect(self._parameters_changed)
        self.height.valueChanged.connect(self._parameters_changed)
        self.acknowledgement.toggled.connect(self._acknowledge)
        self.preview_button.clicked.connect(lambda: self._guard(self.preview))
        self.apply_button.clicked.connect(lambda: self._guard(self.apply))
        self.organization_scroll = QScrollArea()
        self.organization_scroll.setWidgetResizable(True)
        self.organization_scroll.setWidget(page)
        self.tabs.addTab(self.organization_scroll, "Organization preview")
        self._parameters_changed()

    def _guard(self, function):
        try:
            return function()
        except Exception as exc:
            self.message.setText(f"{getattr(exc, 'code', type(exc).__name__)}: {system_error(exc)}")
            self.owner._error(exc, operation="candidate workspace")
            if self.owner._busy:
                self._set_enabled()
            else:
                self._render_preview()

    def _operation_error(self, message):
        if self._waiting:
            self.message.setText(message)

    def _current(self):
        return (self.owner.project is self.project and self.project is not None and
                self.owner._close_coordinator.generation == self.generation and
                self.owner._close_coordinator.state == "open")

    def _parameters_changed(self, *_args):
        if self._rendering:
            return
        mode = self.mode.currentData()
        self.height.setEnabled(mode == "ward")
        self.controller.set_parameters(mode, self.height.value() if mode == "ward" else None)
        self._render_preview()

    def set_mode(self, mode, height=None):
        index = self.mode.findData(mode)
        if index < 0:
            raise ValueError("Unknown organization mode")
        self.mode.setCurrentIndex(index)
        if height is not None:
            self.height.setValue(height)
        self.tabs.setCurrentIndex(2)

    def _acknowledge(self, checked):
        if self._rendering:
            return
        view = self.controller.snapshot()
        if view["preview"] is not None:
            self._guard(lambda: self.controller.acknowledge(acknowledged=checked,
                impact_fingerprint=view["preview"].get("impact", {}).get("fingerprint")))
        self._render_preview()

    def preview(self):
        if self.owner._busy or self._waiting:
            raise ValueError("Wait for the current foreground operation to finish.")
        if not self._current():
            raise ValueError("Reopen the candidate workspace for the current project.")
        self._waiting = True
        self._set_enabled()
        self.message.setText("Preparing a read-only organization preview; no organization is being applied.")
        def done(_result):
            self.owner.state = self.project.snapshot()
            self._waiting = False
            self._revision = None
            self.refresh()
        self.owner._run("Preview candidate organization", lambda _progress, _cancelled:
            self.controller.preview(self.project, generation=self.generation,
                idempotency_key="desktop-preview-" + uuid.uuid4().hex), done)

    def apply(self):
        if self.owner._busy or self._waiting:
            raise ValueError("Wait for the current foreground operation to finish.")
        if not self._current():
            raise ValueError("Reopen the candidate workspace for the current project.")
        self.owner._save_drafts()
        note = self.note.text()
        self._waiting = True
        self._set_enabled()
        def done(_result):
            self.owner.state = self.project.snapshot()
            self._waiting = False
            self.owner._organization_applied()
            self.owner._refresh()
            self.message.setText("Organization applied explicitly. Previous decisions and history are retained.")
            self._revision = None
            self.refresh()
        self.owner._run("Apply reviewed candidate organization", lambda _progress, _cancelled:
            self.controller.apply(self.project, generation=self.generation,
                idempotency_key="desktop-apply-" + uuid.uuid4().hex, note=note), done)

    def _set_enabled(self):
        available = self._current() and not self.owner._busy and not self._waiting
        self.mode.setEnabled(available); self.height.setEnabled(available and self.mode.currentData() == "ward")
        self.preview_button.setEnabled(available)
        self.apply_button.setEnabled(available and self.controller.snapshot()["can_apply"])
        self.note.setEnabled(available)
        self.acknowledgement.setEnabled(available and self.controller.snapshot()["preview"] is not None)

    def _render_preview(self):
        view = self.controller.snapshot(); summary = view.get("summary")
        self._rendering = True
        self.acknowledgement.setChecked(view.get("confirmation") is not None)
        impact = (summary or {}).get("impact", {})
        self.acknowledgement.setVisible(bool(impact.get("requires_confirmation")))
        if summary is None:
            self.preview_summary.setText(view.get("invalid_reason") or "No current preview.")
            _rows(self.groups, []); self.impact_details.clear()
        else:
            self.preview_summary.setText(
                f"Requested: {summary.get('requested_mode')} → Actual: {summary.get('actual_mode')} · "
                f"Microclusters {summary.get('microcluster_count')} → candidate groups {summary.get('candidate_group_count')} · "
                f"Largest group {summary.get('largest_group_size')} · Unclustered {summary.get('unclustered_count')}\n"
                f"Automatic recommendation {'used' if summary.get('recommendation_used') else 'not used'} · "
                f"Recommendation reason: {summary.get('reason_code')} · {system_text(summary.get('reason'))}\n"
                f"Actual height: {summary.get('height')} · Changed group memberships: {impact.get('changed_group_item_count')} · "
                f"Current reviews: {impact.get('current_review_count')} · Saved assignments: {impact.get('saved_assignment_count')} · "
                f"Queue reset scope: {impact.get('reset_scope_item_count')} · {'Applied' if view.get('applied') else 'Read-only preview'}")
            _rows(self.groups, [[row.get("label") or row.get("group_id"), row.get("item_count"),
                ", ".join(row.get("source_cluster_ids", [])), ", ".join(row.get("item_ids", []))]
                for row in summary.get("groups", [])])
            self.impact_details.setPlainText(_json({"impact": impact, "recommendation": summary.get("recommendation"),
                "ward": summary.get("ward"), "unclustered_ids": summary.get("unclustered_ids"),
                "preview_fingerprint": view["preview"].get("preview_fingerprint")}))
        self._rendering = False
        self._set_enabled()

    def _select_input(self):
        row = self.input_items.currentRow()
        self.item_details.setPlainText(_json(self._items[row]) if 0 <= row < len(self._items) else "")

    def refresh(self):
        # Worker apply holds the controller's request lock until the service
        # receipt is settled. Never wait on that lock from the Qt event loop.
        if self._waiting and self.owner._busy:
            self._set_enabled()
            return
        if not self._current():
            self.controller.invalidate("Project changed or is closing. Reopen this workspace before another preview.")
            self._render_preview(); return
        state = self.owner.state or {}
        self.controller.observe_context(state, project_id=self.project.metadata["project_id"], generation=self.generation)
        if not self.owner._busy:
            self._waiting = False
        if state.get("revision") != self._revision:
            self._revision = state.get("revision")
            view = state.get("stage_a", {})
            self.input_summary.setText(_summary_text(view))
            self._items = view.get("items", [])
            _rows(self.input_items, [[row.get("item_id"), row.get("original_item_id") or row.get("original_file"),
                row.get("context_kind"), f"{row.get('source_clip_id')} [{row.get('source_frame_start')}, {row.get('source_frame_stop')})",
                row.get("raw_recording_id") or "Unknown", "in pool" if row.get("in_current_pool") else
                "initially unclustered" if row.get("initially_unclustered") else row.get("processing_state")]
                for row in self._items])
            candidates = view.get("candidates", {})
            organization = state.get("batch_review", {}).get("organization")
            self.applied_summary.setText("No organization has been applied." if not organization else
                f"Current applied organization: {organization.get('organization_id')} · "
                f"{organization.get('requested_mode')} → {organization.get('mode')} · height {organization.get('height')} · "
                f"{candidates.get('organization_group_count')} candidate groups")
            self.source_summary.setText(f"Frozen microclusters: {candidates.get('microcluster_count')} · "
                f"Applied candidate groups: {candidates.get('organization_group_count')} · "
                f"Initially unclustered: {candidates.get('initial_unclustered_count')} · "
                f"Current pool (stable IDs deduplicated): {candidates.get('current_pool_count')}\n"
                "Noise is not exclusion. The nonempty pool is reviewed after the first pass. "
                "Source declarations are read-only here; unknown original recordings remain unknown." +
                ("\nInitial unclustered intake is pending explicit organization application; every listed ID is retained."
                 if candidates.get("initial_unclustered_intake_pending") else ""))
            timing = view.get("timing", {})
            kind = ("Prepared engineering replay (no feature extraction)" if view.get("origin") == "prepared_engineering_replay"
                    else "Real import / candidate preparation")
            seconds = timing.get("total_seconds")
            if seconds is None:
                timing_text = "Not recorded"
            elif timing.get("diagnostics"):
                timing_text = f"Recorded {_seconds(seconds)} s · metadata is incomplete or unverified; inspect the diagnostics below"
            else:
                timing_text = f"{timing.get('status', timing.get('state'))} · {_seconds(seconds)} s"
            if timing.get("observed_lower_bound"):
                timing_text += " · observed lower bound; no offline time added"
            self.timing_summary.setText(kind + " time (not human effort): " + timing_text)
            _rows(self.timing_table, [[system_text(row.get("label", row.get("id"))), _seconds(row.get("seconds"), row.get("status")), row.get("status")]
                                     for row in timing.get("phases", [])])
            self.recipe_details.setPlainText(_json({"recipe": view.get("recipe"), "timing": timing,
                "source_counts": view.get("sources", {}).get("counts"), "not_recorded": view.get("not_recorded")}))
        self._render_preview()

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh(); self.timer.start()

    def closeEvent(self, event):
        self.timer.stop()
        super().closeEvent(event)
