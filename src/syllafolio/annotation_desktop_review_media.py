"""原生B回源、只读参照与显式局部议题；显示和PCM不产生人工标签。"""
from __future__ import annotations
from .annotation_english import system_fields, system_error

import copy
import hashlib
import json
import math
import threading
import uuid
from pathlib import Path

from .annotation_batch_acoustics import DISPLAY_DEFAULTS, NORMALIZATIONS, validate_batch_display
from .annotation_spectrogram import DEFAULTS, VERSION, render
from .annotation_assistant_contracts import canonical_hash
from .annotation_service import atomic_json
from .annotation_page_layout import validate_page_layout

PREFERENCES = "desktop_review_media.json"
SCHEMA = "syllafolio-desktop-review-media-v1"
_PREF_LOCK = threading.RLock()
DEFAULT_REVIEW_PAGES = {"mode": "auto", "count": 10, "ms_per_pixel": None}


def _review_page_preferences(value):
    _require(isinstance(value, dict) and set(value) == set(DEFAULT_REVIEW_PAGES),
             "MEDIA_PREFERENCE_INVALID", "Review page preferences contain unsupported fields.")
    scale = value["ms_per_pixel"]
    layout = validate_page_layout(dict(value, ms_per_pixel=5.0 if scale is None else scale,
                                       plot_width_px=1, label_width_px=1))
    return {"mode": layout["mode"], "count": layout["count"],
            "ms_per_pixel": None if scale is None else layout["ms_per_pixel"]}


class ReviewMediaError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(f"{code}: {message}")


def _require(condition, code, message):
    if not condition:
        raise ReviewMediaError(code, message)


def _waveform_range(value):
    _require(type(value) in (int, float) and math.isfinite(value) and value >= 0,
             "DISPLAY_WAVEFORM_RANGE", "Choose Auto or a finite positive waveform amplitude range.")
    return float(value)


def _identity(project):
    return {"run_id": project.service.run_id, "input_fingerprint": project.dataset.input_fingerprint}


def _checked_path(project, path):
    root, path = project.project_dir.absolute(), Path(path).absolute()
    _require(path.is_relative_to(root), "MEDIA_SCOPE", "Media cache paths must stay inside the current project.")
    cursor = path
    while True:
        _require(not cursor.is_symlink() and not (hasattr(cursor, "is_junction") and cursor.is_junction()),
                 "MEDIA_SCOPE", "Media cache/preferences cannot follow links or junctions.")
        if cursor == root:
            break
        cursor = cursor.parent
    _require(path.resolve().is_relative_to(root.resolve()), "MEDIA_SCOPE", "Media cache path leaves the current project.")
    return path


def _preference_path(project):
    return _checked_path(project, project.project_dir / PREFERENCES)


def _preferences(project):
    path = _preference_path(project)
    if not path.exists():
        return {"schema_version": SCHEMA, **_identity(project), "reference_ids": []}
    def pairs(rows):
        result = {}
        for key, value in rows:
            _require(key not in result, "MEDIA_PREFERENCE_INVALID", "Duplicate preference key.")
            result[key] = value
        return result
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=pairs)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReviewMediaError("MEDIA_PREFERENCE_INVALID", "Media preferences cannot be read; the original is retained.") from exc
    _require(isinstance(value, dict) and value.get("schema_version") == SCHEMA
             and all(value.get(key) == expected for key, expected in _identity(project).items()),
             "MEDIA_PREFERENCE_IDENTITY", "Media preferences belong to a different input or run.")
    return value


def _save_preferences(project, updates):
    with _PREF_LOCK:
        value = _preferences(project)
        if all(value.get(key) == item for key, item in updates.items()):
            return copy.deepcopy(value)
        value.update(copy.deepcopy(updates))
        path = _preference_path(project)
        temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".writing")
        try:
            temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
        return copy.deepcopy(value)


def _ids(project, values, *, nonempty=False):
    _require(isinstance(values, (list, tuple)) and all(isinstance(value, str) and value for value in values)
             and len(values) == len(set(values)), "MEDIA_IDS", "Use distinct stable IDs from this project.")
    _require(not nonempty or bool(values), "MEDIA_IDS", "The declared target scope is empty.")
    _require(set(values) <= set(project.dataset.item_ids), "MEDIA_SCOPE", "An item belongs to another project or is unknown.")
    return list(values)


class ReferenceStore:
    """固定参照sidecar；不访问SQL、不改来源声明、人工决定或计时。"""
    def __init__(self, project):
        self.project = project

    def ids(self):
        with _PREF_LOCK:
            return _ids(self.project, _preferences(self.project).get("reference_ids", []))

    def set(self, ids, *, target_ids=()):
        values = _ids(self.project, ids)
        targets = _ids(self.project, target_ids)
        _require(not set(values) & set(targets), "REFERENCE_TARGET_OVERLAP", "References are read-only and must be outside the declared target scope.")
        _save_preferences(self.project, {"reference_ids": values})
        return values


