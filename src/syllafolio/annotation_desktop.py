"""Native PySide6 engineering prototype for local syllable annotation projects.

The GUI calls the existing protocol service. It does not write labels to SQLite,
infer classifications, start an HTTP server, or load the browser task library.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import threading
import uuid
import weakref

import numpy as np
import soundfile as sf
from PySide6.QtCore import Qt, QEvent, QItemSelectionModel, QLocale, QSettings, QThread, QTimer, Signal
from .annotation_english import system_error, system_fields, system_text
from PySide6.QtGui import QAccessible, QAccessibleTableModelChangeEvent, QAction, QKeySequence
from PySide6.QtWidgets import (
    QApplication, QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QGridLayout,
    QFileDialog, QFormLayout, QFrame, QGroupBox, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QMenu,
    QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox, QPlainTextEdit,
    QProgressBar, QPushButton, QScrollArea, QSplitter, QTreeWidget, QTreeWidgetItem,
    QSizePolicy, QToolButton, QVBoxLayout, QWidget,
)

from .annotation_desktop_project import create_project, open_project, backup_project
from .annotation_desktop_jobs import (
    CancelRequests, CloseCoordinator, DesktopJobError, ForegroundOperation,
    blocking_jobs, project_job_view, sampled_job_view, request_cancel,
)
from .annotation_desktop_widgets import AuditionCanvas, PcmPlayer
from .annotation_desktop_review_tree import ReviewTree
from .annotation_desktop_theme import DESKTOP_STYLE, apply_light_theme
from .annotation_desktop_diagnostics import DesktopDiagnostics
from .annotation_desktop_stage_a import inspect_input
from .annotation_desktop_stage_a_ui import InputInspectionDialog, StageAWorkspaceDialog
from .annotation_desktop_organization import OrganizationControllerError
from .annotation_desktop_family_review import FamilyReviewController
from .annotation_desktop_family_audition import FamilyAuditionPreview, family_review_conflicts
from .annotation_desktop_fine_review import FineReviewDialog
from .annotation_desktop_pool_review import PoolReviewController, PoolReviewDialog
from .annotation_desktop_final_review import FinalReviewDialog
from .annotation_desktop_catalogue import CatalogueReviewDialog
from .annotation_desktop_sources import SourceRegistryDialog
from .annotation_desktop_assessment import AssessmentDialog
from .annotation_desktop_export import ExportDialog
from .annotation_desktop_review_media import DisplayController, DisplayControlsWidget, ReviewMediaDialog
from .annotation_desktop_interval_import import IntervalImportDialog


DISPOSITIONS = [
    ("current", "Current family"), ("other", "Other family"),
    ("new_family", "New family"), ("pool", "Family unresolved"),
    ("discard", "Exclude"),
]
ACTIVE = {"queued", "running", "cancel_requested"}
STYLE = DESKTOP_STYLE


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    allow_nan=False).encode()).hexdigest()


def _write_json(path, value):
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                         encoding="utf-8")
    temporary.replace(path)


class TaskThread(QThread):
    succeeded = Signal(object)
    failed = Signal(object)
    progress = Signal(object)

    def __init__(self, function, parent=None):
        super().__init__(parent)
        self.function = function
        self.cancelled = threading.Event()

    def run(self):
        try:
            self.succeeded.emit(self.function(self.progress.emit, self.cancelled.is_set))
        except Exception as exc:
            self.failed.emit(exc)


class ProjectContractDialog(QDialog):
    """创建时明确用途和操作者；不从系统身份推断，不启用研究条件。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("New project identity")
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.purpose = QComboBox()
        self.purpose.addItem("Daily annotation", "daily_annotation")
        self.purpose.addItem("Engineering example / validation", "engineering")
        self.operator = QLineEdit()
        self.operator.setPlaceholderText("Explicit local operator ID")
        self.budget = QDoubleSpinBox()
        self.budget.setDecimals(0)
        self.budget.setRange(1, 31536000)
        self.budget.setValue(3600)
        self.budget.setSuffix(" s")
        self.policy = QLabel()
        self.policy.setWordWrap(True)
        self.problem = QLabel()
        self.problem.setStyleSheet("color:#a34838;")
        form.addRow("Purpose", self.purpose)
        form.addRow("Operator ID", self.operator)
        form.addRow("Engineering budget", self.budget)
        layout.addLayout(form)
        layout.addWidget(self.policy)
        layout.addWidget(self.problem)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept_contract)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.purpose.currentIndexChanged.connect(self._purpose_changed)
        self._purpose_changed()

    def _purpose_changed(self):
        daily = self.purpose.currentData() == "daily_annotation"
        self.budget.setEnabled(not daily)
        self.policy.setText("No research deadline. Listening and annotation remain your decisions."
                            if daily else "Engineering records remain engineering evidence; this is not a human experiment.")
        if daily and self.operator.text() == "local-engineering-operator":
            self.operator.clear()
        elif not daily and not self.operator.text():
            self.operator.setText("local-engineering-operator")

    def options(self):
        return {"purpose": self.purpose.currentData(), "operator_id": self.operator.text().strip(),
                "budget_seconds": None if self.purpose.currentData() == "daily_annotation" else int(self.budget.value())}

    def _accept_contract(self):
        if not self.operator.text().strip():
            self.problem.setText("Enter the operator ID before creating a project.")
            return
        self.accept()


