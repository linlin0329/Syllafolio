"""流程a：冻结已分割音频、复用105D/log-mel提取和全局候选算法。

不读取历史类别、不创建人工成员关系、不访问模型。原目录只在导入时读取；
后续准备与验证使用冻结副本。低于原实现的12条阈值时明确保留全部未归簇。
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
from functools import lru_cache
import hashlib
import importlib
import importlib.metadata
import importlib.util
import json
from pathlib import Path
import re
import shutil
import sys
from tempfile import TemporaryDirectory
import types

import numpy as np
import soundfile as sf

from . import ei_b_assistance as assistance

SOURCE_SCHEMA = "annotation-segments-v1"
PROVENANCE_SCHEMA = "annotation-stage-a-provenance-v1"
PRESET_ID = "stage-a-existing-105d-umap10-hdbscan-leaf-v1"
LIMITS = {"max_items": 5000, "max_total_audio_bytes": 2 * 1024 * 1024 * 1024,
          "minimum_sample_rate_hz": 32000, "minimum_frames": 8,
          "maximum_segment_seconds": 30.0}
CODE_DIR = Path(__file__).resolve().parents[2] / "workflows/04_syllable_typing/code"
ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$")
INPUT_FORMATS = {".wav": "WAV", ".flac": "FLAC", ".mp3": "MP3", ".ogg": "Ogg Vorbis",
                 ".aif": "AIFF", ".aiff": "AIFF"}
FORMAT_NAMES = ", ".join(dict.fromkeys(INPUT_FORMATS.values()))
OTHER_AUDIO_EXTENSIONS = {".aac", ".m4a", ".wma", ".opus", ".amr", ".aifc", ".caf"}


class StageAError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def _require(condition, code, message):
    if not condition:
        raise StageAError(code, message)


def _sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _read(path):
    def pairs(values):
        result = {}
        for key, value in values:
            _require(key not in result, "SOURCE_JSON", "The JSON contains duplicate fields.")
            result[key] = value
        return result
    def invalid(_value):
        raise StageAError("SOURCE_JSON", "Non-finite numbers are not allowed in JSON.")
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"),
                          object_pairs_hook=pairs, parse_constant=invalid)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StageAError("SOURCE_JSON", "The input manifest could not be read.") from exc


def _write(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2,
                                    sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")


def _inside(root, relative):
    _require(isinstance(relative, str) and relative and not Path(relative).is_absolute()
             and ".." not in Path(relative).parts,
             "SOURCE_PATH", "Audio paths must be relative paths within the import directory.")
    path = root / relative
    _require(not path.is_symlink() and path.resolve().is_relative_to(root)
             and path.is_file(), "SOURCE_PATH", "The audio is missing, outside the import directory or a symbolic link.")
    _require(path.suffix.lower() in INPUT_FORMATS, "SOURCE_FORMAT",
             f"{path.name}: unsupported audio extension. Supported formats: {FORMAT_NAMES}.")
    return path.resolve()


def _audio_header(path):
    try:
        info = sf.info(path)
    except (OSError, RuntimeError) as exc:
        raise StageAError("INVALID_AUDIO", f"{path.name}: the audio could not be decoded.") from exc
    expected = INPUT_FORMATS[path.suffix.lower()]
    detected = {"WAVEX": "WAV", "OGG": "Ogg Vorbis"}.get(info.format, info.format)
    _require(detected == expected and (info.format != "OGG" or info.subtype == "VORBIS")
             and (info.format != "MP3" or info.subtype == "MPEG_LAYER_III"), "SOURCE_FORMAT",
             f"{path.name}: expected {expected} audio, found {info.format} ({info.subtype}).")
    _require(info.channels == 1, "AUDIO_CHANNELS",
             f"{path.name}: audio must be mono; channels are not combined automatically.")
    _require(info.samplerate >= LIMITS["minimum_sample_rate_hz"],
             "AUDIO_SAMPLE_RATE", "The fixed 1–15 kHz recipe requires a sample rate of at least 32000 Hz; audio is not resampled automatically.")
    return info


def _audio_view(path, digest, scratch, *, segment):
    info = _audio_header(path)
    if info.format == "MP3":
        # MPEG time indexes can estimate duration and seek to the wrong frame.
        # Use a sequential decode for both feature extraction and later review.
        from .annotation_audio_decode import decode_to_wave
        decoded = Path(scratch) / (digest + ".wav")
        if not decoded.exists():
            try:
                decode_to_wave(path, decoded, max_seconds=LIMITS["maximum_segment_seconds"] if segment else None)
            except (RuntimeError, ValueError, OSError) as exc:
                raise StageAError("INVALID_AUDIO", f"{path.name}: {exc}") from exc
        pcm_info = sf.info(decoded)
        _require(pcm_info.channels == info.channels and pcm_info.samplerate == info.samplerate,
                 "INVALID_AUDIO", f"{path.name}: decoded channel count or sample rate differs from the source.")
        path, info = decoded, pcm_info
    _require(info.frames >= LIMITS["minimum_frames"], "AUDIO_TOO_SHORT", "The audio is shorter than 8 frames.")
    if segment:
        _require(info.frames / info.samplerate <= LIMITS["maximum_segment_seconds"],
                 "AUDIO_TOO_LONG", "An input clip exceeds 30 seconds; segment it into syllables before importing.")
    return path, info


def _input_requirements(audio_files):
    formats = sorted({INPUT_FORMATS[path.suffix.lower()] for path in audio_files})
    return {"format": ", ".join(formats), "channels": 1, "same_sample_rate_required": True,
            "minimum_sample_rate_hz": LIMITS["minimum_sample_rate_hz"],
            "mfcc_logmel_frequency_band_hz": [1000, 15000],
            "automatic_resampling": False, "automatic_downmixing": False,
            "automatic_bandpass_filtering": False,
            "capacity_note": "The engineering input limits are 5000 items and 2 GiB of audio including parent files. Small-scale acceptance does not establish performance at these limits."}


def _pcm(path, start=0, stop=None):
    try:
        waveform, rate = sf.read(path, start=start, stop=stop, dtype="float32", always_2d=False)
    except (RuntimeError, OSError) as exc:
        raise StageAError("INVALID_AUDIO", f"{path.name}: the audio samples could not be read.") from exc
    _require(waveform.ndim == 1 and len(waveform) >= LIMITS["minimum_frames"]
             and np.isfinite(waveform).all(), "INVALID_AUDIO", "The audio is empty, contains non-finite samples or has an unsupported channel count.")
    digest = hashlib.sha256(np.asarray(waveform, dtype="<f4").tobytes()).hexdigest()
    return waveform, rate, digest


def _inspect(source_dir, scratch):
    root = Path(source_dir).expanduser().resolve()
    _require(root.is_dir(), "SOURCE_DIRECTORY", "The segmented-syllable directory does not exist.")
    descriptor = root / "segments.json"
    skipped_audio = []
    if descriptor.exists():
        data = _read(descriptor)
        _require(isinstance(data, dict) and set(data) == {"schema_version", "items"}
                 and data["schema_version"] == SOURCE_SCHEMA,
                 "SOURCE_SCHEMA", "segments.json must use annotation-segments-v1 and contain an items list.")
        items = data["items"]
    else:
        items = []
        for p in sorted(root.iterdir(), key=lambda p: p.name):
            if p.is_dir():
                continue
            suffix = p.suffix.lower()
            if suffix in INPUT_FORMATS:
                items.append({"file": p.name})
            elif suffix in OTHER_AUDIO_EXTENSIONS:
                skipped_audio.append(p.name)
        if not items and skipped_audio:
            raise StageAError("SOURCE_FORMAT", f"No supported audio files found. Supported formats: {FORMAT_NAMES}.")
    _require(isinstance(items, list) and 0 < len(items) <= LIMITS["max_items"],
             "SOURCE_COUNT", f"Provide 1–5000 segmented syllables in these formats: {FORMAT_NAMES}.")
    rows, seen_files, seen_ids, parent_ids, source_ids = [], set(), set(), {}, {}
    audio_files, rates = {}, set()
    def register_audio(path):
        if path not in audio_files:
            size = path.stat().st_size
            _require(sum(value["bytes"] for value in audio_files.values()) + size
                     <= LIMITS["max_total_audio_bytes"], "SOURCE_SIZE", "The engineering import limit is 2 GiB of audio, including parent files.")
            audio_files[path] = {"sha256": _sha(path), "bytes": size}
    fields = {"file", "item_id", "parent_file", "source_clip_id",
              "source_frame_start", "source_frame_stop", "raw_recording_id", "raw_recording_evidence"}
    parent_pcm = {}
    for index, entry in enumerate(items):
        _require(isinstance(entry, dict) and set(entry) <= fields and "file" in entry,
                 "SOURCE_FIELDS", "The syllable entry fields do not match; historical type labels are not imported.")
        raw_id, raw_evidence = entry.get("raw_recording_id"), entry.get("raw_recording_evidence", "")
        _require(raw_id is None or isinstance(raw_id, str) and ID_PATTERN.fullmatch(raw_id),
                 "SOURCE_ID", "Original-recording IDs must be path-free identifiers; they are not inferred from syllable files or parent-source IDs.")
        _require(isinstance(raw_evidence, str) and len(raw_evidence) <= 4000 and
                 (raw_id is None or bool(raw_evidence.strip())),
                 "SOURCE_EVIDENCE", "An original-recording declaration must include its source evidence in raw_recording_evidence.")
        path = _inside(root, entry["file"])
        _require(path not in seen_files, "DUPLICATE_SOURCE", "The same syllable file cannot be imported more than once.")
        seen_files.add(path)
        original_id = entry.get("item_id", f"input-{index + 1:04d}")
        _require(isinstance(original_id, str) and ID_PATTERN.fullmatch(original_id)
                 and original_id not in seen_ids, "DUPLICATE_ID", "Input syllable IDs must be valid and unique.")
        seen_ids.add(original_id)
        register_audio(path)
        pcm_path, info = _audio_view(path, audio_files[path]["sha256"], scratch, segment=True)
        samples, rate, pcm_sha = _pcm(pcm_path)
        _require(rate == info.samplerate and len(samples) == info.frames,
                 "INPUT_CHANGED", "The audio identity changed during reading.")
        rates.add(rate)
        has_parent = "parent_file" in entry
        if has_parent:
            _require(all(type(entry.get(k)) is int for k in ("source_frame_start", "source_frame_stop")),
                     "SOURCE_BOUNDARY", "Parent-source mappings require integer start and stop frames.")
            parent = _inside(root, entry["parent_file"])
            register_audio(parent)
            parent_pcm_path, parent_info = _audio_view(parent, audio_files[parent]["sha256"], scratch, segment=False)
            start, stop = entry["source_frame_start"], entry["source_frame_stop"]
            _require(parent_info.samplerate == rate and 0 <= start < stop <= parent_info.frames
                     and stop - start == len(samples), "SOURCE_BOUNDARY", "The parent sample rate or frame boundaries do not match the syllable.")
            parent_slice, _, parent_slice_pcm = _pcm(parent_pcm_path, start, stop)
            _require(np.array_equal(samples, parent_slice) and parent_slice_pcm == pcm_sha,
                     "PARENT_PCM_MISMATCH", "The syllable PCM does not match the declared parent-source interval.")
        else:
            _require(not any(k in entry for k in ("source_frame_start", "source_frame_stop", "source_clip_id")),
                     "SOURCE_BOUNDARY", "Source clips or frame boundaries cannot be supplied without a parent source.")
            parent, start, stop = path, 0, len(samples)
            parent_pcm_path = pcm_path
        if "source_clip_id" in entry:
            source_id = entry["source_clip_id"]
            _require(isinstance(source_id, str) and ID_PATTERN.fullmatch(source_id),
                     "SOURCE_ID", "The parent-source ID is invalid.")
            _require(source_id not in source_ids or source_ids[source_id] == parent,
                     "SOURCE_ID", "The same parent-source ID refers to different audio.")
            source_ids[source_id] = parent
        if parent not in parent_ids:
            parent_ids[parent] = f"parent-{len(parent_ids) + 1:04d}"
            from .annotation_source_registry import pcm_hash
            parent_pcm[parent] = pcm_hash(parent_pcm_path)
        rows.append({"item_id": f"syllable-{index + 1:04d}", "original_item_id": original_id,
                     "filename": path.name, "file": path.relative_to(root).as_posix(),
                     "path": path, "pcm_path": pcm_path, "parent_pcm_path": parent_pcm_path,
                     "segment_sha256": audio_files[path]["sha256"],
                     "decoded_pcm_sha256": pcm_sha, "frames": len(samples),
                     "sample_rate_hz": rate, "duration_s": len(samples) / rate,
                     "parent": parent, "parent_sha256": audio_files[parent]["sha256"],
                     "source_clip_id": parent_ids[parent], "source_frame_start": start,
                     "source_frame_stop": stop,
                     "parent_pcm_identity": parent_pcm[parent],
                     "segment_pcm_identity": _hash({"sample_rate_hz": rate, "frames": len(samples), "pcm_sha256": pcm_sha}),
                     "raw_recording_id": raw_id, "raw_recording_evidence": raw_evidence,
                     "context_kind": "source_parent" if has_parent else "segment_only"})
    _require(len(rates) == 1, "MIXED_SAMPLE_RATE", "All audio in a run must use the same sample rate; audio is not resampled automatically.")
    _require(sum(v["bytes"] for v in audio_files.values()) <= LIMITS["max_total_audio_bytes"],
             "SOURCE_SIZE", "The engineering import limit is 2 GiB of audio, including parent files.")
    source_files = {p.relative_to(root).as_posix(): v for p, v in audio_files.items()}
    from .annotation_source_registry import components, declaration_conflicts
    _require(not declaration_conflicts(components(rows), {r["item_id"]: r for r in rows}),
             "SOURCE_CONFLICT", "Items with the same parent source, identical parent PCM or duplicate syllable PCM cannot be declared as different original recordings.")
    receipt = root / "import_receipt.json"
    if receipt.exists():
        frozen = _read(receipt)
        _require(frozen.get("schema_version") == "annotation-stage-a-import-v1"
                 and frozen.get("files") == source_files
                 and frozen.get("segments_json_sha256") == _sha(descriptor),
                 "FROZEN_INPUT_CHANGED", "The frozen import has changed; create a new run version.")
    signature = [{k: row[k] for k in ("item_id", "segment_sha256", "decoded_pcm_sha256",
                  "parent_sha256", "source_clip_id", "source_frame_start", "source_frame_stop", "sample_rate_hz",
                  "context_kind")} for row in rows]
    for value, row in zip(signature, rows):
        if row["raw_recording_id"] is not None or row["raw_recording_evidence"]:
            value.update(raw_recording_id=row["raw_recording_id"], raw_recording_evidence=row["raw_recording_evidence"])
    duplicates = {}
    for row in rows:
        duplicates.setdefault(row["decoded_pcm_sha256"], []).append(row["item_id"])
    plan = {"schema_version": "annotation-stage-a-plan-v1", "status": "validated_input",
            "item_count": len(rows), "total_duration_s": sum(r["duration_s"] for r in rows),
            "sample_rate_hz": rates.pop(),
            "context_available_count": sum(r["context_kind"] == "source_parent" for r in rows),
            "audio_bytes": sum(v["bytes"] for v in audio_files.values()),
            "source_fingerprint": _hash(signature), "limits": copy.deepcopy(LIMITS),
            "input_requirements": _input_requirements(audio_files),
            "duplicate_pcm_groups": [v for v in duplicates.values() if len(v) > 1],
            "raw_recording_declarations": {"declared_items": sum(r["raw_recording_id"] is not None for r in rows),
                "unknown_items": sum(r["raw_recording_id"] is None for r in rows),
                "validation": "operator_declared_consistency_only_not_individual_independence"},
            "items": [{k: row[k] for k in ("item_id", "filename", "duration_s", "context_kind")}
                      for row in rows],
            "global_fit_scope": "all_imported_items", "preset_id": PRESET_ID,
            "parameters": copy.deepcopy(assistance.PARAMETERS), "experiment_enabled": False}
    if skipped_audio:
        plan["skipped_audio_files"] = skipped_audio
    return root, rows, source_files, plan


def inspect_source(source_dir):
    """核查已分割输入；解码暂存随检查结束清理，不改动原件或创建项目。"""
    with TemporaryDirectory(prefix="syl-audio-") as scratch:
        return _inspect(source_dir, scratch)[3]


def stage_source(source_dir, dest):
    """复制并冻结输入，之后无需原目录；禁止覆盖既有任务。"""
    with TemporaryDirectory(prefix="syl-audio-") as scratch:
        return _stage_source(source_dir, dest, scratch)


def _stage_source(source_dir, dest, scratch):
    root, rows, source_files, plan = _inspect(source_dir, scratch)
    dest = Path(dest).absolute()
    _require(not dest.exists() and not dest.resolve().is_relative_to(root),
             "OUTPUT_EXISTS", "Import output must be a new directory outside the source directory.")
    dest.mkdir(parents=True, exist_ok=False)
    (dest / "segments").mkdir()
    (dest / "parents").mkdir()
    entries, copied, mapping = [], {}, []
    for row in rows:
        segment_name = "segments/" + row["item_id"] + row["path"].suffix.lower()
        shutil.copyfile(row["path"], dest / segment_name)
        _require(_sha(dest / segment_name) == row["segment_sha256"], "INPUT_CHANGED", "A syllable file changed during import.")
        entry = {"file": segment_name, "item_id": row["item_id"]}
        if row["raw_recording_id"] is not None or row["raw_recording_evidence"]:
            entry.update(raw_recording_id=row["raw_recording_id"], raw_recording_evidence=row["raw_recording_evidence"])
        if row["context_kind"] == "source_parent":
            parent = row["parent"]
            if parent not in copied:
                copied[parent] = "parents/" + row["source_clip_id"] + parent.suffix.lower()
                shutil.copyfile(parent, dest / copied[parent])
                _require(_sha(dest / copied[parent]) == row["parent_sha256"],
                         "INPUT_CHANGED", "A parent-source file changed during import.")
            entry.update(parent_file=copied[parent], source_clip_id=row["source_clip_id"],
                         source_frame_start=row["source_frame_start"], source_frame_stop=row["source_frame_stop"])
        entries.append(entry)
        mapping.append({"item_id": row["item_id"], "original_item_id": row["original_item_id"],
                        "original_file": row["file"], "segment_sha256": row["segment_sha256"],
                        "decoded_pcm_sha256": row["decoded_pcm_sha256"]})
    for relative, value in source_files.items():
        _require(_sha(root / relative) == value["sha256"], "INPUT_CHANGED", "The source changed during import.")
    _write(dest / "segments.json", {"schema_version": SOURCE_SCHEMA, "items": entries})
    _, _, new_files, imported = _inspect(dest, scratch)
    _require(imported["source_fingerprint"] == plan["source_fingerprint"],
             "INPUT_IDENTITY", "The frozen import did not preserve the PCM, parent sources and frame boundaries.")
    _write(dest / "import_receipt.json", {"schema_version": "annotation-stage-a-import-v1",
           "source_fingerprint": plan["source_fingerprint"], "files": new_files,
           "segments_json_sha256": _sha(dest / "segments.json"),
           "created_at": datetime.now(timezone.utc).isoformat(), "original_source": str(root),
           "original_mapping": mapping, "original_files": source_files,
           "upstream_import": _read(root / "import_receipt.json")
           if (root / "import_receipt.json").is_file() else None})
    return _inspect(dest, scratch)[3]


@lru_cache(maxsize=1)
def _feature_functions():
    """从现行04源码加载原函数，不复制公式，也不执行历史分类／标签任务。"""
    package_name = "thesis_analysis._annotation_feature_agent"
    package = types.ModuleType(package_name)
    package.__path__ = [str(CODE_DIR / "agent")]
    sys.modules.setdefault(package_name, package)
    configuration = importlib.import_module(package_name + ".config")
    audio = importlib.import_module(package_name + ".audio_processor")
    module_name = "thesis_analysis._annotation_logmel_source"
    spec = importlib.util.spec_from_file_location(module_name, CODE_DIR / "prepare_unmapped_macro_review.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return configuration.Config, audio.AudioProcessor, module.log_mel_patch


def _cancel(callback):
    if callback and callback():
        raise StageAError("CANCELLED", "Import cancellation was requested; no complete candidate version was published.")


def _progress(callback, stage, message, completed=0, total=0):
    if callback:
        callback({"stage": stage, "message": message, "completed": completed, "total": total})


def _versions(fingerprint):
    suffix = fingerprint[:20]
    return {"material_version": "stage-a-material-" + suffix,
            "boundary_version": "stage-a-frames-" + suffix,
            "feature_version": "stage-a-existing105-logmel64x32-v1",
            "global_representation_version": "stage-a-global-" + suffix}


def _candidate_payload(scaled, labels, probabilities, ids, versions, fit_status):
    payload, matrices, mapping = assistance._organize(scaled, labels, probabilities, ids)
    retained = [i for i in range(105) if i not in assistance.EXCLUDED_INDICES]
    view = scaled[:, retained]
    lookup = {ident: i for i, ident in enumerate(ids)}
    for index, cluster in enumerate(payload["clusters"]):
        order = sorted(cluster["item_ids"], key=lambda ident: (
            float(np.linalg.norm(view[lookup[ident]] - matrices["prototypes_94d"][index])), ident))
        cluster.update(member_count=len(order), representative_ids=order[:3])
    payload.update(schema_version="annotation-global-candidates-v1", preset_id=PRESET_ID,
                   fit_status=fit_status, input_versions=versions, fit_item_ids=ids,
                   unclustered_ids=[ids[i] for i, label in enumerate(labels) if label < 0],
                   semantics="machine_candidates_not_human_types",
                   no_automatic_rejection=True, no_automatic_type_assignment=True)
    return payload, matrices, mapping


def prepare_stage_a(source_dir, output_dir, progress=None, cancelled=None, plan_only=False):
    """复用现有提取／聚类函数拟合全部输入，生成可接人工工作台的新版本。"""
    with TemporaryDirectory(prefix="syl-audio-") as scratch:
        return _prepare_stage_a(source_dir, output_dir, progress, cancelled, plan_only, scratch)


def _prepare_stage_a(source_dir, output_dir, progress, cancelled, plan_only, scratch):
    plan = _inspect(source_dir, scratch)[3]
    output = Path(output_dir).absolute()
    _require(not output.exists() and not output.resolve().is_relative_to(Path(source_dir).resolve()),
             "OUTPUT_EXISTS", "Candidate output must be a new run version outside the source directory.")
    if plan_only:
        return dict(plan, status="validated_stage_a_plan", output_dir=str(output))
    _cancel(cancelled)
    output.mkdir(parents=True, exist_ok=False)
    _progress(progress, "freeze", "Freezing the complete input", 0, plan["item_count"])
    frozen = output / "coordinator/source"
    _stage_source(source_dir, frozen, scratch)
    _, rows, _, frozen_plan = _inspect(frozen, scratch)
    _require(plan["source_fingerprint"] == frozen_plan["source_fingerprint"],
             "INPUT_CHANGED", "The planned and frozen inputs do not match.")
    dataset, global_dir = output / "dataset", output / "global"
    dataset.mkdir(); (dataset / "parents").mkdir(); global_dir.mkdir()
    ids = [r["item_id"] for r in rows]
    versions = _versions(plan["source_fingerprint"])
    _progress(progress, "features", "Initializing and extracting 105D and log-mel features", 0, len(rows))
    Config, Processor, log_mel_patch = _feature_functions()
    config = Config()
    config.audio.sample_rate = plan["sample_rate_hz"]
    config.feature.feature_types = ["mfcc", "mfcc_delta", "spectral", "temporal", "amplitude"]
    processor = Processor(config)
    _require(processor.get_feature_names() == assistance.FEATURE_NAMES,
             "FEATURE_LAYOUT", "The 105D fields of the shared feature extractor have changed.")
    features, patches, items, parent_map = [], [], [], {}
    for index, row in enumerate(rows):
        _cancel(cancelled)
        waveform, rate, pcm_sha = _pcm(row["pcm_path"])
        _require(pcm_sha == row["decoded_pcm_sha256"], "INPUT_CHANGED", "The frozen syllable PCM has changed.")
        try:
            vector = processor.extract_features(waveform, sr=rate)
            patch = log_mel_patch(waveform, rate).ravel()
        except Exception as exc:
            raise StageAError("FEATURE_EXTRACTION_FAILED", f"{row['item_id']}: feature extraction failed; the item was not removed.") from exc
        _require(vector.shape == (105,) and patch.shape == (2048,)
                 and np.isfinite(vector).all() and np.isfinite(patch).all(),
                 "FEATURE_EXTRACTION_FAILED", f"{row['item_id']}: the feature dimensions or values are invalid.")
        features.append(vector); patches.append(patch)
        if row["parent"] not in parent_map:
            media = row["parent_pcm_path"]
            name = "parents/" + row["source_clip_id"] + media.suffix.lower()
            media_sha = row["parent_sha256"] if media == row["parent"] else _sha(media)
            shutil.copyfile(media, dataset / name)
            _require(_sha(dataset / name) == media_sha, "INPUT_CHANGED", "The copied parent source does not match.")
            parent_map[row["parent"]] = name, media_sha
        name, media_sha = parent_map[row["parent"]]
        items.append({"item_id": row["item_id"], "feature_id": row["item_id"],
                      "feature_row_index": index, "source_clip_id": row["source_clip_id"],
                      "wav_path": name,
                      "source_frame_start": row["source_frame_start"],
                      "source_frame_stop": row["source_frame_stop"],
                      "sample_rate_hz": rate, "parent_audio_sha256": media_sha,
                      "context_kind": row["context_kind"],
                      "decoded_pcm_sha256": row["decoded_pcm_sha256"]})
        if row["raw_recording_id"] is not None or row["raw_recording_evidence"]:
            items[-1].update(raw_recording_id=row["raw_recording_id"], raw_recording_evidence=row["raw_recording_evidence"])
        _progress(progress, "features", "Using the shared 105D and log-mel extraction", index + 1, len(rows))
    features, patches = np.asarray(features), np.asarray(patches)
    np.save(dataset / "features.npy", features, allow_pickle=False)
    np.save(dataset / "logmel.npy", patches, allow_pickle=False)
    _cancel(cancelled)
    _progress(progress, "global_cluster", "Clustering initial global candidates from all syllables in this run", 0, len(rows))
    from sklearn.preprocessing import RobustScaler
    scaler = RobustScaler(quantile_range=(25.0, 75.0), with_centering=True,
                          with_scaling=True, unit_variance=False)
    # 既有prepare_assistance和工作台全局94D都以float64拟合缩放，保持同一表示。
    scaled = scaler.fit_transform(np.asarray(features, dtype=np.float64))
    embedding, labels, probabilities, fit_status = assistance._cluster(scaled)
    _cancel(cancelled)
    _require(embedding.shape in {(len(ids), 0), (len(ids), 10)}
             and labels.shape == probabilities.shape == (len(ids),)
             and labels.dtype.kind in "iu" and (labels >= -1).all()
             and all(np.isfinite(a).all() for a in (scaled, embedding, probabilities))
             and ((probabilities >= 0) & (probabilities <= 1)).all(),
             "CLUSTER_OUTPUT", "The clustering output is invalid; the failure was not substituted with a human type label.")
    _progress(progress, "organize", "Organizing microclusters, examples, prototype relationships and unclustered items", len(rows), len(rows))
    payload, matrices, mapping = _candidate_payload(scaled, labels, probabilities, ids, versions, fit_status)
    matrices.update(scaled_features_105d=scaled, embedding_10d=embedding,
                    cluster_labels=labels, membership_strength=probabilities,
                    robust_scaler_center=scaler.center_, robust_scaler_scale=scaler.scale_)
    for name, matrix in matrices.items():
        np.save(global_dir / (name + ".npy"), matrix, allow_pickle=False)
    _write(global_dir / "suggestions.json", payload)
    _write(output / "coordinator/prototype_mapping.json", mapping)
    manifest = {"schema_version": "annotation-acoustics-input-v1", **versions,
                "feature_names": assistance.FEATURE_NAMES, "feature_item_ids": ids,
                "global_fit_ids": ids, "items": items}
    source_paths = [Path(__file__), Path(__file__).with_name("annotation_source_registry.py"), Path(assistance.__file__), CODE_DIR / "agent/config.py",
                    CODE_DIR / "agent/audio_processor.py", CODE_DIR / "prepare_unmapped_macro_review.py"]
    packages = ["numpy", "scipy", "scikit-learn", "librosa", "soundfile", "umap-learn", "hdbscan"]
    if any(row["pcm_path"] != row["path"] or row["parent_pcm_path"] != row["parent"] for row in rows):
        source_paths.append(Path(__file__).with_name("annotation_audio_decode.py"))
        packages.append("PySide6")
    dependencies = {}
    for name in packages:
        try:
            dependencies[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            dependencies[name] = None
    provenance = {"schema_version": PROVENANCE_SCHEMA, "preset_id": PRESET_ID,
                  "created_at": datetime.now(timezone.utc).isoformat(),
                  "fit_status": fit_status, "fit_item_ids": ids, "fit_item_count": len(ids),
                  "candidate_clusters": len(payload["clusters"]),
                  "unclustered_items": len(payload["unclustered_ids"]),
                  "parameters": copy.deepcopy(assistance.PARAMETERS),
                  "input_versions": versions, "source_fingerprint": plan["source_fingerprint"],
                  "feature_parameters": {"audio": vars(config.audio), "feature": vars(config.feature),
                                         "zero_padding": "existing_extractors_pad_short_input_to_n_fft",
                                         "legacy_duration_filters_applied": False,
                                         "import_maximum_segment_seconds": LIMITS["maximum_segment_seconds"]},
                  "feature_reuse": "existing_AudioProcessor.extract_features_and_log_mel_patch",
                  "algorithm_reuse": "ei_b_assistance._cluster_and._organize",
                  "source_code_sha256": {p.relative_to(Path(__file__).resolve().parents[2]).as_posix(): _sha(p)
                                          for p in source_paths},
                  "dependency_versions": dependencies, "manifest_base": manifest,
                  "files": {p.relative_to(output).as_posix(): _sha(p) for p in sorted(output.rglob("*")) if p.is_file()},
                  "original_source_required_for_resume": False,
                  "human_decisions_created": 0, "type_memberships_created": 0,
                  "experiment_enabled": False, "formal_evaluation": False}
    _write(global_dir / "provenance.json", provenance)
    manifest["global_candidates"] = {
        "suggestions_path": "../global/suggestions.json", "suggestions_sha256": _sha(global_dir / "suggestions.json"),
        "provenance_path": "../global/provenance.json", "provenance_sha256": _sha(global_dir / "provenance.json")}
    _cancel(cancelled)
    _write(dataset / "manifest.json", manifest)
    _progress(progress, "validate", "Verifying the complete input, numeric artifacts and candidate coverage")
    result = _validate_stage_a(output, scratch)
    _progress(progress, "ready", "Initial candidates saved. Browse microclusters and unclustered items, then begin human review.", len(rows), len(rows))
    return result


def validate_stage_a(output_dir):
    """只读重验冻结来源、数值与覆盖；不重拟合UMAP/HDBSCAN、不回读外部输入。"""
    with TemporaryDirectory(prefix="syl-audio-") as scratch:
        return _validate_stage_a(output_dir, scratch)


def _validate_stage_a(output_dir, scratch):
    root = Path(output_dir).resolve()
    manifest_path, provenance_path = root / "dataset/manifest.json", root / "global/provenance.json"
    _require(all(p.is_file() and not p.is_symlink() for p in (
        manifest_path, provenance_path, root / "global/suggestions.json")),
        "OUTPUT_FILES", "The complete candidate manifest or provenance record is missing.")
    manifest, provenance = _read(manifest_path), _read(provenance_path)
    _require(provenance.get("schema_version") == PROVENANCE_SCHEMA
             and provenance.get("parameters") == assistance.PARAMETERS
             and provenance.get("preset_id") == PRESET_ID,
             "OUTPUT_VERSION", "The candidate recipe or provenance version does not match.")
    pointers = {"suggestions_path": "../global/suggestions.json",
                "suggestions_sha256": _sha(root / "global/suggestions.json"),
                "provenance_path": "../global/provenance.json", "provenance_sha256": _sha(provenance_path)}
    expected_manifest = dict(provenance["manifest_base"], global_candidates=pointers)
    _require(manifest == expected_manifest, "OUTPUT_MANIFEST", "The review input does not match the frozen candidate provenance.")
    files = provenance["files"]
    actual = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}
    _require(actual == set(files) | {"global/provenance.json", "dataset/manifest.json"},
             "OUTPUT_FILES", "Candidate artifacts contain unregistered or missing files.")
    for name, digest in files.items():
        path = root / name
        _require(not Path(name).is_absolute() and ".." not in Path(name).parts
                 and path.resolve().is_relative_to(root) and not path.is_symlink()
                 and _sha(path) == digest, "OUTPUT_HASH", "The candidate artifact SHA does not match.")
    frozen = _inspect(root / "coordinator/source", scratch)[3]
    _require(frozen["source_fingerprint"] == provenance["source_fingerprint"],
             "OUTPUT_IDENTITY", "The frozen original-input identity does not match.")
    ids = [row["item_id"] for row in manifest["items"]]
    _require(ids == manifest["global_fit_ids"] == manifest["feature_item_ids"]
             == provenance["fit_item_ids"] and len(ids) == len(set(ids)) == frozen["item_count"],
             "OUTPUT_IDENTITY", "The global fit members do not cover the complete input.")
    features = np.load(root / "dataset/features.npy", allow_pickle=False)
    patches = np.load(root / "dataset/logmel.npy", allow_pickle=False)
    arrays = {p.stem: np.load(p, allow_pickle=False) for p in (root / "global").glob("*.npy")}
    _require(features.shape == (len(ids), 105) and patches.shape == (len(ids), 2048)
             and all(np.isfinite(a).all() for a in [features, patches, *arrays.values()]),
             "OUTPUT_MATRIX", "The feature or candidate matrix has invalid dimensions or values.")
    scaled = arrays["scaled_features_105d"]
    expected_scaled = (features - arrays["robust_scaler_center"]) / arrays["robust_scaler_scale"]
    _require(scaled.shape == features.shape and np.allclose(scaled, expected_scaled, atol=1e-6, rtol=1e-6),
             "OUTPUT_SCALER", "The scaling parameters do not match the features.")
    labels, probabilities = arrays["cluster_labels"], arrays["membership_strength"]
    _require(labels.shape == probabilities.shape == (len(ids),) and labels.dtype.kind in "iu"
             and (labels >= -1).all() and ((probabilities >= 0) & (probabilities <= 1)).all(),
             "OUTPUT_CLUSTER", "The clustering identity or membership strength is invalid.")
    embedding = arrays["embedding_10d"]
    fit_status = provenance["fit_status"]
    small = len(ids) < assistance.PARAMETERS["small_batch"]["minimum_umap_rows"]
    _require((small and fit_status == "small_batch_no_embedding" and embedding.shape == (len(ids), 0)
              and (labels == -1).all() and (probabilities == 0).all())
             or (not small and fit_status == "fixed_umap_hdbscan" and embedding.shape == (len(ids), 10)),
             "OUTPUT_FIT_STATUS", "The recorded fit state does not match the sample count or embedding.")
    expected, matrices, mapping = _candidate_payload(scaled, labels, probabilities, ids,
                                                     provenance["input_versions"], fit_status)
    payload = _read(root / "global/suggestions.json")
    _require(payload == expected and _read(root / "coordinator/prototype_mapping.json") == mapping,
             "OUTPUT_CANDIDATES", "The candidate recommendation does not match the prototype values.")
    for name, value in matrices.items():
        _require(arrays[name].shape == value.shape and np.allclose(arrays[name], value),
                 "OUTPUT_CANDIDATES", "The prototype, neighbor and Ward matrices are inconsistent.")
    assigned = [ident for row in payload["clusters"] for ident in row["item_ids"]]
    remaining = payload["unclustered_ids"]
    _require(len(assigned + remaining) == len(set(assigned + remaining)) == len(ids)
             and set(assigned + remaining) == set(ids), "OUTPUT_COVERAGE", "Candidate groups and unclustered items do not form a complete, disjoint cover.")
    for row in manifest["items"]:
        path = (manifest_path.parent / row["wav_path"]).resolve()
        _require(path.is_relative_to(root / "dataset") and _sha(path) == row["parent_audio_sha256"],
                 "OUTPUT_MEDIA", "The parent-source media identity does not match.")
        _, rate, pcm_sha = _pcm(path, row["source_frame_start"], row["source_frame_stop"])
        _require(rate == row["sample_rate_hz"] and pcm_sha == row["decoded_pcm_sha256"],
                 "OUTPUT_MEDIA", "The review media PCM does not match the input syllable.")
    return {"status": "ready", "item_count": len(ids), "fit_item_count": len(ids),
            "candidate_clusters": len(payload["clusters"]), "unclustered_items": len(remaining),
            "fit_status": fit_status, "input_versions": provenance["input_versions"],
            "dataset_dir": str(root / "dataset"), "suggestions_path": str(root / "global/suggestions.json"),
            "source_fingerprint": frozen["source_fingerprint"], "preset_id": PRESET_ID,
            "sample_rate_hz": frozen["sample_rate_hz"], "total_duration_s": frozen["total_duration_s"],
            "context_available_count": frozen["context_available_count"],
            "limits": copy.deepcopy(LIMITS), "input_requirements": frozen["input_requirements"],
            "human_decisions_created": 0, "type_memberships_created": 0,
            "validation": "hash_identity_coverage_numeric_replay_no_umap_refit"}
