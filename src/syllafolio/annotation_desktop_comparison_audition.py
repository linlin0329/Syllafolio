"""Read-only audition of a short sample from a candidate grouping."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import math
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event

import numpy as np
import soundfile as sf
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from .annotation_acoustics import _cancel
from .annotation_batch_acoustics import render_page_display
from .annotation_desktop_widgets import AuditionCanvas, PcmPlayer
from .annotation_english import system_error


def prepare_audition(project, item_ids, gap_seconds, check_cancelled=None):
    """Read the project's actual PCM, retaining the requested order and exact frame boundaries."""
    if not 1 <= len(item_ids) <= 10 or len(set(item_ids)) != len(item_ids):
        raise ValueError("Choose one to ten distinct syllables for audition.")
    if not math.isfinite(gap_seconds) or not .05 <= gap_seconds <= 2:
        raise ValueError("The inter-syllable gap must be between 0.05 and 2 seconds.")
    rate = project.dataset.sample_rate_hz
    gap = np.zeros(round(gap_seconds * rate), dtype=np.float32)
    pieces, slots = [], []
    offset = 0
    for number, ident in enumerate(item_ids, 1):
        _cancel(check_cancelled)
        samples, actual_rate, _ = project.read_audio(ident)
        if actual_rate != rate:
            raise ValueError("The selected syllables have different sample rates.")
        if number > 1:
            pieces.append(gap)
            offset += len(gap)
        stop = offset + len(samples)
        pieces.append(samples)
        slots.append({"slot": number, "item_id": ident, "start_frame": offset, "stop_frame": stop,
                      "start_seconds": offset / rate, "stop_seconds": stop / rate})
        offset = stop
    _cancel(check_cancelled)
    pcm = np.concatenate(pieces)
    with TemporaryDirectory(prefix="syllafolio-audition-") as temporary:
        folder = Path(temporary)
        audio = folder / "audition.wav"
        sf.write(audio, pcm, rate, subtype="FLOAT")
        display = render_page_display(audio, slots, folder, check_cancelled=check_cancelled)
        image = (folder / display["spectrogram_path"]).read_bytes()
    return pcm, rate, {"slots": slots, "display": display["display"]}, image


class ComparisonAuditionWidget(QWidget):
    def __init__(self, project, parent=None, *, gap_seconds=0.5):
        super().__init__(parent)
        self.project = project
        self.gap_seconds = gap_seconds
        self._cancelled = Event()
        self._future = None
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="comparison-audition")
        self._executor = executor
        self.destroyed.connect(lambda: executor.shutdown(wait=False, cancel_futures=True))
        self._timer = QTimer(self)
        self._timer.setInterval(40)
        self._timer.timeout.connect(self._check_ready)

        self.canvas = AuditionCanvas(self)
        self.player = PcmPlayer(self)
        self.play_button = QPushButton("Play", self)
        self.stop_button = QPushButton("Stop", self)
        self.status = QLabel(self)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        controls = QHBoxLayout()
        controls.addWidget(self.play_button)
        controls.addWidget(self.stop_button)
        controls.addWidget(self.status, 1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(controls)
        layout.addWidget(self.canvas)
        self.player.positionChanged.connect(self.canvas.set_position)
        self.player.playingChanged.connect(self.stop_button.setEnabled)
        self.player.errorOccurred.connect(self.status.setText)
        self.play_button.clicked.connect(self._play)
        self.stop_button.clicked.connect(self.stop)
        self.clear()

    def set_items(self, item_ids):
        self.clear()
        if not item_ids:
            return
        self.status.setText("Loading audition…")
        self._cancelled = Event()
        self._future = self._executor.submit(prepare_audition, self.project, tuple(item_ids),
                                             self.gap_seconds, self._cancelled.is_set)
        self._timer.start()

    def _check_ready(self):
        future = self._future
        if future is None or not future.done():
            return
        self._timer.stop()
        self._future = None
        try:
            pcm, rate, page, image = future.result()
            with BytesIO(image) as stream:
                self.canvas.set_page(pcm, rate, page, stream, gap_seconds=self.gap_seconds)
            self.player.set_audio(pcm, rate)
            self.play_button.setEnabled(True)
            self.status.setText("  ·  ".join(row["item_id"] for row in page["slots"]))
        except Exception as exc:
            self.clear()
            self.status.setText(system_error(exc))

    def _play(self):
        self.player.play()

    def stop(self):
        self.player.stop()

    def clear(self):
        self._timer.stop()
        self._cancelled.set()
        if self._future is not None:
            self._future.cancel()
            self._future = None
        self.player.set_audio([], 1)
        self.canvas.clear()
        self.play_button.setEnabled(False)
        self.status.clear()

    def hideEvent(self, event):
        if event.spontaneous():  # A minimized native window keeps its displayed group on restore.
            self.stop()
        else:
            self.clear()
        super().hideEvent(event)