class DisplayController:
    def __init__(self, settings=None, normalization="per_item"):
        self._lock = threading.RLock()
        self.settings = dict(DISPLAY_DEFAULTS)
        self.normalization = "per_item"
        self._waveform_range = 0.0
        self.review_pages = dict(DEFAULT_REVIEW_PAGES)
        self.recommended_time_scale = 5.0
        self._preference_baselines = {}
        self.set(settings or {}, normalization)
        self._explicit_initial = settings is not None or normalization != "per_item"

    def set(self, settings, normalization=None):
        validated = validate_batch_display(settings)
        normalization = self.normalization if normalization is None else normalization
        _require(isinstance(normalization, str) and normalization in NORMALIZATIONS,
                 "DISPLAY_NORMALIZATION", "Choose per-item or whole-page display normalization.")
        with self._lock:
            self.settings, self.normalization = validated, normalization
            return self.snapshot()

    def snapshot(self):
        with self._lock:
            return {"settings": dict(self.settings), "normalization": self.normalization,
                    "renderer": VERSION, "fft_samples": self.settings["window_samples"] * 2,
                    "frequency_max_hz": DEFAULTS["frequency_max_hz"]}

    @property
    def waveform_range(self):
        with self._lock:
            return self._waveform_range

    def set_waveform_range(self, project, value):
        value = _waveform_range(value)
        # Publish only after the project-bound sidecar has been atomically saved.
        # This preference never enters the STFT request or display-cache identity.
        with _PREF_LOCK:
            saved = _save_preferences(project, {"waveform_range": value})
            with self._lock:
                self._waveform_range = value
        return saved

    def load_preferences(self, project):
        key = (str(project.project_dir), project.service.run_id)
        value = self.snapshot()
        self._preference_baselines[key] = {name: value[name] for name in ("settings", "normalization")}
        with _PREF_LOCK:
            preferences = _preferences(project)
            display = preferences.get("display")
            waveform_range = _waveform_range(preferences.get("waveform_range", 0.0))
            page_preferences = _review_page_preferences(preferences.get("review_pages", DEFAULT_REVIEW_PAGES))
            durations = [row["duration_s"] for row in project.dataset.public_items()]
            # Size shorter clips to about 60 logical pixels. Use the same scale across
            # the project so a short tail group and a full group stay comparable.
            recommended = float(np.clip(np.percentile(durations, 25) * 1000 / 60, .1, 5)) if durations else 5.0
            if display is not None:
                _require(isinstance(display, dict) and set(display) == {"settings", "normalization"},
                         "MEDIA_PREFERENCE_INVALID", "Display preferences contain unsupported fields.")
                self.set(display["settings"], display["normalization"])
            with self._lock:
                self._waveform_range = waveform_range
                self.review_pages = page_preferences
                self.recommended_time_scale = round(recommended, 2)
        value = self.snapshot()
        self._preference_baselines[key] = {name: value[name] for name in ("settings", "normalization")}
        self._explicit_initial = False
        return self.snapshot()

    def save_page_preferences(self, project, value):
        pages = _review_page_preferences(value)
        _save_preferences(project, {"review_pages": pages})
        self.review_pages = pages

    def page_layout(self, width, label_width, preferences=None):
        pages = self.review_pages if preferences is None else preferences
        return validate_page_layout(dict(pages, plot_width_px=width, label_width_px=label_width,
                                         ms_per_pixel=pages["ms_per_pixel"] or self.recommended_time_scale))

    def save_preferences(self, project):
        value = self.snapshot()
        display = {key: value[key] for key in ("settings", "normalization")}
        key = (str(project.project_dir), project.service.run_id)
        baseline = self._preference_baselines.get(key, {"settings": dict(DISPLAY_DEFAULTS), "normalization": "per_item"})
        if display == baseline and not self._explicit_initial:
            return {"status": "unchanged", **_identity(project)}
        saved = _save_preferences(project, {"display": display})
        self._preference_baselines[key] = copy.deepcopy(display)
        self._explicit_initial = False
        return saved

    def page(self, project, review_id, page_id, *, expected_revision, idempotency_key):
        value = self.snapshot()
        return project.execute("batch_display", {"review_id": review_id, "page_id": page_id,
            "settings": value["settings"], "normalization": value["normalization"]},
            expected_revision=expected_revision, idempotency_key=idempotency_key)

    def item(self, project, item_id, *, context=False):
        _ids(project, [item_id])
        row = project.dataset._items[item_id]
        available = row.get("context_kind") == "source_parent"
        _require(not context or available, "MEDIA_CONTEXT_UNAVAILABLE",
                 "No verified parent context is recorded; this segment is not a parent recording.")
        samples, rate, interval = project.read_audio(item_id, context=context)
        settings = {**DEFAULTS, **self.snapshot()["settings"],
                    "time_max_s": min(31.0, max(DEFAULTS["time_max_s"], len(samples) / rate))}
        # 标准渲染kernel在owner锁外运行，Qt状态轮询不等待STFT。这里只缓存显示，
        # 不写权威assets/SQL/人工状态；路径由当前输入和参数身份生成并核对字节。
        identity = {**_identity(project), "item_id": item_id, "context": context, "settings": settings,
                    "renderer": VERSION, "pcm_sha256": hashlib.sha256(samples.tobytes()).hexdigest()}
        folder = _checked_path(project, project.service.work_dir / "desktop_review_display")
        folder.mkdir(exist_ok=True)
        image = folder / (canonical_hash(identity) + ".png")
        receipt = image.with_suffix(".json")
        _checked_path(project, image); _checked_path(project, receipt)
        if receipt.exists():
            metadata = json.loads(receipt.read_text(encoding="utf-8"))
            _require(metadata.get("identity") == identity and image.is_file()
                     and hashlib.sha256(image.read_bytes()).hexdigest() == metadata.get("image_sha256"),
                     "DISPLAY_CHANGED", "The cached display changed; original audio and labels are retained.")
        else:
            _require(not image.exists(), "DISPLAY_CHANGED", "The display cache has no matching receipt.")
            temporary = folder / ("display-" + uuid.uuid4().hex + ".png")
            try:
                target = [interval["target_start_frame_in_context"] / rate,
                          interval["target_stop_frame_in_context"] / rate] if context else None
                rendering = render(samples, rate, settings, temporary, target_interval=target)
                digest = hashlib.sha256(temporary.read_bytes()).hexdigest()
                temporary.replace(image)
                atomic_json(receipt, {"identity": identity, "image_sha256": digest, "rendering": rendering})
            finally:
                temporary.unlink(missing_ok=True)
        return {"item_id": item_id, "samples": samples, "rate": int(rate), "interval": interval,
                "target_frames": (interval["target_start_frame_in_context"], interval["target_stop_frame_in_context"]),
                "image_path": image, "context_requested": context, "context_available": available,
                "context_kind": row.get("context_kind", "not_recorded"), "settings": settings,
                "reference_only": False, "audio_processing": project.metadata.get("preparation_summary", {}).get("audio_preprocessing", "none"),
                "display_normalization": "independent_clip_peak",
                "image_sha256": hashlib.sha256(image.read_bytes()).hexdigest(), "display_cache_receipt": receipt,
                "image_origin": "project_owned_display_cache_not_registered_download_asset"}


