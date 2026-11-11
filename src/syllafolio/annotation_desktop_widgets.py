"""Native Qt audition widgets; derived display/playback never changes source PCM."""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
from PIL import Image
import pyqtgraph as pg
from PySide6.QtCore import QIODevice, QObject, QRectF, QTimer, Qt, Signal
from PySide6.QtMultimedia import QAudio, QAudioFormat, QAudioSink, QMediaDevices
from PySide6.QtWidgets import QVBoxLayout, QWidget


def _page_view_duration(page, duration, review_pages, gap_seconds, *, scope_clusters=False):
    """Use a full page's seconds-per-pixel scale even for the last partial page."""
    slots = page["slots"]
    capacity = 10
    if scope_clusters and review_pages:
        # One time scale for all clusters, including singleton and tail pages.
        total_items = sum(len(p["item_ids"]) for p in review_pages)
        durations = [p["duration_seconds"] for p in review_pages]
        if any(len(p["item_ids"]) == capacity for p in review_pages):
            return max(duration, *durations)
        clip_time = sum(durations) - gap_seconds * (total_items - len(review_pages))
        return max(duration, *durations, capacity * clip_time / total_items + (capacity - 1) * gap_seconds)
    if not 0 < len(slots) < capacity:
        return duration
    for sibling in reversed(review_pages):
        if (sibling.get("subcluster_id") == page.get("subcluster_id")
                and len(sibling["item_ids"]) == capacity and sibling.get("duration_seconds", 0) > 0):
            return max(duration, sibling["duration_seconds"])
    mean_syllable = sum(s["stop_seconds"] - s["start_seconds"] for s in slots) / len(slots)
    if len(slots) > 1:
        gap_seconds = slots[1]["start_seconds"] - slots[0]["stop_seconds"]
    return max(duration, capacity * mean_syllable + (capacity - 1) * gap_seconds)


def waveform_half_range(peak, fixed_range):
    """Use one symmetric amplitude scale for this PCM buffer, with room above the peaks."""
    if fixed_range > 0:
        return fixed_range
    return 1.1 * peak if peak > 0 else 1.0


