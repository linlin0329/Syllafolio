"""Bounded-memory zero-phase Butterworth bandpass for decoded mono audio.

The two SOS passes, steady-state initialization and odd extension follow
scipy.signal.sosfiltfilt. Short inputs use padlen=min(default, frames-1).
The filter order is scipy.signal.butter's prototype order (1 through 8).
Samples are never normalized, clipped, mixed or resampled. A deterministic
IEEE FLOAT WAV writer avoids timestamp-bearing PEAK chunks.
"""
from __future__ import annotations

import math
from numbers import Integral, Real
import os
from pathlib import Path
import struct
import tempfile

import numpy as np
from scipy.signal import butter, sosfilt, sosfilt_zi
import soundfile as sf


_CHUNK_FRAMES = 131_072
_MAX_DATA_BYTES = (1 << 32) - 64


class BandpassCancelled(RuntimeError):
    code = "CANCELLED"


def _cancel(cancelled):
    if cancelled is not None and cancelled():
        raise BandpassCancelled("Bandpass filtering was cancelled.")


def _sample_rate(value):
    if (isinstance(value, bool) or not isinstance(value, Real)
            or not math.isfinite(value) or value <= 0 or int(value) != value):
        raise ValueError("The sample rate must be a positive integer.")
    rate = int(value)
    if rate * 4 > 0xFFFFFFFF:
        raise ValueError("The sample rate exceeds the FLOAT WAV limit.")
    return rate


def validate_bandpass(settings, sample_rate):
    """Return None for off, or a new normalized {low_hz, high_hz, order} dict."""
    rate = _sample_rate(sample_rate)
    if settings is None:
        return None
    if not isinstance(settings, dict) or not {"low_hz", "high_hz"} <= settings.keys():
        raise ValueError("Bandpass settings require low_hz and high_hz.")
    if set(settings) - {"low_hz", "high_hz", "order"}:
        raise ValueError("Unknown bandpass setting.")
    low, high = settings["low_hz"], settings["high_hz"]
    if any(isinstance(value, bool) or not isinstance(value, Real)
           or not math.isfinite(value) for value in (low, high)):
        raise ValueError("Bandpass cutoffs must be finite numbers in Hz.")
    low, high = float(low), float(high)
    if low <= 0:
        raise ValueError("The low cutoff must be greater than 0 Hz.")
    if low >= high:
        raise ValueError("The high cutoff must be greater than the low cutoff.")
    if high >= rate / 2:
        raise ValueError(f"The high cutoff must be below {rate / 2:g} Hz for {rate} Hz audio.")
    order = settings.get("order", 4)
    if isinstance(order, bool) or not isinstance(order, Integral) or not 1 <= order <= 8:
        raise ValueError("The Butterworth order must be an integer from 1 to 8.")
    return {"low_hz": low, "high_hz": high, "order": int(order)}


def _wave_header(rate, frames):
    data_bytes = frames * 4
    if not 0 < data_bytes <= _MAX_DATA_BYTES:
        raise ValueError("The audio is empty or exceeds the RIFF/FLOAT WAV size limit.")
    # fmt=IEEE float with cbSize=0, fact=sample count; no date or PEAK chunk.
    fmt = struct.pack("<HHIIHHH", 3, 1, rate, rate * 4, 4, 32, 0)
    chunks = b"fmt " + struct.pack("<I", len(fmt)) + fmt
    chunks += b"fact" + struct.pack("<II", 4, frames)
    chunks += b"data" + struct.pack("<I", data_bytes)
    return b"RIFF" + struct.pack("<I", 4 + len(chunks) + data_bytes) + b"WAVE" + chunks


def _temporary(parent, prefix):
    descriptor, name = tempfile.mkstemp(prefix=prefix, suffix=".tmp", dir=parent)
    return os.fdopen(descriptor, "w+b"), Path(name)


def _source_stat(path):
    value = path.stat()
    return value.st_size, value.st_mtime_ns, value.st_dev, value.st_ino


def _finite(values):
    if not np.isfinite(values).all():
        raise ValueError("The audio or filtered output contains non-finite samples.")


def _read(stream, frames):
    # Match the application's decoded-PCM definition before SOS promotes the
    # calculation to float64. Odd padding also follows float32 sosfiltfilt input.
    values = stream.read(frames, dtype="float32", always_2d=False)
    if values.ndim != 1 or len(values) != frames:
        raise ValueError("The decoded mono audio read is incomplete.")
    _finite(values)
    return values


def _pad_length(sos, frames):
    taps = 2 * len(sos) + 1
    taps -= min(int((sos[:, 2] == 0).sum()), int((sos[:, 5] == 0).sum()))
    return min(3 * taps, frames - 1)


def _forward(stream, scratch, sos, zi, frames, edge, cancelled):
    first = _read(stream, min(edge + 1, frames))
    left = 2 * first[0] - first[1:edge + 1][::-1]
    state = zi * (left[0] if edge else first[0])
    tail = first
    count = 0

    def write(values):
        nonlocal state, count
        if not len(values):
            return
        _cancel(cancelled)
        filtered, state = sosfilt(sos, values, zi=state)
        _finite(filtered)
        scratch.write(np.asarray(filtered, dtype="<f8").tobytes())
        count += len(filtered)

    write(left)
    write(first)
    remaining = frames - len(first)
    while remaining:
        _cancel(cancelled)
        values = _read(stream, min(_CHUNK_FRAMES, remaining))
        write(values)
        tail = np.concatenate((tail, values))[-(edge + 1):]
        remaining -= len(values)
    right = 2 * tail[-1] - tail[-2::-1] if edge else np.empty(0)
    write(right)
    if count != frames + 2 * edge:
        raise RuntimeError("The bandpass extension length is inconsistent.")
    scratch.flush()
    return count