class ReviewMediaController:
    """仅经共同execute创建具体问题；没有保存标签或自动拟合入口。"""
    def __init__(self, project, display=None):
        self.project = project
        self.display = display or DisplayController()
        self.references = ReferenceStore(project)
        self._issue_requests = {}

    def neighbors(self, target_ids, scope_ids, *, expected_revision, idempotency_key, k=5):
        return self.project.execute("neighbors", {"item_ids": _ids(self.project, target_ids, nonempty=True),
            "scope_ids": _ids(self.project, scope_ids, nonempty=True), "k": k},
            expected_revision=expected_revision, idempotency_key=idempotency_key)

    def comparison(self, target_ids, reference_ids, *, expected_revision, idempotency_key):
        targets, references = _ids(self.project, target_ids, nonempty=True), _ids(self.project, reference_ids)
        _require(not set(targets) & set(references), "REFERENCE_TARGET_OVERLAP", "Comparison references cannot become targets.")
        return self.project.execute("comparison", {"item_ids": targets, "comparison_ids": references},
            expected_revision=expected_revision, idempotency_key=idempotency_key)

    def create_issue(self, *, target_ids, fit_ids, comparison_ids, eligible_change_ids,
                     question, reason, observation_text, action, idempotency_key):
        _require(action in {"local_reorder", "local_recluster"}, "MEDIA_LOCAL_ACTION", "Select a fixed local diagnostic action.")
        for value in (question, reason, observation_text):
            _require(isinstance(value, str) and bool(value.strip()), "MEDIA_QUESTION", "Record the actual question, observation and scope reason.")
        scopes = {key: _ids(self.project, values, nonempty=key in {"target_ids", "fit_ids"}) for key, values in (
            ("target_ids", target_ids), ("fit_ids", fit_ids), ("comparison_ids", comparison_ids),
            ("eligible_change_ids", eligible_change_ids))}
        readonly = set(self.references.ids()) | set(scopes["comparison_ids"])
        _require(not readonly & (set(scopes["target_ids"]) | set(scopes["fit_ids"]) | set(scopes["eligible_change_ids"])),
                 "REFERENCE_TARGET_OVERLAP", "References are outside target, fit and eligible-change scopes.")
        _require(set(scopes["eligible_change_ids"]) <= set(scopes["target_ids"]), "MEDIA_SCOPE", "Eligible changes must be declared targets.")
        _require(isinstance(idempotency_key, str) and 0 < len(idempotency_key) <= 145,
                 "IDEMPOTENCY_REQUIRED", "Use an explicit local issue request key of at most 145 characters.")
        request_hash = canonical_hash({**scopes, "question": question, "reason": reason,
                                      "observation": observation_text, "action": action})
        pending = self._issue_requests.get(idempotency_key)
        if pending is None:
            pending = {"hash": request_hash, "observation_revision": self.project.snapshot()["revision"], "issue_revision": None}
            self._issue_requests[idempotency_key] = pending
        _require(pending["hash"] == request_hash, "IDEMPOTENCY_CONFLICT", "The original issue request changed.")
        observation = self.project.execute("observe", {"item_ids": scopes["target_ids"], "text": observation_text},
            expected_revision=pending["observation_revision"], idempotency_key=idempotency_key + ":observation")
        if pending["issue_revision"] is None:
            pending["issue_revision"] = self.project.snapshot()["revision"]
        result = self.project.execute("issue", {**scopes, "question": question, "scope_reason": reason, "action": action,
            "observation_ids": [observation["record"]["observation_id"]]}, expected_revision=pending["issue_revision"],
            idempotency_key=idempotency_key + ":issue")
        return {"observation": observation, "issue": result}

    def start_local(self, issue_id, *, expected_revision, idempotency_key):
        if self.project.service.store.lookup_event(self.project.service.run_id, idempotency_key) is None:
            state = self.project.snapshot()
            issue = state.get("issues", {}).get(issue_id)
            _require(issue is not None, "ISSUE_NOT_FOUND", "Create the declared local issue first.")
            readonly = set(self.references.ids()) | set(issue.get("comparison_ids", []))
            _require(not readonly & set(item for field in ("target_ids", "fit_ids", "eligible_change_ids")
                                       for item in issue.get(field, [])), "REFERENCE_TARGET_OVERLAP",
                     "References changed after issue creation; inspect its scopes before a new calculation.")
        return self.project.execute("local_job", {"issue_id": issue_id},
            expected_revision=expected_revision, idempotency_key=idempotency_key)