class AuditionCanvas(QWidget):
    """Linked native plots with the existing renderer's exact spectrogram pixels."""

    itemClicked = Signal(str, bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        pg.setConfigOptions(antialias=False, imageAxisOrder="row-major")
        self.wave = pg.PlotWidget(background="w")
        self.wave.setObjectName("waveformPlot")
        self.spec = pg.PlotWidget(background="w")
        self.spec.setObjectName("spectrogramPlot")
        self.wave.setLabel("left", "Amplitude")
        self.wave.getAxis("left").enableAutoSIPrefix(False)
        self.spec.setLabel("left", "Frequency", units="kHz")
        self.spec.setLabel("bottom", "Time", units="s")
        # The time axes are linked by screen position. Reserve the same space
        # for both sets of ticks so small amplitude decimals don't crop the onset.
        metrics = self.fontMetrics()
        left_width = metrics.horizontalAdvance("−0.0005") + metrics.height() + 10
        for plot in (self.wave, self.spec):
            plot.getAxis("left").setWidth(left_width)
        self.wave.setXLink(self.spec)
        self.wave.setMaximumHeight(135)
        self.wave.setMinimumHeight(65)
        self.spec.setMinimumHeight(165)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.addWidget(self.wave, 1)
        layout.addWidget(self.spec, 3)
        self.wave.scene().sigMouseClicked.connect(lambda event: self._clicked(self.wave, event))
        self.spec.scene().sigMouseClicked.connect(lambda event: self._clicked(self.spec, event))
        self._waveform_range = 0.0
        self.clear()

    def clear(self):
        self.wave.clear()
        self.spec.clear()
        self.slots = []
        self._regions = {}
        self._cursor_lines = []
        self.duration = 0.0
        self._waveform_peak = 0.0
        self.selected_item_id = None
        self.selected_item_ids = ()
        self._fit_bounds = QRectF(0, 0, 1, 16)
        self.wave.setXRange(0, 1, padding=0)
        self.fit_page()

    def set_page(self, samples, rate, page, spectrogram_path: str | Path, *, review_pages=(), gap_seconds=0.5,
                 scope_clusters=False):
        self.clear()
        y = np.asarray(samples, dtype=np.float32)
        if y.ndim != 1 or not len(y) or not np.isfinite(y).all():
            raise ValueError("The audition page must contain finite mono PCM.")
        self.slots = list(page["slots"])
        self.duration = len(y) / rate
        self._waveform_peak = float(np.max(np.abs(y)))
        curve = self.wave.plot(np.arange(len(y), dtype=np.float64) / rate, y,
                               pen=pg.mkPen("#286e76", width=1))
        curve.setDownsampling(auto=True, method="peak")
        curve.setClipToView(True)
        # Crop only the graph domain. The shared service generated these pixels
        # using the paper display settings and selected normalization mode.
        image = np.asarray(Image.open(spectrogram_path).convert("RGB"))
        bounds = page.get("display", {}).get("plot_bounds")
        if not bounds:
            raise ValueError("Spectrogram display bounds are missing.")
        height, width = image.shape[:2]
        left, right = round(bounds["left"] * width), round(bounds["right"] * width)
        top, bottom = round(bounds["top"] * height), round(bounds["bottom"] * height)
        cropped = image[top:bottom, left:right]
        if not cropped.size:
            raise ValueError("Spectrogram graph bounds are empty.")
        raster = pg.ImageItem(np.ascontiguousarray(cropped[::-1]), axisOrder="row-major")
        frequency_khz = min(16.0, rate / 2000)
        raster.setRect(QRectF(0, 0, self.duration, frequency_khz))
        view_duration = _page_view_duration(page, self.duration, review_pages, round(gap_seconds * rate) / rate,
                                            scope_clusters=scope_clusters)
        self._fit_bounds = QRectF(0, 0, view_duration, frequency_khz)
        self.spec.addItem(raster)
        for plot in (self.wave, self.spec):
            cursor = pg.InfiniteLine(0, angle=90, pen=pg.mkPen("#cb653f", width=1.3))
            cursor.setZValue(20)
            plot.addItem(cursor)
            self._cursor_lines.append(cursor)
            for slot in self.slots:
                region = pg.LinearRegionItem((slot["start_seconds"], slot["stop_seconds"]), movable=False,
                                             brush=pg.mkBrush(29, 107, 114, 28), pen=pg.mkPen(29, 107, 114, 100))
                region.setZValue(10)
                plot.addItem(region, ignoreBounds=True)
                region.hide()
                self._regions.setdefault(slot["item_id"], []).append(region)
                line = pg.InfiniteLine(slot["start_seconds"], angle=90,
                                       pen=pg.mkPen("#b9cccf", width=.7))
                plot.addItem(line)
        self.spec.setLimits(xMin=0, xMax=view_duration, yMin=0, yMax=frequency_khz)
        self.fit_page()

    def set_waveform_range(self, fixed_range):
        self._waveform_range = fixed_range
        half_range = waveform_half_range(self._waveform_peak, fixed_range)
        self.wave.setYRange(-half_range, half_range, padding=0)

    def select_item(self, item_id):
        self.select_items((item_id,))

    def select_items(self, item_ids):
        selected = set(item_ids)
        self.selected_item_ids = tuple(slot["item_id"] for slot in self.slots if slot["item_id"] in selected)
        self.selected_item_id = self.selected_item_ids[0] if len(self.selected_item_ids) == 1 else None
        for item_id, regions in self._regions.items():
            for region in regions:
                region.setVisible(item_id in selected)

    def set_position(self, seconds):
        for cursor in self._cursor_lines:
            cursor.setValue(float(seconds))

    def fit_page(self):
        self.spec.setRange(rect=self._fit_bounds, padding=0)
        self.set_waveform_range(self._waveform_range)

    def _clicked(self, plot, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        if not plot.getViewBox().sceneBoundingRect().contains(event.scenePos()):
            return
        seconds = plot.getViewBox().mapSceneToView(event.scenePos()).x()
        slot = next((x for x in self.slots if x["start_seconds"] <= seconds < x["stop_seconds"]), None)
        if slot:
            self.itemClicked.emit(slot["item_id"], bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier))


class _PcmStream(QIODevice):
    def __init__(self, data: bytes, loop: bool, parent=None):
        super().__init__(parent)
        self.data = data
        self.loop = loop
        self.position = 0
        self.open(QIODevice.OpenModeFlag.ReadOnly)

    def isSequential(self):
        return True

    def bytesAvailable(self):
        return max(0, len(self.data) - self.position) + super().bytesAvailable()

    def atEnd(self):
        return not self.loop and self.position >= len(self.data)

    def readData(self, maxlen):
        if maxlen <= 0 or not self.data:
            return bytes()
        if not self.loop:
            stop = min(len(self.data), self.position + maxlen)
            data = self.data[self.position:stop]
            self.position = stop
            return data
        first = self.data[self.position:self.position + maxlen]
        missing = maxlen - len(first)
        if missing:
            count, remainder = divmod(missing, len(self.data))
            first += self.data * count + self.data[:remainder]
        self.position = (self.position + maxlen) % len(self.data)
        return first

    def writeData(self, _data):
        return -1


class PcmPlayer(QObject):
    """Qt PCM output; unsupported/no devices are reported, never counted as listening."""

    positionChanged = Signal(float)
    statusChanged = Signal(str)
    errorOccurred = Signal(str)
    playingChanged = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.samples = np.empty(0, dtype=np.float32)
        self.rate = 0
        self.sink = None
        self.stream = None
        self.timer = QTimer(self)
        self.timer.setInterval(25)
        self.timer.timeout.connect(self._tick)
        self.start_seconds = 0.0
        self.segment_seconds = 0.0
        self.looping = False
        self.paused = False
        self.output_conversion = None
        self._playback_ranges = ()
        self._gap_frames = 0
        self._position_segments = ()

    @property
    def playback_ranges(self):
        return self._playback_ranges

    @property
    def gap_frames(self):
        return self._gap_frames

    def set_audio(self, samples, rate):
        self.stop()
        self.samples = np.array(samples, dtype=np.float32, copy=True)
        if self.samples.ndim != 1 or not np.isfinite(self.samples).all() or rate <= 0:
            raise ValueError("Playback requires finite mono PCM and a valid sample rate.")
        self.rate = int(rate)

    def play(self, start_frame=0, stop_frame=None, loop=False):
        self.stop(announce=False)
        stop_frame = len(self.samples) if stop_frame is None else int(stop_frame)
        start_frame = int(start_frame)
        if not (0 <= start_frame < stop_frame <= len(self.samples)):
            self.errorOccurred.emit("Choose a nonempty audition region first.")
            return False
        return self._start_segment(self.samples[start_frame:stop_frame],
            ranges=((start_frame, stop_frame),), gap_frames=0, loop=loop,
            positions=((0, stop_frame - start_frame, start_frame, stop_frame),))

    def play_ranges(self, ranges, *, gap_frames, loop=False):
        """只拼选中帧区间；间隔为零PCM，播放头在间隔停留于前一条末尾。"""
        self.stop(announce=False)
        integer = lambda value: isinstance(value, (int, np.integer)) and not isinstance(value, (bool, np.bool_))
        try:
            frozen = tuple(ranges)
            valid = integer(gap_frames) and gap_frames >= 0 and bool(frozen)
            ordered = []
            previous_stop = 0
            for pair in frozen:
                if not isinstance(pair, (tuple, list, np.ndarray)) or len(pair) != 2:
                    valid = False
                    break
                start, stop = pair
                if not (integer(start) and integer(stop) and 0 <= start < stop <= len(self.samples)
                        and start >= previous_stop):
                    valid = False
                    break
                ordered.append((int(start), int(stop)))
                previous_stop = int(stop)
        except (TypeError, ValueError):
            valid = False
        if not valid:
            self.errorOccurred.emit("Choose nonempty, ordered, non-overlapping frame ranges and a nonnegative integer gap.")
            return False
        ranges = tuple(ordered)
        gap_frames = int(gap_frames)
        parts, positions = [], []
        cursor = 0
        for index, (start, stop) in enumerate(ranges):
            if index and gap_frames:
                parts.append(np.zeros(gap_frames, dtype=np.float32))
                previous_end = ranges[index - 1][1]
                positions.append((cursor, cursor + gap_frames, previous_end, previous_end))
                cursor += gap_frames
            parts.append(self.samples[start:stop])
            positions.append((cursor, cursor + stop - start, start, stop))
            cursor += stop - start
        segment = np.concatenate(parts)
        return self._start_segment(segment, ranges=ranges, gap_frames=gap_frames, loop=loop,
                                   positions=tuple(positions))

    def _start_segment(self, segment, *, ranges, gap_frames, loop, positions):
        device = QMediaDevices.defaultAudioOutput()
        if device.isNull():
            self.errorOccurred.emit("No audio output device is available. Playback has not started.")
            return False
        fmt = QAudioFormat()
        fmt.setSampleRate(self.rate)
        fmt.setChannelCount(1)
        fmt.setSampleFormat(QAudioFormat.SampleFormat.Float)
        source_frames = len(segment)
        self.output_conversion = None
        if not device.isFormatSupported(fmt):
            fmt = device.preferredFormat()
            if fmt.sampleFormat() not in {QAudioFormat.SampleFormat.Float,
                                          QAudioFormat.SampleFormat.Int16,
                                          QAudioFormat.SampleFormat.Int32,
                                          QAudioFormat.SampleFormat.UInt8}:
                self.errorOccurred.emit("The output device uses an unsupported PCM format.")
                return False
            if fmt.sampleRate() != self.rate:
                from scipy.signal import resample_poly
                common = math.gcd(fmt.sampleRate(), self.rate)
                segment = resample_poly(segment, fmt.sampleRate() // common, self.rate // common)
            self.output_conversion = f"Playback output only: {fmt.sampleRate()} Hz, {fmt.channelCount()} channel(s)."
        channels = max(1, fmt.channelCount())
        if channels > 1:
            segment = np.repeat(segment[:, None], channels, axis=1)
        if fmt.sampleFormat() == QAudioFormat.SampleFormat.Float:
            output = np.asarray(segment, dtype="<f4")
        elif fmt.sampleFormat() == QAudioFormat.SampleFormat.Int16:
            output = (np.clip(segment, -1, 1) * 32767).astype("<i2")
        elif fmt.sampleFormat() == QAudioFormat.SampleFormat.Int32:
            output = (np.clip(segment, -1, 1) * 2147483647).astype("<i4")
        else:
            output = np.clip((segment + 1) * 127.5, 0, 255).astype(np.uint8)
        self.start_seconds = ranges[0][0] / self.rate
        self.segment_seconds = source_frames / self.rate
        self.looping = bool(loop)
        self._playback_ranges = ranges
        self._gap_frames = gap_frames
        self._position_segments = positions
        self.stream = _PcmStream(np.ascontiguousarray(output).tobytes(), self.looping, self)
        sink = QAudioSink(device, fmt, self)
        self.sink = sink
        sink.setBufferSize(max(4096, fmt.bytesForDuration(50000)))
        sink.stateChanged.connect(self._state_changed)
        sink.start(self.stream)
        # A device can synchronously report failure/end from start(). The
        # state callback already released that output and published its result.
        if self.sink is not sink:
            return False
        if sink.error() != QAudio.Error.NoError:
            message = f"Audio output could not start ({sink.error().name})."
            self.stop(announce=False)
            self.errorOccurred.emit(message)
            return False
        if not self.paused:
            self.positionChanged.emit(self.start_seconds)
            self.timer.start()
            self.playingChanged.emit(True)
            self.statusChanged.emit(self._playback_status() +
                                    (" " + self.output_conversion if self.output_conversion else ""))
        return True

    def _playback_status(self, paused=False):
        if paused:
            return "Playback paused."
        if self.looping:
            return "Looping."
        return "Playing."

    def pause(self):
        if self.sink is not None and not self.paused:
            self.sink.suspend()

    def resume(self):
        if self.sink is not None and self.paused:
            self.sink.resume()

    def stop(self, *, announce=False):
        active = self.sink is not None or self.stream is not None or self.timer.isActive()
        self.timer.stop()
        sink, stream = self.sink, self.stream
        # Clear ownership before sink.stop() can emit another state signal.
        self.sink = self.stream = None
        self.looping = False
        self.paused = False
        self.output_conversion = None
        self._playback_ranges = ()
        self._gap_frames = 0
        self._position_segments = ()
        if sink is not None:
            sink.stop()
            sink.deleteLater()
        if stream is not None:
            stream.close()
            stream.deleteLater()
        self.playingChanged.emit(False)
        if announce and active:
            self.statusChanged.emit("Playback stopped. No annotation has been confirmed automatically.")

    def _tick(self):
        if self.sink is None or self.paused or not self.segment_seconds:
            return
        seconds = self.sink.processedUSecs() / 1_000_000
        if self.looping:
            seconds %= self.segment_seconds
        seconds = max(0.0, min(seconds, self.segment_seconds))
        if not self._position_segments:
            self.positionChanged.emit(self.start_seconds + seconds)
            return
        frame = seconds * self.rate
        page_frame = self._position_segments[-1][3]
        for begin, end, page_begin, page_end in self._position_segments:
            if frame < end:
                page_frame = page_begin if page_begin == page_end else page_begin + frame - begin
                break
        self.positionChanged.emit(page_frame / self.rate)

    def _state_changed(self, state):
        if self.sink is None:
            return
        source = self.sender()
        if source is not None and source is not self.sink:
            return  # Ignore a late signal from an already replaced output.
        if state == QAudio.State.StoppedState:
            error = self.sink.error()
            self.stop(announce=error == QAudio.Error.NoError)
            if error != QAudio.Error.NoError:
                self.errorOccurred.emit(f"Audio output stopped ({error.name}).")
        elif state == QAudio.State.IdleState and self.stream is not None and self.stream.atEnd():
            self._tick()
            self.stop(announce=False)
            self.statusChanged.emit("Playback ended. No annotation has been confirmed automatically.")
        elif state == QAudio.State.SuspendedState and not self.paused:
            self._tick()
            self.paused = True
            self.timer.stop()
            self.playingChanged.emit(False)
            self.statusChanged.emit(self._playback_status(paused=True))
        elif state in (QAudio.State.ActiveState, QAudio.State.IdleState) and self.paused:
            self.paused = False
            self.timer.start()
            self.playingChanged.emit(True)
            self.statusChanged.emit(self._playback_status())