def _backward(scratch, output, sos, zi, extended_frames, frames, edge, header_bytes, cancelled):
    scratch.seek((extended_frames - 1) * 8)
    last = np.frombuffer(scratch.read(8), dtype="<f8")[0]
    state = zi * last
    position = extended_frames
    written = 0
    while position:
        _cancel(cancelled)
        size = min(_CHUNK_FRAMES, position)
        start = position - size
        scratch.seek(start * 8)
        raw = scratch.read(size * 8)
        if len(raw) != size * 8:
            raise RuntimeError("The bandpass forward-pass file is incomplete.")
        values = np.frombuffer(raw, dtype="<f8")[::-1]
        filtered, state = sosfilt(sos, values, zi=state)
        _finite(filtered)
        keep_start, keep_stop = max(start, edge), min(position, edge + frames)
        if keep_start < keep_stop:
            samples = filtered[::-1][keep_start - start:keep_stop - start].astype("<f4")
            _finite(samples)
            output.seek(header_bytes + (keep_start - edge) * 4)
            output.write(samples.tobytes())
            written += len(samples)
        position = start
    if written != frames:
        raise RuntimeError("The bandpass output does not contain every source frame.")


def _publish(temporary, destination):
    if os.name == "nt":
        # Windows rename refuses an existing destination, including a race.
        os.rename(temporary, destination)
    else:
        os.link(temporary, destination)
        temporary.unlink()


def filter_audio_file(source, destination, settings, *, cancelled=None):
    """Write a new FLOAT WAV and return processing metadata.

    Memory is O(chunk size + filter order). The forward pass uses a temporary
    float64 disk file; backward chunks write directly to their final WAV offsets.
    Publication is the last cancellation boundary. Original-file SHA verification
    remains the caller's responsibility; these PCM results do not replace it.
    Raw MP3 must first be sequentially decoded by the recording importer.
    """
    source = Path(source).resolve(strict=True)
    requested = Path(destination).absolute()
    if requested.exists() or requested.is_symlink():
        raise FileExistsError("The filtered destination already exists.")
    destination = requested.parent.resolve(strict=True) / requested.name
    if source == destination or not source.is_file():
        raise ValueError("Choose a new filtered file, distinct from the source.")
    _cancel(cancelled)
    before = _source_stat(source)
    output_path = forward_path = None
    output = forward = None
    try:
        with sf.SoundFile(source) as stream:
            if stream.channels != 1 or not stream.seekable():
                raise ValueError("Filtering requires a seekable decoded mono audio source.")
            if stream.format == "MP3":
                raise ValueError("Sequentially decode MP3 to PCM before bandpass filtering.")
            rate = _sample_rate(stream.samplerate)
            frames = len(stream)
            normalized = validate_bandpass(settings, rate)
            header = _wave_header(rate, frames)
            output, output_path = _temporary(destination.parent, ".bandpass-output-")
            output.write(header)
            output.truncate(len(header) + frames * 4)
            edge = 0
            if normalized is None:
                remaining = frames
                while remaining:
                    _cancel(cancelled)
                    values = _read(stream, min(_CHUNK_FRAMES, remaining)).astype("<f4")
                    _finite(values)
                    output.write(values.tobytes())
                    remaining -= len(values)
            else:
                sos = butter(normalized["order"], [normalized["low_hz"], normalized["high_hz"]],
                             btype="bandpass", fs=rate, output="sos")
                _finite(sos)
                try:
                    zi = sosfilt_zi(sos)
                except np.linalg.LinAlgError as error:
                    raise ValueError("The requested bandpass is numerically ill-conditioned.") from error
                _finite(zi)
                edge = _pad_length(sos, frames)
                forward, forward_path = _temporary(destination.parent, ".bandpass-forward-")
                extended = _forward(stream, forward, sos, zi, frames, edge, cancelled)
                _backward(forward, output, sos, zi, extended, frames, edge, len(header), cancelled)
            _cancel(cancelled)
            if _source_stat(source) != before:
                raise ValueError("The audio source changed during filtering.")
        if forward is not None:
            forward.close()
            forward = None
            forward_path.unlink()
            forward_path = None
        output.flush()
        os.fsync(output.fileno())
        output.close()
        output = None
        info = sf.info(output_path)
        if (info.format != "WAV" or info.subtype != "FLOAT" or info.channels != 1
                or info.frames != frames or info.samplerate != rate):
            raise RuntimeError("The filtered WAV does not preserve the audio frame identity.")
        _cancel(cancelled)
        _publish(output_path, destination)
        output_path = None
        return {"settings": normalized, "sample_rate_hz": rate, "frames": frames,
                "phase": "zero" if normalized else "off", "padlen": edge,
                "input_read_dtype": "float32", "computation_dtype": "float64", "output_subtype": "FLOAT",
                "normalization": "none"}
    finally:
        if forward is not None:
            forward.close()
        if output is not None:
            output.close()
        for path in (forward_path, output_path):
            if path is not None:
                path.unlink(missing_ok=True)