# Qt入口在同一模块，纯控制器不持锁进行STFT/execute；耗时工作由owner._run承载。
from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QPlainTextEdit, QPushButton, QSpinBox, QSplitter,
    QScrollArea, QTabWidget, QVBoxLayout, QWidget,
)
import numpy as np
import pyqtgraph as pg
from .annotation_desktop_widgets import PcmPlayer, waveform_half_range


class DisplayControlsWidget(QWidget):
    requested = Signal(dict, str)
    waveformRangeRequested = Signal(float)

    def __init__(self, controller=None, parent=None):
        super().__init__(parent)
        self.controller = controller or DisplayController()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        layout = QHBoxLayout()
        outer.addLayout(layout)
        self.dynamic_range = QSpinBox(); self.dynamic_range.setRange(20, 100)
        self.window = QComboBox()
        for size in (256, 512, 1024, 2048):
            self.window.addItem(str(size), size)
        self.hop = QSpinBox(); self.hop.setRange(32, 512)
        self.normalization = QComboBox()
        self.normalization.addItem("Per syllable display", "per_item")
        self.normalization.addItem("Whole page display", "whole_page")
        for label, widget in (("Range dB", self.dynamic_range), ("Hann window", self.window), ("Hop", self.hop)):
            layout.addWidget(QLabel(label)); layout.addWidget(widget)
        layout.addWidget(self.normalization)
        self.redraw = QPushButton("Redraw display")
        layout.addWidget(self.redraw)
        self.redraw.clicked.connect(lambda: self.requested.emit(
            {"dynamic_range_db": self.dynamic_range.value(), "window_samples": self.window.currentData(),
             "hop_samples": self.hop.value()}, self.normalization.currentData()))
        waveform = QHBoxLayout()
        waveform.addWidget(QLabel("Waveform amplitude"))
        self.waveform_range = QComboBox()
        self.waveform_range.setObjectName("waveformRange")
        self.waveform_range.addItem("Auto (fit peaks)", 0.0)
        for value in (1.0, .5, .2, .1, .05, .02, .01, .005, .002, .001):
            self.waveform_range.addItem(f"±{value:g}", value)
        self.waveform_range.setToolTip("Auto fits peaks in each waveform. Choose a fixed ± limit to compare recordings on the same scale.")
        self.waveform_range.currentIndexChanged.connect(lambda _index:
            self.waveformRangeRequested.emit(float(self.waveform_range.currentData())))
        waveform.addWidget(self.waveform_range)
        waveform.addStretch()
        outer.addLayout(waveform)
        self.setToolTip("Display only. FFT is twice the Hann window (default 1024); frequency limit stays fixed at 16 kHz. "
                       "Whole-page normalization applies to frozen batch pages. Single/context/reference panels keep independent clip peaks.")
        self.sync()

    def sync(self):
        value = self.controller.snapshot(); settings = value["settings"]
        self.dynamic_range.setValue(settings["dynamic_range_db"])
        self.window.setCurrentIndex(self.window.findData(settings["window_samples"]))
        self.hop.setValue(settings["hop_samples"])
        self.normalization.setCurrentIndex(self.normalization.findData(value["normalization"]))
        self.sync_waveform_range()

    def sync_waveform_range(self):
        previous = self.waveform_range.blockSignals(True)
        try:
            value = self.controller.waveform_range
            index = self.waveform_range.findData(value)
            if index < 0:
                self.waveform_range.addItem(f"±{value:g}", value)
                index = self.waveform_range.count() - 1
            self.waveform_range.setCurrentIndex(index)
        finally:
            self.waveform_range.blockSignals(previous)


