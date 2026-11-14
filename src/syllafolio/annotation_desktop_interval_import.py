"""Read-only recording/annotation preview before an explicit project import."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, QRectF, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPushButton, QSizePolicy,
    QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from .annotation_desktop_widgets import PcmPlayer
from .annotation_desktop_stage_a_ui import BandpassControls
from .annotation_english import system_error


def open_preview(*args, **kwargs):
    # Keep the application entry independent of parser initialization until used.
    from .annotation_interval_import import open_preview as operation
    return operation(*args, **kwargs)


@dataclass
class IntervalImportSelection:
    preview: object
    tier_index: int
    include_empty: bool

    def take_preview(self):
        if self.preview is None:
            raise ValueError("The recording preview has already been released.")
        result, self.preview = self.preview, None
        return result

    def close(self):
        if self.preview is not None:
            self.take_preview().close()


def _wave_points(samples, start, rate):
    """Bound drawing work while retaining real extrema and their source frames."""
    stride = max(1, (len(samples) + 11999) // 12000)
    if stride == 1:
        indices = np.arange(len(samples))
    else:
        count = len(samples) // stride
        blocks = samples[:count * stride].reshape(count, stride)
        low, high = blocks.argmin(axis=1), blocks.argmax(axis=1)
        offsets = np.column_stack((np.minimum(low, high), np.maximum(low, high)))
        indices = (np.arange(count)[:, None] * stride + offsets).reshape(-1)
        indices = np.concatenate((indices, np.arange(count * stride, len(samples))))
    return (indices + start) / rate, samples[indices]


class IntervalImportDialog(QDialog):
    """All recording IO is delegated to the main window's existing foreground job."""

    def __init__(self, owner, on_confirm):
        super().__init__(owner)
        self.owner, self.on_confirm = owner, on_confirm
        self.project = owner.project
        self.generation = owner._close_coordinator.generation
        self._preview = None
        self._regions = []
        self._pcm = None
        self._pcm_key = None
        self._epoch = 0
        self._row_epoch = 0
        self._thread = None
        self._waiting = False
        self._retired = False
        self._pending_region = False
        self._deferred_previews = []
        self.setWindowTitle("New project from recording and annotations")
        self.resize(1060, 760)
        screen = owner.screen()
        if screen:
            area = screen.availableGeometry()
            self.resize(min(self.width(), area.width() - 32), min(self.height(), area.height() - 64))
        outer = QVBoxLayout(self)
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 0, 0)
        intro = QLabel("Choose one recording and its annotation file, then preview the intervals.")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        form = QFormLayout()
        self.recording_input = QLineEdit(); self.recording_input.setObjectName("intervalRecording")
        self.annotation_input = QLineEdit(); self.annotation_input.setObjectName("intervalAnnotation")
        self.recording_browse = QPushButton("Choose recording…")
        self.annotation_browse = QPushButton("Choose annotations…")
        for title, edit, button in (("Recording", self.recording_input, self.recording_browse),
                                     ("Annotations", self.annotation_input, self.annotation_browse)):
            row = QHBoxLayout(); row.addWidget(edit, 1); row.addWidget(button)
            form.addRow(title, row)
        self.kind = QComboBox(); self.kind.setObjectName("intervalFormat")
        self.kind.addItem("Detect automatically", "auto")
        for name, value in (("Raven selection table", "raven"), ("Praat TextGrid", "textgrid"),
                            ("Audacity label file", "audacity")):
            self.kind.addItem(name, value)
        form.addRow("Annotation format", self.kind)
        layout.addLayout(form)
        self.bandpass = BandpassControls(self)
        layout.addWidget(self.bandpass)
        self.preview_button = QPushButton("Preview recording and intervals")
        self.preview_button.setObjectName("intervalPreview")
        layout.addWidget(self.preview_button)
        self.tier_controls = QWidget()
        tier_row = QHBoxLayout(self.tier_controls)
        tier_row.setContentsMargins(0, 0, 0, 0)
        self.tier_label = QLabel("Interval tier")
        tier_row.addWidget(self.tier_label)
        self.tier = QComboBox(); self.tier.setObjectName("intervalTier")
        tier_row.addWidget(self.tier, 1)
        self.include_empty = QCheckBox("Include empty TextGrid intervals")
        self.include_empty.setObjectName("intervalIncludeEmpty")
        tier_row.addWidget(self.include_empty)
        layout.addWidget(self.tier_controls)
        self.summary = QLabel("No recording preview loaded."); self.summary.setWordWrap(True)
        self.summary.setObjectName("intervalSummary"); layout.addWidget(self.summary)
        splitter = QSplitter(Qt.Orientation.Vertical)
        self.regions_table = QTableWidget(0, 5)
        self.regions_table.setObjectName("intervalRegions")
        self.regions_table.setHorizontalHeaderLabels(["Source ID", "Name / label", "Start (s)", "Stop (s)", "Duration (s)"])
        self.regions_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.regions_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.regions_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.regions_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.regions_table.verticalHeader().hide()
        self.regions_table.setMinimumHeight(130)
        self.regions_table.setMinimumHeight(80)
        self.regions_table.setMaximumHeight(180)
        self.regions_table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Ignored)
        splitter.addWidget(self.regions_table)
        wave_panel = QWidget(); wave_layout = QVBoxLayout(wave_panel)
        wave_layout.setContentsMargins(0, 0, 0, 0)
        self.wave_title = QLabel("Select an interval to inspect its actual waveform.")
        self.wave_title.setTextFormat(Qt.TextFormat.PlainText)
        self.wave_title.setWordWrap(True); wave_layout.addWidget(self.wave_title)
        self.waveform = pg.PlotWidget(background="w")
        self.waveform.setObjectName("intervalWaveform")
        self.waveform.setLabel("bottom", "Recording time", units="s")
        self.waveform.hideAxis("bottom")
        self.waveform.setLabel("left", "Amplitude")
        self.waveform.setMinimumHeight(55)
        self.waveform.setMaximumHeight(105)
        self.waveform.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Ignored)
        self.curve = self.waveform.plot([], [], pen="#286e76")
        self.playhead = pg.InfiniteLine(0, pen="#b45b3c")
        self.waveform.addItem(self.playhead)
        wave_layout.addWidget(self.waveform, 1)
        self.spectrogram = pg.PlotWidget(background="w")
        self.spectrogram.setObjectName("intervalSpectrogram")
        self.spectrogram.setLabel("bottom", "Recording time", units="s")
        self.spectrogram.setLabel("left", "Frequency", units="kHz")
        self.spectrogram.setMinimumHeight(120)
        self.spectrogram.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Ignored)
        self.spectrogram.setXLink(self.waveform)
        self.spectrum_image = pg.ImageItem(axisOrder="row-major")
        self.spectrum_image.setLookupTable(np.repeat(np.arange(255, -1, -1, dtype=np.uint8)[:, None], 3, axis=1))
        self.spectrogram.addItem(self.spectrum_image)
        self.spec_playhead = pg.InfiniteLine(0, pen="#b45b3c")
        self.spectrogram.addItem(self.spec_playhead)
        wave_layout.addWidget(self.spectrogram, 3)
        self.spectrum_note = QLabel(); self.spectrum_note.setWordWrap(True)
        wave_layout.addWidget(self.spectrum_note)
        splitter.addWidget(wave_panel); splitter.setSizes([110, 280])
        splitter.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Ignored)
        layout.addWidget(splitter, 1)
        self.message = QLabel(); self.message.setWordWrap(True)
        self.message.setTextFormat(Qt.TextFormat.PlainText)
        self.message.setObjectName("intervalMessage"); layout.addWidget(self.message)
        outer.addWidget(body, 1)
        footer = QHBoxLayout()
        self.play_button = QPushButton("Play selected interval"); self.play_button.setObjectName("intervalPlay")
        self.stop_button = QPushButton("Stop"); self.stop_button.setObjectName("intervalStop")
        self.confirm_button = QPushButton("Create project…"); self.confirm_button.setObjectName("intervalConfirm")
        self.cancel_button = QPushButton("Cancel"); self.cancel_button.setObjectName("intervalCancel")
        for button in (self.play_button, self.stop_button): footer.addWidget(button)
        footer.addStretch()
        footer.addWidget(self.confirm_button); footer.addWidget(self.cancel_button)
        outer.addLayout(footer)
        self.player = PcmPlayer(self)
        self.player.positionChanged.connect(self._position)
        self.player.errorOccurred.connect(lambda text: self._message(text, True))
        self.player.playingChanged.connect(lambda _active: self._update_enabled())
        self.recording_browse.clicked.connect(self._choose_recording)
        self.annotation_browse.clicked.connect(self._choose_annotations)
        for edit in (self.recording_input, self.annotation_input): edit.textChanged.connect(self._input_changed)
        self.kind.currentIndexChanged.connect(self._input_changed)
        self.bandpass.changed.connect(self._bandpass_changed)
        self.preview_button.clicked.connect(self.load_preview)
        self.tier.currentIndexChanged.connect(self._tier_changed)
        self.include_empty.toggled.connect(self._tier_changed)
        self.regions_table.itemSelectionChanged.connect(self._selection_changed)
        self.play_button.clicked.connect(self.play_selected)
        self.stop_button.clicked.connect(self.stop)
        self.confirm_button.clicked.connect(lambda: self.owner._guard(lambda: self.on_confirm(self), operation="confirm interval import"))
        self.cancel_button.clicked.connect(self.reject)
        owner.operationFailed.connect(self._operation_failed)
        self._pending_timer = QTimer(self)
        self._pending_timer.setInterval(75)
        self._pending_timer.timeout.connect(self._load_pending_region)
        self._pending_timer.start()
        self._update_enabled()

    @property
    def preview(self):
        return self._preview

    def _current(self, context=None):
        good = (not self._retired and self.owner.project is self.project
                and self.owner._close_coordinator.accepts_callback(self.generation))
        return good and (context is None or context == (self._epoch, self._row_epoch))

    def _message(self, text, error=False):
        if self._retired:
            return
        self.message.setText(text)
        self.message.setStyleSheet("color:#a34838;" if error else "")

    def _choose_recording(self):
        path, _ = QFileDialog.getOpenFileName(self, "Choose one recording", "", "Audio (*.wav *.flac *.mp3 *.ogg *.aif *.aiff)")
        if path: self.recording_input.setText(path)

    def _choose_annotations(self):
        path, _ = QFileDialog.getOpenFileName(self, "Choose the matching annotation file", "", "Annotations (*.txt *.tsv *.csv *.TextGrid *.textgrid);;All files (*)")
        if path: self.annotation_input.setText(path)

    def _release_preview(self):
        preview, self._preview = self._preview, None
        if preview is None:
            return
        if self._thread is not None and self._thread.isRunning():
            self._deferred_previews.append(preview)
        else:
            preview.close()

    def _input_changed(self, *_args):
        self._epoch += 1
        self._release_preview()
        self._regions = []
        self.tier.blockSignals(True); self.tier.clear(); self.tier.blockSignals(False)
        self.regions_table.setRowCount(0)
        self._clear_pcm()
        self.summary.setText("Preview again after changing either file or the annotation format.")
        self._update_enabled()

    def _bandpass_changed(self):
        self._input_changed()
        self._message("Band-pass settings changed. Preview again.")

    def _clear_pcm(self):
        self.stop()
        self._pcm = self._pcm_key = None
        self._pending_region = False
        self.curve.setData([], [])
        self.spectrum_image.clear()
        self.spectrum_note.clear()
        self.wave_title.setText("Select an interval to inspect its actual waveform.")

    def _run(self, label, function, success):
        if not self._current() or self.owner._busy:
            return False
        self.stop()
        context = (self._epoch, self._row_epoch)
        self._waiting = True
        def completed(result):
            if not self._current(context):
                if hasattr(result, "close"): result.close()
                return
            success(result)
        try:
            self.owner._run(label, function, completed)
            self._thread = self.owner._worker
            thread = self._thread
            thread.finished.connect(lambda: self._settled(thread))
        except Exception:
            self._waiting = False
            raise
        self._update_enabled()
        return True

    def load_preview(self):
        if not self._current() or self.owner._busy:
            return
        recording, annotation, kind = self.recording_input.text().strip(), self.annotation_input.text().strip(), self.kind.currentData()
        if not recording or not annotation or kind is None:
            self._message("Choose one recording, its annotation file and an annotation format.", True)
            return
        try:
            # The file may have been replaced since a prior preview. Validate
            # Nyquist against the newly opened recording in the worker.
            bandpass = self.bandpass.settings()
        except (ValueError, RuntimeError) as error:
            self._message(system_error(error), True)
            return
        self._epoch += 1
        self._release_preview(); self._clear_pcm()
        self.regions_table.setRowCount(0); self._regions = []
        self._message("Reading recording and annotations…")
        def inspected(preview):
            self._preview = preview
            self.tier.blockSignals(True); self.tier.clear()
            for index, tier in enumerate(preview.tiers): self.tier.addItem(tier.name, index)
            self.tier.setCurrentIndex(0); self.tier.blockSignals(False)
            self._tier_changed()
        self._run("Preview recording and annotations", lambda _progress, cancelled: open_preview(recording, annotation, kind, cancelled=cancelled, bandpass=bandpass), inspected)

    def _tier_changed(self, *_args):
        self._row_epoch += 1
        self._clear_pcm()
        self._regions = []
        self.regions_table.setRowCount(0)
        if self._preview is None or self.tier.currentData() is None:
            self._update_enabled(); return
        try:
            index = self.tier.currentData()
            self._regions = self._preview.regions(index, self.include_empty.isChecked())
            info = getattr(self._preview.tiers[index], "info", {}) or {}
            text = f"{len(self._regions)} importable interval(s)."
            if self._preview.recording_channels == 2:
                text += " Left and right channels are averaged for playback and analysis."
            if self._preview.kind == "textgrid" and not self.include_empty.isChecked():
                skipped = sum(not item.label.strip() for item in self._preview.tiers[index].intervals)
                if skipped: text += f" Skipped {skipped} empty interval(s)."
            for key, label in (("skipped_point_count", "point marker(s)"), ("skipped_point_tier_count", "point tier(s)"),
                               ("deduplicated_view_rows", "duplicate view row(s)")):
                if info.get(key): text += f" Skipped {info[key]} {label}."
            self.summary.setText(text)
            self.regions_table.setUpdatesEnabled(False)
            self.regions_table.setRowCount(len(self._regions))
            for row, region in enumerate(self._regions):
                rate = self._preview.rate
                values = (region.source_id, region.label or "(empty label)", f"{region.start_frame/rate:.6f}",
                          f"{region.stop_frame/rate:.6f}", f"{(region.stop_frame-region.start_frame)/rate:.6f}")
                for column, value in enumerate(values):
                    item = QTableWidgetItem(str(value)); item.setToolTip(str(value))
                    self.regions_table.setItem(row, column, item)
            self.regions_table.setUpdatesEnabled(True)
            self._message("")
            if self._regions: self.regions_table.selectRow(0)
            else: self._message("This tier has no importable intervals. Choose another tier or the applicable empty-interval option.")
        except Exception as error:
            self._regions = []
            self.regions_table.setRowCount(0)
            self.summary.setText("No valid intervals in this tier.")
            self._message(system_error(error), True)
        finally:
            self.regions_table.setUpdatesEnabled(True)
            self._update_enabled()

    def _selection_changed(self):
        self._row_epoch += 1
        self._clear_pcm()
        self._pending_region = self.selected_region() is not None
        self._update_enabled()

    def selected_region(self):
        row = self.regions_table.currentRow()
        return self._regions[row] if 0 <= row < len(self._regions) else None

    def _load_pending_region(self):
        if not self._pending_region or not self._current() or self.owner._busy or self._waiting:
            return
        preview, region = self._preview, self.selected_region()
        if preview is None or region is None: return
        self._pending_region = False
        key = (id(preview), self.tier.currentData(), self.include_empty.isChecked(), region.source_id, region.start_frame, region.stop_frame)
        def read(_progress, cancelled):
            if cancelled(): raise ValueError("Interval preview cancelled.")
            pcm = np.array(preview.samples(region), dtype=np.float32, copy=True)
            if cancelled(): raise ValueError("Interval preview cancelled.")
            x, y = _wave_points(pcm, region.start_frame, preview.rate)
            from .annotation_spectrogram import DEFAULTS, spectrum
            settings = dict(DEFAULTS, time_max_s=max(.1, min(31, len(pcm)/preview.rate)))
            bins = min(513, int(min(settings["frequency_max_hz"], preview.rate/2) * 1024 / preview.rate) + 1)
            if max(0, (len(pcm)-512)//96+1) * bins > 2_000_000:
                settings["hop_samples"] = 512
            data = spectrum(pcm, preview.rate, settings)
            if cancelled(): raise ValueError("Interval preview cancelled.")
            return pcm, x, y, data, settings
        def loaded(result):
            self._pcm = result[0]; self._pcm_key = key
            self.player.set_audio(self._pcm, preview.rate)
            self.curve.setData(result[1], result[2])
            self.waveform.setXRange(region.start_frame/preview.rate, region.stop_frame/preview.rate, padding=0)
            self.waveform.enableAutoRange(axis="y")
            data, settings = result[3:]
            if data["db"].shape[1]:
                hop = settings["hop_samples"] / preview.rate
                df = preview.rate / (2 * settings["window_samples"])
                left = region.start_frame/preview.rate + data["centers_s"][0] - hop/2
                self.spectrum_image.setImage(data["db"], autoLevels=False, levels=(-settings["dynamic_range_db"], 0))
                self.spectrum_image.setRect(QRectF(left, -df/2000, data["db"].shape[1]*hop, len(data["frequencies_hz"])*df/1000))
                self.spectrogram.setYRange(0, min(settings["frequency_max_hz"], preview.rate/2)/1000, padding=0)
                self.spectrum_note.setText(f"Spectrogram · {settings['dynamic_range_db']} dB relative to interval peak")
                self.spectrum_note.setToolTip(f"Hann {settings['window_samples']}; FFT {2*settings['window_samples']}; hop {settings['hop_samples']}; display only.")
            else:
                self.spectrum_image.clear()
                self.spectrum_note.setText("This interval is shorter than one complete spectrogram window.")
            self.wave_title.setText(f"{region.label or '(empty label)'} · {(region.stop_frame-region.start_frame)/preview.rate:.3f} s · {preview.rate} Hz")
            self.wave_title.setToolTip(f"Recording frames [{region.start_frame}, {region.stop_frame}). The original samples are unchanged.")
            self.playhead.setPos(region.start_frame/preview.rate)
            self._update_enabled()
        self._run("Read selected annotation interval", read, loaded)

    def _settled(self, thread):
        if self._thread is thread:
            self._thread = None; self._waiting = False
        for preview in self._deferred_previews: preview.close()
        self._deferred_previews.clear()
        if self._retired:
            self.deleteLater()
        else:
            self._update_enabled()

    def _operation_failed(self, text):
        if self._thread is self.owner._worker and self._waiting:
            self._message(text, True)

    def set_busy(self, _busy):
        if _busy: self.stop()
        self._update_enabled()

    def _update_enabled(self):
        if self._retired: return
        available = self._current() and not self.owner._busy and not self._waiting
        for widget in (self.recording_input, self.annotation_input, self.recording_browse, self.annotation_browse,
                       self.kind, self.preview_button, self.regions_table, self.bandpass): widget.setEnabled(available)
        self.tier.setEnabled(available and self._preview is not None)
        is_textgrid = (self._preview.kind if self._preview else self.kind.currentData()) == "textgrid"
        self.tier_controls.setVisible(is_textgrid and self._preview is not None)
        multiple = self._preview is not None and len(self._preview.tiers) > 1
        self.tier_label.setVisible(multiple)
        self.tier.setVisible(multiple)
        self.include_empty.setEnabled(available and is_textgrid)
        ready = available and self._preview is not None and self._pcm is not None and bool(self._regions)
        self.play_button.setEnabled(ready)
        self.confirm_button.setEnabled(ready)
        self.stop_button.setEnabled(self.player.sink is not None)

    def _position(self, seconds):
        region = self.selected_region()
        if region is not None and self._preview is not None:
            self.playhead.setPos(region.start_frame/self._preview.rate + seconds)
            self.spec_playhead.setPos(region.start_frame/self._preview.rate + seconds)

    def play_selected(self):
        if not self._current() or self.owner._busy or self._waiting or self._pcm is None: return
        self.owner._stop_review_audio()
        self.player.play()

    def stop(self, announce=False):
        if hasattr(self, "player"): self.player.stop(announce=announce)

    def take_selection(self):
        if not self._current() or self.owner._busy or self._waiting or self._preview is None or self._pcm is None or not self._regions:
            raise ValueError("Preview and inspect an importable interval before creating a project.")
        self.stop()
        preview, self._preview = self._preview, None
        self._update_enabled()
        return IntervalImportSelection(preview, self.tier.currentData(),
            preview.kind == "textgrid" and self.include_empty.isChecked())

    def restore_selection(self, selection):
        if self._retired:
            selection.close(); return
        self._preview = selection.take_preview()
        self._update_enabled()

    def invalidate(self, reason):
        self._message(reason, True)
        self.close()

    def reject(self):
        self.close()

    def closeEvent(self, event):
        if self._retired:
            event.accept()
            return
        self._retired = True; self._epoch += 1
        self.stop()
        self._pending_timer.stop()
        if self._thread is not None and self._thread.isRunning(): self._thread.cancelled.set()
        self._release_preview()
        if getattr(self.owner, "_interval_import_dialog", None) is self:
            self.owner._interval_import_dialog = None
        event.accept()
        if self._thread is None or not self._thread.isRunning():
            self.deleteLater()
