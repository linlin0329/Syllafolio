"""Preview recording intervals and materialize precisely those samples for the project importer."""
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory

import numpy as np
import soundfile as sf
from scipy.io import wavfile

from . import annotation_stage_a as stage
from .acoustics.bandpass import filter_audio_file, validate_bandpass
from .annotation_interval_formats import detect_format, parse_annotations


def _discard_scratch(temporary):
    # On Windows an indexer can briefly hold a preview file. A cache cleanup
    # error must never suppress the returned project and its active owner.
    try:
        temporary.cleanup()
    except OSError as error:
        logging.getLogger(__name__).warning("Could not remove temporary audio at %s: %s", temporary.name, error)


@contextmanager
def _clip_folder(parent):
    temporary = TemporaryDirectory(prefix="input-", dir=parent)
    try:
        yield Path(temporary.name)
    finally:
        _discard_scratch(temporary)


@dataclass(frozen=True)
class Region:
    source_id: str
    label: str
    start_frame: int
    stop_frame: int
    start_seconds: Decimal
    end_seconds: Decimal


def _read_recording(path, cancelled, copy_to=None):
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as stream:
        while True:
            stage._cancel(cancelled)
            block = stream.read(1024 * 1024)
            if not block:
                break
            total += len(block)
            if total > stage.LIMITS["max_total_audio_bytes"]:
                raise ValueError("The recording exceeds the 2 GiB project input limit.")
            digest.update(block)
            if copy_to is not None:
                copy_to.write(block)
    return digest.hexdigest()


@dataclass
class RecordingPreview:
    _temporary: TemporaryDirectory
    _original: Path
    _annotation: Path
    _snapshot: Path
    _pcm: Path
    _annotation_bytes: bytes
    recording_sha256: str
    kind: str
    tiers: list
    rate: int
    frames: int
    _audition_pcm: Path
    _bandpass: dict | None
    recording_channels: int = 1

    @property
    def bandpass(self):
        return dict(self._bandpass) if self._bandpass is not None else None

    @property
    def recording_name(self):
        return self._original.name

    @property
    def annotation_name(self):
        return self._annotation.name

    def close(self):
        _discard_scratch(self._temporary)

    def regions(self, tier_index, include_empty=False):
        if not 0 <= tier_index < len(self.tiers):
            raise ValueError("Choose an interval tier to preview.")
        regions = []
        for interval in self.tiers[tier_index].intervals:
            if self.kind == "textgrid" and not include_empty and not interval.label.strip():
                continue
            start, end = interval.start, interval.end
            prefix = f"Interval {interval.source_id}"
            if (not start.is_finite() or not end.is_finite() or not 0 <= start < end
                    or end > (Decimal(self.frames) + Decimal("0.5")) / self.rate):
                raise ValueError(f"{prefix} is outside the selected recording or has invalid times.")
            first, last = (int((time * self.rate).to_integral_value(rounding=ROUND_HALF_UP))
                           for time in (start, end))
            if last > self.frames or last - first < stage.LIMITS["minimum_frames"]:
                raise ValueError(f"{prefix} must cover at least 8 sample frames inside the recording.")
            if last - first > self.rate * stage.LIMITS["maximum_segment_seconds"]:
                raise ValueError(f"{prefix} exceeds the 30-second limit for a syllable.")
            regions.append(Region(interval.source_id, interval.label, first, last, start, end))
        if len(regions) > stage.LIMITS["max_items"]:
            raise ValueError("The selected tier exceeds the project limit of 5000 syllables.")
        return regions

    def samples(self, region):
        with sf.SoundFile(self._audition_pcm) as stream:
            return _read_region(stream, region)


def _read_region(stream, region):
    size = region.stop_frame - region.start_frame
    if not (0 <= region.start_frame < region.stop_frame <= len(stream)
            and size <= stream.samplerate * stage.LIMITS["maximum_segment_seconds"]):
        raise ValueError("The preview interval is outside the supported sample range.")
    stream.seek(region.start_frame)
    samples = stream.read(size, dtype="float32")
    if len(samples) != size or not np.isfinite(samples).all():
        raise ValueError(f"Could not read all samples of interval {region.source_id}.")
    return samples


def open_preview(recording, annotation, kind="auto", *, cancelled=None, bandpass=None):
    recording, annotation = Path(recording).resolve(strict=True), Path(annotation).resolve(strict=True)
    if recording.suffix.lower() not in stage.INPUT_FORMATS or not recording.is_file():
        raise ValueError(f"Choose a recording in one of these formats: {stage.FORMAT_NAMES}.")
    if not annotation.is_file() or recording == annotation:
        raise ValueError("Choose the annotation file that belongs to this recording.")
    initial = recording.stat()
    if initial.st_size > stage.LIMITS["max_total_audio_bytes"]:
        raise ValueError("The recording exceeds the 2 GiB project input limit.")
    stage._cancel(cancelled)
    if bandpass is not None:
        # Reject out-of-band settings before copying or decoding a long recording.
        bandpass = validate_bandpass(bandpass, stage._audio_header(recording).samplerate)
    data = annotation.read_bytes()
    if kind == "auto":
        kind = detect_format(data, annotation.name)
    # The dialog and worker share this immutable snapshot. Even if the original
    # is replaced, the plotted and auditioned frames always refer to one signal.
    temp = TemporaryDirectory(prefix="syl-regions-")
    try:
        scratch = Path(temp.name)
        snapshot = scratch / ("recording" + recording.suffix.lower())
        with snapshot.open("xb") as output:
            digest = _read_recording(recording, cancelled, output)
        final = recording.stat()
        if (final.st_size, final.st_mtime_ns) != (initial.st_size, initial.st_mtime_ns):
            raise ValueError("The recording changed while opening the preview. Try again after it has finished writing.")
        source_info = stage._audio_header(snapshot)
        tiers = parse_annotations(data, kind, recording.name, recording_channels=source_info.channels)
        if not tiers:
            raise ValueError("The annotation file contains no interval tiers.")
        pcm, info = stage._audio_view(snapshot, digest, scratch, segment=False, cancelled=cancelled,
                                      source_info=source_info)
        settings = validate_bandpass(bandpass, info.samplerate)
        audition = pcm
        if settings:
            audition = scratch / "bandpass-preview.wav"
            filter_audio_file(pcm, audition, settings, cancelled=cancelled)
        stage._cancel(cancelled)
        return RecordingPreview(temp, recording, annotation, snapshot, pcm, data, digest, kind,
                                tiers, info.samplerate, info.frames, audition, settings,
                                recording_channels=source_info.channels)
    except BaseException:
        _discard_scratch(temp)
        raise