class _MediaPane(QWidget):
    def __init__(self, dialog, title):
        super().__init__(dialog)
        self.dialog = dialog
        self.data = None
        layout = QVBoxLayout(self)
        self.title = QLabel(title); self.title.setWordWrap(True); layout.addWidget(self.title)
        self.wave = pg.PlotWidget(background="w")
        self.wave.setLabel("bottom", "Time", units="s"); self.wave.setMaximumHeight(130)
        layout.addWidget(self.wave)
        self.image = QLabel(); self.image.setAlignment(Qt.AlignmentFlag.AlignCenter); self.image.setMinimumHeight(200)
        self.image.setScaledContents(False); layout.addWidget(self.image, 1)
        buttons = QHBoxLayout()
        self.play = QPushButton("Play this audio"); self.loop = QPushButton("Loop highlighted syllable")
        buttons.addWidget(self.play); buttons.addWidget(self.loop); layout.addLayout(buttons)
        self.play.clicked.connect(lambda: dialog._play(self, False))
        self.loop.clicked.connect(lambda: dialog._play(self, True))
        self.play.setEnabled(False); self.loop.setEnabled(False)

    def show_data(self, data, *, reference=False):
        self.data = data
        samples, rate = data["samples"], data["rate"]
        self.wave.clear(); self.wave.plot(np.arange(len(samples)) / rate, samples, pen="#286e76")
        start, stop = data["target_frames"]
        self.wave.addItem(pg.LinearRegionItem((start / rate, stop / rate), movable=False,
            brush=pg.mkBrush(29, 107, 114, 40), pen=pg.mkPen("#168c91")))
        self.wave.setXRange(0, len(samples) / rate, padding=0)
        self.sync_waveform_range()
        pixmap = QPixmap(str(data["image_path"]))
        self.image.setPixmap(pixmap.scaled(520, 300, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        self.title.setText(("READ-ONLY REFERENCE · " if reference else "TARGET · ") + data["item_id"] +
            (" · verified parent context" if data["context_requested"] else " · segment audio") +
            f"\nHighlighted target frames [{start}, {stop}) / {rate} Hz. " +
            ("Parent context available." if data["context_available"] else "No verified parent context; segment-only or not recorded."))
        self.play.setEnabled(True); self.loop.setEnabled(True)

    def sync_waveform_range(self):
        if self.data is None:
            return
        samples = self.data["samples"]
        peak = float(np.max(np.abs(samples))) if len(samples) else 0.0
        half = waveform_half_range(peak, self.dialog.display_controller.waveform_range)
        self.wave.setYRange(-half, half, padding=0)


class ReviewMediaDialog(QDialog):
    playbackRequested = Signal()

    def __init__(self, owner, target_id, review_id=None, display_controller=None, *, context=False):
        super().__init__(owner)
        self.owner, self.project = owner, owner.project
        _ids(self.project, [target_id], nonempty=True)
        _require(not context or self.project.dataset._items[target_id].get("context_kind") == "source_parent",
                 "MEDIA_CONTEXT_UNAVAILABLE", "No verified parent context is recorded; open the segment explicitly instead.")
        self.target_id, self.review_id = target_id, review_id
        self.generation = owner._close_coordinator.generation
        self.display_controller = display_controller or getattr(owner, "display_controller", None) or DisplayController()
        self.controller = ReviewMediaController(self.project, self.display_controller)
        self.reference_id = None
        self._context_requested = bool(context)
        self._preference_warning = None
        self._ui_busy = False
        self._action_widgets = []
        self.player = self.audio = PcmPlayer(self)
        self.setWindowTitle("Parent context, references and local questions")
        screen = owner.screen()
        available = screen.availableGeometry() if screen else None
        self.resize(min(1160, available.width() - 32) if available else 1160,
                    min(820, available.height() - 64) if available else 820)
        if available:
            self.move(max(available.left(), min(owner.frameGeometry().center().x() - self.width() // 2, available.right() - self.width())),
                      max(available.top(), min(owner.frameGeometry().center().y() - self.height() // 2, available.bottom() - self.height())))
        layout = QVBoxLayout(self)
        self.message = QLabel("Opening reads audio and draws it; it does not fit a model, change a label or confirm listening.")
        self.message.setWordWrap(True); layout.addWidget(self.message)
        self.controls = DisplayControlsWidget(self.display_controller, self)
        self.controls.requested.connect(self._request_display); layout.addWidget(self.controls)
        self.controls.waveformRangeRequested.connect(self._request_waveform_range)
        self.tabs = QTabWidget(); layout.addWidget(self.tabs, 1)
        media = QWidget(); media_layout = QVBoxLayout(media)
        self.context = QComboBox(); self.context.addItem("Selected segment", False); self.context.addItem("Verified parent context", True)
        self.context.setCurrentIndex(1 if context else 0)
        self.context.currentIndexChanged.connect(self._context_changed)
        media_layout.addWidget(self.context)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.target_pane, self.reference_pane = _MediaPane(self, "Target"), _MediaPane(self, "Reference is read-only")
        splitter.addWidget(self.target_pane); splitter.addWidget(self.reference_pane); media_layout.addWidget(splitter, 1)
        self._add_tab(media, "Audio / parent / comparison")
        self._build_references(); self._build_issue()
        self.stop_button = QPushButton("Stop playback")
        self.stop_button.clicked.connect(lambda: self.stop(announce=True)); layout.addWidget(self.stop_button)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close); close.rejected.connect(self.reject); layout.addWidget(close)
        self.player.statusChanged.connect(self.message.setText); self.player.errorOccurred.connect(self.message.setText)
        self.finished.connect(lambda _result: self.stop())
        self._job_timer = QTimer(self); self._job_timer.setInterval(350)
        self._job_timer.timeout.connect(self._update_local_result); self._job_timer.start()
        self.finished.connect(lambda _result: self._job_timer.stop())
        self._reload_references()
        self.scope_fields["comparison_ids"].setText(", ".join(self.references.item(index).text() for index in range(self.references.count())))
        self.refresh_display()

    def _add_tab(self, body, title):
        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        scroll.setWidget(body)
        self.tabs.addTab(scroll, title)

    def _current(self):
        return (self.owner.project is self.project and self.owner._close_coordinator.generation == self.generation
                and self.owner._close_coordinator.state == "open")

    def _guard(self, call):
        try:
            return call()
        except Exception as exc:
            self.message.setText(system_error(exc)); self.owner._error(exc, operation="review media")

    def _run(self, label, work, done):
        _require(self._current(), "MEDIA_PROJECT_CHANGED", "Reopen media for the current project.")
        self.stop()
        self.set_busy(True)
        try:
            self.owner._run(label, lambda _progress, _cancelled: work(),
                lambda result: done(result) if self._current() else None)
        finally:
            if not getattr(self.owner, "_busy", False):
                self.set_busy(False)

    def set_busy(self, busy):
        """与主窗口同一前台任务互斥；Stop/关闭始终可用。"""
        self._ui_busy = bool(busy)
        available = self._current() and not self._ui_busy and not getattr(self.owner, "_busy", False)
        for widget in [self.context, self.references, self.neighbor_items, self.controls,
                       self.reference_input, self.local_action, self.question, self.reason, self.observation,
                       *self.scope_fields.values(), *self._action_widgets, self.create_issue_button]:
            widget.setEnabled(available)
        self.run_local_button.setEnabled(available and self.issue_id is not None)
        for pane in (self.target_pane, self.reference_pane):
            pane.play.setEnabled(available and pane.data is not None)
            pane.loop.setEnabled(available and pane.data is not None)
        self.stop_button.setEnabled(True)

    def _build_references(self):
        page = QWidget(); layout = QVBoxLayout(page)
        note = QLabel("Fixed stable IDs are read-only references, outside target, fit and eligible-change scopes. No labels are submitted here.")
        note.setWordWrap(True); layout.addWidget(note)
        self.references = QListWidget(); self.references.currentItemChanged.connect(self._reference_selected); layout.addWidget(self.references)
        controls = QHBoxLayout(); self.reference_input = QLineEdit(); self.reference_input.setPlaceholderText("Stable ID from this project")
        add, remove = QPushButton("Pin reference"), QPushButton("Unpin selected reference")
        controls.addWidget(self.reference_input); controls.addWidget(add); controls.addWidget(remove); layout.addLayout(controls)
        add.clicked.connect(lambda: self._guard(self._add_reference)); remove.clicked.connect(lambda: self._guard(self._remove_reference))
        comparison, neighbors = QPushButton("Build explicit target/reference comparison"), QPushButton("Find fixed global neighbors")
        comparison.clicked.connect(lambda: self._guard(self._comparison)); neighbors.clicked.connect(lambda: self._guard(self._neighbors))
        self._action_widgets.extend([add, remove, comparison, neighbors])
        layout.addWidget(comparison); layout.addWidget(neighbors)
        self.comparison_details = QPlainTextEdit(); self.comparison_details.setReadOnly(True); layout.addWidget(self.comparison_details)
        self.neighbor_items = QListWidget(); self.neighbor_items.setMaximumHeight(130)
        self.neighbor_items.currentItemChanged.connect(self._reference_selected); layout.addWidget(self.neighbor_items)
        self._add_tab(page, "Read-only references / neighbors")

    def _target_scope(self):
        if self.review_id:
            review = (self.owner.state or {}).get("batch_review", {}).get("reviews", {}).get(self.review_id)
            _require(review is not None, "BATCH_REVIEW_NOT_FOUND", "The declared review no longer exists.")
            return list(review["item_ids"])
        return [self.target_id]

    def _reload_references(self):
        self.references.blockSignals(True); self.references.clear()
        try:
            ids = self.controller.references.ids()
        except ReviewMediaError as exc:
            self._preference_warning = system_error(exc)
            self.message.setText("Original preferences retained; references not loaded. " + system_error(exc))
            ids = []
        for ident in ids:
            item = QListWidgetItem(ident); item.setData(Qt.ItemDataRole.UserRole, ident); self.references.addItem(item)
        self.references.blockSignals(False)

    def _add_reference(self):
        self.controller.references.set([*self.controller.references.ids(), self.reference_input.text().strip()], target_ids=self._target_scope())
        self._reload_references()
        self._issue_changed()

    def _remove_reference(self):
        current = self.references.currentItem()
        if current:
            self.stop(); self.reference_id = None
            self.controller.references.set([ident for ident in self.controller.references.ids() if ident != current.text()])
            self._reload_references()
            self._issue_changed()

    def _reference_selected(self, current, _previous):
        if current:
            self.reference_id = current.data(Qt.ItemDataRole.UserRole)
            self._guard(self.refresh_display)

    def _context_changed(self, _index):
        self._context_requested = bool(self.context.currentData())
        self._guard(self.refresh_display)

    def display_request(self):
        """主线程捕获可见选择，worker不访问Qt控件。"""
        return {"target_id": self.target_id, "reference_id": self.reference_id, "context": self._context_requested}

    def prepare_display(self, controller=None, *, request=None):
        requested = dict(request or {"target_id": self.target_id, "reference_id": self.reference_id,
                                    "context": self._context_requested})
        display = controller or self.display_controller
        return {"request": requested, "display": display.snapshot(),
                "target": display.item(self.project, requested["target_id"], context=requested["context"]),
                "reference": display.item(self.project, requested["reference_id"]) if requested["reference_id"] else None}

    def install_display(self, bundle):
        if not self._current() or bundle["request"] != self.display_request():
            return False
        self.target_pane.show_data(bundle["target"]); self.reference_pane.setVisible(bundle["reference"] is not None)
        if bundle["reference"]:
            self.reference_pane.show_data(bundle["reference"], reference=True)
        self.context.model().item(1).setEnabled(bundle["target"]["context_available"])
        self.controls.sync()
        settings = bundle["display"]["settings"]
        self.message.setText(f"Displayed {settings['dynamic_range_db']} dB / Hann {settings['window_samples']} / hop {settings['hop_samples']} / "
            f"FFT {bundle['display']['fft_samples']}. Comparison panels use the same core settings, independently normalized. No labels changed." +
            (" Original preferences retained; references not loaded. " + self._preference_warning if self._preference_warning else ""))
        self.set_busy(getattr(self.owner, "_busy", False))
        return True

    def refresh_display(self):
        _require(self._current(), "MEDIA_PROJECT_CHANGED", "Reopen media for the current project.")
        self.controls.sync()
        request = self.display_request()
        self._run("Draw selected media and reference", lambda: self.prepare_display(request=request), self.install_display)

    redraw_current = refresh_display

    def sync_waveform_range(self):
        for pane in (self.target_pane, self.reference_pane):
            pane.sync_waveform_range()
        self.controls.sync_waveform_range()

    def _request_waveform_range(self, value):
        def apply():
            _require(self._current(), "MEDIA_PROJECT_CHANGED", "Reopen media for the current project.")
            _require(not self._ui_busy and not getattr(self.owner, "_busy", False),
                     "MEDIA_BUSY", "Wait for the active operation before changing waveform amplitude.")
            if hasattr(self.owner, "apply_waveform_range"):
                self.owner.apply_waveform_range(value)
            else:
                self.display_controller.set_waveform_range(self.project, value)
            self.sync_waveform_range()
        try:
            return self._guard(apply)
        finally:
            # Keep any un-applied STFT controls intact, including on save failure.
            self.controls.sync_waveform_range()

    def _request_display(self, settings, normalization):
        if hasattr(self.owner, "apply_review_display"):
            self._guard(lambda: self.owner.apply_review_display(settings, normalization))
        else:
            def request():
                temporary = DisplayController(settings, normalization)
                selection = self.display_request()
                def prepare():
                    bundle = self.prepare_display(temporary, request=selection)
                    temporary.save_preferences(self.project)
                    return bundle
                def install(bundle):
                    self.display_controller.set(settings, normalization)
                    self.display_controller.load_preferences(self.project)
                    self.install_display(bundle)
                self._run("Redraw reviewed media with shared core settings", prepare, install)
            self._guard(request)

    def _play(self, pane, loop):
        if not pane.data or not self._current():
            return False
        if self._ui_busy or getattr(self.owner, "_busy", False):
            self.message.setText("Wait for the active operation before playing a media selection.")
            return False
        expected_id = self.target_id if pane is self.target_pane else self.reference_id
        if (pane.data["item_id"] != expected_id or
                (pane is self.target_pane and pane.data["context_requested"] != self._context_requested)):
            self.message.setText("Media selection changed; redraw the matching identity before playback.")
            return False
        self.playbackRequested.emit(); self.stop()
        data = pane.data; self.player.set_audio(data["samples"], data["rate"])
        start, stop = data["target_frames"] if loop else (0, len(data["samples"]))
        return self.player.play(start, stop, loop=loop)

    def stop(self, *, announce=False):
        self.player.stop(announce=announce)

    def isActive(self):
        return self.player.timer.isActive() or self.player.sink is not None

    def save_preferences(self):
        return self.display_controller.save_preferences(self.project)

    def _comparison(self):
        refs = self.controller.references.ids()
        revision = self.project.snapshot()["revision"]
        self._run("Build explicit read-only comparison", lambda: self.controller.comparison([self.target_id], refs,
            expected_revision=revision, idempotency_key="desktop-comparison-" + uuid.uuid4().hex),
            lambda result: self.comparison_details.setPlainText(json.dumps(system_fields(result), ensure_ascii=False, indent=2)))

    def _neighbors(self):
        revision = self.project.snapshot()["revision"]
        self._run("Read fixed global neighbors", lambda: self.controller.neighbors([self.target_id], self.project.dataset.item_ids,
            expected_revision=revision, idempotency_key="desktop-neighbors-" + uuid.uuid4().hex),
            self._show_neighbors)

    def _show_neighbors(self, result):
        self.comparison_details.setPlainText(json.dumps(system_fields(result), ensure_ascii=False, indent=2))
        self.neighbor_items.blockSignals(True); self.neighbor_items.clear()
        for row in result.get("neighbors", []):
            for neighbor in row.get("neighbors", []):
                ident = neighbor["item_id"]
                item = QListWidgetItem("Read-only neighbor · " + ident)
                item.setData(Qt.ItemDataRole.UserRole, ident); self.neighbor_items.addItem(item)
        self.neighbor_items.blockSignals(False)

    def _build_issue(self):
        page = QWidget(); layout = QVBoxLayout(page); form = QFormLayout()
        scope = self._target_scope()
        self.question, self.reason, self.observation = QLineEdit(), QLineEdit(), QLineEdit()
        self.local_action = QComboBox(); self.local_action.addItem("Fixed local reorder", "local_reorder"); self.local_action.addItem("Fixed local recluster", "local_recluster")
        self.scope_fields = {}
        for name, values in (("target_ids", scope), ("fit_ids", scope), ("comparison_ids", []), ("eligible_change_ids", scope)):
            self.scope_fields[name] = QLineEdit(", ".join(values)); form.addRow(name, self.scope_fields[name])
        for label, widget in (("Actual observation", self.observation), ("Concrete question", self.question),
                              ("Scope reason", self.reason), ("Fixed diagnostic action", self.local_action)):
            form.addRow(label, widget)
        layout.addLayout(form)
        note = QLabel("This diagnostic does not prepare a normal family review, replace the 30/31 route, change a type or unlock a direct-listening exception. All declared scopes are explicit.")
        note.setWordWrap(True); layout.addWidget(note)
        self.create_issue_button, self.run_local_button = QPushButton("Create this explicit issue"), QPushButton("Run the saved issue's fixed local job")
        self.run_local_button.setEnabled(False); self.issue_id = None
        self.create_issue_button.clicked.connect(lambda: self._guard(self._create_issue)); self.run_local_button.clicked.connect(lambda: self._guard(self._start_local))
        layout.addWidget(self.create_issue_button); layout.addWidget(self.run_local_button)
        self.issue_details = QPlainTextEdit(); self.issue_details.setReadOnly(True); layout.addWidget(self.issue_details)
        for field in [*self.scope_fields.values(), self.question, self.reason, self.observation]:
            field.textChanged.connect(self._issue_changed)
        self.local_action.currentIndexChanged.connect(self._issue_changed)
        self._add_tab(page, "Specific local question")

    def _create_issue(self):
        scopes = {key: [value.strip() for value in field.text().split(",") if value.strip()] for key, field in self.scope_fields.items()}
        options = {**scopes, "question": self.question.text(), "reason": self.reason.text(),
                   "observation_text": self.observation.text(), "action": self.local_action.currentData(),
                   "idempotency_key": "desktop-local-issue-" + uuid.uuid4().hex}
        def done(result):
            self.issue_id = result["issue"]["record"]["issue_id"]
            self.run_local_button.setEnabled(True)
            self.issue_details.setPlainText(json.dumps(system_fields(result), ensure_ascii=False, indent=2))
        self._run("Create explicit local question", lambda: self.controller.create_issue(**options), done)

    def _issue_changed(self, *_args):
        self.issue_id = None; self.run_local_button.setEnabled(False)

    def _update_local_result(self):
        self.set_busy(getattr(self.owner, "_busy", False))
        if not self._current() or self.issue_id is None:
            return
        jobs = [row for row in (self.owner.state or {}).get("jobs", {}).values() if row.get("issue_id") == self.issue_id]
        if jobs:
            self.issue_details.setPlainText(json.dumps(system_fields({"issue_id": self.issue_id, "jobs": jobs,
                "meaning": "Diagnostic results only; no family/leaf or listening confirmation was assigned."}), ensure_ascii=False, indent=2))

    def _start_local(self):
        _require(self.issue_id is not None, "ISSUE_NOT_FOUND", "Create the explicit issue first.")
        revision = self.project.snapshot()["revision"]
        self._run("Request the issue's fixed local job", lambda: self.controller.start_local(self.issue_id,
            expected_revision=revision, idempotency_key="desktop-local-job-" + uuid.uuid4().hex),
            lambda result: self.issue_details.setPlainText(json.dumps(system_fields(result), ensure_ascii=False, indent=2) +
                "\nQueued diagnostic only. Follow its identity in Background tasks; no labels were assigned."))

    def closeEvent(self, event):
        self.stop()
        self._job_timer.stop()
        super().closeEvent(event)
