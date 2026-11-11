"""Sequential decoding with the packaged Qt FFmpeg backend, without playback.

The caller enforces its allowed input formats. Container duration and estimated
frame counts are deliberately not used. Frame indices belong to the resulting
canonical FLOAT WAV, not to compressed packets or an estimated seek table.
"""
from __future__ import annotations

from contextlib import suppress
import math
import os
from pathlib import Path
import tempfile
import threading


_NO_PROGRESS_TIMEOUT_MS = 30_000
# RIFF/WAVE uses a 32-bit size field. Reserve space for libsndfile's headers.
_MAX_WAV_DATA_BYTES = (1 << 32) - 256


def decode_to_wave(source: Path, destination: Path, *,
                   max_seconds: float | None = None, cancelled=None) -> None:
    """Decode from the beginning to EOF into a new mono FLOAT WAV.

    No resampling, channel mixing, amplitude normalization or audio output is
    performed. A rate/channel/sample-format change, non-finite samples, empty
    audio, a stalled decoder, or a duration beyond ``max_seconds`` is rejected.
    ``None`` does not impose a duration cap; the RIFF size limit still applies.

    GUI workers use their own local event loop and the existing application.
    CLI callers may create a temporary QCoreApplication on the main thread.
    A worker without any QCoreApplication is rejected instead of creating an
    application on the wrong thread. All failure paths remove owned temporary
    output. The caller supplies a unique owned output directory; an existing
    destination is rejected before decoding.
    """
    source, destination = Path(source), Path(destination)
    if not source.is_file() or source.is_symlink():
        raise ValueError("The audio source must be an existing regular file.")
    source = source.resolve(strict=True)
    if destination.exists() or destination.is_symlink():
        raise ValueError("The decoded destination already exists.")
    if not destination.parent.is_dir():
        raise ValueError("The decoded destination folder does not exist.")
    destination = destination.absolute()
    if source == destination:
        raise ValueError("The decoded destination must differ from the source.")
    if max_seconds is not None:
        if isinstance(max_seconds, bool):
            raise ValueError("The maximum duration must be a positive finite number.")
        try:
            max_seconds = float(max_seconds)
        except (TypeError, ValueError):
            raise ValueError("The maximum duration must be a positive finite number.") from None
        if not math.isfinite(max_seconds) or max_seconds <= 0:
            raise ValueError("The maximum duration must be a positive finite number.")

    import numpy as np
    import soundfile as sf
    from PySide6.QtCore import QCoreApplication, QEvent, QEventLoop, QTimer, QUrl
    from PySide6.QtMultimedia import QAudioDecoder, QAudioFormat

    application = QCoreApplication.instance()
    owned_application = application is None
    if owned_application:
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError("A QCoreApplication is required before decoding in a worker thread.")
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix="." + destination.name + ".", suffix=".decode.tmp", dir=destination.parent)
    except OSError as error:
        raise RuntimeError("The temporary decoded WAV could not be created.") from error
    os.close(descriptor)
    temporary = Path(temporary_name)
    if owned_application:
        try:
            application = QCoreApplication([])
        except Exception as error:
            temporary.unlink(missing_ok=True)
            raise RuntimeError("The command-line Qt decoder could not be initialized.") from error
    decoder = None
    writer = None
    failure = None
    completed = False
    rate = None
    sample_format = None
    frame_count = 0
    loop = None

    def abort(error):
        nonlocal failure
        if failure is None:
            failure = error
        loop.quit()

    def consume():
        nonlocal writer, rate, sample_format, frame_count
        if failure is not None:
            return
        try:
            while decoder.bufferAvailable():
                if cancelled and cancelled():
                    raise ValueError("Audio decoding was cancelled.")
                buffer = decoder.read()
                if not buffer.isValid() or buffer.frameCount() <= 0:
                    continue
                audio_format = buffer.format()
                current_rate = audio_format.sampleRate()
                if audio_format.channelCount() != 1:
                    raise ValueError("Audio must be mono; channels are not combined automatically.")
                if current_rate <= 0:
                    raise ValueError("The decoded sample rate is invalid.")
                if rate is None:
                    rate, sample_format = current_rate, audio_format.sampleFormat()
                    writer = sf.SoundFile(temporary, mode="w", samplerate=rate,
                                          channels=1, format="WAV", subtype="FLOAT")
                elif current_rate != rate or audio_format.sampleFormat() != sample_format:
                    raise ValueError("The audio format changes within the decoded stream.")
                raw = bytes(buffer.constData())[:buffer.byteCount()]
                if len(raw) != buffer.byteCount():
                    raise ValueError("The decoded audio buffer has an invalid byte count.")
                formats = QAudioFormat.SampleFormat
                if sample_format == formats.Float:
                    samples = np.frombuffer(raw, dtype="<f4")
                elif sample_format == formats.Int16:
                    samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
                elif sample_format == formats.Int32:
                    samples = (np.frombuffer(raw, dtype="<i4").astype(np.float64)
                               / 2147483648.0).astype(np.float32)
                elif sample_format == formats.UInt8:
                    samples = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
                else:
                    raise ValueError("The decoded PCM sample format is unsupported.")
                if len(samples) != buffer.frameCount() or not np.isfinite(samples).all():
                    raise ValueError("The decoded audio is invalid or contains non-finite samples.")
                next_count = frame_count + len(samples)
                if max_seconds is not None and next_count / rate > max_seconds:
                    raise ValueError("The decoded audio exceeds the maximum duration.")
                if next_count * 4 > _MAX_WAV_DATA_BYTES:
                    raise ValueError("The decoded FLOAT WAV exceeds the RIFF/WAVE size limit.")
                samples = np.asarray(samples, dtype="<f4")
                writer.write(samples)
                frame_count = next_count
                # Only actual valid PCM counts as progress, not duration hints.
                watchdog.start(_NO_PROGRESS_TIMEOUT_MS)
        except (ValueError, RuntimeError, OSError) as error:
            abort(error)
        except Exception as error:
            abort(RuntimeError("The decoded audio buffer could not be read: " + type(error).__name__ + "."))

    def finished():
        nonlocal completed
        consume()
        completed = failure is None
        loop.quit()

    def decoder_error(error):
        abort(ValueError("The audio could not be decoded: " + decoder.errorString()))

    try:
        # Do not request a rate or channel count: Qt returns the native stream
        # format. Conversion to FLOAT below changes only PCM representation.
        loop = QEventLoop()
        decoder = QAudioDecoder()
        watchdog = QTimer(decoder)
        watchdog.setSingleShot(True)
        watchdog.timeout.connect(lambda: abort(RuntimeError("Audio decoding timed out without progress.")))
        decoder.bufferReady.connect(consume)
        decoder.finished.connect(finished)
        decoder.error.connect(decoder_error)
        decoder.setSource(QUrl.fromLocalFile(str(source)))
        watchdog.start(_NO_PROGRESS_TIMEOUT_MS)
        QTimer.singleShot(0, decoder.start)
        loop.exec()
        watchdog.stop()
        decoder.stop()
        if failure is not None:
            raise failure
        if not completed or writer is None or frame_count == 0:
            raise ValueError("The audio contains no decodable samples.")
        writer.close()
        writer = None
        info = sf.info(temporary)
        if (info.format != "WAV" or info.subtype != "FLOAT" or info.channels != 1
                or info.samplerate != rate or info.frames != frame_count):
            raise RuntimeError("The decoded WAV header does not match the emitted PCM.")
        temporary.replace(destination)
    except (ValueError, RuntimeError):
        raise
    except OSError as error:
        raise RuntimeError("The decoded FLOAT WAV could not be written; check folder access, free space and the WAV size limit.") from error
    finally:
        try:
            if writer is not None:
                with suppress(RuntimeError, OSError):
                    writer.close()
            if decoder is not None:
                decoder.stop()
                for signal, callback in ((decoder.bufferReady, consume),
                                         (decoder.finished, finished),
                                         (decoder.error, decoder_error)):
                    with suppress(RuntimeError, TypeError):
                        signal.disconnect(callback)
                decoder.deleteLater()
                QCoreApplication.sendPostedEvents(decoder, QEvent.Type.DeferredDelete)
            if loop is not None:
                loop.deleteLater()
                QCoreApplication.sendPostedEvents(loop, QEvent.Type.DeferredDelete)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            finally:
                if owned_application:
                    # Never destroy or quit a caller-owned application.
                    import shiboken6
                    shiboken6.delete(application)