class MainWindow(QMainWindow):
    projectReady = Signal(object)
    operationFinished = Signal(str)
    operationFailed = Signal(str)

    @property
    def selected_item_id(self):
        items = getattr(self, "selected_item_ids", ())
        return items[0] if len(items) == 1 else None

    @selected_item_id.setter
    def selected_item_id(self, item_id):
        self.selected_item_ids = () if item_id is None else (item_id,)

    def __init__(self, parent=None, *, diagnostics_dir=None):
        super().__init__(parent)
        apply_light_theme()
        self.diagnostics = DesktopDiagnostics(diagnostics_dir)
        self._active_operation = "desktop startup"
        self.setWindowTitle("Syllafolio — Native Desktop Technical Prototype")
        self.resize(1440, 1000)
        self.setMinimumSize(980, 600)
        self.setStyleSheet(STYLE)
        self.project = None
        self.state = None
        self.selected_group = None
        self.selected_review_id = None
        self.selected_page_id = None
        self.selected_item_id = None
        self._page = None
        self._page_audio = None
        self._page_rate = 0
        self._page_key = None
        self._page_load_error = False
        self._rows = {}
        self._page_defaults = {}
        self._explicit_items = set()
        self._new_families = {}
        self._draft_base = {}
        self._draft_store = {}
        self._draft_source_issue = None
        self._pending_submission = None
        self._dirty = False
        self._rendering = False
        self._busy = False
        self._worker = None
        self._foreground = None
        self._close_coordinator = CloseCoordinator()
        self._transition = None
        self._closing_approved = False
        self._job_view = {"jobs": [], "blocking_jobs": [], "can_close": True}
        self._job_signature = None
        self._task_issue_seen = set()
        self.project_details_dialog = None
        self._pending_refresh = False
        self._sampling_pending = False
        self._cancel_requests = None
        self._reported_cancellations = set()
        self._after_operation = None
        self._stage_a_dialog = None
        self._interval_import_dialog = None
        self._interval_import_ticket = None
        self._audition_preview = None
        self._fine_review_dialog = None
        self._review_media_dialog = None
        self._display_dialog = None
        self._pool_review_dialog = None
        self._final_review_dialog = None
        self._catalogue_review_dialog = None
        self._source_registry_dialog = None
        self._assessment_dialog = None
        self._catalogue_export_dialog = None
        self._closure_requests = {}
        self._closure_drafts = {}
        self.display_controller = DisplayController()
        self._preview = None
        self._queue_signature = None
        self._pages_signature = None
        self._logged_review_failures = set()
        self._build_ui()
        self.audio = PcmPlayer(self)
        self.audio.positionChanged.connect(self.canvas.set_position)
        self.audio.playingChanged.connect(self._update_playback_buttons)
        self.audio.statusChanged.connect(self._status)
        self.audio.errorOccurred.connect(self._audio_error)
        self._update_playback_buttons(False)
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(350)
        self._poll_timer.timeout.connect(self._poll)
        self._poll_timer.start()
        self.settings = QSettings("Syllafolio", "DesktopPrototype")
        geometry = self.settings.value("geometry")
        if geometry:
            self.restoreGeometry(geometry)
        self._fit_available_screen()
        self._watch_workspace_focus()
        self._set_enabled()
        self.diagnostics.install_exception_hook(self._diagnostic_context)
        self.diagnostics.record_event("INFO", "Native desktop window created.", "startup")

    def _fit_available_screen(self):
        screen = QApplication.screenAt(self.frameGeometry().center()) or QApplication.primaryScreen()
        if screen is None:
            return
        available = screen.availableGeometry()
        width = max(1, available.width() - 32)
        height = max(1, available.height() - 64)
        self.setMinimumSize(min(980, width), min(600, height))
        self.resize(min(self.width(), width), min(self.height(), height))
        x = min(max(self.x(), available.left()), available.right() - self.width() + 1)
        y = min(max(self.y(), available.top()), available.bottom() - self.height() + 1)
        self.move(x, y)

    def _button(self, text, name, handler, primary=False):
        button = QPushButton(text)
        button.setObjectName(name)
        button.setProperty("primary", primary)
        button.clicked.connect(lambda _checked=False: self._guard(handler, operation=name))
        return button

    def _menu_action(self, menu, text, name, handler):
        action = QAction(text, self)
        action.setObjectName(name)
        action.triggered.connect(lambda _checked=False: self._guard(handler, operation=name))
        menu.addAction(action)
        return action

    def _build_ui(self):
        self.project_menu = self.menuBar().addMenu("Project")
        self.project_menu.setObjectName("projectMenu")
        self.workspace_menu = self.menuBar().addMenu("Workspace")
        self.workspace_menu.setObjectName("workspaceMenu")
        self.view_menu = self.menuBar().addMenu("View")
        self.view_menu.setObjectName("viewMenu")
        self.help_menu = self.menuBar().addMenu("Help")
        self.help_menu.setObjectName("helpMenu")
        self.import_button = self._menu_action(self.project_menu, "New project from syllables…", "importProject", self._import_dialog)
        self.interval_import_action = self._menu_action(self.project_menu, "New project from recording and annotations…", "importRecordingAnnotations", self._show_interval_import)
        self.open_button = self._menu_action(self.project_menu, "Open project…", "openProject", self._open_dialog)
        self.backup_button = self._menu_action(self.project_menu, "Back up a project copy…", "backupProject", self._backup_dialog)
        self.export_button = self._menu_action(self.project_menu, "Export saved records…", "exportRecords", self._export_dialog)
        self.project_menu.addSeparator()
        self._dialog_menu_actions = {}
        for menu, title, name, dialog_attr, callback in (
            (self.workspace_menu, "Final checks", "finalWorkspace", "_final_review_dialog", self._show_final_review),
            (self.workspace_menu, "Type definitions and directed review", "catalogueWorkspace", "_catalogue_review_dialog", self._show_catalogue_review),
            (self.workspace_menu, "Internal assessment", "assessmentWorkspace", "_assessment_dialog", self._show_assessment),
            (self.workspace_menu, "Catalogue delivery", "catalogueExportWorkspace", "_catalogue_export_dialog", self._show_catalogue_export),
            (self.project_menu, "Recording sources…", "sourceRegistryWorkspace", "_source_registry_dialog", self._show_source_registry),
            (self.project_menu, "Checkpoints and history…", "checkpointWorkspace", "_final_review_dialog", self._show_checkpoint_history),
        ):
            action = menu.addAction(title)
            action.setObjectName(name)
            action.setCheckable(True)
            action.triggered.connect(lambda _checked=False, fn=callback: self._activate_workspace(fn))
            self._dialog_menu_actions[action] = dialog_attr
        self.project_menu.aboutToShow.connect(self._sync_workspace_checks)
        self.workspace_menu.aboutToShow.connect(self._sync_workspace_checks)
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(12, 10, 12, 8)
        summary = QHBoxLayout()
        summary.setSpacing(0)
        summary.addStretch(1)
        self.navigation_label = QLabel("Open a project to begin")
        self.navigation_label.setObjectName("projectNavigation")
        self.navigation_label.setMinimumWidth(0)
        self.navigation_label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.navigation_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        summary.addWidget(self.navigation_label)
        # Balance the left stretch even when the task button is visible.
        task_area = QHBoxLayout()
        task_area.addStretch()
        summary.addLayout(task_area, 1)
        self.tasks_button = self._button("Tasks", "projectTasksWindow", self._show_tasks)
        self.tasks_button.hide()
        task_area.addWidget(self.tasks_button)
        root.addLayout(summary)
        self.header_blockers = QLabel()
        self.header_blockers.setObjectName("headerBlockers")
        self.header_blockers.setWordWrap(True)
        self.header_blockers.hide()
        root.addWidget(self.header_blockers)
        warning_row = QHBoxLayout()
        self.draft_warning = QLabel()
        self.draft_warning.setObjectName("draftWarning")
        self.draft_warning.setWordWrap(True)
        self.draft_warning.setStyleSheet("background:#fff3d8;color:#694800;padding:7px;border:1px solid #d9bd7b;")
        self.draft_warning.hide()
        self.dismiss_draft_warning = QPushButton("Dismiss draft warning")
        self.dismiss_draft_warning.setObjectName("dismissDraftWarning")
        self.dismiss_draft_warning.hide()
        self.dismiss_draft_warning.clicked.connect(self._dismiss_draft_warning)
        warning_row.addWidget(self.draft_warning, 1)
        warning_row.addWidget(self.dismiss_draft_warning)
        root.addLayout(warning_row)
        self.task_dialog = QDialog(self)
        self.task_dialog.setWindowTitle("Project tasks")
        self.task_dialog.setModal(False)
        self.task_dialog.resize(900, 280)
        task_layout = QVBoxLayout(self.task_dialog)
        self.job_box = QGroupBox("Tasks")
        job_layout = QVBoxLayout(self.job_box)
        self.job_list = QTreeWidget()
        self.job_list.setObjectName("projectTasks")
        self.job_list.setHeaderLabels(["Task / ID", "State", "Progress / reason"])
        self.job_list.setMaximumHeight(105)
        self.job_list.setRootIsDecorated(False)
        job_layout.addWidget(self.job_list)
        job_actions = QHBoxLayout()
        self.job_summary = QLabel()
        self.job_summary.setWordWrap(True)
        job_actions.addWidget(self.job_summary, 1)
        self.cancel_selected_job = self._button("Cancel selected task", "cancelSelectedTask", self.cancel_operation)
        self.return_to_workspace = self._button("Return to workspace", "abortClose", self.abort_transition)
        self.return_to_workspace.hide()
        job_actions.addWidget(self.cancel_selected_job)
        job_actions.addWidget(self.return_to_workspace)
        job_layout.addLayout(job_actions)
        self.job_box.hide()
        task_layout.addWidget(self.job_box)
        self.main_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.main_splitter.setObjectName("mainSplitter")
        sidebar = QWidget()
        sidebar.setObjectName("reviewSidebar")
        sidebar.setMinimumWidth(274)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(0, 0, 6, 0)
        self.groups_heading = QLabel("Review scopes")
        self.groups_heading.setStyleSheet("font-weight:600;")
        side.addWidget(self.groups_heading)
        self.group_list = ReviewTree()
        grouping = self.group_list.candidate_menu
        self.open_candidate_workspace_action = self._menu_action(
            grouping, "Open candidate workspace…", "openCandidateWorkspace",
            lambda: self._activate_workspace(self._show_organization_tab))
        grouping.addSeparator()
        self.apply_recommendation = self._menu_action(grouping, "Preview recommended groups…", "applyRecommendation",
                                                     lambda: self.preview_organization("auto"))
        self.keep_clusters = self._menu_action(grouping, "Preview original microclusters…", "keepMicroclusters",
                                              lambda: self.preview_organization("microclusters"))
        self.group_list.setObjectName("groupList")
        self.group_list.setHeaderLabels(["Group", "Syllables", "Review"])
        self.group_list.setUniformRowHeights(True)
        self.group_list.setIndentation(14)
        self.group_list.setExpandsOnDoubleClick(False)
        self._expanded_review_sections = set()
        self._pending_tree_navigation = None
        self._tree_navigation_timer = QTimer(self)
        self._tree_navigation_timer.setSingleShot(True)
        self._tree_navigation_timer.timeout.connect(self._apply_tree_navigation)
        self.group_list.itemSelectionChanged.connect(self._group_changed)
        self.group_list.itemExpanded.connect(self._tree_item_expanded)
        self.group_list.itemCollapsed.connect(self._tree_item_collapsed)
        self.group_list.itemActivated.connect(self._tree_item_activated)
        self.group_list.createFamilyRequested.connect(
            lambda: self._guard(lambda: self._family_dialog(reveal=True), operation="create human family"))
        self.group_list.renameFamilyRequested.connect(self._queue_family_rename)
        header = self.group_list.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col in (1, 2):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        self.group_list.headerItem().setTextAlignment(1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.group_list.setStyleSheet("QTreeView::item { padding: 3px 1px; }")
        side.addWidget(self.group_list, 1)
        scope_actions = QHBoxLayout()
        self.prepare_button = self._button("Prepare group", "prepareReview", self._prepare_review_ui, True)
        self.close_review_button = self._button("Finish group…", "finishReview", self._finish_dialog)
        scope_actions.addWidget(self.prepare_button)
        scope_actions.addWidget(self.close_review_button)
        scope_actions.addStretch()
        self.more_button = QToolButton()
        self.more_button.setObjectName("reviewOptions")
        self.more_button.setText("More")
        self.more_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        more = QMenu(self.more_button)
        self.more_button.setMenu(more)
        self.review_details_action = self._menu_action(more, "Review details…", "reviewDetails", self._show_scope_details)
        self.manage_scope_action = self._menu_action(more, "Pool details…", "manageScope", self._show_pool_details)
        self.prepare_again_action = self._menu_action(more, "Prepare again…", "prepareAgain", self._prepare_again)
        self.exception_action = self._menu_action(more, "Prepare with exception…", "prepareException",
                                                 lambda: self._prepare_again(use_exception=True))
        scope_actions.addWidget(self.more_button)
        side.addLayout(scope_actions)
        self.main_splitter.addWidget(sidebar)
        right = QWidget()
        work = QVBoxLayout(right)
        work.setContentsMargins(6, 0, 0, 0)
        self.review_controls = QWidget()
        self.review_controls.setObjectName("reviewControls")
        self.review_controls_layout = QGridLayout(self.review_controls)
        self.review_controls_layout.setContentsMargins(0, 0, 0, 0)
        self.review_controls_layout.setColumnStretch(1, 1)
        self.review_playback_controls = QWidget()
        self.review_playback_controls.setObjectName("playbackControls")
        tools = QHBoxLayout(self.review_playback_controls)
        tools.setContentsMargins(0, 0, 0, 0)
        self.play_page = self._button("Play", "playPage", self.play_current_page)
        self.loop_item = self._button("Loop", "loopItem", self.loop_current_item)
        self.play_page.setToolTip("Play the entire page")
        self.loop_item.setToolTip("Loop the selected syllable")
        pause_width = self.play_page.fontMetrics().horizontalAdvance("Pause") + 24
        for button in (self.play_page, self.loop_item):
            button.setMinimumWidth(pause_width)
        self.fit_button = self._button("Fit", "fitPage", lambda: self.canvas.fit_page())
        for button in (self.play_page, self.loop_item, self.fit_button):
            tools.addWidget(button)
        self.review_controls_layout.addWidget(self.review_playback_controls, 0, 0)
        self.review_family_controls = QWidget()
        self.review_family_controls.setObjectName("familyControls")
        context = QHBoxLayout(self.review_family_controls)
        context.setContentsMargins(0, 0, 0, 0)
        context.addWidget(QLabel("Current human family"))
        self.current_family = QComboBox()
        self.current_family.setObjectName("currentFamily")
        self.current_family.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.current_family.setMinimumContentsLength(12)
        self.current_family.setMinimumWidth(120)
        self.current_family.currentIndexChanged.connect(self._current_family_changed)
        context.addWidget(self.current_family, 1)
        self.create_family_button = self._button("Create family…", "createFamily", self._family_dialog)
        context.addWidget(self.create_family_button)
        self._review_controls_wrapped = True
        self.review_controls_layout.addWidget(self.review_family_controls, 1, 0, 1, 3)
        self.review_gap_controls = QWidget()
        gap_layout = QHBoxLayout(self.review_gap_controls)
        gap_layout.setContentsMargins(0, 0, 0, 0)
        gap_layout.addWidget(QLabel("Page gap"))
        self.gap = QDoubleSpinBox()
        self.gap.setObjectName("silenceGap")
        self.gap.setRange(.05, 2)
        self.gap.setSingleStep(.05)
        self.gap.setValue(.5)
        self.gap.setSuffix(" s")
        gap_layout.addWidget(self.gap)
        self.review_controls_layout.addWidget(self.review_gap_controls, 0, 2)
        self.review_controls.installEventFilter(self)
        work.addWidget(self.review_controls)
        self.work_splitter = QSplitter(Qt.Orientation.Vertical)
        self.work_splitter.setObjectName("auditionSplitter")
        self.plot_panel = QWidget()
        plot_layout = QVBoxLayout(self.plot_panel)
        plot_layout.setContentsMargins(0, 0, 0, 0)
        self.canvas = AuditionCanvas()
        # The plot can shrink on a laptop while the confirmation/save controls
        # remain fixed and reachable; users can enlarge it with the splitter.
        self.canvas.wave.setMinimumHeight(45)
        self.canvas.spec.setMinimumHeight(100)
        self.canvas.itemClicked.connect(lambda item_id, ctrl: self._guard(
            lambda: self._item_clicked(item_id, ctrl), operation="change syllable selection"))
        plot_layout.addWidget(self.canvas, 1)
        self.slot_list = QListWidget()
        self.slot_list.setObjectName("slotList")
        self.slot_list.setFlow(QListWidget.Flow.LeftToRight)
        self.slot_list.setWrapping(False)
        self.slot_list.setUniformItemSizes(True)
        self.slot_list.setMaximumHeight(65)
        self.slot_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.slot_list.viewport().installEventFilter(self)
        self.slot_list.installEventFilter(self)
        self.slot_list.currentItemChanged.connect(self._slot_changed)
        plot_layout.addWidget(self.slot_list)
        self.work_splitter.addWidget(self.plot_panel)
        # Keep both splitter panes present even when there is no single target.
        # Selection, validation messages and form fields belong below the handle.
        inspector = QWidget()
        inspector.setObjectName("reviewInspectorPane")
        inspector.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        inspector_layout = QVBoxLayout(inspector)
        inspector_layout.setContentsMargins(0, 0, 0, 0)
        self.item_editor = QFrame()
        self.item_editor.setObjectName("itemEditor")
        edit = QVBoxLayout(self.item_editor)
        edit.setContentsMargins(10, 7, 10, 7)
        edit.setSpacing(4)
        heading = QHBoxLayout()
        self.item_title = QLabel("Choose a syllable above")
        self.item_title.setObjectName("itemTitle")
        self.item_title.setStyleSheet("font-weight:600;")
        self.item_status = QLabel("")
        self.item_status.setObjectName("itemStatus")
        self.item_title.setMinimumWidth(0)
        self.item_status.setMinimumWidth(0)
        heading.addWidget(self.item_title)
        heading.addWidget(self.item_status)
        heading.addStretch()
        self.previous_item = self._button("Previous", "previousItem", lambda: self._step_item(-1))
        self.next_item = self._button("Next", "nextItem", lambda: self._step_item(1))
        for button in (self.previous_item, self.next_item):
            button.setFlat(True)
            heading.addWidget(button)
        self.item_tools = QToolButton()
        self.item_tools.setObjectName("itemTools")
        self.item_tools.setText("Item tools")
        self.item_tools.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(self.item_tools)
        self.item_tools.setMenu(menu)
        self.review_media_button = self._menu_action(menu, "Source / references…", "reviewSourceReferences", self._show_review_media)
        self.fine_review_button = self._menu_action(menu, "Fine review…", "fineReview", self._show_fine_review)
        menu.addSeparator()
        self.details_toggle = menu.addAction("Boundary and form details")
        self.details_toggle.setObjectName("detailsToggle")
        self.details_toggle.setCheckable(True)
        heading.addWidget(self.item_tools)
        edit.addLayout(heading)
        editor_scroll = QScrollArea()
        self.item_form_scroll = editor_scroll
        editor_scroll.setObjectName("itemFormScroll")
        editor_scroll.setWidgetResizable(True)
        editor_scroll.setFrameShape(QFrame.Shape.NoFrame)
        form_body = QWidget()
        form_body.setObjectName("itemExtraFields")
        form = QFormLayout(form_body)
        form.setContentsMargins(0, 0, 2, 0)
        self.disposition = QComboBox()
        self.disposition.setObjectName("disposition")
        self.disposition.addItem("Not reviewed", "")
        for value, label in DISPOSITIONS:
            self.disposition.addItem(label, value)
        self.disposition.currentIndexChanged.connect(self._disposition_changed)
        self.disposition.setMaximumWidth(320)
        outcome = QHBoxLayout()
        outcome.addWidget(QLabel("Outcome"))
        outcome.addWidget(self.disposition)
        self.notes_toggle = QToolButton()
        self.notes_toggle.setObjectName("notesToggle")
        self.notes_toggle.setText("Add note")
        self.notes_toggle.setCheckable(True)
        self.notes_toggle.setAutoRaise(True)
        outcome.addWidget(self.notes_toggle)
        outcome.addStretch()
        edit.addLayout(outcome)
        self.reason_code = QComboBox()
        self.reason_code.setObjectName("reasonCode")
        self.reason_code.currentIndexChanged.connect(self._editor_changed)
        self.reason_code_label = QLabel("Reason category")
        form.addRow(self.reason_code_label, self.reason_code)
        self.target_family = QComboBox()
        self.target_family.setObjectName("targetFamily")
        self.target_family.currentIndexChanged.connect(self._editor_changed)
        self.target_label = QLabel("Target family")
        form.addRow(self.target_label, self.target_family)
        self.target_empty = QLabel("")
        self.target_empty.setObjectName("targetFamilyEmpty")
        self.target_empty.setWordWrap(True)
        form.addRow(self.target_empty)
        self.new_family_draft = QComboBox()
        self.new_family_draft.setObjectName("newFamilyDraft")
        self.new_family_draft.currentIndexChanged.connect(self._proposal_changed)
        self.new_draft_label = QLabel("Shared new-family draft")
        form.addRow(self.new_draft_label, self.new_family_draft)
        self.new_title = QLineEdit()
        self.new_title.setObjectName("newFamilyTitle")
        self.new_reason = QLineEdit()
        self.new_reason.setObjectName("newFamilyReason")
        self.new_title.textEdited.connect(self._editor_changed)
        self.new_reason.textEdited.connect(self._editor_changed)
        self.new_title_label = QLabel("Shared family name")
        self.new_reason_label = QLabel("Shared creation basis")
        form.addRow(self.new_title_label, self.new_title)
        form.addRow(self.new_reason_label, self.new_reason)
        self.reason = QPlainTextEdit()
        self.reason.setObjectName("reason")
        self.reason.setMaximumHeight(65)
        self.reason.setPlaceholderText("Optional note. Leave blank when there is no additional observation to record.")
        self.reason.textChanged.connect(self._editor_changed)
        self.notes_body = QWidget()
        notes_layout = QVBoxLayout(self.notes_body)
        notes_layout.setContentsMargins(0, 0, 0, 0)
        self.evidence_label = QLabel("Evidence (required)")
        self.evidence_label.setBuddy(self.reason)
        notes_layout.addWidget(self.evidence_label)
        notes_layout.addWidget(self.reason)
        form.addRow(self.notes_body)
        self.notes_body.hide()
        self.notes_toggle.toggled.connect(self._toggle_notes)
        self.form_status = QComboBox()
        self.form_status.setObjectName("formStatus")
        self.form_status.addItem("Form not assessed", "not_assessed")
        self.form_status.addItem("Form reviewed, still unresolved", "unresolved")
        self.form_status.currentIndexChanged.connect(self._editor_changed)
        self.form_status_label = QLabel("Within-family form")
        form.addRow(self.form_status_label, self.form_status)
        self.details_body = QWidget()
        details_layout = QVBoxLayout(self.details_body)
        details_layout.setContentsMargins(0, 0, 0, 0)
        self.legacy_form_info = QLabel()
        self.legacy_form_info.setTextFormat(Qt.TextFormat.PlainText)
        self.legacy_form_info.setWordWrap(True)
        details_layout.addWidget(self.legacy_form_info)
        self.boundary = QCheckBox("Boundary needs checking")
        self.boundary.setObjectName("boundaryFlag")
        self.boundary.toggled.connect(self._editor_changed)
        details_layout.addWidget(self.boundary)
        self.boundary_help = QLabel("An existing pending boundary blocks Finish. Clear it only after checking it; detailed form decisions use Fine review.")
        self.boundary_help.setWordWrap(True)
        details_layout.addWidget(self.boundary_help)
        form.addRow(self.details_body)
        self.details_body.hide()
        self.details_toggle.toggled.connect(self._toggle_details)
        editor_scroll.setWidget(form_body)
        editor_scroll.hide()
        edit.addWidget(editor_scroll, 1)
        self.confirm = QCheckBox("I listened to / inspected this syllable and checked the evidence and outcome.")
        self.confirm.setObjectName("confirm")
        edit.addWidget(self.confirm)
        self.confirm.hide()
        inspector_layout.addWidget(self.item_editor, 1)
        # Use the reserve for real fields first; leave blank space only after
        # the editor reaches its content height (or when no item is selected).
        inspector_layout.addStretch()
        self.page_save_bar = QFrame()
        self.page_save_bar.setObjectName("pageSaveBar")
        footer = QVBoxLayout(self.page_save_bar)
        footer.setContentsMargins(10, 5, 10, 5)
        footer.setSpacing(3)
        self.page_defaults_info = QLabel("")
        self.page_defaults_info.setObjectName("pageDefaultsInfo")
        self.page_defaults_info.setWordWrap(True)
        footer.addWidget(self.page_defaults_info)
        actions = QHBoxLayout()
        self.page_summary = QLabel("No review page")
        self.page_summary.setObjectName("pageSummary")
        self.page_summary.setWordWrap(True)
        self.page_summary.setToolTip("* = pending save. Save page submits pending outcomes on this page, regardless of playback selection.")
        actions.addWidget(self.page_summary, 1)
        self.save_next = self._button("Save and next", "saveNext", lambda: self.save_selected(True))
        self.save_next.setShortcut(QKeySequence("Ctrl+Return"))
        actions.addWidget(self.save_next)
        self.save_options = QToolButton()
        self.save_options.setObjectName("itemSaveOptions")
        self.save_options.setText("…")
        self.save_options.setAccessibleName("Item save options")
        self.save_options.setToolTip("Item save options")
        self.save_options.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        choices = QMenu(self.save_options)
        self.save_options.setMenu(choices)
        self.save_item = self._menu_action(choices, "Save item", "saveItem", lambda: self.save_selected(False))
        self.save_item.setShortcut(QKeySequence("Ctrl+S"))
        self.addAction(self.save_item)
        actions.addWidget(self.save_options)
        self.save_page = self._button("Save page", "savePage", self.save_page_decisions, True)
        actions.addWidget(self.save_page)
        footer.addLayout(actions)
        inspector_layout.addWidget(self.page_save_bar)
        # A stable minimum leaves room for both editor rows, one scrollable
        # field, directed confirmation and the footer even on a small window.
        row_height = self.disposition.sizeHint().height()
        inspector.setMinimumHeight(3 * row_height + self.save_page.sizeHint().height()
                                   + self.confirm.sizeHint().height() + 36)
        self.work_splitter.addWidget(inspector)
        self.work_splitter.setChildrenCollapsible(False)
        self.work_splitter.setSizes([470, 330])  # Original audition / annotation split.
        work.addWidget(self.work_splitter, 1)
        info = self.display_info = QLabel()
        info.setWordWrap(True)
        info.setStyleSheet("font-size:10px;color:#607b83;")
        # Parameters remain available from Display and its tooltip, rather than
        # taking a permanent row away from the ordinary disposition workflow.
        info.hide()
        self.main_splitter.addWidget(right)
        self.main_splitter.setSizes([310, 1090])
        root.addWidget(self.main_splitter, 1)
        progress = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setObjectName("operationProgress")
        self.progress.hide()
        progress.addWidget(self.progress, 1)
        self.cancel_button = self._button("Cancel calculation", "cancelOperation", self.cancel_operation)
        self.cancel_button.hide()
        progress.addWidget(self.cancel_button)
        root.addLayout(progress)
        self.setCentralWidget(central)
        self.status_label = QLabel("Ready. No project is open.")
        self.status_label.setObjectName("statusLabel")
        self.status_label.setWordWrap(True)
        self.statusBar().addWidget(self.status_label, 1)
        help_action = QAction("About this development preview…", self)
        help_action.setShortcut(QKeySequence.StandardKey.HelpContents)
        help_action.triggered.connect(lambda: QMessageBox.information(self, "Syllafolio development preview",
            "Native syllable review uses the existing project and annotation protocol.\n"
            "Research mode and model calls are disabled. Listening, type criteria and final judgments remain the operator's responsibility.\n"
            "A development preview does not replace an accepted release or verify scientific efficacy."))
        self.project_details_action = self._menu_action(self.help_menu, "Project details…", "projectDetailsAction", self._show_project_details)
        self.help_menu.addAction(help_action)
        self.tasks_action = self._menu_action(self.view_menu, "Tasks…", "projectTasksAction", self._show_tasks)
        self.display_action = self._menu_action(self.view_menu, "Display settings…", "displaySettings", self._show_review_display)
        self._update_display_info()

    def eventFilter(self, watched, event):
        slots = getattr(self, "slot_list", None)
        if slots is not None and watched in (slots, slots.viewport()):
            if (watched is slots.viewport() and event.type() in
                    (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonDblClick)
                    and event.button() == Qt.MouseButton.LeftButton):
                item = slots.itemAt(event.position().toPoint())
                ctrl = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
                if item is not None:
                    self._guard(lambda: self._item_clicked(item.data(Qt.ItemDataRole.UserRole), ctrl), operation="change list selection")
                elif not ctrl:
                    self._guard(lambda: self.select_items(()), operation="clear list selection")
                return True
            if (watched is slots.viewport() and event.type() == QEvent.Type.MouseButtonRelease
                    and event.button() == Qt.MouseButton.LeftButton):
                return True
            if event.type() == QEvent.Type.KeyPress:
                key = event.key()
                ctrl = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
                if key == Qt.Key.Key_Escape:
                    self._guard(lambda: self.select_items(()), operation="clear list selection")
                    return True
                if ctrl and key == Qt.Key.Key_A:
                    self._guard(lambda: self.select_items((self._page or {}).get("item_ids", [])), operation="select page items")
                    return True
                if key in (Qt.Key.Key_Left, Qt.Key.Key_Up, Qt.Key.Key_Right, Qt.Key.Key_Down):
                    self._guard(lambda: self._step_item(-1 if key in (Qt.Key.Key_Left, Qt.Key.Key_Up) else 1),
                                operation="navigate syllable selection")
                    return True
                if key == Qt.Key.Key_Space:
                    item = slots.currentItem()
                    if item is not None:
                        self._guard(lambda: self._item_clicked(item.data(Qt.ItemDataRole.UserRole), ctrl),
                                    operation="toggle list selection")
                    return True
        if watched is getattr(self, "review_controls", None) and event.type() == QEvent.Type.Resize:
            self._layout_review_controls()
        return super().eventFilter(watched, event)

    def _layout_review_controls(self):
        layout = self.review_controls_layout
        needed = (self.review_playback_controls.minimumSizeHint().width()
                  + self.review_family_controls.minimumSizeHint().width()
                  + self.review_gap_controls.minimumSizeHint().width() + 2 * layout.horizontalSpacing() + 12)
        wrapped = self.review_controls.width() < needed
        if wrapped != self._review_controls_wrapped:
            self._review_controls_wrapped = wrapped
            layout.removeWidget(self.review_family_controls)
            if wrapped:
                layout.addWidget(self.review_family_controls, 1, 0, 1, 3)
            else:
                layout.addWidget(self.review_family_controls, 0, 1)

    def _activate_workspace(self, callback):
        result = self._guard(callback, operation="open workspace")
        if isinstance(result, QWidget):
            project = self.project
            generation = self._close_coordinator.generation
            def activate_after_menu_close():
                if (self.project is not project or not self._close_coordinator.accepts_callback(generation)
                        or not getattr(self, "_workspace_focus_alive", False)):
                    return
                try:
                    if result.isVisible() and getattr(result, "project", project) is project:
                        result.raise_()
                        result.activateWindow()
                except RuntimeError:
                    pass  # A dialog closed/deleted before the queued focus request stays closed.
            QTimer.singleShot(0, activate_after_menu_close)
        self._sync_workspace_checks()
        QTimer.singleShot(0, self._sync_workspace_checks)
        return result

    def _watch_workspace_focus(self):
        application = QApplication.instance()
        owner = weakref.ref(self)
        self._workspace_focus_alive = True
        connected = True
        def changed(_window):
            def after_activation():
                window = owner()
                if window is not None and window._workspace_focus_alive and not window._closing_approved:
                    window._sync_workspace_checks()
            # Native menu closure and QWidget activation finish after the
            # triggered callback. Read the actual active window afterwards.
            QTimer.singleShot(0, after_activation)
        def disconnect(*_args):
            nonlocal connected
            if not connected:
                return
            connected = False
            window = owner()
            if window is not None:
                window._workspace_focus_alive = False
            try:
                application.focusWindowChanged.disconnect(changed)
            except (RuntimeError, TypeError):
                pass
        self._disconnect_workspace_focus = disconnect
        application.focusWindowChanged.connect(changed)
        self.destroyed.connect(disconnect)

    def _sync_workspace_checks(self):
        if (not hasattr(self, "_dialog_menu_actions") or self._closing_approved
                or not getattr(self, "_workspace_focus_alive", True)):
            return
        if QApplication.activePopupWidget() is not None:
            return  # A popup is not a switch away from the last known workspace.
        active = QApplication.activeWindow()
        for action, attr in self._dialog_menu_actions.items():
            dialog = getattr(self, attr, None)
            checked = False
            if dialog is not None and dialog.isVisible():
                checked = action.isChecked() if active is None else active is dialog or dialog.isAncestorOf(active)
                if checked and dialog is self._final_review_dialog:
                    checked = (action.objectName() == "checkpointWorkspace") == dialog.history_visible()
            action.setChecked(checked)

    def _project_identity_text(self):
        if not self.project or not self.state:
            return "No project is open."
        contract = self.state.get("contract", {})
        budget = contract.get("budget_seconds")
        purpose = "Daily annotation" if contract.get("purpose") == "daily_annotation" else "Engineering project"
        navigation = self.state.get("desktop_view", {}).get("navigation", {})
        workflow_names = {"A": "Candidates and organisation", "B": "Candidate and family review",
                          "C": "Global candidate pool", "D": "Final checks"}
        prerequisite_names = {"ORGANIZATION_REQUIRED": "Apply a candidate organisation",
            "BATCH_FIRST_PASS_PENDING": "Finish the candidate review groups", "POOL_REVIEW_PENDING": "Finish the global pool review"}
        prerequisites = list(dict.fromkeys(prerequisite_names.get(code, "Inspect the outstanding workspace requirements")
            for row in navigation.get("stages", []) for code in row.get("blockers", [])))
        preprocessing = self.project.metadata.get("preparation_summary", {}).get("audio_preprocessing")
        filter_details = ()
        if isinstance(preprocessing, dict) and all(key in preprocessing for key in ("low_hz", "high_hz", "order")):
            filter_details = (f"Import band-pass filter: {preprocessing['low_hz']:g}–{preprocessing['high_hz']:g} Hz",
                f"Filter order: {preprocessing['order']}", f"Filter method: {preprocessing.get('method', 'Not recorded')}")
        return "\n".join((f"Project: {self.project.project_dir.name}", f"Location: {self.project.project_dir}",
            f"Project ID: {self.project.metadata.get('project_id')}", f"Run ID: {self.project.service.run_id}",
            f"Purpose: {purpose}", f"Operator: {contract.get('operator_id')}",
            "Budget: No deadline" if budget is None else f"Budget: {budget} s engineering budget",
            f"Syllables: {len(self.state.get('items', []))}", f"Protocol: {self.state.get('batch_review', {}).get('protocol_version')}",
            f"Evidence source: {contract.get('evidence_source', contract.get('source', 'Not recorded'))}",
            f"Record kind: {contract.get('record_kind', 'Not recorded')}", "Research: disabled", "Model calls: disabled",
            "Workflow status: " + workflow_names.get(navigation.get("current_stage", "A"), "Review"),
            "Workflow prerequisites: " + ("; ".join(prerequisites) if prerequisites else "None outstanding"), *filter_details))

    def _show_project_details(self):
        if not self.project:
            raise ValueError("Open a project to view its identity and budget.")
        if self.project_details_dialog is None:
            self.project_details_dialog = QDialog(self)
            self.project_details_dialog.setObjectName("projectDetailsDialog")
            self.project_details_dialog.setWindowTitle("Project details")
            self.project_details_dialog.resize(680, 430)
            layout = QVBoxLayout(self.project_details_dialog)
            self.project_details_text = QPlainTextEdit()
            self.project_details_text.setObjectName("projectIdentityDetails")
            self.project_details_text.setReadOnly(True)
            layout.addWidget(self.project_details_text)
            close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
            close.rejected.connect(self.project_details_dialog.close)
            layout.addWidget(close)
        self.project_details_text.setPlainText(self._project_identity_text())
        self.project_details_dialog.show(); self.project_details_dialog.raise_(); self.project_details_dialog.activateWindow()
        return self.project_details_dialog

    def _update_header(self):
        if not self.project or not self.state:
            self.navigation_label.setText("Open a project to begin")
            self.header_blockers.hide()
            if self.project_details_dialog is not None:
                self.project_details_text.setPlainText("No project is open.")
            return
        batch = self.state.get("batch_review", {})
        review = self._review()
        navigation = self.state.get("desktop_view", {}).get("navigation", {})
        current = navigation.get("current_stage", "A")
        scope = {"A": "Candidate organisation", "B": "Candidate review", "C": "Global pool", "D": "Final review"}.get(current, "Review")
        if self.selected_group == "pool":
            current, scope = "C", "Global pool"
        elif self.selected_group and self.selected_group.startswith("family:"):
            fid = self.selected_group.removeprefix("family:")
            family = next((f for f in batch.get("families", []) if f["family_id"] == fid), {})
            current = "D2" if self._directed_editor() else "B"
            scope = ("Directed review / " if current == "D2" else "Family review / ") + (family.get("title") or fid)
        elif self.selected_group:
            group = next((g for g in (batch.get("organization") or {}).get("groups", []) if g["group_id"] == self.selected_group), {})
            current, scope = "B", "Candidate review / " + group.get("label", self.selected_group)
        summary = scope
        self.navigation_label.setText(summary)
        self.navigation_label.setToolTip(summary)
        if self.project_details_dialog is not None and self.project_details_dialog.isVisible():
            identity = self._project_identity_text()
            if self.project_details_text.toPlainText() != identity:
                self.project_details_text.setPlainText(identity)
        blockers = []
        if self.state.get("deadline_locked"):
            blockers.append("Submission deadline reached. Listening is still available.")
        if self._sampling_pending:
            blockers.append("Updating project status; writes are temporarily unavailable.")
        if review.get("scope_stale"):
            blockers.append("Review scope changed. Inspect the current scope before saving.")
        elif review.get("audition_only"):
            if review["stage"] == "family":
                blockers.append("Listening only. Finish the overlapping review(s) to edit this family: "
                                + ", ".join(review["review_blockers"]) + ".")
        elif any(review.get(k) for k in ("read_only", "archived", "closed")):
            blockers.append("This review is read-only; saved outcomes are retained.")
        elif review.get("status") == "failed":
            blockers.append("Review preparation failed. Open Tasks for the error and next step.")
        if self.selected_group == "pool":
            pool = batch.get("pool", {})
            if review.get("pool_refresh_required") and pool.get("active_ids"):
                action = "Prepare group" if review.get("closed") else "More → Prepare again…"
                blockers.append(f"The current pool has changed. Use {action} to review the updated pool.")
            elif not review and not pool.get("active_ids") and batch.get("organization") and not pool.get("can_prepare"):
                blockers.append("Finish the outstanding reviews before recording an empty-pool skip.")
        self.header_blockers.setText(" ".join(blockers))
        self.header_blockers.setVisible(bool(blockers))

    def _task_issue_keys(self, view):
        return {(row.get("run_id"), row.get("kind"), row.get("job_id"), _hash(row.get("error") or row.get("raw_status")))
            for row in view.get("jobs", []) if row.get("worker_settled") is True
            and (row.get("phase") in {"failed", "interrupted"} or row.get("raw_status") in {"failed", "interrupted"})}

    def _update_task_entry(self, view):
        blocking = view.get("blocking_jobs", [])
        unread = self._task_issue_keys(view) - self._task_issue_seen
        ids = {(row.get("run_id"), row.get("kind"), row.get("job_id")) for row in blocking} | {key[:3] for key in unread}
        self.tasks_button.setText(f"Tasks ({len(ids)})")
        self.tasks_button.setToolTip("Open tasks to inspect progress, unfinished workers and new errors.")
        self.tasks_button.setVisible(bool(ids) or bool(self._transition))
        self.tasks_button.setEnabled(True)
        self.tasks_action.setEnabled(bool(self.project or self._foreground or view.get("jobs")))

    def _guard(self, function, operation=None):
        try:
            return function()
        except Exception as exc:
            self._error(exc, operation=operation or getattr(function, "__name__", "UI action"))
            return None

    def _status(self, text, error=False):
        self.status_label.setText(system_text(str(text)))
        self.status_label.setStyleSheet("color:#a34838;" if error else "color:#315d68;")

    def _diagnostic_context(self):
        return {"project_path": str(self.project.project_dir) if self.project else None,
                "run_id": self.project.service.run_id if self.project else None,
                "review_id": self.selected_review_id, "page_id": self.selected_page_id,
                "item_id": self.selected_item_id,
                "revision": (self.state or {}).get("revision"),
                "catalogue_revision": (self.state or {}).get("catalogue_revision"),
                "input_fingerprint": (self.state or {}).get("input_fingerprint")}

    def _record_failed_reviews(self):
        for review in (self.state or {}).get("batch_review", {}).get("reviews", {}).values():
            if review.get("status") != "failed":
                continue
            failure = review.get("error") or {}
            key = (str(self.project.project_dir), review["review_id"], _hash(failure))
            if key in self._logged_review_failures:
                continue
            self._logged_review_failures.add(key)
            context = self._diagnostic_context()
            context.update(review_id=review["review_id"], recorded_error=failure)
            self.diagnostics.record_event("ERROR", "Observed a recorded review computation failure. "
                "The service error is retained; no Python traceback was supplied by that job.",
                "review computation state", context)
        for job in self._job_view.get("jobs", []):
            if job.get("phase") not in {"failed", "interrupted", "unknown"}:
                continue
            key = (job.get("run_id"), job.get("kind"), job.get("job_id"), _hash(job.get("error")))
            if key in self._logged_review_failures:
                continue
            self._logged_review_failures.add(key)
            context = {**self._diagnostic_context(), "task": copy.deepcopy(job)}
            self.diagnostics.record_event("ERROR", "Recorded background task needs attention.",
                                          "task lifecycle", context)

    def _audio_error(self, message):
        self.diagnostics.record_event("ERROR", message, "audio playback", self._diagnostic_context())
        self._status(message, error=True)

    def _error(self, error, operation=None):
        context = self._diagnostic_context()
        if getattr(error, "desktop_receipt", None):
            context["command_receipt"] = error.desktop_receipt
        self.diagnostics.record_exception(error, operation or self._active_operation or "desktop",
                                          context)
        code = getattr(error, "code", type(error).__name__)
        english = {
            "FAMILY_NOT_FOUND": "This human family is no longer available. Refresh the family list.",
            "FAMILY_TITLE_REQUIRED": "Enter a family name.",
            "FAMILY_TITLE_CHANGED": "The name changed while you were editing. Check the current name and try again.",
            "FAMILY_TITLE_CONFLICT": "Another family already uses that name. Choose a different name.",
            "FAMILY_TITLE_UNCHANGED": "The family already has that name.",
            "REVISION_CONFLICT": "The project changed. Refresh and review the saved records before retrying.",
            "BATCH_FIRST_PASS_PENDING": "Finish every candidate group's first pass before pooled review.",
            "BATCH_POOL_IN_PROGRESS": "Continue the existing pooled review before preparing another one.",
            "BATCH_FAMILY_PENDING": "Complete current family reviews before preparing the pool.",
            "BATCH_SCOPE_STALE": "The review scope is stale. Prepare a current version before saving.",
            "STORE_IN_USE": "This project is already open in another application process.",
            "MEDIA_CHANGED": "Source audio identity has changed. The operation was stopped.",
            "BATCH_ROUTE": "The selected preparation route does not match the protocol.",
            "PROJECT_DRAINING": "Closing is pending. Wait, cancel the listed tasks, or return to the workspace.",
            "FINE_REVIEW_PENDING": "Close the ordinary review before explicitly submitting its fine decisions. Drafts remain unsent.",
            "PROJECT_OPERATOR": "Enter an explicit operator ID for this project.",
            "OPERATOR_REQUIRED": "Enter an explicit operator ID for this project.",
            "PROJECT_PURPOSE": "Choose daily annotation or engineering. Research conditions require a separate contract.",
            "DAILY_BUDGET": "Daily annotation uses no forced deadline in this version.",
            "PROJECT_NOT_READY": "The import did not complete. Inspect its diagnostic record and create a new project directory.",
            "PROJECT_STATE_MISSING": "The saved project state is missing. Restore a verified backup copy.",
            "PROJECT_OWNER_LOCK": "The project owner lock is missing. Keep the original and inspect a verified copy.",
            "IDEMPOTENCY_CONFLICT": "This request key was already used with a different payload or revision. Check the original receipt.",
            "PROJECT_CONTRACT": "The project identity and immutable run contract do not match.",
            "UNKNOWN_JOB_STATE": "A task has an unknown state. Keep the project open and inspect its diagnostic record.",
            "SNAPSHOT_PENDING": "A consistent state sample is pending. Keep the current input; wait for the owner operation or return to the workspace.",
        }.get(code, system_error(error))
        english = english.removeprefix(f"{code}: ")
        self._status(f"{code}: {english}", error=True)
        self.operationFailed.emit(f"{code}: {english}")

    def _run(self, label, function, success=None, *, kind="foreground"):
        if self._busy or self._close_coordinator.state != "open":
            raise ValueError("Wait for the active operation to finish.")
        self._busy = True
        self._active_operation = label
        if self._review_media_dialog is not None:
            self._review_media_dialog.stop()
            if hasattr(self._review_media_dialog, "set_busy"):
                self._review_media_dialog.set_busy(True)
        if self._interval_import_dialog is not None:
            self._interval_import_dialog.set_busy(True)
        self.diagnostics.record_event("INFO", "Operation started.", label, self._diagnostic_context())
        self._status(label)
        self.progress.setRange(0, 0)
        self.progress.show()
        self.cancel_button.show()
        self._set_enabled()
        thread = TaskThread(function, self)
        self._worker = thread
        generation = self._close_coordinator.generation
        self._foreground = ForegroundOperation("desktop-op-" + uuid.uuid4().hex,
            kind=kind, label=label, cancel_event=thread.cancelled, worker=thread,
            project_id=self.project.metadata["project_id"] if self.project else None,
            run_id=self.project.service.run_id if self.project else None, generation=generation)
        self._refresh_job_view()
        thread.progress.connect(lambda data: self._progress(data, thread=thread))
        thread.failed.connect(lambda error: self._task_failed(error, thread=thread, generation=generation))
        thread.succeeded.connect(lambda result: self._task_succeeded(label, result, success,
                                                                     thread=thread, generation=generation))
        thread.finished.connect(lambda: self._worker_finished(thread))
        thread.start()

    def _progress(self, data, *, thread=None):
        if thread is not None and self._worker is not thread:
            return
        if isinstance(data, dict):
            if self._foreground:
                self._foreground.update(progress=data)
            stage = data.get("stage", "working")
            done, total = data.get("completed", 0), data.get("total", 0)
            self._status(f"{stage}: {done}/{total}" if total else str(stage))
            if total:
                self.progress.setRange(0, int(total))
                self.progress.setValue(int(done))
            timing = data.get("timing")
            if isinstance(timing, dict) and type(timing.get("total_seconds")) in {int, float}:
                self._status(f"{stage}: {done}/{total} · import/preparation elapsed {timing['total_seconds']:.2f} s")

    def _task_succeeded(self, label, result, success, *, thread=None, generation=None):
        try:
            if self._foreground and (thread is None or self._worker is thread):
                if hasattr(result, "metadata") and hasattr(result, "service"):
                    self._foreground.update(project_id=result.metadata["project_id"], run_id=result.service.run_id)
                self._foreground.mark_finished()
            if (thread is not None and self._worker is not thread) or (
                    generation is not None and not self._close_coordinator.accepts_callback(generation)):
                # A project may finish opening after cancellation reached its last safe boundary.
                # Release that owner without installing it into a closing or different window.
                if hasattr(result, "close") and result is not self.project:
                    result.close()
                return
            if success:
                success(result)
            self.diagnostics.record_event("INFO", "Operation completed.", label, self._diagnostic_context())
            self.operationFinished.emit(label)
        except Exception as exc:
            if label == "Load audition page":
                self._page_load_error = True
                self._restore_failed_page_load()
            self._error(exc, operation=label + " completion")

    def _task_failed(self, error, *, thread=None, generation=None):
        if thread is not None and self._worker is not thread:
            return
        if self._foreground:
            self._foreground.mark_finished("cancelled" if getattr(error, "code", None) == "CANCELLED" else "failed",
                error={"code": getattr(error, "code", type(error).__name__), "message": system_error(error)})
        if self._active_operation == "Load audition page":
            self._page_load_error = True
            self._restore_failed_page_load()
        self._error(error)

    def _worker_finished(self, thread):
        if self._worker is thread:
            if self._foreground:
                if self._foreground.snapshot()["status"] == "creating":
                    self._foreground.mark_finished("failed", error={"code": "WORKER_NO_RECEIPT",
                        "message": "The foreground worker ended without a completion receipt."})
                self._foreground.bind_worker(None)
            self._worker = None
            self._busy = False
            self._active_operation = None
            self.progress.hide()
            self.cancel_button.hide()
            self._set_enabled()
            if self._review_media_dialog is not None and hasattr(self._review_media_dialog, "set_busy"):
                self._review_media_dialog.set_busy(False)
            if self._transition:
                self._guard(self._advance_transition, operation="finish pending close")
            else:
                if self.project and self._pending_refresh:
                    self._guard(self._refresh, operation="refresh after foreground operation")
                self._guard(self._load_first_available_page, operation="load selected review page")
            pending = self._after_operation
            self._after_operation = None
            if pending:
                generation, function = pending
                if self._close_coordinator.accepts_callback(generation):
                    QTimer.singleShot(0, lambda: self._guard(function, operation="review inspected input")
                        if self._close_coordinator.accepts_callback(generation) else None)
        thread.deleteLater()

    def _set_enabled(self):
        opened = self.project is not None
        available = not self._busy and self._close_coordinator.state == "open" and not self._sampling_pending
        self.import_button.setEnabled(available)
        self.interval_import_action.setEnabled(available)
        if self._interval_import_dialog is not None:
            self._interval_import_dialog.set_busy(not available)
        self.open_button.setEnabled(available)
        self.backup_button.setEnabled(available)
        self.project_details_action.setEnabled(opened)
        self.display_action.setEnabled(opened and available)
        for action in self._dialog_menu_actions:
            action.setEnabled(opened and available)
        for widget in (self.group_list, self.slot_list, self.current_family, self.create_family_button,
                       self.open_candidate_workspace_action, self.apply_recommendation, self.keep_clusters,
                       self.export_button):
            widget.setEnabled(opened and available)
        self.group_list.family_actions_enabled = bool(opened and available and not (self.state or {}).get("deadline_locked"))
        self.create_family_button.setEnabled(self.group_list.family_actions_enabled)
        ready = bool(opened and self._page and available)
        review = self._review()
        editable = ready and bool(review) and not any(review.get(k) for k in
                          ("closed", "scope_stale", "archived", "read_only", "superseded_by_protocol_upgrade"))
        single = self.selected_item_id in self._rows
        locked = self.selected_item_id in review.get("item_locks", {})
        item_editable = bool(editable and single and not locked)
        for widget in (self.disposition, self.reason_code, self.target_family, self.new_family_draft, self.new_title,
                       self.new_reason, self.reason, self.form_status, self.boundary, self.confirm,
                       self.save_item, self.save_next):
            widget.setEnabled(item_editable)
        audition_only = bool(review.get("audition_only"))
        self.item_editor.setVisible(single and not audition_only)
        self.save_next.setVisible(single and not audition_only)
        self.save_options.setVisible(single and not audition_only)
        self.save_options.setEnabled(item_editable)
        if not single:
            self._render_selection_summary()
        self._refresh_page_defaults()
        page_count = len(self._page_submission_ids())
        if self._pending_submission and self._pending_submission.get("whole_page") and self._pending_submission["page_key"] == self._page_key:
            page_count = len(self._pending_submission["item_ids"])
        pages = review.get("pages", [])
        index = next((i for i, page in enumerate(pages, 1) if page["page_id"] == self.selected_page_id), None)
        if self._page and index is not None:
            context = [f"Page {index}/{len(pages)}"]
            if audition_only:
                context.append("Listening only")
            elif self._directed_editor():
                context.append("Directed review · save individually")
            else:
                context.append(f"{page_count} pending")
            if len(self.selected_item_ids) > 1:
                context.append(f"{len(self.selected_item_ids)} selected for playback")
            self.page_summary.setText(" · ".join(context))
        else:
            self.page_summary.setText("No review page")
        self.save_page.setVisible(not audition_only and not self._directed_editor())
        self.save_page.setEnabled(bool(editable and not self._directed_editor() and
            (page_count or self._pending_submission and self._pending_submission.get("whole_page"))))
        for widget in (self.play_page, self.loop_item, self.fit_button,
                       self.previous_item, self.next_item):
            widget.setEnabled(ready)
        self.fine_review_button.setEnabled(bool(ready and single and not audition_only and not locked))
        self.review_media_button.setEnabled(bool(ready and single and not audition_only))
        self.item_tools.setEnabled(bool(ready and single and not audition_only))
        self.current_family.setEnabled(opened and available and not audition_only and
                                       not (self.selected_group or "").startswith("family:"))
        self._update_scope_actions(opened, available, review)
        self._update_header()
        self._sync_workspace_checks()

    def _review(self):
        return self._review_by_id(self.selected_review_id)

    def _review_by_id(self, review_id):
        if self._audition_preview is not None and review_id == self._audition_preview.review_id:
            return self._audition_preview.review
        return (self.state or {}).get("batch_review", {}).get("reviews", {}).get(review_id, {})

    def _directed_editor(self):
        review = self._review()
        return bool(review.get("directed_issue_id") or review.get("directed_context"))

    def _submission_capabilities(self):
        return (self.state or {}).get("batch_review", {}).get("submission_capabilities", {})

    def _toggle_notes(self, expanded):
        self.notes_body.setVisible(expanded)
        self.notes_toggle.setText("Hide note" if expanded else "View note" if self.reason.toPlainText() else "Add note")
        if not self._rendering:
            self._layout_item_fields()

    def _toggle_details(self, expanded):
        self.details_body.setVisible(expanded)
        if not self._rendering:
            self._layout_item_fields()

    def _layout_item_fields(self):
        # Fit short forms inside the inspector. Longer ones scroll inside the
        # same pane; their size hints never set the size of the plot pane.
        scroll = self.item_form_scroll
        extras = any(not w.isHidden() for w in (self.reason_code, self.target_family,
            self.new_family_draft, self.notes_body, self.form_status, self.details_body))
        scroll.setVisible(extras)
        natural = minimum = 0
        if extras:
            form = scroll.widget().layout()
            form.invalidate()
            natural = max(form.sizeHint().height(), form.heightForWidth(max(1, self.item_editor.width() - 40)))
            minimum = min(natural, self.disposition.sizeHint().height() + 4)
            scroll.setMinimumHeight(minimum)
            scroll.setMaximumHeight(natural)
        edit = self.item_editor.layout()
        edit.invalidate()
        self.item_editor.setMaximumHeight(edit.minimumSize().height() + natural - minimum)

    def _import_dialog(self):
        source = QFileDialog.getExistingDirectory(self, "Choose segmented mono audio input")
        if not source:
            return
        base = Path(os.environ.get("SYLLAFOLIO_PROJECTS_DIR", str(Path.home() / "SyllafolioProjects")))
        suggestion = base / ("SyllafolioProject-" + uuid.uuid4().hex[:6])
        destination, _ = QFileDialog.getSaveFileName(self, "Choose a new project directory", str(suggestion), "Project directory (*)")
        if destination:
            self.inspect_import_path(source, destination)

    def _show_interval_import(self):
        if self._busy or self._close_coordinator.state != "open":
            raise ValueError("Wait for the active operation before opening an import preview.")
        self._stop_review_audio()
        if self._interval_import_dialog is None:
            self._interval_import_dialog = IntervalImportDialog(self, self._confirm_interval_import)
        self._interval_import_dialog.show()
        self._interval_import_dialog.raise_()
        return self._interval_import_dialog

    def _confirm_interval_import(self, dialog):
        if dialog is not self._interval_import_dialog or not dialog._current() or self._busy:
            raise ValueError("Reopen the current recording preview before creating a project.")
        base = Path(os.environ.get("SYLLAFOLIO_PROJECTS_DIR", str(Path.home() / "SyllafolioProjects")))
        destination, _ = QFileDialog.getSaveFileName(self, "Choose a new project directory",
            str(base / ("SyllafolioProject-" + uuid.uuid4().hex[:6])), "Project directory (*)")
        if not destination:
            return
        contract = ProjectContractDialog(self)
        if contract.exec() != QDialog.DialogCode.Accepted:
            return
        selection = dialog.take_selection()
        try:
            started = self.import_interval_project(selection, destination, **contract.options())
        except Exception:
            if selection.preview is not None and dialog._current():
                dialog.restore_selection(selection)
            else:
                selection.close()
            raise
        if started:
            dialog.close()
        else:
            dialog.restore_selection(selection)

    def import_interval_project(self, selection, project_dir, **contract_options):
        """Transfer one preview through the normal drain/import/owner lifecycle."""
        if self._interval_import_ticket is not None:
            raise ValueError("A recording import is already pending.")
        self._interval_import_ticket = selection
        def start():
            if self._interval_import_ticket is not selection:
                return
            self._interval_import_ticket = None
            preview = selection.take_preview()
            def create(progress, cancelled):
                try:
                    from .annotation_interval_import import create_interval_project
                    return create_interval_project(preview, selection.tier_index, selection.include_empty,
                        project_dir, progress=progress, cancelled=cancelled, **contract_options)
                finally:
                    preview.close()
            try:
                self._run("Import recording intervals and calculate candidates", create,
                    self._install_project, kind="import")
            except Exception:
                preview.close()
                raise
        try:
            accepted = self._request_transition(start)
        except Exception:
            if self._interval_import_ticket is selection:
                self._interval_import_ticket = None
            raise
        if accepted is False:
            self._interval_import_ticket = None
            return False
        return True

    def _retire_interval_import(self, reason):
        if self._interval_import_dialog is not None:
            self._interval_import_dialog.invalidate(reason)

    def _cancel_pending_interval_import(self):
        if self._interval_import_ticket is not None:
            selection, self._interval_import_ticket = self._interval_import_ticket, None
            selection.close()

    def inspect_import_path(self, source, destination):
        def inspect(_progress, cancelled):
            result = inspect_input(source)
            if cancelled():
                from .annotation_stage_a import StageAError
                raise StageAError("CANCELLED", "Input inspection cancelled; no project was created.")
            return result
        def inspected(result):
            generation = self._close_coordinator.generation
            self._after_operation = (generation, lambda: self._confirm_inspected_import(source, destination, result))
        self._run("Inspect segmented audio input", inspect, inspected)

    def _confirm_inspected_import(self, source, destination, view):
        dialog = InputInspectionDialog(view, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            self._status("Input inspected read-only; import was not requested.")
            return
        bandpass = dialog.bandpass_settings()
        contract = ProjectContractDialog(self)
        if contract.exec() == QDialog.DialogCode.Accepted:
            options = contract.options()
            options["bandpass"] = bandpass
            options["expected_source_fingerprint"] = view["summary"]["source_fingerprint"]
            if view.get("inspection_fingerprint"):
                options["expected_inspection_fingerprint"] = view["inspection_fingerprint"]
            self.import_project_path(source, destination, **options)

    def _show_stage_a_workspace(self):
        if not self.project:
            raise ValueError("Open a project first.")
        self.state = self._sample_project_state(require_fresh=True)
        if self._stage_a_dialog is None or self._stage_a_dialog.project is not self.project:
            if self._stage_a_dialog is not None:
                self._stage_a_dialog.close()
            self._stage_a_dialog = StageAWorkspaceDialog(self)
        self._stage_a_dialog.show()
        self._stage_a_dialog.raise_()
        self._stage_a_dialog.refresh()
        return self._stage_a_dialog

    def _show_organization_tab(self):
        dialog = self._show_stage_a_workspace()
        dialog.tabs.setCurrentWidget(dialog.organization_scroll)
        return dialog

    def preview_organization(self, mode="auto", height=None):
        if self._busy:
            raise ValueError("Wait for the current foreground operation to finish.")
        dialog = self._show_stage_a_workspace()
        dialog.set_mode(mode, height)
        dialog.preview()
        return dialog

    def _show_family_reviews(self, family_id=None):
        if not self.project or self._busy or self._close_coordinator.state != "open":
            raise ValueError("Open a project and wait for the foreground operation first.")
        self._cancel_tree_navigation()
        self._refresh()
        tree = self.group_list
        directory = next(row for row in self._tree_items()
                         if row.data(0, Qt.ItemDataRole.UserRole).get("kind") == "families_root")
        directory.setExpanded(True)
        target = family_id or self.current_family.currentData()
        if target:
            if not any(row["family_id"] == target for row in self.state["batch_review"].get("families", [])):
                raise ValueError("Choose a current human family.")
            self._navigate_tree_data({"kind": "family", "id": target})
            self._restore_group_highlight(reveal=True)
        else:
            tree.setCurrentItem(directory)
            tree.scrollToItem(directory)
            self._status("Select a family and choose Prepare group." if directory.childCount() else
                         "Create a human family and assign members before preparing its review.")
        self._focus_audition()
        tree.setFocus()
        return self

    def _require_workflow_owner(self):
        if not self.project or self._busy or self._close_coordinator.state != "open":
            raise ValueError("Open a project and wait for its active operation before opening this workspace.")
        self.state = self._sample_project_state(require_fresh=True)
        self._save_drafts()
        self._stop_review_audio()

    def _show_pool_review(self):
        self._require_workflow_owner()
        self._cancel_tree_navigation()
        self._navigate_tree_data({"kind": "pool", "id": "pool"})
        self._restore_group_highlight(reveal=True)
        self._focus_audition()
        self.group_list.setFocus()
        return self

    def _show_pool_details(self):
        self._require_workflow_owner()
        if not self._workflow_dialog_current(self._pool_review_dialog):
            if self._pool_review_dialog is not None:
                self._pool_review_dialog.close()
            self._pool_review_dialog = PoolReviewDialog(self, self.open_review_scope,
                self._show_final_review, self.open_candidate_group)
        dialog = self._pool_review_dialog
        dialog.show(); dialog.raise_(); dialog.select_scope()
        return dialog

    def _show_final_review(self, *, history=False):
        self._require_workflow_owner()
        if not self._workflow_dialog_current(self._final_review_dialog):
            if self._final_review_dialog is not None:
                self._final_review_dialog.close()
            dialog = FinalReviewDialog(self)
            dialog.itemRequested.connect(lambda item: self._guard(lambda: self.open_fine_item(item), operation="final-check item"))
            dialog.typeRequested.connect(lambda ident: self._guard(lambda: self._show_catalogue_review(ident), operation="type card"))
            dialog.groupRequested.connect(lambda ident: self._guard(lambda: self.open_candidate_group(ident), operation="first-pass gap"))
            dialog.reviewRequested.connect(lambda ident: self._guard(lambda: self._open_located_review(ident), operation="review gap"))
            dialog.changed.connect(self._workflow_changed)
            self._final_review_dialog = dialog
            dialog.tabs.currentChanged.connect(self._sync_workspace_checks)
        dialog = self._final_review_dialog
        if history:
            dialog.show_history()
        elif dialog.history_visible():
            dialog.tabs.setCurrentIndex(0)
        dialog.show(); dialog.raise_(); dialog.refresh()
        return dialog

    def _show_checkpoint_history(self):
        return self._show_final_review(history=True)

    def _show_catalogue_review(self, type_id=None):
        self._require_workflow_owner()
        if not self._workflow_dialog_current(self._catalogue_review_dialog):
            if self._catalogue_review_dialog is not None:
                self._catalogue_review_dialog.close()
            dialog = CatalogueReviewDialog(self, self._open_directed_review)
            dialog.auditionRequested.connect(self.audition_fine_item)
            dialog.fineRequested.connect(lambda item: self._guard(lambda: self.open_fine_item(item), operation="fine-review item"))
            dialog.cardSaved.connect(self._workflow_changed)
            self._catalogue_review_dialog = dialog
        dialog = self._catalogue_review_dialog
        dialog.show(); dialog.raise_(); dialog.refresh()
        if type_id is not None:
            dialog.select_type(type_id)
        return dialog

    def _workflow_dialog_current(self, dialog):
        return (dialog is not None and dialog.project is self.project and
                getattr(dialog, "generation", None) == self._close_coordinator.generation and
                not getattr(dialog, "_owner_transition_invalidated", False) and not getattr(dialog, "_invalid", None))

    def _show_source_registry(self):
        self._require_workflow_owner()
        if not self._workflow_dialog_current(self._source_registry_dialog):
            if self._source_registry_dialog is not None:
                self._source_registry_dialog.close()
            dialog = SourceRegistryDialog(self)
            dialog.changed.connect(self._workflow_changed)
            dialog.locateItem.connect(lambda item: self._guard(lambda: self.open_fine_item(item), operation="Source item"))
            self._source_registry_dialog = dialog
        dialog = self._source_registry_dialog
        dialog.show(); dialog.raise_(); dialog.refresh()
        return dialog

    def _show_assessment(self):
        self._require_workflow_owner()
        if not self._workflow_dialog_current(self._assessment_dialog):
            if self._assessment_dialog is not None:
                self._assessment_dialog.close()
            dialog = AssessmentDialog(self)
            dialog.sourceRequested.connect(lambda: self._guard(self._show_source_registry, operation="assessment sources"))
            dialog.directedRequested.connect(lambda value: self._guard(lambda: self._prefill_assessment_question(value), operation="assessment review question"))
            dialog.assessmentChanged.connect(self._workflow_changed)
            self._assessment_dialog = dialog
        dialog = self._assessment_dialog
        dialog.show(); dialog.raise_(); dialog.refresh()
        return dialog

    def _prefill_assessment_question(self, value):
        """Human-selected diagnostic text is an unsubmitted D2 form, never a review job."""
        dialog = self._show_catalogue_review(value.get("family_id"))
        dialog.prefill_directed_question(value)
        return dialog

    def _show_catalogue_export(self):
        self._require_workflow_owner()
        if not self._workflow_dialog_current(self._catalogue_export_dialog):
            if self._catalogue_export_dialog is not None:
                self._catalogue_export_dialog.close()
            dialog = ExportDialog(self)
            dialog.changed.connect(self._workflow_changed)
            dialog.scopeRequested.connect(lambda: self._guard(self._show_final_review, operation="catalogue scope and completion"))
            dialog.assessmentRequested.connect(lambda: self._guard(self._show_assessment, operation="catalogue assessment selection"))
            dialog.saved.connect(lambda _result: self._status("Catalogue ZIP and manifest saved as one verified package directory."))
            dialog.previewOpened.connect(lambda _result: self._status("Opened the selected verified catalogue HTML with its local assets."))
            self._catalogue_export_dialog = dialog
        dialog = self._catalogue_export_dialog
        dialog.show(); dialog.raise_(); dialog.refresh()
        return dialog

    def _focus_audition(self):
        if self._final_review_dialog is not None:
            self._final_review_dialog.stop()
            self._final_review_dialog.hide()
        self.raise_(); self.activateWindow()

    def _open_located_review(self, review_id):
        self.open_review_scope(review_id)
        self._focus_audition()

    def _open_directed_review(self, review_id):
        self.open_review_scope(review_id)
        if self._catalogue_review_dialog is not None:
            self._catalogue_review_dialog.accept()
        self._focus_audition()

    def open_candidate_group(self, group_id):
        if group_id == "pool":
            return self._show_pool_review()
        self._require_workflow_owner()
        self._refresh()
        for row in self._tree_items():
            data = row.data(0, Qt.ItemDataRole.UserRole)
            if data.get("kind") == "group" and data.get("id") == group_id:
                self._cancel_tree_navigation()
                self._navigate_tree_data(data)
                self._restore_group_highlight(reveal=True)
                self._focus_audition()
                return
        raise ValueError("This group is no longer in the current organization. Refresh the first-pass gaps.")

    def _prepare_review_ui(self):
        if self.selected_group == "pool":
            self._save_drafts()
            return self._prepare_pool_audition(interactive=True)
        return self.prepare_selected()

    def _update_scope_actions(self, opened, available, review):
        batch = (self.state or {}).get("batch_review", {})
        organized = bool(batch.get("organization"))
        pool = self.selected_group == "pool"
        family = (self.selected_group or "").startswith("family:")
        candidate = bool(self.selected_group) and not (pool or family)
        new_scope = opened and (organized or family) and self.selected_group and not review
        kind = "pool" if pool else "family" if family else "group"
        pool_state = batch.get("pool", {})
        empty_pool = pool and not pool_state.get("active_ids")
        can_start_pool = True
        if pool:
            # Offer a fresh round for residual/new IDs after a historical round,
            # but keep an unfinished, stale freeze available for explicit archiving.
            historical = review.get("closed") or review.get("archived") or review.get("read_only")
            retryable = review.get("status") in {"failed", "cancelled", "interrupted"} and not review.get("scope_stale")
            prerequisites_met = pool_state.get("first_pass_complete") and not pool_state.get("pending_family_review_ids")
            complete = prerequisites_met and pool_state.get("stage_complete")
            can_start_pool = pool_state.get("can_prepare") if empty_pool else pool_state.get("can_prepare_current_pool")
            new_scope = organized and not review.get("audition_only") and not complete and (
                not review or historical or retryable)
            if (not empty_pool and review.get("pool_mode") == "current_pool" and review.get("closed")
                    and not review.get("pool_refresh_required")):
                new_scope = False
        self.prepare_button.setText("Skip empty pool…" if empty_pool else "Prepare group")
        family_row = next((row for row in batch.get("families", [])
                           if self.selected_group == "family:" + row["family_id"]), {}) if family else {}
        empty_family = family and not family_row.get("member_ids")
        self.prepare_button.setToolTip("Assign at least one syllable to this family first." if empty_family else
                                       "Prepare all current family members for comparison." if family else "")
        self.prepare_button.setVisible(bool(new_scope))
        self.prepare_button.setEnabled(bool(new_scope and available and not empty_family and can_start_pool))
        can_finish = review.get("status") == "ready" and not any(review.get(k) for k in
            ("closed", "archived", "read_only", "scope_stale", "superseded_by_protocol_upgrade"))
        self.close_review_button.setText(f"Finish {kind}…")
        self.close_review_button.setVisible(can_finish)
        self.close_review_button.setEnabled(bool(can_finish and available))
        self.more_button.setVisible(bool(pool or review and not review.get("audition_only")))
        self.more_button.setEnabled(bool(opened and available))
        self.review_details_action.setVisible(bool(review and not review.get("audition_only")))
        self.manage_scope_action.setVisible(pool)
        active_directed = self._directed_editor() and not review.get("closed")
        ordinary_family = family and not active_directed and not review.get("audition_only")
        family_repeat = ordinary_family and not empty_family and (
            review.get("closed") or review.get("scope_stale") or review.get("status") in {"failed", "cancelled", "interrupted"})
        pool_repeat = pool and review.get("pool_refresh_required") and not review.get("closed") and not empty_pool
        can_repeat = (candidate and organized or family_repeat or pool_repeat) and bool(review) and review.get("status") not in ACTIVE
        self.prepare_again_action.setVisible(bool(can_repeat))
        self.prepare_again_action.setEnabled(bool(can_repeat and available))
        can_use_exception = ((candidate or pool) and organized or ordinary_family) and review.get("direct_exception", {}).get("allowed", False)
        self.exception_action.setVisible(bool(can_use_exception))
        self.exception_action.setEnabled(bool(can_use_exception and available))

    def _show_scope_details(self):
        review = self._review()
        if review:
            lines = [f"Status: {'Finished' if review.get('closed') else review['status']}",
                     f"Syllables: {len(review.get('item_ids', []))}", f"Saved: {len(review.get('decisions', {}))}",
                     f"Preparation: {review.get('route', '').replace('_', ' ')}"]
            for key in ("scope_stale_reason", "read_only_reason"):
                if review.get(key):
                    lines.append(system_text(str(review[key])))
            error = review.get("error") or {}
            if error:
                lines.append(system_text(str(error.get("message") or error.get("code"))))
            dialog = QMessageBox(self)
            dialog.setWindowTitle("Review details")
            dialog.setTextFormat(Qt.TextFormat.PlainText)
            dialog.setText("\n".join(lines))
            details = {key: review.get(key) for key in ("review_id", "stage", "family_id", "route", "input_fingerprint",
                                                         "preparation_binding", "direct_exception", "error")}
            dialog.setDetailedText(json.dumps(system_fields(details), ensure_ascii=False, indent=2))
            dialog.exec()

    def _prepare_again(self, *, use_exception=False):
        review = self._review()
        if (not review or not self.selected_group or (self._directed_editor() and not review.get("closed"))
                or review.get("audition_only")
                or (self.selected_group == "pool" and not use_exception and not review.get("pool_refresh_required"))):
            raise ValueError("Select a prepared candidate group.")
        reason = ""
        if use_exception or review.get("closed") or review.get("scope_stale") or review.get("decisions") or review.get("pool_refresh_required"):
            context = (self.project, self.selected_group, self.selected_review_id)
            prompt = "Why does this group require direct review?" if use_exception else "What new evidence or reason warrants preparing this group again?"
            reason, accepted = QInputDialog.getText(self, "Prepare with exception" if use_exception else "Prepare again", prompt)
            if not accepted:
                return
            if context != (self.project, self.selected_group, self.selected_review_id):
                raise ValueError("The selected review changed. Check the group and try again.")
        if self.selected_group == "pool" and not use_exception:
            self._save_drafts()
            return self._prepare_pool_audition(reason=reason, refreeze=True)
        self.prepare_selected(reason=reason, use_exception=use_exception)

    def before_catalogue_change(self):
        self._save_drafts()
        self._retire_review_views("Catalogue scope or definition is being changed; inspect restored fine input again.")
        self._stop_review_audio()

    def before_checkpoint_restore(self):
        self.before_catalogue_change()

    def _workflow_changed(self, result):
        if not self.project:
            return
        action = (result or {}).get("desktop_receipt", {}).get("action")
        if action == "restore":
            self._retire_review_views("A label checkpoint was restored; old media/fine input is not a restored decision.")
            self.selected_group = self.selected_review_id = None
            self._clear_review_page()
            self._pages_signature = self._queue_signature = None
        self._refresh()
        self._status("Shared project state updated. Draft restoration remains separate from label history restoration.")

    def _clear_review_page(self):
        self._stop_review_audio()
        self.selected_page_id = self.selected_item_id = None
        self._page = self._page_key = self._page_audio = None
        self._page_rate = 0
        self._page_load_error = False
        self._page_request = None
        self._page_request_token = None
        self._rows = {}; self._draft_base = {}; self._new_families = {}
        self._page_defaults = {}; self._explicit_items = set()
        self._dirty = False
        self._pending_submission = None
        self.canvas.clear(); self.slot_list.clear()
        self.audio.set_audio(np.empty(0, dtype=np.float32), 48000)
        self._update_header()
        self.item_title.setText("No current syllable")
        self.item_status.clear()
        rendering, self._rendering = self._rendering, True
        try:
            self.confirm.setChecked(False)
            self.confirm.hide()
            self.disposition.setCurrentIndex(0)
            self.reason.clear(); self.new_title.clear(); self.new_reason.clear()
            self.reason_code.clear(); self.reason_code.hide(); self.reason_code_label.hide()
            self.notes_toggle.setChecked(False); self.details_toggle.setChecked(False)
            self.notes_body.hide(); self.details_body.hide()
            self._editor_target_key = None
            self.boundary.setChecked(False); self.form_status.setCurrentIndex(0)
            self.new_family_draft.clear()
            for widget in (self.target_family, self.target_label, self.target_empty, self.new_family_draft,
                           self.new_draft_label, self.new_title, self.new_title_label, self.new_reason,
                           self.new_reason_label, self.form_status):
                widget.hide()
        finally:
            self._rendering = rendering

    def _retire_workflow_views(self, reason):
        for name in ("_pool_review_dialog", "_final_review_dialog", "_catalogue_review_dialog",
                     "_source_registry_dialog", "_assessment_dialog", "_catalogue_export_dialog"):
            dialog = getattr(self, name)
            if dialog is None:
                continue
            if hasattr(dialog, "save_draft"):
                dialog.save_draft()
            if hasattr(dialog, "stop"):
                dialog.stop()
            dialog.close()
            if hasattr(dialog, "invalidate"):
                dialog.invalidate(reason)
            setattr(self, name, None)

    def open_review_scope(self, review_id):
        """Install an already prepared current review into the one main audition/editor."""
        if self._busy or not self.project:
            raise ValueError("Wait for the foreground operation before opening a review.")
        self._cancel_tree_navigation()
        self._save_drafts()
        state = self._sample_project_state(require_fresh=True)
        review = state["batch_review"]["reviews"].get(review_id)
        if not review or review.get("status") != "ready" or any(review.get(key) for key in ("scope_stale", "read_only", "archived")):
            raise ValueError("Select a current ready review scope.")
        self._retire_review_views("The active review scope changed.")
        rollback = self._navigation_snapshot()
        self.state = state
        self._clear_review_page()
        self.selected_group = ("family:" + review["family_id"] if review.get("stage") == "family" else
                               "pool" if review.get("stage") == "pool" else review.get("group_id"))
        self.selected_review_id = review_id
        self._pages_signature = self._queue_signature = None
        self._defer_tree_page_load = True
        try:
            self._refresh()
        finally:
            self._defer_tree_page_load = False
        self._restore_group_highlight(reveal=True)
        self._load_first_available_page(rollback=rollback)

    def _show_fine_review(self):
        if self._review().get("audition_only"):
            raise ValueError("Finish the outstanding reviews before editing this listening-only scope.")
        if not self.project or not self.selected_item_id or self._busy:
            raise ValueError("Choose an actual review target and wait for the foreground operation first.")
        return self.open_fine_item(self.selected_item_id, self.selected_review_id)

    def open_fine_item(self, item_id, review_id=None):
        if not self.project or item_id not in self.project.dataset.item_ids or self._busy:
            raise ValueError("Choose a current stable project item and wait for the foreground operation.")
        state = self._sample_project_state(require_fresh=True)
        batch = state["batch_review"]
        directed = [row for row in batch.get("reviews", {}).values()
            if row.get("directed_issue_id") and not row.get("closed") and not row.get("archived")
            and batch.get("active_family_reviews", {}).get(row.get("family_id")) == row["review_id"]
            and item_id in row.get("item_ids", [])]
        if directed:
            legal = [row for row in directed if row.get("status") == "ready" and not row.get("scope_stale") and not row.get("read_only")]
            requested = next((row for row in legal if row["review_id"] == review_id), None)
            if requested is not None:
                review_id = requested["review_id"]
            elif len(directed) == len(legal) == 1:
                review_id = legal[0]["review_id"]
            else:
                raise ValueError("This target has an unfinished directed scope. Wait for its current ready round or reopen the changed scope before Fine submission.")
        if self._fine_review_dialog is not None:
            if (self._fine_review_dialog.project is self.project and self._fine_review_dialog.item_id == item_id
                    and self._fine_review_dialog.review_id == review_id and self._fine_review_dialog.is_current()
                    and self._fine_review_dialog.controller.invalid_reason is None):
                self._fine_review_dialog.show(); self._fine_review_dialog.raise_()
                return self._fine_review_dialog
            self._fine_review_dialog.save_draft()
            self._fine_review_dialog.close()
        dialog = FineReviewDialog(self, item_id, review_id)
        self._fine_review_dialog = dialog
        dialog.auditionRequested.connect(self.audition_fine_item)
        dialog.inspectRequested.connect(self.inspect_fine_item)
        dialog.stopRequested.connect(lambda _item: self._stop_review_audio())
        dialog.submitted.connect(lambda _receipt, target=item_id: self._fine_submitted(target))
        dialog.show(); dialog.raise_()
        return dialog

    def _show_review_media(self, item_id=None, context=False):
        if not self.project or self._busy:
            raise ValueError("Open a project and wait for the foreground operation first.")
        target = item_id or self.selected_item_id
        if target not in self.project.dataset.item_ids:
            raise ValueError("Choose a stable target ID from this project.")
        if self._review_media_dialog is not None:
            self._review_media_dialog.stop()
            self._review_media_dialog.close()
        self.audio.stop()
        review = self._review()
        review_id = self.selected_review_id if not review.get("audition_only") and target in review.get("item_ids", []) else None
        dialog = ReviewMediaDialog(self, target, review_id, self.display_controller, context=context)
        self._review_media_dialog = dialog
        dialog.playbackRequested.connect(self._stop_review_audio)
        dialog.show(); dialog.raise_()
        return dialog

    def inspect_fine_item(self, item_id, context=False):
        return self._show_review_media(item_id, context)

    def audition_fine_item(self, item_id, context=False):
        # This path is an explicit Play action, distinct from opening a viewer.
        # Wait for the bound PCM/image before starting that exact target.
        dialog = self._show_review_media(item_id, context)
        generation = self._close_coordinator.generation
        def play_bound():
            if self._review_media_dialog is dialog and dialog.isVisible() and dialog.target_id == item_id:
                dialog._play(dialog.target_pane, False)
        self._after_operation = (generation, play_bound)
        return dialog

    def _stop_review_audio(self, announce=False):
        self._playback_scope = None
        if hasattr(self, "audio"):
            self.audio.stop(announce=announce)
        if self._review_media_dialog is not None:
            self._review_media_dialog.stop(announce=announce)
        if self._interval_import_dialog is not None:
            self._interval_import_dialog.stop(announce=announce)

    def _fine_submitted(self, item_id):
        if not self.project:
            return
        latest = self.project.snapshot()
        saved = self._saved_page(latest)
        for target in (self._page or {}).get("item_ids", []):
            old = self._row(target, self._draft_base.get(target))
            if self._rows.get(target) == old:
                self._rows[target] = self._row(target, saved.get(target))
                self._draft_base[target] = copy.deepcopy(saved.get(target))
        self.state = latest
        self.confirm.setChecked(False)
        self._refresh()
        self._status(f"Fine decision saved for {item_id}. Unsent disposition drafts remain unsubmitted.")

    def _retire_review_views(self, reason):
        if self._fine_review_dialog is not None:
            self._fine_review_dialog.save_draft()
            self._fine_review_dialog.stop()
            self._fine_review_dialog.close()
            self._fine_review_dialog.invalidate(reason)
            self._fine_review_dialog = None
        if self._review_media_dialog is not None:
            self._review_media_dialog.save_preferences()
            self._review_media_dialog.stop()
            self._review_media_dialog.close()
            self._review_media_dialog = None

    def _show_review_display(self):
        if not self.project:
            raise ValueError("Open a project first.")
        if self._display_dialog is not None:
            controls = self._display_dialog.findChild(DisplayControlsWidget)
            if not self._display_dialog.isVisible():
                controls.sync()
            self._display_dialog.show()
            self._display_dialog.raise_()
            self._display_dialog.activateWindow()
            return controls
        dialog = QDialog(self); dialog.setWindowTitle("Shared review display")
        layout = QVBoxLayout(dialog)
        controls = DisplayControlsWidget(self.display_controller, dialog)
        controls.requested.connect(lambda settings, normalization:
            self._guard(lambda: self.apply_review_display(settings, normalization), operation="review display"))
        controls.waveformRangeRequested.connect(lambda limit:
            self._guard(lambda: self.apply_waveform_range(limit), operation="waveform amplitude"))
        layout.addWidget(controls)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(dialog.reject); layout.addWidget(close)
        dialog.show()
        self._display_dialog = dialog
        return controls

    def apply_waveform_range(self, limit):
        try:
            if not self.project or self._busy or self._close_coordinator.state != "open":
                raise ValueError("Wait for the current operation before changing waveform amplitude.")
            self.display_controller.set_waveform_range(self.project, limit)
        finally:
            if self._display_dialog is not None:
                self._display_dialog.findChild(DisplayControlsWidget).sync_waveform_range()
        self.canvas.set_waveform_range(self.display_controller.waveform_range)
        if self._review_media_dialog is not None:
            self._review_media_dialog.sync_waveform_range()

    def apply_review_display(self, settings, normalization):
        if not self.project or self._busy:
            raise ValueError("Wait for the foreground operation before changing display.")
        project = self.project
        candidate = DisplayController(settings, normalization)
        old = self.display_controller.snapshot()
        review_id, page_id = self.selected_review_id, self.selected_page_id
        preview = self._audition_preview if self._review().get("audition_only") else None
        page_key = self._page_key
        media = self._review_media_dialog
        request = media.display_request() if media is not None and media.isVisible() else None
        revision = self._sample_project_state(require_fresh=True)["revision"]
        def render(_progress, _cancelled):
            page = None
            if review_id and page_id:
                if preview is not None:
                    page = preview.load_page(page_id, candidate.snapshot(), _cancelled)["page"]
                else:
                    page = candidate.page(project, review_id, page_id, expected_revision=revision,
                                          idempotency_key="desktop-display-" + uuid.uuid4().hex)
            detail = media.prepare_display(candidate, request=request) if request is not None else None
            return {"page": page, "detail": detail}
        def done(result):
            if request is not None and (self._review_media_dialog is not media or media.display_request() != request):
                raise ValueError("The media selection changed during rendering. Reapply display to the current selection.")
            self.display_controller.set(settings, normalization)
            try:
                self.display_controller.save_preferences(project)
            except Exception:
                self.display_controller.set(old["settings"], old["normalization"])
                raise
            self._update_display_info()
            if result["page"] is not None and self._page_key == page_key:
                self._install_page_display(result["page"])
            if result["detail"] is not None and self._review_media_dialog is media:
                media.install_display(result["detail"])
            self._status("Display updated uniformly; PCM, frozen slots and saved decisions are unchanged.")
        self._run("Redraw shared review display", render, done)

    def _install_page_display(self, result):
        if not self._page:
            return
        for key in ("waveform_url", "spectrogram_url", "preview_spectrogram_path", "display"):
            if key in result:
                self._page[key] = copy.deepcopy(result[key])
        self._draw_audition_page()
        self.canvas.select_items(self.selected_item_ids)
        self._update_display_info()

    def _draw_audition_page(self):
        review = self._review()
        image = (self._page["preview_spectrogram_path"] if review.get("audition_only")
                 else self.project.media_path(self._page["spectrogram_url"]))
        self.canvas.set_page(self._page_audio, self._page_rate, self._page,
                             image, review_pages=review.get("pages", ()), gap_seconds=review.get("gap_seconds", 0.5),
                             scope_clusters=review.get("route") in {"family_cluster", "pool_cluster"})
        self.canvas.select_items(self.selected_item_ids)

    def _update_display_info(self):
        value = self.display_controller.snapshot()
        settings = value["settings"]
        normalization = "per-syllable peak" if value["normalization"] == "per_item" else "whole-page peak"
        self.display_info.setText(f"Display: periodic Hann {settings['window_samples']} / hop {settings['hop_samples']} / "
            f"FFT {value['fft_samples']} · 0–16 kHz · {settings['dynamic_range_db']:g} dB · {normalization}. "
            "Colour is not absolute loudness. Machine groups do not define human families.")
        self.display_action.setToolTip(self.display_info.text())

    def _open_dialog(self):
        path = QFileDialog.getExistingDirectory(self, "Open Syllafolio desktop project (use a backup copy for P5)")
        if path:
            self.open_project_path(path)

    def _backup_dialog(self):
        source = QFileDialog.getExistingDirectory(self, "Choose a closed project to back up")
        if not source:
            return
        base = Path(os.environ.get("SYLLAFOLIO_PROJECTS_DIR", str(Path.home() / "SyllafolioProjects")))
        target, _ = QFileDialog.getSaveFileName(self, "Choose a new backup directory",
            str(base / ("ProjectBackup-" + uuid.uuid4().hex[:6])), "Project directory (*)")
        if target:
            self._run("Back up a closed project", lambda _progress, _cancelled: backup_project(source, target),
                lambda result: self._status("Verified project copy saved: " + result["project_dir"]))

    def import_project_path(self, input_dir, project_dir, **contract_options):
        def start():
            self._run("Import and global candidate calculation",
                      lambda progress, cancelled: create_project(input_dir, project_dir,
                          progress=progress, cancelled=cancelled, **contract_options),
                      self._install_project, kind="import")
        self._request_transition(start)

    def open_project_path(self, path):
        self._request_transition(lambda: self._run("Open desktop project",
            lambda _progress, _cancelled: open_project(path), self._install_project))

    def _assert_project_switchable(self):
        if self._busy or blocking_jobs(self._current_job_view(self._sample_project_state())) or self._transition:
            raise ValueError("Finish or cancel the active calculation before switching projects.")
        self._save_drafts()

    def _install_project(self, project):
        if self._transition or self._close_coordinator.state != "open":
            project.close()
            raise ValueError("A pending close cannot install another project.")
        self._retire_interval_import("Project changed.")
        self.audio.stop()
        if self.project:
            latest = self._sample_project_state()
            if blocking_jobs(self._current_job_view(latest, include_foreground=False)):
                project.close()
                raise ValueError("Wait for all previous project workers to settle before installation.")
            self._save_drafts()
            self._retire_review_views("Project replaced.")
            self._retire_workflow_views("Project replaced.")
            self.project.close()
        if self._cancel_requests is not None:
            self._report_cancel_results(); self._cancel_requests.close(); self._cancel_requests = None
        self.project = project
        self._discard_audition_preview()
        self._sampling_pending = False
        self.state = project.snapshot()
        self._task_issue_seen = self._task_issue_keys(self.state.get("desktop_jobs", {}))
        self._closure_requests = {}; self._closure_drafts = {}
        if self._display_dialog is not None:
            self._display_dialog.close()
            self._display_dialog.deleteLater()
            self._display_dialog = None
        self.display_controller = DisplayController()
        try:
            self.display_controller.load_preferences(project)
        except Exception as exc:
            self.diagnostics.record_exception(exc, "load review display preferences", self._diagnostic_context())
            self._status("Review media preferences could not be loaded; their original bytes are retained.", True)
        self.canvas.set_waveform_range(self.display_controller.waveform_range)
        self._update_display_info()
        self._rows = {}
        self._new_families = {}
        self._dirty = False
        self._page = None
        self._page_key = None
        self.selected_group = self.selected_review_id = self.selected_page_id = self.selected_item_id = None
        self._queue_signature = self._pages_signature = None
        self._expanded_review_sections.clear()
        self._draft_store = {}
        self._draft_source_issue = None
        self._dismiss_draft_warning()
        drafts = self.project.project_dir / "desktop_drafts.json"
        if drafts.is_file():
            raw = None
            try:
                raw = drafts.read_bytes()
                stored = json.loads(raw.decode("utf-8"))
                if not isinstance(stored, dict) or stored.get("schema") != "syllafolio-desktop-drafts-v1":
                    raise ValueError("The draft has an unsupported document schema.")
                if not isinstance(stored.get("pages"), dict) or not all(
                        isinstance(key, str) and isinstance(value, dict) for key, value in stored["pages"].items()):
                    raise ValueError("The draft pages do not have the required structure.")
                if stored.get("input_fingerprint") != self.state["input_fingerprint"]:
                    raise ValueError("The draft belongs to another frozen input; it was not restored.")
                self._draft_store = stored["pages"]
            except (OSError, UnicodeError, ValueError, TypeError) as exc:
                self._draft_source_issue = {"sha256": hashlib.sha256(raw).hexdigest() if raw is not None else None,
                                            "backup": None}
                self.diagnostics.record_exception(exc, "load project drafts", self._diagnostic_context())
                try:
                    backup = self._preserve_draft_source(raw)
                    message = ("Draft not loaded. Original retained; recovery copy: " + backup.name +
                               ". Saved decisions remain available.")
                except OSError as backup_error:
                    self.diagnostics.record_exception(backup_error, "preserve unreadable draft", self._diagnostic_context())
                    message = ("Draft not loaded; a recovery copy could not be written. The original is retained. "
                               "New drafts will not overwrite it until a backup can be created.")
                self._show_draft_warning(message)
        self.canvas.clear()
        self._refresh()
        if self._draft_source_issue:
            self._status("Project opened with a draft warning. No draft was restored; see the warning above.", True)
        else:
            self._status("Desktop project opened. Candidate organisation requires an explicit choice.")
        self.projectReady.emit(project)

    def _dismiss_draft_warning(self):
        self.draft_warning.hide()
        self.dismiss_draft_warning.hide()

    def _show_draft_warning(self, message):
        self.draft_warning.setText(message)
        self.draft_warning.show()
        self.dismiss_draft_warning.show()
        self.diagnostics.record_event("WARNING", message, "draft recovery", self._diagnostic_context())

    def _preserve_draft_source(self, raw=None):
        """Copy original bytes before any replacement; a failed copy blocks writes."""
        source = self.project.project_dir / "desktop_drafts.json"
        raw = source.read_bytes() if raw is None else raw
        digest = hashlib.sha256(raw).hexdigest()
        backup = source.with_name("desktop_drafts.recovery-" + digest[:16] + ".json")
        if backup.exists():
            if backup.read_bytes() != raw:
                raise OSError("The draft recovery filename contains different bytes; no draft was overwritten.")
        else:
            with backup.open("xb") as handle:
                handle.write(raw)
        self._draft_source_issue.update(sha256=digest, backup=str(backup))
        return backup

    def _guard_draft_overwrite(self):
        if not self._draft_source_issue:
            return
        source = self.project.project_dir / "desktop_drafts.json"
        if source.exists():
            current = source.read_bytes()
            digest = hashlib.sha256(current).hexdigest()
            previous = self._draft_source_issue.get("sha256")
            if previous is not None and digest != previous:
                raise ValueError("The draft file changed after the recovery warning. Reopen and inspect it before saving drafts.")
            backup = self._draft_source_issue.get("backup")
            if not backup or not Path(backup).is_file() or Path(backup).read_bytes() != current:
                self._preserve_draft_source(current)

    def _write_drafts(self):
        self._guard_draft_overwrite()
        _write_json(self.project.project_dir / "desktop_drafts.json",
                    {"schema": "syllafolio-desktop-drafts-v1", "input_fingerprint": self.state["input_fingerprint"],
                     "pages": self._draft_store})
        # The previous document is now safely backed up, not an active write basis.
        self._draft_source_issue = None

    def _execute(self, action, payload, *, success=None):
        if not self.project:
            raise ValueError("Open a desktop project first.")
        revision = self.state["revision"]
        key = "desktop-" + uuid.uuid4().hex
        def run(_progress, _cancelled):
            return self.project.execute(action, copy.deepcopy(payload),
                                        expected_revision=revision, idempotency_key=key)
        def completed(result):
            self.state = self.project.snapshot()
            if success:
                success(result)
            self._refresh()
        self._run(action.replace("batch_", "").replace("_", " ").title(), run, completed)

    @staticmethod
    def _tree_scope_key(data):
        if data.get("kind") == "page":
            return data.get("group_key")
        if data.get("kind") == "family":
            return "family:" + data["id"]
        return data.get("id") if data.get("kind") in {"group", "pool"} else None

    def _scope_review_id(self, key, state=None):
        state = state if state is not None else self.state or {}
        batch = state.get("batch_review", {})
        preview = self._audition_preview
        if preview is not None and key == preview.scope_key and preview.matches(state):
            return preview.review_id
        if key == "pool":
            return batch.get("pool", {}).get("latest_review_id")
        if key and key.startswith("family:"):
            return batch.get("active_family_reviews", {}).get(key.removeprefix("family:"))
        return batch.get("active_reviews", {}).get(key)

    @staticmethod
    def _tree_review_status(review):
        if not review:
            return "New"
        if review.get("audition_only"):
            return "Listening only"
        if review.get("archived"):
            return "Archived"
        if review.get("read_only"):
            return "Read only"
        if review.get("scope_stale"):
            return "Changed"
        if review.get("closed"):
            return "Finished"
        if review.get("status") == "ready":
            ids = set(review.get("item_ids", []))
            return f"{len(ids & set(review.get('decisions', {})))}/{len(ids)} saved"
        return {"queued": "Preparing", "running": "Preparing", "cancel_requested": "Cancelling",
                "cancelled": "Cancelled", "failed": "Failed", "interrupted": "Interrupted"}.get(review.get("status"), "Unknown")

    def _tree_items(self):
        def descendants(item):
            yield item
            for index in range(item.childCount()):
                yield from descendants(item.child(index))
        for index in range(self.group_list.topLevelItemCount()):
            yield from descendants(self.group_list.topLevelItem(index))

    def _refresh_group_tree(self, groups):
        batch = self.state["batch_review"]
        families = batch.get("families", [])
        def review_signature(key):
            rid = self._scope_review_id(key)
            row = self._review_by_id(rid)
            saved = set(row.get("decisions", {}))
            return (rid, row.get("status"), row.get("closed"), row.get("archived"), row.get("read_only"),
                row.get("scope_stale"), len(row.get("item_ids", [])), len(saved),
                [(page["page_id"], len(page.get("item_ids", [])), len(saved & set(page.get("item_ids", []))))
                 for page in row.get("pages", [])])
        signature = _hash({"organization": (batch.get("organization") or {}).get("organization_id"),
            "groups": [(g["group_id"], g.get("label"), len(g["item_ids"]), review_signature(g["group_id"])) for g in groups],
            "pool": (len(batch.get("pool", {}).get("active_ids", [])), review_signature("pool")),
            "families": [(f["family_id"], f.get("title"), f.get("member_count", len(f.get("member_ids", []))),
                          review_signature("family:" + f["family_id"])) for f in families]})
        if signature != self._queue_signature:
            current = self.group_list.currentItem()
            focused = current.data(0, Qt.ItemDataRole.UserRole) if current is not None else {}
            previously_blocked = self.group_list.blockSignals(True)
            try:
                self.group_list.clear()
                def add_directory(title, kind):
                    node = QTreeWidgetItem([title, "", ""])
                    node.setData(0, Qt.ItemDataRole.UserRole, {"kind": kind, "id": kind})
                    node.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                    self.group_list.addTopLevelItem(node)
                    return node

                def add_scope(parent, title, ids, kind, ident):
                    key = "family:" + ident if kind == "family" else ident
                    rid = self._scope_review_id(key)
                    review = self._review_by_id(rid)
                    row = QTreeWidgetItem([title, str(len(ids)), self._tree_review_status(review)])
                    row.setTextAlignment(1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                    row.setData(0, Qt.ItemDataRole.UserRole, {"kind": kind, "id": ident})
                    if kind == "family":
                        row.setFlags(row.flags() | Qt.ItemFlag.ItemIsEditable)
                    row.setToolTip(0, key)
                    row.setToolTip(2, json.dumps(system_fields({k: review.get(k) for k in
                        ("review_id", "status", "closed", "scope_stale_reason", "read_only_reason", "error")}), ensure_ascii=False))
                    self.group_list.addTopLevelItem(row) if parent is None else parent.addChild(row)
                    cluster_numbers = {}
                    for index, page in enumerate(review.get("pages", []), 1):
                        page_ids = page.get("item_ids", [])
                        saved = len(set(page_ids) & set(review.get("decisions", {})))
                        label = f"Page {index}"
                        if review.get("route") in {"family_cluster", "pool_cluster"} and len(review.get("item_ids", [])) > 1:
                            cluster = page.get("subcluster_id")
                            number = cluster_numbers.setdefault(cluster, len(cluster_numbers) + 1)
                            label += " · Unclustered" if cluster == "unclustered" else f" · Cluster {number}"
                        status = ("Listening only" if review.get("audition_only") else "Finished"
                                  if review.get("closed") else f"{saved}/{len(page_ids)} saved")
                        child = QTreeWidgetItem([label, str(len(page_ids)), status])
                        child.setTextAlignment(1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                        child.setData(0, Qt.ItemDataRole.UserRole, {"kind": "page", "id": page["page_id"],
                            "page_id": page["page_id"], "review_id": rid, "group_key": key})
                        child.setToolTip(0, f"{rid}\n{page['page_id']}")
                        row.addChild(child)
                    return row
                candidates = add_directory("Candidate Groups", "candidates_root")
                for group in groups:
                    add_scope(candidates, group.get("label", group["group_id"]), group["item_ids"], "group", group["group_id"])
                candidates.setText(2, "1 group" if len(groups) == 1 else f"{len(groups)} groups")
                candidates.setExpanded("candidates_root" in self._expanded_review_sections)
                add_scope(None, "Global pool", batch.get("pool", {}).get("active_ids", []), "pool", "pool")
                directory = add_directory("Family reviews", "families_root")
                for family in families:
                    row = add_scope(directory, family.get("title") or family["family_id"], family.get("member_ids", []),
                                    "family", family["family_id"])
                    if ("families_root" in self._expanded_review_sections
                            and focused and focused.get("kind") == "family" and focused["id"] == family["family_id"]
                            and self.selected_group == "family:" + family["family_id"]):
                        self.group_list.setCurrentItem(row)
                directory.setExpanded("families_root" in self._expanded_review_sections)
                self._queue_signature = signature
            finally:
                self.group_list.blockSignals(previously_blocked)
        self._restore_group_highlight()

    def _refresh(self):
        if not self.project:
            return
        latest = self._sample_project_state()
        if latest.get("desktop_sampling", {}).get("pending"):
            self._refresh_job_view(latest); self._set_enabled()
            return
        self.state = latest
        self._pending_refresh = False
        if self._audition_preview is not None and not self._audition_preview.matches(latest):
            if self.selected_review_id == self._audition_preview.review_id:
                self._clear_review_page()
                self.selected_review_id = None
            self._discard_audition_preview()
        self._refresh_job_view(self.state)
        self._record_failed_reviews()
        batch = self.state["batch_review"]
        self._update_header()
        self._refresh_families()
        organization = batch.get("organization")
        if organization:
            groups = organization["groups"]
        else:
            result = self.project.execute("batch_preview", {"mode": "auto"},
                    expected_revision=self.state["revision"], idempotency_key="desktop-preview-"+uuid.uuid4().hex)
            self._preview = result["preview"]
            groups = self._preview["groups"]
        self._tree_groups = groups
        self._refresh_group_tree(groups)
        self._refresh_pages()
        self._set_enabled()

    def _refresh_families(self):
        selected = self.current_family.currentData()
        if (self.selected_group or "").startswith("family:"):
            selected = self.selected_group.removeprefix("family:")
        self._rendering = True
        self.current_family.clear()
        self.current_family.addItem("Choose or create an evidenced human family", "")
        for family in self.state["batch_review"].get("families", []):
            self.current_family.addItem(f"{family.get('title') or family['family_id']} · {family['family_id']}", family["family_id"])
        self.current_family.setCurrentIndex(max(0, self.current_family.findData(selected)))
        self._rendering = False
        if self.selected_item_id:
            self._render_editor()

    def apply_organization(self, mode=None):
        """Apply only an already displayed preview; never silently request another one."""
        if self._busy:
            raise ValueError("Wait for the current foreground operation to finish.")
        dialog = self._stage_a_dialog
        if dialog is None or (mode is not None and dialog.controller.snapshot()["parameters"]["mode"] != mode):
            raise OrganizationControllerError("ORGANIZATION_PREVIEW_REQUIRED", "Right-click Candidate Groups and open the candidate workspace to preview the organisation first.")
        dialog.apply()

    def _organization_applied(self):
        self._cancel_tree_navigation()
        self._retire_review_views("Candidate organization changed.")
        self.selected_group = self.selected_review_id = self.selected_page_id = self.selected_item_id = None
        self._page_key = None
        self._page = None
        self._page_load_error = False
        self._rows = {}
        self._dirty = False
        self.canvas.clear()
        self.slot_list.clear()
        self._queue_signature = self._pages_signature = None
        self._status("Candidate organisation saved. Choose a complete group to prepare.")

    def _group_changed(self):
        selected = self.group_list.selectedItems()
        if selected:
            self._queue_tree_navigation(selected[0])

    def _tree_item_expanded(self, item):
        kind = (item.data(0, Qt.ItemDataRole.UserRole) or {}).get("kind")
        if kind == "pool":
            self._expanded_review_sections.add("pool")
        if kind in ("candidates_root", "families_root"):
            self._expanded_review_sections.add(kind)
            if self.selected_group and self.selected_group != "pool":
                active = "families_root" if self.selected_group.startswith("family:") else "candidates_root"
                if kind == active:
                    QTimer.singleShot(0, self, self._restore_group_highlight)
            return
        self._queue_tree_navigation(item)

    def _tree_item_collapsed(self, item):
        kind = (item.data(0, Qt.ItemDataRole.UserRole) or {}).get("kind")
        self._expanded_review_sections.discard(kind)
        self._reset_accessible_tree_rows()

    def _tree_item_activated(self, item, _column=0):
        data = item.data(0, Qt.ItemDataRole.UserRole) or {}
        if data.get("kind") in ("candidates_root", "families_root"):
            item.setExpanded(not item.isExpanded())
            return
        self._queue_tree_navigation(item, reload_page=data.get("kind") == "page")

    def _queue_tree_navigation(self, item, *, reload_page=False):
        data = item.data(0, Qt.ItemDataRole.UserRole) or {}
        if self._rendering or not self.project or not self._tree_scope_key(data):
            return
        # Qt can still be dispatching the parent's mouse/selection event. Only
        # copy the intent here; collapsing rows or selecting a child must wait.
        self._pending_tree_navigation = (self.project, self._close_coordinator.generation, dict(data), reload_page)
        self._tree_navigation_timer.start(0)

    def _cancel_tree_navigation(self):
        self._tree_navigation_timer.stop()
        self._pending_tree_navigation = None

    def _apply_tree_navigation(self):
        pending = self._pending_tree_navigation
        self._pending_tree_navigation = None
        if pending is None:
            return
        project, generation, data, reload_page = pending
        if self.project is not project or not self._close_coordinator.accepts_callback(generation):
            return
        if self._busy or self._close_coordinator.state != "open":
            self._restore_group_highlight()
            return
        try:
            self._navigate_tree_data(data, reload_page=reload_page)
        except Exception as exc:
            self._restore_group_highlight()
            self._error(exc, operation="open review tree page")

    def _navigation_snapshot(self):
        fields = ("selected_group", "selected_review_id", "selected_page_id", "selected_item_ids", "_page_key", "_page", "_page_rate",
                  "_rows", "_draft_base", "_new_families", "_page_defaults", "_explicit_items", "_pending_submission", "_dirty")
        return {"project": self.project, "values": {key: copy.deepcopy(getattr(self, key)) for key in fields},
                "audio": self._page_audio, "family": self.current_family.currentData()}

    def _restore_failed_page_load(self):
        rollback = getattr(self, "_page_load_rollback", None)
        self._page_load_rollback = None
        if not rollback or rollback["project"] is not self.project or self._close_coordinator.state != "open":
            return
        try:
            for key, value in rollback["values"].items():
                setattr(self, key, value)
            self._page_audio = rollback["audio"]
            self._page_request = self._page_request_token = None
            self._page_load_error = True  # A failed target is retried only by a new explicit navigation.
            rendering, self._rendering = self._rendering, True
            try:
                self.current_family.setCurrentIndex(max(0, self.current_family.findData(rollback["family"])))
            finally:
                self._rendering = rendering
            self.slot_list.blockSignals(True)
            try:
                self.slot_list.clear()
                for item_id in (self._page or {}).get("item_ids", []):
                    item = QListWidgetItem()
                    item.setData(Qt.ItemDataRole.UserRole, item_id)
                    self.slot_list.addItem(item)
                self._update_slot_labels()
            finally:
                self.slot_list.blockSignals(False)
            if self._page is not None:
                self._draw_audition_page()
                self.audio.set_audio(self._page_audio, self._page_rate)
            else:
                self.canvas.clear()
            self.confirm.setChecked(False)
            self._sync_item_selection()
            self._render_editor()
            self._restore_group_highlight()
        except Exception as exc:
            self.diagnostics.record_exception(exc, "restore previous page after load failure", self._diagnostic_context())

    def _navigate_tree_data(self, data, *, reload_page=False):
        key = self._tree_scope_key(data)
        if key is None:
            self._restore_group_highlight()
            return
        rid = self._scope_review_id(key)
        if data.get("kind") == "page" and (not rid or data.get("review_id") != rid):
            raise ValueError("This page belongs to an older review round. Select a page from the current review.")
        review = self._review_by_id(rid)
        target = data.get("page_id") if data.get("kind") == "page" else None
        if target is not None and target not in {page["page_id"] for page in review.get("pages", [])}:
            raise ValueError("The selected page does not belong to this review.")
        changed = key != self.selected_group or rid != self.selected_review_id
        rollback = None
        if changed:
            self._capture_editor()
            self._save_drafts()
            self._retire_review_views("Review scope selection changed.")
            rollback = self._navigation_snapshot()
            self._clear_review_page()
            self.selected_group, self.selected_review_id = key, rid
            if key == "pool":
                self._expanded_review_sections.add("pool")
            self._pages_signature = None
            self._refresh_families()
        self._restore_group_highlight()
        if target is None and changed and review.get("status") == "ready" and review.get("pages"):
            target = review["pages"][0]["page_id"]
        if target is None and self._page is None and review.get("status") == "ready" and review.get("pages"):
            target = review["pages"][0]["page_id"]
        if target is not None and (reload_page or changed or target != self.selected_page_id or self._page is None):
            self._load_page(target, expected_review_id=rid, rollback=rollback)
        self._set_enabled()

    def _reset_accessible_tree_rows(self, _item=None):
        # Qt 6.8's accessible tree caches cells by visible row number. Expand
        # invalidates this cache, but collapse removes visible rows without a
        # model-reset notification. UIA can then follow a sibling back to itself.
        event = QAccessibleTableModelChangeEvent(self.group_list, QAccessibleTableModelChangeEvent.ModelChangeType.ModelReset)
        QAccessible.updateAccessibility(event)

    def _restore_group_highlight(self, *, reveal=False):
        tree = self.group_list
        if reveal and self.selected_group == "pool":
            self._expanded_review_sections.add("pool")
        previously_blocked = tree.blockSignals(True)
        try:
            chosen = parent = None
            collapsed = False
            requested = getattr(self, "_page_request", None)
            page_id = self.selected_page_id or (requested[1] if requested and requested[0] == self.selected_review_id else None)
            for row in self._tree_items():
                data = row.data(0, Qt.ItemDataRole.UserRole) or {}
                key = self._tree_scope_key(data)
                if data.get("kind") in {"group", "pool", "family"}:
                    expand = key == self.selected_group and (key != "pool" or "pool" in self._expanded_review_sections)
                    if row.childCount() and row.isExpanded() != expand:
                        row.setExpanded(expand)
                        collapsed |= not expand
                    if key == self.selected_group:
                        parent = row
                elif (data.get("kind") == "page" and key == self.selected_group
                      and data.get("review_id") == self.selected_review_id and data.get("page_id") == page_id):
                    chosen = row
            # A selected family heading is also a name that can be edited with F2.
            # Keep focus there when its first page loads; an explicit page click still selects the page.
            if parent is not None and tree.currentItem() is parent and parent.data(0, Qt.ItemDataRole.UserRole)["kind"] == "family":
                chosen = parent
            chosen = chosen or parent
            if chosen is not None:
                ancestor = chosen.parent()
                while ancestor is not None:
                    if not ancestor.isExpanded():
                        kind = ancestor.data(0, Qt.ItemDataRole.UserRole)["kind"]
                        if kind in ("candidates_root", "pool", "families_root") and not reveal:
                            # Selecting a hidden page would make Qt reopen its directory.
                            chosen = ancestor
                        else:
                            ancestor.setExpanded(True)
                            if kind in ("candidates_root", "pool", "families_root"):
                                self._expanded_review_sections.add(kind)
                    ancestor = ancestor.parent()
            if collapsed:
                # itemCollapsed is muted during programmatic highlight sync.
                self._reset_accessible_tree_rows()
            if tree.currentItem() is not chosen or (chosen is not None and not chosen.isSelected()):
                tree.setCurrentItem(chosen, 0, QItemSelectionModel.SelectionFlag.ClearAndSelect | QItemSelectionModel.SelectionFlag.Rows)
                if chosen is not None:
                    tree.scrollToItem(chosen)
        finally:
            tree.blockSignals(previously_blocked)

    def _refresh_pages(self):
        rid = self._scope_review_id(self.selected_group)
        rollback = None
        if rid != self.selected_review_id:
            # Page IDs are local to a round. A new round's page-0001 must never
            # inherit the previous round's PCM, slots or unsent draft binding.
            self._save_drafts()
            self._retire_review_views("The current review round changed.")
            rollback = self._navigation_snapshot()
            self._clear_review_page()
            self.selected_review_id = rid
            self._pages_signature = None
        review = self._review()
        pages = review.get("pages", [])
        signature = _hash([self.selected_review_id, [(p["page_id"], p["item_ids"]) for p in pages]])
        self._pages_signature = signature
        self._restore_group_highlight()
        self._load_first_available_page(rollback=rollback)
        if self._page:
            self._update_slot_labels()
        self._update_header()

    def _load_first_available_page(self, *, rollback=None):
        if (not self.project or self._busy or self._page is not None or self._page_load_error
                or self._close_coordinator.state != "open" or getattr(self, "_defer_tree_page_load", False)):
            return
        review = self._review()
        if review.get("status") == "ready" and review.get("pages"):
            self._load_page(review["pages"][0]["page_id"], expected_review_id=self.selected_review_id, rollback=rollback)

    def prepare_selected(self, *, reason="", use_exception=False):
        if not self.project or self._busy or self._close_coordinator.state != "open" or self._sampling_pending:
            raise ValueError("Wait for the current project operation before preparing a review.")
        if not self.selected_group:
            raise ValueError("Choose a candidate group or the global pool first.")
        self._save_drafts()
        if self.selected_group.startswith("family:"):
            return self._prepare_family_audition(self.selected_group.removeprefix("family:"), reason, use_exception)
        if self.selected_group == "pool":
            return self._prepare_pool_audition(reason=reason, use_exception=use_exception)
        batch = self.state["batch_review"]
        payload = {"gap_seconds": self.gap.value(), "normalization": "per_item"}
        review = self._review()
        group = next((x for x in (batch.get("organization") or {}).get("groups", []) if x["group_id"] == self.selected_group), None)
        if not group:
            raise ValueError("Explicitly apply a candidate organisation before preparing review.")
        payload.update(group_id=self.selected_group, route="direct" if len(group["item_ids"]) <= 30 else "cluster")
        reason = reason.strip()
        if use_exception:
            if not review.get("direct_exception", {}).get("allowed") or not reason:
                raise ValueError("A current recorded calculation exception and actual reason are required.")
            payload.update(route="direct_exception", exception_reason=reason)
        if review.get("closed") or review.get("decisions"):
            if not reason:
                raise ValueError("State the new evidence / reopening reason before another preparation.")
            payload["reopen_reason"] = reason
        self._execute("batch_prepare", payload, success=lambda _result: self._prepared())

    def _prepared(self):
        self._pages_signature = None
        self._status("Preparation requested. All targets remain in the scope; waiting for its recorded result.")

    def _prepare_pool_audition(self, *, reason="", use_exception=False, interactive=False, refreeze=False):
        project = self.project
        if not project or self.selected_group != "pool" or self._busy or self._close_coordinator.state != "open":
            raise ValueError("Select Global pool and wait for the current operation to finish.")
        state = self._sample_project_state(require_fresh=True)
        generation = self._close_coordinator.generation
        controller = PoolReviewController(cluster_for_audition=True)
        scope = controller.select(project, snapshot=state, generation=generation)["scope"]
        reason = reason.strip()
        if use_exception and (not scope["direct_exception"].get("allowed") or not reason):
            raise ValueError("A matching clustering failure and an explicit exception reason are required.")
        if not refreeze and not use_exception and (scope["can_continue"] or scope["can_go_final"]):
            self._refresh()
            return
        gap = self.gap.value()
        display = self.display_controller.snapshot()
        empty = not scope["current_pool_ids"]
        if not (scope["can_refreeze"] if refreeze else scope["can_prepare"]):
            message = " ".join(row["message"] for row in scope["blockers"])
            raise ValueError(message or "Inspect Pool details before preparing the current pool.")
        if scope["requires_reopen_reason"] and not reason and interactive:
            reason, accepted = QInputDialog.getText(self, "Prepare pool again", "Why does the current pool need a new round?")
            if not accepted:
                return
            reason = reason.strip()
        if empty:
            if not interactive or use_exception:
                raise ValueError("Use Skip empty pool to confirm that this pool review can be skipped.")
            if QMessageBox.question(self, "Skip empty pool", "Record that the current global pool contains no syllables?",
                                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                    QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
                return
        if self.project is not project or self.selected_group != "pool" or not self._close_coordinator.accepts_callback(generation):
            raise ValueError("The selected project or review changed. Select the pool again.")
        key = "desktop-pool-" + uuid.uuid4().hex
        def prepare_round(_progress, _cancelled):
            return controller.prepare(project, generation=generation, idempotency_key=key,
                reopen_reason=reason if refreeze or scope["requires_reopen_reason"] else "", refreeze=refreeze,
                use_direct_exception=use_exception, exception_reason=reason if use_exception else "",
                gap_seconds=gap, normalization=display["normalization"], empty_only=empty)
        def requested(result):
            self._review_preparation_requested(result)
            if result["empty_pool_skip"]:
                self._status("Empty-pool skip recorded.")
        self._run("Record empty-pool skip" if empty else "Prepare global pool", prepare_round, requested)

    def _prepare_family_audition(self, family_id, reason, use_exception):
        project = self.project
        state = self._sample_project_state(require_fresh=True)
        generation = self._close_coordinator.generation
        controller = FamilyReviewController()
        scope = controller.select(project, family_id, snapshot=state, generation=generation)["scope"]
        if scope["directed_review_managed_elsewhere"]:
            raise ValueError("Continue the existing directed review from Type definitions and directed review.")
        if scope["can_continue"] and not use_exception:
            self._refresh()
            return
        if scope["empty"]:
            raise ValueError("Assign at least one syllable to this family first.")
        if use_exception and (not scope["direct_exception"].get("allowed") or not reason.strip()):
            raise ValueError("A matching clustering failure and an explicit exception reason are required.")
        gap = self.gap.value()
        display = self.display_controller.snapshot()
        if family_review_conflicts(state, family_id):
            def prepare_preview(_progress, cancel):
                return FamilyAuditionPreview.prepare(project, family_id, state, gap_seconds=gap,
                    normalization=display["normalization"], settings=display["settings"], check_cancelled=cancel)

            self._run("Prepare family listening pages", prepare_preview, self._install_audition_preview)
            return
        key = "desktop-family-" + uuid.uuid4().hex
        def prepare_round(_progress, _cancel):
            return controller.prepare(project, generation=generation, idempotency_key=key,
                cluster_for_audition=True, reopen_reason=reason, use_direct_exception=use_exception,
                exception_reason=reason if use_exception else "", gap_seconds=gap, normalization=display["normalization"])
        self._run("Prepare family review", prepare_round, self._review_preparation_requested)

    def _install_audition_preview(self, preview):
        if self.selected_group != preview.scope_key or not preview.matches(self.project.snapshot()):
            preview.close()
            self._status("The review scope changed. Select the current scope to prepare its listening pages.")
            return
        self._discard_audition_preview()
        self._audition_preview = preview
        self._refresh()

    def _review_preparation_requested(self, _result):
        self._prepared()
        self._refresh()

    def _discard_audition_preview(self):
        if self._audition_preview is not None:
            self._audition_preview.close()
            self._audition_preview = None

    def _page_changed(self, current, _previous):
        """Compatibility callback for a real page node, never a second list."""
        if self._busy or current is None:
            return
        try:
            self._navigate_tree_data(current.data(0, Qt.ItemDataRole.UserRole) or {})
        except Exception as exc:
            self._restore_group_highlight()
            self._error(exc, operation="change audition page")

    def _saved_page(self, state=None):
        state = state or self.state
        review = state["batch_review"].get("reviews", {}).get(self.selected_review_id, {})
        return {item: copy.deepcopy(review.get("decisions", {}).get(item)) for item in (self._page or {}).get("item_ids", [])}

    @staticmethod
    def _row(item_id, saved):
        saved = saved or {}
        return {"item_id": item_id, "disposition": saved.get("disposition", ""),
                "family_id": saved.get("family_id") or "", "reason": saved.get("reason", ""),
                "reason_code": saved.get("reason_code") or "",
                "boundary_flag": bool(saved.get("boundary_flag", False)),
                "form_status": saved.get("form_status", "not_assessed"),
                **({"pending_steps": saved["pending_steps"]} if "pending_steps" in saved else {}),
                "new_family_key": saved.get("new_family_key", ""),
                "creation_record_id": saved.get("creation_record_id", "")}

    def page_default_ids(self):
        return [item for item in (self._page or {}).get("item_ids", []) if item in self._page_defaults]

    def _default_candidates(self, state):
        if not self._page or not state:
            return {}
        batch = state.get("batch_review", {})
        review = batch.get("reviews", {}).get(self.selected_review_id, {})
        family = self.current_family.currentData() or ""
        if (review.get("compact_submit_allowed") is not True or review.get("directed_issue_id")
                or review.get("directed_context") or not family
                or family not in {row["family_id"] for row in batch.get("families", [])}):
            return {}
        protected = set(batch.get("assignments", {})) | set(self._explicit_items)
        protected.update(review.get("item_locks", {}))
        for row in batch.get("families", []):
            protected.update(row.get("member_ids", []))
        for issue in state.get("issues", {}).values():
            protected.update(issue.get("target_ids", []))
        for other_id, other in batch.get("reviews", {}).items():
            if (other_id != self.selected_review_id and not any(other.get(k) for k in
                    ("closed", "archived", "read_only", "superseded_by_protocol_upgrade"))
                    and other.get("status") in ACTIVE | {"ready"}):
                protected.update(other.get("item_ids", []))
        return {item: family for item in self._page["item_ids"] if item not in protected
                and not self._draft_base.get(item)
                and self._rows.get(item) == self._row(item, self._draft_base.get(item))}

    def _refresh_page_defaults(self):
        self._page_defaults = self._default_candidates(self.state)
        if self._pending_submission and self._pending_submission.get("whole_page"):
            text = "Receipt not yet confirmed. Retry Save page reuses the original request."
        elif self._directed_editor() or self._review().get("audition_only"):
            text = ""
        elif lock := self._review().get("item_locks", {}).get(self.selected_item_id):
            text = f"{self.selected_item_id}: {lock['message']}"
        elif (self._page and not self._page_submission_ids() and not self.current_family.currentData()
                and not all(self._draft_base.get(item) for item in self._page["item_ids"])):
            text = "Choose Current human family to preview unsaved defaults. Only Save writes outcomes."
        else:
            text = ""
        self.page_defaults_info.setText(text)
        self.page_defaults_info.setVisible(bool(text))

    def _editor_row(self, item):
        row = copy.deepcopy(self._rows[item])
        if item in self._page_defaults:
            row.update(disposition="current", family_id=self._page_defaults[item])
        if not self._directed_editor() and row.get("disposition") in {"current", "other"} and row.get("family_id"):
            row["disposition"] = "current" if row["family_id"] == self.current_family.currentData() else "other"
        return row

    def _page_submission_ids(self):
        locks = self._review().get("item_locks", {})
        return [item for item in (self._page or {}).get("item_ids", []) if item not in locks
                and (item in self._page_defaults or self._rows.get(item, {}).get("disposition")
                     and self._rows[item] != self._row(item, self._draft_base.get(item)))]

    def _editor_values(self):
        return (self.disposition.currentData(), self.target_family.currentData(), self.reason_code.currentData(),
                self.reason.toPlainText(), self.boundary.isChecked(), self.form_status.currentData(),
                self.new_family_draft.currentData(), self.new_title.text(), self.new_reason.text())

    def _recompute_dirty(self):
        self._dirty = (any(row != self._row(item, self._draft_base.get(item)) for item, row in self._rows.items())
            or any(item in self._rows and not self._rows[item].get("disposition") for item in self._explicit_items)
            or bool(self._new_families))

    def _load_page(self, page_id, *, expected_review_id=None, rollback=None):
        if self._busy:
            raise ValueError("Wait for the foreground operation before loading another page.")
        if expected_review_id is not None and expected_review_id != self.selected_review_id:
            raise ValueError("The selected page belongs to a different review round.")
        review = self._review()
        if review.get("status") != "ready" or page_id not in {row["page_id"] for row in review.get("pages", [])}:
            raise ValueError("Choose an existing prepared page in the current review.")
        self._save_drafts()
        self._retire_review_views("The audition page changed.")
        rollback = rollback or self._navigation_snapshot()
        self._stop_review_audio()
        project, review_id = self.project, self.selected_review_id
        group_key = self.selected_group
        display = self.display_controller.snapshot()
        controller = DisplayController(display["settings"], display["normalization"])
        preview = self._audition_preview if review.get("audition_only") else None
        self._page_load_error = False
        self._page = None
        self._page_key = None
        self.selected_page_id = None
        self.selected_item_id = None
        self._rows = {}; self._dirty = False
        self._page_defaults = {}; self._explicit_items = set()
        self.slot_list.clear()
        self.canvas.clear()
        token = uuid.uuid4().hex
        self._page_request = (review_id, page_id)
        self._page_request_token = token
        self._page_load_rollback = rollback
        self._restore_group_highlight()
        def load(_progress, _cancelled):
            if self._scope_review_id(group_key, project.snapshot()) != review_id:
                raise ValueError("The active review changed before its page could be loaded.")
            if preview is not None:
                result = preview.load_page(page_id, display, _cancelled)
                return {**result, "state": project.snapshot()}
            page = project.review_page(review_id, page_id)
            samples, rate = sf.read(project.media_path(page["audio_url"]), dtype="float32")
            shown = controller.page(project, review_id, page_id, expected_revision=project.snapshot()["revision"],
                                    idempotency_key="desktop-page-display-" + uuid.uuid4().hex)
            for key in ("waveform_url", "spectrogram_url", "display"):
                if key in shown:
                    page[key] = copy.deepcopy(shown[key])
            state = project.snapshot()
            if self._scope_review_id(group_key, state) != review_id:
                raise ValueError("The active review changed while its page was loading.")
            return {"page": page, "samples": samples, "rate": int(rate), "state": state}
        self._run("Load audition page", load, lambda result: self._install_loaded_page(result, review_id, page_id,
                                                                                   project=project, request_token=token))

    def _install_loaded_page(self, result, review_id, page_id, *, project=None, request_token=None):
        if (not self.project or self.selected_review_id != review_id or project is not None and self.project is not project
                or request_token is not None and request_token != getattr(self, "_page_request_token", None)):
            return
        page, samples, rate = result["page"], result["samples"], result["rate"]
        if page.get("page_id") != page_id:
            raise ValueError("Loaded media does not match the requested review page.")
        state = result["state"] if result["state"]["revision"] >= self.state["revision"] else self.state
        if self._scope_review_id(self.selected_group, state) != review_id:
            raise ValueError("The active review changed before this page could be displayed.")
        self._page_load_error = False
        self._page = page
        self._page_audio, self._page_rate = samples, int(rate)
        self.selected_page_id = page_id
        self._page_key = f"{self.selected_review_id}:{page_id}"
        self.state = state
        self.selected_item_id = None
        self.confirm.setChecked(False)
        self._refresh_families()
        self._draft_base = self._saved_page()
        self._rows = {item: self._row(item, self._draft_base[item]) for item in page["item_ids"]}
        self._new_families = {}
        self._explicit_items = set()
        self._dirty = False
        self._pending_submission = None
        if not self._review().get("audition_only"):
            self._restore_page_inputs()
        self._refresh_page_defaults()
        self._update_display_info()
        self._draw_audition_page()
        self.audio.set_audio(samples, rate)
        self.slot_list.blockSignals(True)
        self.slot_list.clear()
        for item_id in page["item_ids"]:
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, item_id)
            self.slot_list.addItem(item)
        self._update_slot_labels()
        self.slot_list.blockSignals(False)
        self.selected_item_id = None
        self._update_header()
        self._sync_item_selection()
        self._render_editor()
        self._page_request = self._page_request_token = None
        self._page_load_rollback = None
        self._restore_group_highlight()
        self._set_enabled()

    def _slot_changed(self, current, _previous):
        if current is None or self._rendering or self._busy:
            self._sync_item_selection()
            return
        try:
            self.select_item(current.data(Qt.ItemDataRole.UserRole))
        except Exception as exc:
            self._sync_item_selection()
            self._error(exc, operation="change selected target")

    def _sync_item_selection(self):
        self.canvas.select_items(self.selected_item_ids)
        self.slot_list.blockSignals(True)
        try:
            self.slot_list.clearSelection()
            current = None
            for i in range(self.slot_list.count()):
                item = self.slot_list.item(i)
                chosen = item.data(Qt.ItemDataRole.UserRole) in self.selected_item_ids
                item.setSelected(chosen)
                if chosen:
                    current = item
            self.slot_list.setCurrentItem(current, QItemSelectionModel.SelectionFlag.NoUpdate)
        finally:
            self.slot_list.blockSignals(False)

    def _item_clicked(self, item_id, ctrl=False):
        if item_id not in self._rows:
            return
        chosen = set(self.selected_item_ids)
        if ctrl:
            chosen.symmetric_difference_update({item_id})
        else:
            chosen = set() if self.selected_item_ids == (item_id,) else {item_id}
        self.select_items(chosen)

    def select_item(self, item_id):
        """Programmatic navigation is always an explicit single selection."""
        if item_id in self._rows:
            self.select_items((item_id,))

    def select_items(self, item_ids):
        if self._busy or not self._page or self._close_coordinator.state != "open":
            self._sync_item_selection()
            return
        requested = set(item_ids)
        order = self._page.get("item_ids", list(self._rows))
        if not requested <= set(order) or not requested <= set(self._rows):
            raise ValueError("Selection must contain only items on the current page.")
        selected = tuple(item for item in order if item in requested)
        if selected == self.selected_item_ids:
            self._sync_item_selection()
            return
        media_player = getattr(self._review_media_dialog, "player", None)
        had_audio = self.audio.sink is not None or getattr(media_player, "sink", None) is not None
        try:
            self._capture_editor()
            self._save_drafts()
            self._retire_review_views("Selection changed; recheck any restored fine draft before submitting.")
        except Exception:
            self._sync_item_selection()
            raise
        self._stop_review_audio()
        self.selected_item_ids = selected
        self.confirm.setChecked(False)
        self._sync_item_selection()
        if selected:
            slot = next(x for x in self._page["slots"] if x["item_id"] == selected[0])
            self.canvas.set_position(slot["start_seconds"])
        else:
            self.canvas.set_position(0)
        self._render_editor()
        self._update_playback_buttons(False)
        if had_audio:
            self._status("Selection changed. Playback stopped; choose Play or Loop to audition the new scope.")

    def _render_selection_summary(self):
        count = len(self.selected_item_ids)
        self.item_title.setText(f"{count} syllables selected" if count > 1 else "No syllable selected")
        self.item_status.setText("Select one to edit; playback follows the selection." if count > 1 else
            "Play and Loop use the whole page." if self._page else "Choose a review page.")
        self.confirm.hide()
        self._editor_target_key = None
        self._rendered_editor_values = None

    def _render_editor(self):
        if self.selected_item_id not in self._rows or self._review().get("audition_only"):
            self.item_form_scroll.hide()
            self._render_selection_summary()
            self._set_enabled()
            return
        self._refresh_page_defaults()
        row = self._editor_row(self.selected_item_id)
        self._rendering = True
        self.item_title.setText(self.selected_item_id)
        self.item_title.setToolTip(self.selected_item_id)
        family_id = self.current_family.currentData()
        families = self.state.get("batch_review", {}).get("families", [])
        family_name = next((f.get("title") or family_id for f in families if f["family_id"] == family_id), family_id)
        self.disposition.setItemText(self.disposition.findData("current"),
                                     f"Current family · {family_name}" if family_name else "Current family")
        self.disposition.setCurrentIndex(max(0, self.disposition.findData(row["disposition"])))
        directed = self._directed_editor()
        target_key = (self.selected_review_id, self.selected_page_id, self.selected_item_id)
        if getattr(self, "_editor_target_key", None) != target_key:
            self.notes_toggle.setChecked(False)
            self.details_toggle.setChecked(False)
            self._editor_target_key = target_key
        self.confirm.setVisible(directed)
        if not directed:
            self.confirm.setChecked(False)
        self.notes_toggle.setVisible(not directed)
        self.evidence_label.setVisible(directed)
        self.notes_toggle.setText("Hide note" if self.notes_toggle.isChecked() else "View note" if row.get("reason") else "Add note")
        self.notes_body.setVisible(directed or self.notes_toggle.isChecked())
        self.reason.setPlaceholderText("Record actual evidence or uncertainty for this directed review." if directed else
            "Optional note. Leave blank when there is no additional observation to record.")
        self.reason_code.clear()
        self.reason_code.addItem("Choose a reason category", "")
        codes = self._submission_capabilities().get("reason_codes", {}).get(row["disposition"], [])
        for option in codes:
            if isinstance(option, dict) and isinstance(option.get("code"), str) and isinstance(option.get("label"), str):
                self.reason_code.addItem(option["label"], option["code"])
        self.reason_code.setCurrentIndex(max(0, self.reason_code.findData(row.get("reason_code", ""))))
        needs_code = not directed and row["disposition"] in {"pool", "discard"}
        self.reason_code.setVisible(needs_code)
        self.reason_code_label.setVisible(needs_code)
        current = self.current_family.currentData() or ""
        self.target_family.clear()
        self.target_family.addItem("Choose an existing other family", "")
        for family in self.state["batch_review"].get("families", []):
            if family["family_id"] != current:
                self.target_family.addItem(f"{family.get('title') or family['family_id']} · {family['family_id']}", family["family_id"])
        self.target_family.setCurrentIndex(max(0, self.target_family.findData(row.get("family_id"))))
        other = row["disposition"] == "other"
        self.target_family.setVisible(other)
        self.target_label.setVisible(other)
        self.target_empty.setVisible(other and self.target_family.count() == 1)
        self.target_empty.setText("No other human family exists yet. Choose New family or Family unresolved; machine groups are not human families.")
        is_new = row["disposition"] == "new_family"
        self.new_family_draft.clear()
        self.new_family_draft.addItem("New proposal", "")
        for key, spec in self._new_families.items():
            self.new_family_draft.addItem(spec["title"] or "Untitled proposal", key)
        for saved in self._review().get("decisions", {}).values():
            key = saved.get("new_family_key")
            if saved.get("disposition") == "new_family" and key and self.new_family_draft.findData(key) == -1:
                self.new_family_draft.addItem(f"Created: {saved.get('family_id')}", key)
        self.new_family_draft.setCurrentIndex(max(0, self.new_family_draft.findData(row.get("new_family_key"))))
        spec = self._new_families.get(row.get("new_family_key"), {})
        self.new_title.setText(spec.get("title", ""))
        self.new_reason.setText(spec.get("reason", ""))
        for widget in (self.new_family_draft, self.new_draft_label):
            widget.setVisible(is_new)
        for widget in (self.new_title, self.new_title_label, self.new_reason, self.new_reason_label):
            widget.setVisible(is_new and not row.get("creation_record_id"))
        self.new_title.setReadOnly(bool(row.get("creation_record_id")))
        self.new_reason.setReadOnly(bool(row.get("creation_record_id")))
        self.reason.setPlainText(row["reason"])
        self.form_status.setCurrentIndex(max(0, self.form_status.findData(row["form_status"])))
        show_form = directed and row["disposition"] in {"current", "other", "new_family"}
        self.form_status.setVisible(show_form)
        self.form_status_label.setVisible(show_form)
        pending_steps = (row.get("pending_steps") or "").strip()
        self.legacy_form_info.setText("Within-family form: " + str(row.get("form_status", "not_assessed")).replace("_", " ")
            + ". Fine review is the place for a new detailed form decision."
            + ("\nPending follow-up (blocks Finish): " + pending_steps if pending_steps else ""))
        self.boundary.setChecked(row["boundary_flag"])
        if row["boundary_flag"] or pending_steps:
            self.details_toggle.setChecked(True)
        status = self._item_save_status(self.selected_item_id)
        lock = self._review().get("item_locks", {}).get(self.selected_item_id)
        self.item_status.setText("Read-only" if lock else status)
        self.item_status.setToolTip(lock["message"] if lock else row.get("family_id") or status)
        self._rendered_editor_values = self._editor_values()
        self._rendering = False
        self._layout_item_fields()
        self._set_enabled()

    def _item_save_status(self, item):
        base = self._draft_base.get(item)
        if item in self._page_defaults:
            return "Default · not saved"
        if self._rows[item] != self._row(item, base):
            return "Edited · not saved"
        return "Saved" if base else "Not reviewed"

    def _can_capture_item(self):
        review = self._review()
        return (not self._rendering and self.selected_item_id in self._rows and not review.get("audition_only")
                and self.selected_item_id not in review.get("item_locks", {}))

    def _capture_editor(self, *, explicit=False):
        if not self._can_capture_item():
            return
        if not explicit and self._editor_values() == getattr(self, "_rendered_editor_values", None):
            return
        row = self._editor_row(self.selected_item_id)
        self._rows[self.selected_item_id] = row
        self._explicit_items.add(self.selected_item_id)
        self._page_defaults.pop(self.selected_item_id, None)
        self._pending_submission = None
        row.update(disposition=self.disposition.currentData() or "", reason=self.reason.toPlainText(),
                   boundary_flag=self.boundary.isChecked())
        if self._directed_editor():
            row["form_status"] = self.form_status.currentData() or "not_assessed"
        else:
            row["reason_code"] = (self.reason_code.currentData() or "") if row["disposition"] in {"pool", "discard"} else ""
        if row["disposition"] == "current":
            row["family_id"] = self.current_family.currentData() or ""
        elif row["disposition"] == "other":
            row["family_id"] = self.target_family.currentData() or ""
        elif row["disposition"] == "new_family":
            if not row.get("creation_record_id"):
                key = row.get("new_family_key") or ("new-" + uuid.uuid4().hex)
                row["new_family_key"] = key
                spec = self._new_families.setdefault(key, {"diagnostic_body": ""})
                spec.update(title=self.new_title.text().strip(), reason=self.new_reason.text().strip())
                row["family_id"] = ""
        else:
            row["family_id"] = ""
            if self._directed_editor():
                row["form_status"] = "not_assessed"
        self._rendered_editor_values = self._editor_values()

    def _editor_changed(self, *_args):
        if not self._can_capture_item():
            return
        self._capture_editor(explicit=True)
        self._dirty = True
        self._pending_submission = None
        self.confirm.setChecked(False)
        self.item_status.setText(self._item_save_status(self.selected_item_id))
        self._update_slot_labels()
        self._set_enabled()
        self._guard(self._save_drafts)

    def _disposition_changed(self):
        if not self._can_capture_item():
            return
        row = self._rows[self.selected_item_id]
        value = self.disposition.currentData() or ""
        if value != row["disposition"]:
            row.update(disposition=value, family_id="", new_family_key="", creation_record_id="", reason_code="")
            referenced = {x.get("new_family_key") for x in self._rows.values()
                          if x.get("disposition") == "new_family"}
            self._new_families = {k: v for k, v in self._new_families.items() if k in referenced}
        self._editor_changed()
        self._render_editor()

    def _current_family_changed(self):
        if self._rendering:
            return
        self._pending_submission = None
        self._refresh_page_defaults()
        self._render_editor()
        self._update_slot_labels()

    def _proposal_changed(self):
        if not self._can_capture_item():
            return
        key = self.new_family_draft.currentData() or ""
        row = self._rows[self.selected_item_id]
        row.update(new_family_key=key, family_id="", creation_record_id="")
        saved = next((x for x in self._review().get("decisions", {}).values()
                      if x.get("new_family_key") == key and x.get("creation_record_id")), None)
        if saved:
            row.update(family_id=saved["family_id"], creation_record_id=saved["creation_record_id"])
        self._render_editor()
        self._editor_changed()

    def _update_slot_labels(self):
        labels = dict(DISPOSITIONS)
        review = self._review()
        listening = review.get("audition_only")
        for i in range(self.slot_list.count()):
            item = self.slot_list.item(i)
            ident = item.data(Qt.ItemDataRole.UserRole)
            if listening:
                label = "Pool member" if review["stage"] == "pool" else "Family member"
                item.setText(f"{i+1} · {ident}\n{label}")
                item.setToolTip(f"{ident}\n{review.get('family_id') or 'Global pool'}")
                continue
            row = self._editor_row(ident)
            saved = self._row(ident, self._draft_base.get(ident))
            mark = " *" if ident in self._page_defaults or self._rows[ident] != saved else ""
            label = labels.get(row.get("disposition"), "Not reviewed")
            if row.get("disposition") == "other" and row.get("family_id"):
                label = "Other · " + row["family_id"][:12]
            lock = review.get("item_locks", {}).get(ident)
            if lock:
                if not row.get("disposition"):
                    label = "In another review" if lock["code"] == "BATCH_ITEM_OVERLAP" else "Updated elsewhere"
                item.setText(f"{i+1} · {ident}\n{label}{mark}")
                item.setToolTip(f"{ident}\n{lock['message']}")
                continue
            item.setText(f"{i+1} · {ident}\n{label}{mark}")
            pending = "Current family (pending Save page)\n" if ident in self._page_defaults else ""
            item.setToolTip(f"{ident}\n{pending}{row.get('family_id') or ''}\n{row.get('reason') or ''}")

    def _save_drafts(self):
        if self._fine_review_dialog is not None and self._fine_review_dialog.project is self.project:
            self._fine_review_dialog.save_draft()
        for dialog in (self._pool_review_dialog, self._final_review_dialog, self._catalogue_review_dialog,
                       self._source_registry_dialog, self._assessment_dialog, self._catalogue_export_dialog):
            if dialog is not None and dialog.project is self.project and hasattr(dialog, "save_draft"):
                dialog.save_draft()
        if not self.project or not self._page_key or not self._dirty:
            return
        self._draft_store[self._page_key] = {"input_fingerprint": self.state["input_fingerprint"],
            "review_id": self.selected_review_id, "page_id": self.selected_page_id,
            "item_ids": list(self._page["item_ids"]), "base": copy.deepcopy(self._draft_base),
            "items": copy.deepcopy(self._rows), "new_families": copy.deepcopy(self._new_families),
            "current_family_id": self.current_family.currentData() or "",
            "explicit_item_ids": sorted(self._explicit_items)}
        self._write_drafts()

    def _restore_page_inputs(self):
        """Overlay a matching sidecar on the freshly loaded page, before projecting defaults."""
        stored = self._draft_store.get(self._page_key)
        review = self._review()
        if stored is None or review.get("status") != "ready" or any(review.get(key) for key in
                ("closed", "scope_stale", "archived", "read_only", "superseded_by_protocol_upgrade")):
            return
        try:
            if (stored.get("review_id") != self.selected_review_id or stored.get("page_id") != self.selected_page_id
                    or stored.get("input_fingerprint") != self.state["input_fingerprint"]
                    or stored.get("item_ids") != self._page["item_ids"] or stored.get("base") != self._draft_base):
                raise ValueError("The draft no longer matches this page or its saved outcomes.")
            items = stored.get("items")
            if not isinstance(items, dict) or items.keys() != self._rows.keys():
                raise ValueError("The draft syllables do not match this page.")
            marked = stored.get("explicit_item_ids", [])
            if not isinstance(marked, list) or not all(isinstance(item, str) and item in items for item in marked):
                raise ValueError("The draft contains an invalid edited syllable.")
            explicit = set(marked)
            rows = {}
            outcomes = {"", *dict(DISPOSITIONS)}
            for item, base_row in self._rows.items():
                fields = items[item]
                if not isinstance(fields, dict) or fields.get("item_id", item) != item:
                    raise ValueError("The draft contains an invalid syllable record.")
                row = {**base_row, **fields}
                if (any(not isinstance(row[key], str) for key in base_row if key != "boundary_flag")
                        or not isinstance(row.get("pending_steps", ""), str)
                        or not isinstance(row["boundary_flag"], bool)
                        or row["disposition"] not in outcomes):
                    raise ValueError("The draft contains invalid annotation fields.")
                rows[item] = row
                # Older v1 drafts stored blank placeholders for every syllable.
                if row != base_row:
                    explicit.add(item)
            proposals = stored.get("new_families", {})
            if not isinstance(proposals, dict) or not all(isinstance(spec, dict) and
                    isinstance(spec.get("title"), str) and isinstance(spec.get("reason", ""), str) and
                    isinstance(spec.get("diagnostic_body", ""), str) for spec in proposals.values()):
                raise ValueError("The draft contains an invalid family proposal.")
            family = stored.get("current_family_id", "")
            if not isinstance(family, str):
                raise ValueError("The draft contains an invalid current family.")
        except ValueError as exc:
            # Keep the original sidecar recoverable before any later replacement.
            if not self._draft_source_issue:
                self._draft_source_issue = {"sha256": None, "backup": None}
            self._show_draft_warning(f"Draft skipped for {self.selected_page_id}. {system_error(exc)}")
            return
        self._rows = rows
        self._explicit_items = explicit
        self._new_families = copy.deepcopy(proposals)
        index = self.current_family.findData(family)
        if family and index >= 0 and review.get("stage") != "family" and not self._directed_editor():
            blocked = self.current_family.blockSignals(True)
            try:
                self.current_family.setCurrentIndex(index)
            finally:
                self.current_family.blockSignals(blocked)
        self._recompute_dirty()

    def save_selected(self, advance=False):
        if self.selected_item_id not in self._rows:
            raise ValueError("Select exactly one syllable before saving an item.")
        self._capture_editor()
        self._save_items([self.selected_item_id], advance=advance)

    def save_page_decisions(self):
        if self._review().get("audition_only"):
            raise ValueError("This page is for listening only.")
        self._capture_editor()
        if self._directed_editor():
            raise ValueError("Directed review keeps its explicit per-item confirmation; Save page is unavailable.")
        pending = self._pending_submission
        if pending and pending.get("whole_page") and pending["page_key"] == self._page_key:
            return self._run_item_submission(pending)
        self._refresh_page_defaults()
        items = self._page_submission_ids()
        if not items:
            raise ValueError("No pending page defaults or changed outcomes to save. Not reviewed items remain unsubmitted.")
        self._save_items(items, whole_page=True)

    def _submission_row(self, item, directed, current):
        row = self._editor_row(item)
        if not row["disposition"]:
            raise ValueError(item + ": choose an outcome before saving this syllable.")
        if directed and not row["reason"].strip():
            raise ValueError(item + ": record actual evidence or uncertainty for this directed review.")
        if row["disposition"] in {"current", "other"}:
            family = row.get("family_id")
            if not family:
                raise ValueError(item + ": choose Current human family above or an explicit target family before saving.")
            if directed:
                if row["disposition"] == "current" and family != current:
                    raise ValueError(item + ": Current family must match this directed review's source family.")
                if row["disposition"] == "other" and family == current:
                    raise ValueError(item + ": choose a different existing family explicitly.")
            else:
                # A context change changes the relative word, never the chosen ID.
                row["disposition"] = "current" if family == current else "other"
        definitions = {}
        if row["disposition"] == "new_family":
            if not row.get("new_family_key"):
                raise ValueError(item + ": name the shared family proposal and give its creation basis.")
            if not row.get("creation_record_id"):
                spec = self._new_families.get(row["new_family_key"], {})
                if not spec.get("title") or not spec.get("reason"):
                    raise ValueError(item + ": name the shared family proposal and give its creation basis.")
                definitions[row["new_family_key"]] = copy.deepcopy(spec)
                row.pop("family_id", None)
                row.pop("creation_record_id", None)
        else:
            row.pop("new_family_key", None)
            row.pop("creation_record_id", None)
        if row["disposition"] in {"pool", "discard"}:
            row.pop("family_id", None)
            if directed:
                row["form_status"] = "not_assessed"
            else:
                options = self._submission_capabilities().get("reason_codes", {}).get(row["disposition"], [])
                if row.get("reason_code") not in {x.get("code") for x in options if isinstance(x, dict)}:
                    raise ValueError(item + ": choose a reason category for " + dict(DISPOSITIONS)[row["disposition"]] + ". Notes are optional.")
        if directed or row["disposition"] not in {"pool", "discard"}:
            row.pop("reason_code", None)
        return row, definitions

    def _save_items(self, items, *, advance=False, whole_page=False):
        if self._review().get("audition_only"):
            raise ValueError("This page is for listening only.")
        self._guard_draft_overwrite()
        if not self._page or not items or any(item not in self._rows for item in items):
            raise ValueError("Choose a syllable on the current page first.")
        directed = self._directed_editor()
        if directed and (whole_page or not self.confirm.isChecked()):
            raise ValueError("Confirm that you actually listened to / inspected this syllable.")
        pending = self._pending_submission
        if (pending and pending["page_key"] == self._page_key and pending.get("item_ids") == items
                and pending.get("whole_page", False) == whole_page):
            return self._run_item_submission(pending)
        review = self._review()
        latest = self._sample_project_state(require_fresh=True)
        latest_review = latest.get("batch_review", {}).get("reviews", {}).get(self.selected_review_id, {})
        for item in items:
            if lock := latest_review.get("item_locks", {}).get(item):
                raise ValueError(f"{item}: {lock['message']}")
        if any(review.get(key) for key in ("closed", "scope_stale", "archived", "read_only")):
            raise ValueError("This review is not open for new decisions.")
        current = self.current_family.currentData() or ""
        payload = {"review_id": self.selected_review_id, "page_id": self.selected_page_id,
                   "current_family_id": current or None, "items": []}
        definitions = {}
        for item in items:
            row, specs = self._submission_row(item, directed, current)
            payload["items"].append(row)
            definitions.update(specs)
        if definitions:
            payload["new_families"] = definitions
        if self._draft_base != self._saved_page(latest):
            raise ValueError("Another operation changed this page. The draft is retained; reopen and compare saved records.")
        action = "batch_submit"
        if not directed:
            capabilities = latest.get("batch_review", {}).get("submission_capabilities", {})
            if ("batch_submit_v2" not in capabilities.get("available_actions", [])
                    or capabilities.get("compact_action") != "batch_submit_v2"
                    or latest_review.get("compact_submit_allowed") is not True):
                raise ValueError("Compact submission is unavailable for this current review. Refresh its scope and resolve its status before saving.")
            fresh_defaults = self._default_candidates(latest)
            if any(item in self._page_defaults and fresh_defaults.get(item) != self._page_defaults[item] for item in items):
                raise ValueError("A pending default acquired another assignment or review. Refresh and inspect it before saving.")
            action = capabilities["compact_action"]
        pending = {"page_key": self._page_key, "signature": _hash({"action": action, "payload": payload}),
            "revision": latest["revision"], "key": "desktop-save-" + uuid.uuid4().hex,
            "action": action, "payload": copy.deepcopy(payload), "item_ids": list(items),
            "whole_page": whole_page, "advance": advance, "retained": copy.deepcopy(self._rows)}
        self._pending_submission = pending
        self._run_item_submission(pending)

    def _run_item_submission(self, pending):
        project = self.project
        def run(_progress, _cancelled):
            return project.execute(pending["action"], copy.deepcopy(pending["payload"]),
                expected_revision=pending["revision"], idempotency_key=pending["key"])
        def done(_result):
            self.state = project.snapshot()
            saved = self._saved_page()
            for item in pending["item_ids"]:
                if self._rows.get(item) == pending["retained"].get(item):
                    self._rows[item] = self._row(item, saved[item])
                    self._explicit_items.discard(item)
                self._page_defaults.pop(item, None)
                created = self._row(item, saved[item])
                if created.get("creation_record_id"):
                    for draft in self._rows.values():
                        if draft["disposition"] == "new_family" and draft.get("new_family_key") == created.get("new_family_key"):
                            draft.update(family_id=created["family_id"], creation_record_id=created["creation_record_id"])
                    self._new_families.pop(created["new_family_key"], None)
            self._draft_base = saved
            if self._pending_submission is pending:
                self._pending_submission = None
            self._recompute_dirty()
            self._draft_store.pop(pending["page_key"], None)
            if self._dirty:
                self._save_drafts()
            else:
                self._write_drafts()
            self.confirm.setChecked(False)
            self._refresh()
            self._render_editor()
            self._update_slot_labels()
            self._status(f"{len(pending['item_ids'])} page outcomes saved. Other unsent drafts are retained." if pending["whole_page"] else
                         "Selected item saved. Other unsent drafts are retained.")
            if pending["advance"]:
                self._step_item(1, allow_busy=True)
        self._run("Save page outcomes" if pending["whole_page"] else "Save selected syllable", run, done)

    def _step_item(self, amount, allow_busy=False):
        if not self._page or self._busy and not allow_busy:
            return
        ids = self._page["item_ids"]
        if not ids:
            return
        selected = [ids.index(item) for item in self.selected_item_ids if item in ids]
        next_index = ((max(selected) + amount if amount > 0 else min(selected) + amount)
                      if selected else 0 if amount > 0 else len(ids) - 1)
        if not 0 <= next_index < len(ids):
            self._status("End of this page. Review the page list before moving to another page.")
            return
        was_busy = self._busy
        if allow_busy:
            self._busy = False
        try:
            self.select_item(ids[next_index])
        finally:
            self._busy = was_busy

    def _family_dialog(self, *, reveal=False):
        dialog = QDialog(self)
        dialog.setWindowTitle("Create an evidenced human family")
        layout = QFormLayout(dialog)
        title, body, reason = QLineEdit(), QPlainTextEdit(), QLineEdit()
        title.setObjectName("familyTitle")
        body.setObjectName("familyBody")
        reason.setObjectName("familyReason")
        layout.addRow("Name", title)
        layout.addRow("Diagnostic evidence", body)
        layout.addRow("Creation reason", reason)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addRow(buttons)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.create_family(title.text(), body.toPlainText(), reason.text(), reveal=reveal)

    def create_family(self, title, body, reason, *, reveal=False):
        if not all(x.strip() for x in (title, body, reason)):
            raise ValueError("Provide the family name, actual diagnostic evidence and creation reason.")
        def created(result):
            record = result.get("record", result)
            self._refresh_families()
            if self._review().get("stage") != "family":
                self.current_family.setCurrentIndex(max(0, self.current_family.findData(record.get("family_id"))))
            if reveal:
                for row in self._tree_items():
                    if row.data(0, Qt.ItemDataRole.UserRole).get("kind") == "families_root":
                        row.setExpanded(True)
                        break
            self._status("Human family created as a draft definition. Assign members using actual evidence.")
        self._execute("batch_family", {"title": title.strip(), "diagnostic_body": body.strip(), "reason": reason.strip()}, success=created)

    def _queue_family_rename(self, family_id, previous_title, text):
        title = text.strip()
        if title == previous_title:
            return
        project = self.project
        generation = self._close_coordinator.generation

        def submit():
            if self.project is not project or not self._close_coordinator.accepts_callback(generation):
                return
            if not self.group_list.family_actions_enabled:
                raise ValueError("Wait for the project to become editable before renaming a family.")
            if not title:
                raise ValueError("Enter a family name.")
            self._execute("family_rename", {"family_id": family_id, "expected_title": previous_title, "title": title},
                          success=lambda _result: self._status("Family renamed."))

        # Let Qt finish closing the inline editor before disabling the tree or rebuilding rows.
        QTimer.singleShot(0, self, lambda: self._guard(submit, operation="rename human family"))

    def _update_playback_buttons(self, playing):
        page_playing = playing and not self.audio.looping
        loop_playing = playing and self.audio.looping
        self.play_page.setText("Pause" if page_playing else "Play")
        self.loop_item.setText("Pause" if loop_playing else "Loop")
        scope = ("the whole page" if not self.selected_item_ids else "the selected syllable"
                 if len(self.selected_item_ids) == 1 else "the selected syllables")
        self.play_page.setToolTip("Pause playback" if page_playing else
            f"Resume playback of {scope}" if self.audio.paused and not self.audio.looping else f"Play {scope}")
        self.loop_item.setToolTip("Pause the loop" if loop_playing else
            f"Resume the loop of {scope}" if self.audio.paused and self.audio.looping else f"Loop {scope}")

    def play_current_page(self):
        self._toggle_playback(loop=False)

    def loop_current_item(self):
        self._toggle_playback(loop=True)

    def _toggle_playback(self, *, loop):
        if (not self.project or not self._page or self._busy or self._sampling_pending
                or self._close_coordinator.state != "open"):
            return
        if self._review_media_dialog is not None:
            self._review_media_dialog.stop()
        if self._interval_import_dialog is not None:
            self._interval_import_dialog.stop()
        scope = (self._page_key, self.selected_item_ids)
        if (self.audio.sink is not None and self.audio.looping == loop
                and getattr(self, "_playback_scope", None) == scope):
            if self.audio.paused:
                self.audio.resume()
            else:
                self.audio.pause()
        else:
            slots = [slot for slot in self._page["slots"] if slot["item_id"] in self.selected_item_ids]
            if not slots:
                started = self.audio.play(loop=loop)
            elif len(slots) == 1:
                started = self.audio.play(slots[0]["start_frame"], slots[0]["stop_frame"], loop=loop)
            else:
                original = self._page["slots"]
                gap = int(original[1]["start_frame"] - original[0]["stop_frame"])
                if gap < 0:
                    raise ValueError("Prepared page slots have an invalid original gap.")
                started = self.audio.play_ranges([(slot["start_frame"], slot["stop_frame"]) for slot in slots],
                                                 gap_frames=gap, loop=loop)
            self._playback_scope = scope if started else None

    def _export_dialog(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export saved records", "syllafolio-records.json", "JSON (*.json)")
        if not path:
            return
        def completed(result):
            shutil.copy2(self.project.media_path(result["download_url"]), path)
            self._status("Saved records exported. This does not declare the catalogue complete.")
        self._execute("export", {}, success=completed)

    def _finish_dialog(self):
        review = self._review()
        if not review or review.get("status") != "ready" or review.get("closed") or review.get("read_only"):
            raise ValueError("Choose an open, ready review scope first.")
        if self._dirty:
            raise ValueError("Save or explicitly set aside the remaining page drafts before finishing the scope.")
        project, review_id = self.project, self.selected_review_id
        revision = self._sample_project_state(require_fresh=True)["revision"]
        draft = self._closure_drafts.get(review_id, {})
        dialog = QDialog(self)
        self._closure_dialog = dialog
        dialog.setWindowTitle("Finish this review scope")
        available = self.screen().availableGeometry()
        dialog.resize(min(800, available.width() - 32), min(740, available.height() - 64))
        layout = QVBoxLayout(dialog)
        scope = QLabel(f"Frozen review {review_id} · {len(review['item_ids'])} targets · "
                       f"{len(review.get('decisions', {}))} saved outcomes. Saving is separate from closing this scope.")
        scope.setWordWrap(True); layout.addWidget(scope)
        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        body = QWidget(); fields = QVBoxLayout(body)
        scroll.setWidget(body); layout.addWidget(scroll, 1)
        checks = {}
        for key, text in [("coverage_confirmed", "All targets, subclusters, unclustered items and tail pages were covered."),
                          ("comparisons_completed", "The required comparisons were completed."),
                          ("affected_relations_checked", "Affected family relationships were checked."),
                          ("no_required_steps_pending", "No required step remains pending for this scope.")]:
            check = QCheckBox(text)
            check.setObjectName("close_" + key)
            checks[key] = check
            fields.addWidget(check)
        note = QPlainTextEdit()
        note.setObjectName("closureNote")
        note.setMaximumHeight(100)
        note.setPlaceholderText("Actual conclusion and remaining uncertainty")
        note.setPlainText(draft.get("note", ""))
        fields.addWidget(note)
        summary = {}
        if review.get("directed_issue_id"):
            context = review.get("directed_context", {})
            directed = QLabel("Return to final checks after this exact directed scope closes. "
                f"Question: {context.get('question', '')}\nRequired fine targets: " +
                ", ".join(context.get("fine_required_ids", [])))
            directed.setWordWrap(True); fields.addWidget(directed)
            form = QFormLayout()
            for key, label in (("answer", "Actual answer"), ("comparison_note", "Completed comparisons"),
                ("affected_relations_note", "Affected relationships checked"),
                ("unresolved_level", "Remaining unresolved level"), ("missing_information", "Missing information")):
                edit = QPlainTextEdit(); edit.setMaximumHeight(75); edit.setObjectName("directedClose_" + key)
                edit.setPlainText(draft.get("directed_summary", {}).get(key, ""))
                form.addRow(label, edit); summary[key] = edit
            fields.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        retained = {"note": note.toPlainText(), "directed_summary": {key: edit.toPlainText() for key, edit in summary.items()}}
        self._closure_drafts[review_id] = retained
        if not accepted:
            return
        if not all(x.isChecked() for x in checks.values()) or not note.toPlainText().strip():
            raise ValueError("Confirm each check and provide the actual scope conclusion.")
        payload = {"review_id": review_id, "checks": {k: v.isChecked() for k, v in checks.items()}, "note": note.toPlainText().strip()}
        if summary:
            payload["directed_summary"] = {key: edit.toPlainText().strip() for key, edit in summary.items()}
        signature = _hash(payload)
        pending = self._closure_requests.get(review_id)
        current_revision = self._sample_project_state(require_fresh=True)["revision"]
        if pending and pending["signature"] == signature:
            recorded = project.service.store.lookup_event(project.service.run_id, pending["key"])
            if recorded is None and current_revision != pending["revision"]:
                # A rejected closure wrote nothing. The user has now reopened
                # and explicitly reconfirmed this form against a newer scope.
                pending = None
        if pending is None or pending["signature"] != signature:
            if current_revision != revision:
                raise ValueError("The project changed while this closure form was open. Reopen and confirm its current scope.")
            pending = {"signature": signature, "revision": revision, "key": "desktop-close-" + uuid.uuid4().hex}
            self._closure_requests[review_id] = pending
        def completed(result):
            self._closure_requests.pop(review_id, None)
            self._closure_drafts.pop(review_id, None)
            self._refresh()
            closed = self.state["batch_review"]["reviews"].get(review_id, {})
            pool = self.state["batch_review"].get("pool", {})
            if closed.get("directed_issue_id") or (closed.get("stage") == "pool" and pool.get("stage_complete")
                    and pool.get("first_pass_complete") and not pool.get("pending_family_review_ids")):
                self._after_operation = (self._close_coordinator.generation, self._show_final_review)
            elif closed.get("stage") == "pool":
                self._status("Pool round finished. Check remaining reviews in the sidebar.")
            elif pool.get("first_pass_complete"):
                self._after_operation = (self._close_coordinator.generation, self._show_pool_review)
        self._run("Close explicit review scope", lambda _progress, _cancelled:
            project.execute("batch_close", payload, expected_revision=pending["revision"], idempotency_key=pending["key"]), completed)

    def _has_active_review(self):
        # Historical name retained for the P5 validator; all task types now participate.
        latest = self._sample_project_state()
        return bool(blocking_jobs(self._current_job_view(latest)))

    def _sample_project_state(self, *, require_fresh=False):
        """Nonwaiting GUI sample; a cached packet can display but never close."""
        if self.project is None:
            self._sampling_pending = False
            return {}
        latest = self.project.try_snapshot()
        if latest is None:
            self._sampling_pending = True; self._pending_refresh = True
            if require_fresh:
                raise DesktopJobError("SNAPSHOT_PENDING", "Wait for the current owner operation to release its state sample; unsent input remains intact.")
            return {**(self.state or {}), "desktop_sampling": {"pending": True}}
        self._sampling_pending = False
        latest["desktop_sampling"] = {"pending": False}
        return latest

    def _ensure_cancel_requests(self):
        queue = self._cancel_requests
        generation = self._close_coordinator.generation
        if queue is not None and (queue.owner is not self.project or queue.generation != generation):
            queue.close(); queue = None
        if queue is None:
            queue = CancelRequests(self.project, generation=generation)
            self._cancel_requests = queue
        return queue

    def _report_cancel_results(self):
        if self._cancel_requests is None: return
        for row in self._cancel_requests.snapshot()["requests"]:
            if row["state"] not in {"applied", "rejected"} or not row["worker_settled"] or row["request_key"] in self._reported_cancellations:
                continue
            self._reported_cancellations.add(row["request_key"])
            context = {**self._diagnostic_context(), "cancellation_request": row}
            self.diagnostics.record_event("ERROR" if row["state"] == "rejected" else "INFO",
                "Original cancellation request rejected." if row["state"] == "rejected" else
                "Original cancellation request applied; task worker exit is checked separately.", "cancel task request", context)
            if row["state"] == "rejected":
                error = row["error"] or {}
                self._status(str(error.get("code", "CANCEL_REQUEST_FAILED")) + ": Cancellation request rejected. The original target, revision and request key are retained in Tasks and diagnostics.", error=True)

    def _current_job_view(self, latest=None, *, include_foreground=True):
        state = latest if latest is not None else (self.state or {})
        pending = bool(state.get("desktop_sampling", {}).get("pending"))
        if latest is None: pending = pending or self._sampling_pending
        if self.project is not None and not isinstance(state.get("desktop_jobs"), dict): pending = True
        return sampled_job_view(state,
            project_id=self.project.metadata["project_id"] if self.project else None,
            run_id=self.project.service.run_id if self.project else None,
            foreground=self._foreground if include_foreground else None, sampling_pending=pending,
            cancellation_requests=self._cancel_requests.snapshot() if self._cancel_requests is not None else None)

    def _show_tasks(self):
        self.task_dialog.show()
        view = self._refresh_job_view(self._sample_project_state())
        self._task_issue_seen.update(self._task_issue_keys(view))
        self._update_task_entry(view)
        self.task_dialog.raise_()

    def _refresh_job_view(self, latest=None):
        view = self._current_job_view(latest)
        self._job_view = view
        signature = _hash(view)
        if signature != self._job_signature:
            self._job_signature = signature
            selected = self.job_list.currentItem()
            selected_id = selected.data(0, Qt.ItemDataRole.UserRole) if selected else None
            self.job_list.clear()
            for job in view["jobs"]:
                progress = job.get("progress")
                detail = json.dumps(system_fields(progress), ensure_ascii=False) if progress else ""
                if job.get("error"):
                    error = job["error"]
                    detail = (str(error.get("code", "")) + ": " + system_text(str(error.get("message", "")))
                              if isinstance(error, dict) else system_error(error))
                if job.get("kind") == "batch" and job.get("raw_status") == "ready":
                    detail = "Review materials ready; " + ("human review closed" if job.get("closed") else "human review still open")
                if job.get("raw_status") == "not_estimable":
                    detail = "Complete support report; retain its coverage and limitations."
                if job.get("stale_for_current"):
                    detail += (" · Frozen task scope differs from current state" if job.get("is_active")
                               else " · Result stale for current state")
                item = QTreeWidgetItem([f"{job.get('label') or job['kind']} · {job['job_id']}", job["phase"], detail])
                ident = (job.get("run_id"), job["kind"], job["job_id"])
                item.setData(0, Qt.ItemDataRole.UserRole, ident)
                item.setToolTip(2, detail)
                self.job_list.addTopLevelItem(item)
                if ident == selected_id:
                    self.job_list.setCurrentItem(item)
            self.job_list.resizeColumnToContents(0)
            self.job_list.resizeColumnToContents(1)
            row_height = max(self.job_list.sizeHintForRow(0), self.job_list.fontMetrics().height() + 4)
            list_height = self.job_list.header().sizeHint().height() + row_height * min(len(view["jobs"]), 3) + 4
            self.job_list.setFixedHeight(min(105, max(36, list_height)))
        active = view["blocking_jobs"]
        self.job_summary.setText(("Closing / switching: " if self._transition else "") +
            f"{len(active)} task(s) waiting for a terminal state and worker exit.")
        self._update_task_entry(view)
        self.job_box.setVisible(bool(view["jobs"]) or bool(self._transition) or self.task_dialog.isVisible())
        if self._transition:
            self.task_dialog.show()
        self.return_to_workspace.setVisible(bool(self._transition))
        self.cancel_selected_job.setEnabled(any(job.get("is_active") and job.get("kind") in
            {"local", "batch", "assessment", "catalogue_export", "foreground", "import"} for job in view["jobs"]))
        return view

    def _cancel_job(self, row):
        latest = self._sample_project_state()
        receipt = self._ensure_cancel_requests().request(row, expected_revision=latest.get("revision", 0),
            idempotency_key="desktop-cancel-" + uuid.uuid4().hex,
            reason="Explicit cancellation from the native task list.", foreground=self._foreground,
            generation=self._close_coordinator.generation)
        self.diagnostics.record_event("INFO", "Cancellation intent retained with its original scope/key; execution and worker termination remain separate.",
                                      "cancel task", {**self._diagnostic_context(), "receipt": receipt})
        self._report_cancel_results()
        self._refresh_job_view(latest)
        if receipt["request_state"] != "rejected":
            self._status(("Cancellation queued for " if receipt["request_state"] in {"queued", "running"} else "Cancellation requested for ")
                + row["kind"] + " " + row["job_id"] + "; waiting for the original request and task worker to settle.")
        return receipt

    def cancel_operation(self):
        latest = self._sample_project_state()
        view = self._refresh_job_view(latest)
        current = self.job_list.currentItem()
        selected = current.data(0, Qt.ItemDataRole.UserRole) if current else None
        choices = [job for job in view["jobs"] if job.get("is_active") and job.get("kind") in
                   {"local", "batch", "assessment", "catalogue_export", "foreground", "import"}]
        row = next((job for job in choices if (job.get("run_id"), job["kind"], job["job_id"]) == selected), None)
        if row is None and selected is None and len(choices) == 1:
            row = choices[0]
        if row is None:
            raise ValueError("Select the exact active task to cancel.")
        self._cancel_job(row)

    def _choose_drain_action(self, jobs):
        box = QMessageBox(self)
        box.setWindowTitle("Tasks are still active")
        box.setText("Wait for all listed tasks before closing or switching projects.")
        box.setInformativeText("\n".join(f"{job['kind']} · {job['job_id']} · {job['phase']}" for job in jobs))
        wait = box.addButton("Wait and continue", QMessageBox.ButtonRole.AcceptRole)
        cancel = box.addButton("Cancel listed tasks and continue", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton("Return to workspace", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        return "wait" if box.clickedButton() is wait else "cancel" if box.clickedButton() is cancel else "return"

    def _request_transition(self, action=None, *, closing_window=False, mode=None):
        if self._transition:
            raise ValueError("A close or project switch is already pending.")
        if not closing_window and self.project is None and not self._busy:
            return action()
        latest = self._sample_project_state()
        view = self._refresh_job_view(latest)
        choice = mode or (self._choose_drain_action(view["blocking_jobs"]) if view["blocking_jobs"] else "wait")
        if choice == "return":
            return False
        if choice not in {"wait", "cancel"}:
            raise ValueError("Choose wait, cancel or return.")
        self._stop_review_audio()
        if self.project:
            self.project.service.begin_drain()
        self._close_coordinator.begin(view)
        self._transition = {"action": action, "closing_window": closing_window}
        self._set_enabled()
        self._refresh_job_view(latest)
        if choice == "cancel":
            for job in view["blocking_jobs"]:
                if job.get("is_active") and job.get("kind") in {"local", "batch", "assessment", "catalogue_export", "foreground", "import"}:
                    try:
                        self._cancel_job(job)
                    except Exception as exc:
                        # A different task may finish or change revision between requests.
                        # Keep the close pending and the exact failure visible; do not alter keys or tokens.
                        self._error(exc, operation="cancel task before close")
        self._advance_transition()
        return True

    def request_close(self, mode="wait"):
        """Nonblocking explicit wait/cancel entry shared by native controls and regression tests."""
        return self._request_transition(closing_window=True, mode=mode)

    def abort_transition(self):
        if not self._transition:
            return
        if self.project:
            self.project.service.end_drain()
        self._close_coordinator.abort()
        self._transition = None
        self._cancel_pending_interval_import()
        self._retire_interval_import("The pending close or switch was cancelled. Open a fresh recording preview.")
        if self._pool_review_dialog is not None:
            # Keep same-owner unsubmitted reason text, but renew any exception
            # choice and scope inspection when C is explicitly opened again.
            self._pool_review_dialog.exception.setChecked(False)
            self._pool_review_dialog.stop()
        for dialog in (self._final_review_dialog, self._catalogue_review_dialog,
                       self._source_registry_dialog, self._assessment_dialog, self._catalogue_export_dialog):
            if dialog is not None:
                dialog._owner_transition_invalidated = True
                if hasattr(dialog, "invalidate"):
                    dialog.invalidate("Returned from a pending close/switch. Reopen this workspace and explicitly restore unsubmitted input; confirmations must be renewed.")
        if self._fine_review_dialog is not None:
            self._fine_review_dialog.invalidate("Returned from a pending close/switch. Reopen Fine and inspect any restored unsubmitted input again.")
        self._set_enabled()
        self._refresh_job_view()
        self._status("Returned to the workspace. Previously requested cancellations remain in effect.")

    def _advance_transition(self):
        if not self._transition:
            return
        latest = self._sample_project_state()
        view = self._refresh_job_view(latest)
        if view["sampling_pending"] or not self._close_coordinator.ready(view):
            return
        pending = self._transition
        try:
            self._save_drafts()
            self._retire_interval_import("Project closed or switched.")
            self._retire_review_views("Project closed or switched.")
            self._retire_workflow_views("Project closed or switched.")
            self._close_coordinator.start_closing(view)
            if self._stage_a_dialog is not None:
                self._stage_a_dialog.controller.invalidate("Project closed or switched.")
                self._stage_a_dialog.close()
            self._discard_audition_preview()
            if getattr(self, "_display_dialog", None) is not None:
                self._display_dialog.close()
            self.audio.stop()
            if self._cancel_requests is not None:
                self._report_cancel_results(); self._cancel_requests.close(); self._cancel_requests = None
            if self.project:
                self.project.close()
                self.project = None
            self.state = None
            self._foreground = None
            self._sampling_pending = False
            self._close_coordinator.finish()
            self._transition = None
            if pending["closing_window"]:
                self.task_dialog.hide()
                self.settings.setValue("geometry", self.saveGeometry())
                self.diagnostics.record_event("INFO", "Native desktop window closed after all workers settled.", "close window")
                self.diagnostics.close()
                self._poll_timer.stop()
                self._closing_approved = True
                QTimer.singleShot(0, self.close)
            else:
                self._close_coordinator.reopen()
                self._job_signature = None
                pending["action"]()
        except Exception as exc:
            if self._close_coordinator.state in {"draining", "closing"}:
                self.abort_transition()
            self._error(exc, operation="close project and save drafts")

    def _poll(self):
        try:
            latest = self._sample_project_state()
            pending = latest.get("desktop_sampling", {}).get("pending", False)
            changed = self.project is not None and not pending and (self.state is None or latest["revision"] != self.state["revision"])
            self._pending_refresh = self._pending_refresh or changed
            if self.project and not pending:
                self.state = latest
            view = self._refresh_job_view(latest)
            self._report_cancel_results()
            self._record_failed_reviews()
            if self._transition:
                self._advance_transition()
                return
            if self._pending_refresh and not self._busy and not pending:
                self._refresh()
            if pending: self._set_enabled()
            # Background progress/cancellation live in the task window. Keep the
            # foreground row from reducing the fixed editor on a small screen.
            self.cancel_button.setVisible(self._busy)
            if not self._busy:
                self.progress.hide()
        except Exception as exc:
            self._error(exc)

    def closeEvent(self, event):
        if self._closing_approved:
            self._disconnect_workspace_focus()
            event.accept()
            return
        event.ignore()
        if not self._transition:
            self._guard(lambda: self._request_transition(closing_window=True), operation="request close")
        if self._closing_approved:
            self._disconnect_workspace_focus()
            event.accept()


DesktopWindow = MainWindow


def main(argv=None):
    parser = argparse.ArgumentParser(description="Syllafolio native desktop technical prototype")
    parser.add_argument("--project", type=Path, help="Open an existing desktop project")
    parser.add_argument("--smoke-output", type=Path, help="Write a limited startup report and close without annotating")
    args = parser.parse_args(argv)
    QLocale.setDefault(QLocale("en_US"))
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_DontUseNativeDialogs, True)
    application = QApplication.instance() or QApplication(sys.argv[:1])
    application.setApplicationName("Syllafolio Desktop Prototype")
    window = MainWindow()
    window.show()
    if args.project:
        window.open_project_path(args.project)
    if args.smoke_output:
        def finish_smoke():
            if window._busy:
                QTimer.singleShot(100, finish_smoke)
                return
            args.smoke_output.parent.mkdir(parents=True, exist_ok=True)
            _write_json(args.smoke_output, {"schema": "syllafolio-desktop-startup-smoke-v1",
                "platform": sys.platform, "native_qt_window": True, "visible": window.isVisible(),
                "project_open": window.project is not None, "audio_output_devices": "not_tested",
                "human_annotation_submitted": False, "native_windows_process": sys.platform == "win32",
                "windows_acceptance": "not_performed_by_startup_smoke"})
            project_open = window.project is not None
            window.close()
            application.exit(0 if args.project is None or project_open else 1)
        QTimer.singleShot(150, finish_smoke)
    return application.exec()


if __name__ == "__main__":
    raise SystemExit(main())