@dataclass
class _AnnotationEvidence:
    data: bytes
    manifest: dict

    def write(self, project_root):
        directory = project_root / "source_annotations"
        directory.mkdir()
        (directory / self.manifest["annotation_file"]).write_bytes(self.data)
        payload = (json.dumps(self.manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        (directory / "import.json").write_bytes(payload)
        return hashlib.sha256(payload).hexdigest()


def create_interval_project(preview, tier_index, include_empty, project_dir, progress=None, cancelled=None,
                            **contract_options):
    from .annotation_desktop_project import create_project

    requested = contract_options.pop("bandpass", preview.bandpass)
    if validate_bandpass(requested, preview.rate) != preview.bandpass:
        raise ValueError("The band-pass settings changed. Preview the recording again.")
    if Path(project_dir).expanduser().resolve().is_relative_to(Path(preview._temporary.name).resolve()):
        raise ValueError("Choose a permanent project folder outside the temporary recording preview.")
    regions = preview.regions(tier_index, include_empty)
    if not regions:
        raise ValueError("Choose a tier containing syllable intervals before creating the project.")
    try:
        changed = (preview._annotation.read_bytes() != preview._annotation_bytes or
                   _read_recording(preview._original, cancelled) != preview.recording_sha256)
    except OSError as exc:
        raise stage.StageAError("INPUT_CHANGED", "The recording or annotation file is unavailable. Choose the files again.") from exc
    if changed:
        raise stage.StageAError("INPUT_CHANGED", "The recording or annotations changed. Open a new preview.")
    min_bytes = sum(r.stop_frame - r.start_frame for r in regions) * 4 + preview._snapshot.stat().st_size
    if min_bytes > stage.LIMITS["max_total_audio_bytes"]:
        raise ValueError("The recording and selected syllables exceed the 2 GiB input limit.")
    with _clip_folder(preview._temporary.name) as folder:
        (folder / "recordings").mkdir()
        (folder / "clips").mkdir()
        parent = folder / "recordings" / preview.recording_name
        try:
            os.link(preview._snapshot, parent)
        except OSError:
            shutil.copyfile(preview._snapshot, parent)
        entries, mapping = [], []
        with sf.SoundFile(preview._pcm) as source:
            for index, region in enumerate(regions, 1):
                stage._cancel(cancelled)
                ident = f"syllable-{index:04d}"
                name = "clips/" + ident + ".wav"
                # Stage A tracks both file bytes and decoded samples. This writer
                # emits IEEE float WAV without a wall-clock timestamp in a PEAK chunk.
                wavfile.write(folder / name, preview.rate, _read_region(source, region))
                entries.append({"item_id": ident, "file": name, "parent_file": parent.relative_to(folder).as_posix(),
                                "source_frame_start": region.start_frame, "source_frame_stop": region.stop_frame})
                mapping.append({"item_id": ident, "source_id": region.source_id, "label": region.label,
                                "start_seconds": str(region.start_seconds), "end_seconds": str(region.end_seconds),
                                "start_frame": region.start_frame, "stop_frame": region.stop_frame})
                if index == 1 or index % 25 == 0 or index == len(regions):
                    stage._progress(progress, "freeze", "Reading selected recording intervals", index, len(regions))
        stage._write(folder / "segments.json", {"schema_version": stage.SOURCE_SCHEMA, "items": entries})
        evidence = _AnnotationEvidence(preview._annotation_bytes, {
            "schema": "syllafolio-recording-intervals-v1", "format": preview.kind,
            "recording_name": preview.recording_name, "recording_sha256": preview.recording_sha256,
            "sample_rate_hz": preview.rate, "recording_frames": preview.frames,
            **({"recording_channels": 2, "channel_mix": "mean_left_right"} if preview.recording_channels == 2 else {}),
            "annotation_name": preview.annotation_name,
            "annotation_file": "boundaries.TextGrid" if preview.kind == "textgrid" else "boundaries.txt",
            "annotation_sha256": hashlib.sha256(preview._annotation_bytes).hexdigest(),
            **({"audio_preprocessing": stage.bandpass_metadata(preview.bandpass, preview.rate)} if preview.bandpass else {}),
            "tier": preview.tiers[tier_index].name, "tier_index": tier_index,
            "include_empty_intervals": bool(include_empty), "intervals": mapping})
        return create_project(folder, project_dir, progress, cancelled, _annotation_evidence=evidence,
                              bandpass=preview.bandpass, **contract_options)
