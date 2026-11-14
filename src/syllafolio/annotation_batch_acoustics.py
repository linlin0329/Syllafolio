"""批量审听的候选组织与保真拼接媒体；不写人工类型或原始音频。

Ward只剪切已冻结的94D原型树。局部聚类复用既有双视图固定配方，
随后逐子簇（包括未归簇）完整分页。显示归一化与音频PCM完全分离。
"""
from __future__ import annotations

import copy
from functools import lru_cache
import hashlib
from io import BytesIO
import math
import time
from pathlib import Path

import numpy as np
import soundfile as sf
from PIL import Image, ImageDraw, ImageFont
from matplotlib import colormaps
from scipy.cluster.hierarchy import fcluster, is_valid_linkage
from scipy.signal.windows import hann

from .annotation_acoustics import (
    AcousticError, _cancel, _hash, _ids, _read_json, _require, _sha, _write_json,
)
from .annotation_page_layout import MAX_PAGE_ITEMS, page_capacity, page_slices, validate_page_layout
from .annotation_family_acoustics import FAMILY_AUDITION_POLICY, FAMILY_ORDER_METHOD, family_similarity_order

VERSION = "annotation-batch-acoustics-v1"
DISPLAY_DEFAULTS = {"dynamic_range_db": 45, "window_samples": 512, "hop_samples": 96}
NORMALIZATIONS = {"per_item", "whole_page"}
GROUPING_MIN_SIZES = {"fine": 5, "microclusters": 10, "coarse": 20}


def validate_batch_display(settings=None):
    _require(settings is None or isinstance(settings, dict), "DISPLAY_SETTINGS", "Display settings must be an object.")
    settings = settings or {}
    _require(set(settings) <= set(DISPLAY_DEFAULTS), "DISPLAY_SETTINGS", "Unsupported concatenated-page display setting.")
    result = dict(DISPLAY_DEFAULTS, **settings)
    _require(all(type(v) in {int, float} and math.isfinite(v) and int(v) == v
                 for v in result.values()), "DISPLAY_SETTINGS", "Display settings must be finite integers.")
    result = {k: int(v) for k, v in result.items()}
    _require(20 <= result["dynamic_range_db"] <= 100 and
             result["window_samples"] in {256, 512, 1024, 2048} and
             32 <= result["hop_samples"] <= min(512, result["window_samples"]),
             "DISPLAY_SETTINGS", "The display settings are outside the allowed range.")
    return result


def _coverage(groups, expected):
    actual = [i for row in groups for i in row["item_ids"]]
    _require(len(actual) == len(set(actual)) == len(expected) and set(actual) == set(expected),
             "BATCH_COVERAGE", "The candidate organization does not cover all targets completely and without overlap.")


@lru_cache(maxsize=4)
def _partition_labels(frozen_embedding, item_count, parameters):
    """Use the same verified embedding on preview and apply; cache no project or review state."""
    import hdbscan
    embedding = np.load(BytesIO(frozen_embedding), allow_pickle=False)
    _require(embedding.shape == (item_count, 10) and np.isfinite(embedding).all(),
             "GROUPING_EMBEDDING", "The saved global embedding does not match the complete syllable list.")
    return tuple(map(int, hdbscan.HDBSCAN(**dict(parameters)).fit_predict(embedding)))


def _alternative_partition(root, provenance, ids, mode, source_hashes):
    _require(provenance["fit_status"] == "fixed_umap_hdbscan", "GROUPING_UNAVAILABLE",
             "This project has no fitted global embedding. Only the original grouping is available.")
    _require(provenance["fit_item_ids"] == ids, "GROUPING_EMBEDDING",
             "The saved embedding has a different syllable order.")
    relative = "global/embedding_10d.npy"
    path = root / relative
    _require(path.is_file() and not path.is_symlink(), "GROUPING_EMBEDDING",
             "The saved global embedding is missing.")
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    _require(digest == provenance["files"].get(relative), "GLOBAL_CANDIDATES_CHANGED",
             "The saved global embedding has changed.")
    parameters = dict(provenance["parameters"]["hdbscan"], min_cluster_size=GROUPING_MIN_SIZES[mode])
    labels = _partition_labels(raw, len(ids), tuple(sorted(parameters.items())))
    grouped, noise = {}, []
    for item, label in zip(ids, labels, strict=True):
        if label < 0:
            noise.append(item)
        else:
            grouped.setdefault(label, []).append(item)
    groups = []
    for index, members in enumerate(sorted(sorted(items) for items in grouped.values()), 1):
        groups.append({"group_id": "group-" + _hash([mode, members])[:16], "label": f"c_{index:04d}",
                       "item_ids": members, "source_cluster_ids": [], "kind": "microcluster"})
    receipt = {"granularity": mode, "hdbscan_parameters": parameters, "embedding_sha256": digest,
               "partition_sha256": _hash([labels, ids]), "embedding_refitted": False}
    source_hashes.update(receipt)
    return groups, sorted(noise), receipt


def ward_preview(dataset_dir, global_candidates, mode="microclusters", height=None, *, protocol_version="0.5"):
    """Preview the frozen groups or Ward tree, or fit an explicit density preset on the saved embedding."""
    _require(protocol_version in {"0.4", "0.5"}, "BATCH_PROTOCOL_VERSION", "Unsupported batch protocol version.")
    _require(mode in {"auto", "microclusters", "skip", "ward", "fine", "coarse"}, "ORGANIZATION_MODE", "Unknown candidate organization mode.")
    mode = "microclusters" if mode == "skip" else mode
    requested_mode = mode
    directory = Path(dataset_dir).resolve()
    manifest = _read_json(directory / "manifest.json")
    ids = _ids([row["item_id"] for row in manifest["items"]], "items", nonempty=True)
    _require(isinstance(global_candidates, dict), "GLOBAL_CANDIDATES_INVALID", "The candidate view must be an object.")
    available = global_candidates.get("available", False)
    clusters, remaining, tree = [], sorted(ids), np.empty((0, 4))
    hashes = {"manifest": _sha(directory / "manifest.json")}
    prototype_ids = []
    if available:
        link = manifest.get("global_candidates", {})
        root = directory.parent
        files = {}
        for name in ("suggestions", "provenance"):
            path = root / "global" / (name + ".json")
            _require(path.is_file() and not path.is_symlink() and
                     _sha(path) == link.get(name + "_sha256"),
                     "GLOBAL_CANDIDATES_CHANGED", "The candidate provenance fingerprint does not match.")
            files[name] = _read_json(path)
            hashes[name] = _sha(path)
        saved = files["suggestions"]
        clusters = saved["clusters"]
        _require(global_candidates.get("clusters") == clusters and
                 global_candidates.get("unclustered_ids", []) == saved.get("unclustered_ids", []),
                 "GLOBAL_CANDIDATES_CHANGED", "The supplied candidate view is not the current frozen version.")
        remaining = sorted(saved.get("unclustered_ids", []))
        mapping_path = root / "coordinator/prototype_mapping.json"
        tree_path = root / "global/ward_linkage_94d.npy"
        for path in (mapping_path, tree_path, root / "global/prototypes_94d.npy"):
            relative = path.relative_to(root).as_posix()
            _require(path.is_file() and not path.is_symlink() and
                     files["provenance"].get("files", {}).get(relative) == _sha(path),
                     "GLOBAL_CANDIDATES_CHANGED", "The frozen prototype or Ward-tree fingerprint has changed.")
            hashes[relative] = _sha(path)
        mapping = _read_json(mapping_path)
        _require(sorted(row["prototype_row_index"] for row in mapping) == list(range(len(clusters))),
                 "PROTOTYPE_MAPPING", "The prototype row identities are incomplete.")
        prototype_ids = [row["candidate_cluster_id"] for row in
                         sorted(mapping, key=lambda row: row["prototype_row_index"])]
        _require(len(prototype_ids) == len(set(prototype_ids)) and
                 set(prototype_ids) == {row["candidate_cluster_id"] for row in clusters},
                 "PROTOTYPE_MAPPING", "The prototype rows do not match the microcluster identities.")
        tree = np.load(tree_path, allow_pickle=False)
        prototypes = np.load(root / "global/prototypes_94d.npy", allow_pickle=False)
        _require(prototypes.shape == (len(clusters), 94) and np.isfinite(prototypes).all() and
                 tree.shape == (max(0, len(clusters) - 1), 4) and np.isfinite(tree).all(),
                 "WARD_TREE", "The prototype or Ward-tree structure is invalid.")
        if len(tree):
            _require(is_valid_linkage(tree) and np.all(np.diff(tree[:, 2]) >= -1e-10),
                     "WARD_TREE", "The Ward tree is invalid or its heights are not monotonic.")
    elif manifest.get("global_candidates") is not None:
        raise AcousticError("GLOBAL_CANDIDATES_UNAVAILABLE", "The registered global result is unavailable; it cannot be replaced silently.")
    alternative = mode in {"fine", "coarse"}
    if alternative:
        _require(available, "GROUPING_UNAVAILABLE", "This project has no saved global embedding.")
    heights = sorted({0.0, *(float(v) for v in tree[:, 2])})
    ward = {"available": len(clusters) >= 2, "heights": heights,
            "min_height": 0.0, "max_height": heights[-1],
            "semantics": "Ward merge height; not maximum pairwise prototype distance",
            "prototype": "coordinate_median_scaled94", "prototype_weight": "equal"}
    from .annotation_ward_recommendation import recommend_ward
    lookup = {row["candidate_cluster_id"]: row for row in clusters}
    recommendation = {}
    if not alternative:
        recommendation = recommend_ward(tree, [len(lookup[cid]["item_ids"]) for cid in prototype_ids])
        recommendation["source_fingerprint"] = _hash(hashes)
        recommendation["recommendation_id"] = "ward-rec-" + _hash(recommendation)[:24]
    if mode == "auto":
        _require(height is None, "WARD_HEIGHT", "Automatic recommendation does not accept a hidden manual height.")
        mode, height = recommendation["recommended_mode"], recommendation["height"]
    groups = []
    extra = {}
    if alternative:
        _require(height is None, "WARD_HEIGHT", "A clustering preset does not accept a Ward merge height.")
        groups, remaining, extra = _alternative_partition(root, files["provenance"], ids, mode, hashes)
        ward = {"available": False, "heights": [], "min_height": 0.0, "max_height": 0.0}
    elif mode == "ward":
        _require(ward["available"], "WARD_UNAVAILABLE", "Ward aggregation requires at least two valid microclusters.")
        _require(type(height) in {int, float} and math.isfinite(height) and
                 0 <= height <= ward["max_height"], "WARD_HEIGHT", "Explicitly choose a merge height within this batch tree.")
        labels = fcluster(tree, t=float(height), criterion="distance")
        lookup = {row["candidate_cluster_id"]: row for row in clusters}
        buckets = {}
        for cid, label in zip(prototype_ids, labels):
            buckets.setdefault(int(label), []).append(cid)
        for index, source_ids in enumerate(sorted(buckets.values(), key=lambda v: min(v)), 1):
            members = sorted(i for cid in source_ids for i in lookup[cid]["item_ids"])
            groups.append({"group_id": "group-" + _hash(sorted(source_ids))[:16],
                           "label": f"Candidate group {index}", "item_ids": members,
                           "source_cluster_ids": sorted(source_ids), "kind": "ward_candidate"})
    else:
        _require(height is None, "WARD_HEIGHT", "Skipping aggregation does not accept a hidden height parameter.")
        for row in sorted(clusters, key=lambda value: value["candidate_cluster_id"]):
            cid = row["candidate_cluster_id"]
            groups.append({"group_id": "group-" + _hash([cid])[:16], "label": cid,
                           "item_ids": sorted(row["item_ids"]), "source_cluster_ids": [cid],
                           "kind": "microcluster"})
    microcluster_count = len(groups) if alternative else len(clusters)
    if remaining and protocol_version == "0.4":
        groups.append({"group_id": "machine-unclustered", "label": "Unclustered items",
                       "item_ids": remaining, "source_cluster_ids": [], "kind": "unclustered"})
    coverage = groups + ([{"item_ids": remaining}] if protocol_version == "0.5" else [])
    _coverage(coverage, ids)
    return {"schema_version": VERSION, "protocol_version": protocol_version,
            "requested_mode": requested_mode, "recommendation": recommendation,
            "recommendation_used": requested_mode == "auto",
            "algorithmic_unclustered_ids": remaining if protocol_version == "0.5" else [], "mode": mode, "height": float(height) if height is not None else None,
            "groups": groups, "ward": ward, "source_fingerprint": _hash(hashes),
            "input_item_count": len(ids), "microcluster_count": microcluster_count,
            "unclustered_count": len(remaining), "new_fit": alternative,
            "is_human_classification": False, **extra}


def _fonts():
    try:
        return ImageFont.truetype("DejaVuSans.ttf", 21), ImageFont.truetype("DejaVuSans.ttf", 17)
    except OSError:
        return ImageFont.load_default(), ImageFont.load_default()


def _spectral_columns(samples, rate, params, frame_indices, check_cancelled):
    """精确扫描全片段峰值；显示列按真正STFT帧抽取，分块限制中间内存。"""
    win, hop = params["window_samples"], params["hop_samples"]
    frequencies = np.fft.rfftfreq(win * 2, d=1 / rate)
    mask = frequencies <= min(16000, rate / 2)
    count = max(0, (len(samples) - win) // hop + 1)
    selected = np.empty((int(mask.sum()), len(frame_indices)), dtype=np.float64)
    peak = 0.0
    window = hann(win, sym=False)
    if not count:
        return selected, peak, count
    frames = np.lib.stride_tricks.sliding_window_view(samples, win)[::hop]
    for start in range(0, count, 512):
        _cancel(check_cancelled)
        end = min(count, start + 512)
        power = np.abs(np.fft.rfft(frames[start:end] * window, n=win * 2, axis=1)[:, mask]) ** 2
        peak = max(peak, float(power.max(initial=0)))
        positions = np.flatnonzero((frame_indices >= start) & (frame_indices < end))
        if len(positions):
            selected[:, positions] = power[frame_indices[positions] - start].T
    return selected, peak, count


def _waveform_envelope(path, frame_count, columns, check_cancelled):
    edges = np.linspace(0, frame_count, columns + 1, dtype=int)
    extrema = np.empty((columns, 2), dtype=np.float32)
    with sf.SoundFile(path) as stream:
        for first in range(0, columns, 128):
            _cancel(check_cancelled)
            end = min(first + 128, columns)
            start_frame = edges[first]
            stream.seek(start_frame)
            stop_frame = min(frame_count, max(edges[end], edges[end - 1] + 1))
            block = stream.read(int(stop_frame - start_frame), dtype="float32")
            _require(np.isfinite(block).all(), "INVALID_MEDIA", "Audition audio contains non-finite samples.")
            for i in range(first, end):
                a, b = edges[i] - start_frame, edges[i + 1] - start_frame
                samples = block[a:max(a + 1, b)]
                extrema[i] = samples.min(), samples.max()
    return extrema


def render_page_display(audio_path, slots, output_dir, settings=None,
                        normalization="per_item", check_cancelled=None, *, page_layout=None):
    """渲染整个拼接时间轴；不变更WAV。返回路径相对output_dir。"""
    params = validate_batch_display(settings)
    _require(normalization in NORMALIZATIONS, "NORMALIZATION", "Choose per-item or whole-page normalization explicitly.")
    _require(isinstance(slots, list) and 1 <= len(slots) <= MAX_PAGE_ITEMS,
             "PAGE_SIZE", f"An audition page must contain 1–{MAX_PAGE_ITEMS} slots.")
    source = Path(audio_path)
    before = _sha(source)
    header = sf.info(source)
    frame_count, rate = header.frames, header.samplerate
    _require(header.channels == 1 and frame_count > 0, "INVALID_MEDIA", "Audition audio must be non-empty and mono.")
    prior_stop = 0
    for number, row in enumerate(slots, 1):
        start, stop = row.get("start_frame"), row.get("stop_frame")
        _require(type(start) is int and type(stop) is int and prior_stop <= start < stop <= frame_count,
                 "SLOT_BOUNDARY", "The page slot boundaries are incomplete or overlapping.")
        _require(row.get("slot") == number, "SLOT_BOUNDARY", "Page slot numbers must be consecutive, starting at 1.")
        prior_stop = stop
    _require(slots[0]["start_frame"] == 0 and prior_stop == frame_count,
             "SLOT_BOUNDARY", "The page cannot contain unregistered audio before its first slot or after its last slot.")
    duration = frame_count / rate
    pixels_per_second = 350
    if page_layout is not None:
        page_layout = validate_page_layout(page_layout)
        pixels_per_second = max(pixels_per_second, 1000 / page_layout["ms_per_pixel"])
    width, height, wave_height = min(6000, max(2000, int(math.ceil(duration * pixels_per_second)))), 500, 190
    left, right, top, bottom = 86, 24, 34, 64
    pw, ph = width - left - right, height - top - bottom
    x_times = (np.arange(pw) + .5) * duration / pw
    max_hz = min(16000, rate / 2)
    freq_indices = np.clip(np.floor((1 - (np.arange(ph) + .5) / ph) * max_hz *
                                  (params["window_samples"] * 2) / rate + .5).astype(int),
                           0, int(np.floor(max_hz * (params["window_samples"] * 2) / rate)))
    panels, peaks, incomplete = [], [], []
    for row in slots:
        _cancel(check_cancelled)
        y, _ = sf.read(source, start=row["start_frame"], stop=row["stop_frame"], dtype="float32")
        count = max(0, (len(y) - params["window_samples"]) // params["hop_samples"] + 1)
        offset = row["start_frame"] / rate
        t0 = offset + (params["window_samples"] - params["hop_samples"]) / (2 * rate)
        columns = np.floor((x_times - t0) * rate / params["hop_samples"]).astype(int)
        valid = np.flatnonzero((columns >= 0) & (columns < count))
        power, peak, _ = _spectral_columns(y, rate, params, columns[valid], check_cancelled)
        panels.append((valid, power, peak))
        peaks.append(peak)
        if not count:
            incomplete.append(row["item_id"])
    rgb = np.full((ph, pw, 3), 255, dtype=np.uint8)
    for columns, power, peak in panels:
        reference = peak if normalization == "per_item" else max(peaks, default=0)
        if len(columns) and reference > 0:
            db = np.clip(10 * np.log10(np.maximum(power[freq_indices] / reference,
                                                np.finfo(float).tiny)), -params["dynamic_range_db"], 0)
            rgb[:, columns] = colormaps["Greys"]((db + params["dynamic_range_db"]) /
                                                params["dynamic_range_db"], bytes=True)[:, :, :3]
    font, small = _fonts()
    spectrum = Image.new("RGB", (width, height), "white")
    spectrum.paste(Image.fromarray(rgb), (left, top))
    draw = ImageDraw.Draw(spectrum)
    draw.rectangle((left, top, left + pw, top + ph), outline="#4a565c", width=1)
    for fraction in (0, .25, .5, .75, 1):
        y = top + int((1 - fraction) * ph)
        draw.text((left - 10, y), f"{fraction * max_hz / 1000:g}", font=small, fill="#24343b", anchor="rm")
    draw.text((left, 3), "Frequency (kHz)", font=small, fill="#24343b")
    reference_label = "Per-syllable peak" if normalization == "per_item" else "Whole-page peak"
    draw.text((width - right, 3), f"-{params['dynamic_range_db']} to 0 dB / {reference_label}",
              font=small, fill="#57676e", anchor="rt")
    wave = Image.new("RGB", (width, wave_height), "white")
    wd = ImageDraw.Draw(wave)
    wave_top, wave_bottom = 26, 40
    wave_ph = wave_height - wave_top - wave_bottom
    center = wave_top + wave_ph / 2
    wd.line((left, center, left + pw, center), fill="#d6dfe1")
    extrema = _waveform_envelope(source, frame_count, pw, check_cancelled)
    amplitude = max(1.0, float(np.abs(extrema).max()))
    for i, (low, high) in enumerate(extrema):
        wd.line((left + i, center - float(high) / amplitude * wave_ph / 2,
                 left + i, center - float(low) / amplitude * wave_ph / 2), fill="#27777b")
    wd.text((left, 2), "Amplitude", font=small, fill="#24343b")
    for target, drawer, plot_top, plot_bottom in (
            (spectrum, draw, top, top + ph), (wave, wd, wave_top, wave_top + wave_ph)):
        for row in slots:
            start = left + pw * row["start_frame"] / frame_count
            stop = left + pw * row["stop_frame"] / frame_count
            drawer.line((start, plot_top, start, plot_bottom), fill="#a3bfc0", width=1)
            drawer.text(((start + stop) / 2, plot_top + 4), str(row["slot"]),
                        font=small, fill="#11636a", anchor="mt")
        for fraction in (0, .2, .4, .6, .8, 1):
            x = left + fraction * pw
            drawer.text((x, plot_bottom + 9), f"{fraction * duration:.2f}",
                        font=small, fill="#24343b", anchor="mt")
        drawer.text((left + pw / 2, target.height - 2), "Time (s)", font=small,
                    fill="#24343b", anchor="mb")
    if incomplete:
        draw.text((left + pw - 8, top + ph - 8), "Some slots: no complete STFT window",
                  font=small, fill="#9c571c", anchor="rb")
    _cancel(check_cancelled)
    _require(_sha(source) == before, "MEDIA_CHANGED", "The audition audio changed during display rendering.")
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    identity = _hash({"version": VERSION, "audio_sha256": before, "slots": slots,
                      "settings": params, "normalization": normalization,
                      **({"ms_per_pixel": page_layout["ms_per_pixel"]} if page_layout else {})})[:24]
    names = {"waveform_path": "waveform-" + identity + ".png",
             "spectrogram_path": "spectrogram-" + identity + ".png"}
    for key, img in (("waveform_path", wave), ("spectrogram_path", spectrum)):
        path = out / names[key]
        _require(not path.exists(), "OUTPUT_EXISTS", "The display output already exists; historical media cannot be overwritten.")
        img.save(path)
    display = {"renderer": VERSION, "parameters": dict(params, fft_samples=2 * params["window_samples"]),
               "normalization": normalization, "frequency_max_hz": max_hz,
               "duration_s": duration, "duration_seconds": duration, "time_max_s": duration,
               "view_cropped": False, "endpoint_padding": False,
               "stft_scope": "each original syllable independently; artificial silence shown blank",
               "shorter_than_window_ids": incomplete, "audio_processing": "none",
               "width_px": width, "height_px": height,
               "plot_bounds": {"left": left / width, "right": (left + pw) / width,
                               "top": top / height, "bottom": (top + ph) / height},
               "waveform_plot_bounds": {"left": left / width, "right": (left + pw) / width,
                                        "top": wave_top / wave_height,
                                        "bottom": (wave_top + wave_ph) / wave_height}}
    artifacts = {name: {"path": name, "sha256": _sha(out / name)} for name in names.values()}
    return dict(names, display=display, artifacts=artifacts, audio_sha256=before)


def _write_page_audio(dataset, page_ids, audio, gap_frames, check_cancelled):
    """Write one syllable at a time, verifying its round-trip PCM including gaps."""
    rate = dataset.sample_rate_hz
    silence = np.zeros(gap_frames, dtype="<f4")
    expected = hashlib.sha256()
    slots = []
    with sf.SoundFile(audio, mode="x", channels=1, samplerate=rate, subtype="FLOAT") as stream:
        for number, ident in enumerate(page_ids, 1):
            _cancel(check_cancelled)
            if number > 1:
                stream.write(silence)
                expected.update(silence)
            samples, actual_rate, source = dataset.read_audio(ident)
            _require(actual_rate == rate, "MIXED_SAMPLE_RATE", "Concatenation does not resample audio automatically.")
            samples = np.asarray(samples, dtype="<f4", order="C")
            offset = stream.tell()
            stream.write(samples)
            expected.update(samples)
            stop = stream.tell()
            row = dataset._items[ident]
            slots.append({"slot": number, "item_id": ident, "start_frame": offset, "stop_frame": stop,
                          "start_seconds": offset / rate, "stop_seconds": stop / rate,
                          "source_clip_id": source["source_clip_id"],
                          "source_frame_start": row["source_frame_start"],
                          "source_frame_stop": row["source_frame_stop"], "sample_rate_hz": rate,
                          "parent_audio_sha256": dataset._parents[row["source_clip_id"]]["sha256"]})
    decoded = hashlib.sha256()
    with sf.SoundFile(audio) as stream:
        _require(stream.samplerate == rate and stream.frames == stop, "PCM_MISMATCH",
                 "The decoded audition WAV has different frame timing.")
        for block in stream.blocks(blocksize=65536, dtype="float32"):
            _cancel(check_cancelled)
            decoded.update(np.asarray(block, dtype="<f4", order="C"))
    _require(decoded.digest() == expected.digest(), "PCM_MISMATCH",
             "The decoded audition WAV does not match the original syllables and inserted silence.")
    return slots


def prepare_review(dataset, item_ids, output_dir, route, gap_seconds, normalization,
                   display=None, check_cancelled=None, *, page_layout=None):
    """Prepare listening pages within the existing acoustic groups and frozen layout."""
    ids = dataset._select(item_ids, "item_ids", nonempty=True)
    ids = sorted(ids)
    _require(route in {"direct", "cluster", "direct_exception", "family_order", "family_cluster", "pool_cluster"}, "REVIEW_ROUTE", "Unknown audition organization route.")
    _require(type(gap_seconds) in {int, float} and math.isfinite(gap_seconds) and .05 <= gap_seconds <= 2,
             "SILENCE_GAP", "Explicitly set the inter-syllable silence to 0.05–2 seconds.")
    _require(normalization in NORMALIZATIONS, "NORMALIZATION", "Choose per-item or whole-page display normalization explicitly.")
    params = validate_batch_display(display)
    if page_layout is not None:
        page_layout = validate_page_layout(page_layout)
    _cancel(check_cancelled)
    out = Path(output_dir)
    _require(not out.exists(), "OUTPUT_EXISTS", "The audition output already exists; use a new organization version.")
    out.mkdir(parents=True)
    started = time.monotonic()
    cluster_seconds = 0.0
    order_seconds = 0.0
    local = None
    try:
        if route in {"cluster", "family_cluster", "pool_cluster"}:
            t0 = time.monotonic()
            try:
                if route in {"family_cluster", "pool_cluster"} and len(ids) < dataset.preset["local"]["minimum_recluster_rows"]:
                    from .annotation_family_acoustics import prepare_small_family, prepare_small_pool
                    prepare_small = prepare_small_pool if route == "pool_cluster" else prepare_small_family
                    local = prepare_small(dataset, ids, out / "local", check_cancelled=check_cancelled)
                else:
                    local = dataset.run_local("local_recluster", ids, [], out / "local", check_cancelled=check_cancelled)
                cluster_seconds = time.monotonic() - t0
                assignments = {row["item_id"]: row["candidate_cluster_id"] for row in local["assignments"]}
                _require(set(assignments) == set(ids) and len(local["assignments"]) == len(ids),
                         "BATCH_COVERAGE", "Local clustering does not cover the complete fit set.")
                order = local["ordered_ids"]
                _require(len(order) == len(set(order)) == len(ids) and set(order) == set(ids),
                         "BATCH_COVERAGE", "The local ordering omits or duplicates members.")
                buckets = {}
                for ident in order:
                    buckets.setdefault(assignments[ident] or "unclustered", []).append(ident)
                buckets = sorted(buckets.items(), key=lambda row: (row[0] == "unclustered", row[0]))
            except Exception as exc:
                # 只有实际调用聚类这一阶段的失败可供后端核对例外。
                # 取消另行处理；后续拼接/显示失败不能冒充聚类不适用。
                if route == "cluster" or len(ids) >= dataset.preset["local"]["minimum_recluster_rows"]:
                    exc.batch_failure_phase = "clustering"
                raise
        elif route == "family_order":
            t0 = time.monotonic()
            order = family_similarity_order(dataset, ids, check_cancelled)
            order_seconds = time.monotonic() - t0
            buckets = [("ordered", order)]
        else:
            buckets = [("direct", ids)]
        pages, artifacts = [], {}
        rate = dataset.sample_rate_hz
        gap_frames = int(round(float(gap_seconds) * rate))
        _require(gap_frames > 0, "SILENCE_GAP", "The silence is shorter than one sample frame.")
        for subcluster_id, members in buckets:
            frames = [dataset._items[i]["source_frame_stop"] - dataset._items[i]["source_frame_start"] for i in members]
            for selection in page_slices(frames, rate, gap_frames, page_layout):
                _cancel(check_cancelled)
                page_ids = members[selection]
                page_id = f"page-{len(pages) + 1:04d}"
                folder = out / page_id
                folder.mkdir()
                audio = folder / "review.wav"
                slots = _write_page_audio(dataset, page_ids, audio, gap_frames, check_cancelled)
                frame_count = slots[-1]["stop_frame"]
                rendered = render_page_display(audio, slots, folder, params, normalization,
                                               check_cancelled=check_cancelled, page_layout=page_layout)
                page = {"page_id": page_id, "subcluster_id": subcluster_id, "item_ids": page_ids,
                        "slots": slots, "sample_rate_hz": rate, "duration_s": frame_count / rate,
                        "duration_seconds": frame_count / rate, "frame_count": frame_count,
                        "audio_path": f"{page_id}/review.wav", "audio_sha256": _sha(audio),
                        "waveform_path": f"{page_id}/" + rendered["waveform_path"],
                        "spectrogram_path": f"{page_id}/" + rendered["spectrogram_path"],
                        "display": rendered["display"]}
                pages.append(page)
                if page_layout is not None:
                    page["page_layout"] = page_layout.copy()
                for path in (page["audio_path"], page["waveform_path"], page["spectrogram_path"]):
                    artifacts[path] = {"path": path, "sha256": _sha(out / path)}
                _write_json(folder / "page.json", page)
                artifacts[f"{page_id}/page.json"] = {"path": f"{page_id}/page.json", "sha256": _sha(folder / "page.json")}
        _coverage(pages, ids)
        result = {"schema_version": VERSION, "status": "succeeded", "route": route,
                  "clustering_performed": route == "cluster" or route in {"family_cluster", "pool_cluster"} and len(ids) > 1,
                  "fit_ids": local.get("fit_ids", ids) if local else [],
                  "item_ids": ids, "input_fingerprint": dataset.input_fingerprint,
                  "input_versions": copy.deepcopy(dataset.input_versions), "pages": pages,
                  "local_result_id": local["result_id"] if local else None,
                  "local_result_path": "local/result.json" if local else None,
                  "parameters": {"page_size_max": page_capacity(page_layout),
                                 **({"page_layout": page_layout} if page_layout is not None else {}),
                                 "gap_seconds_requested": float(gap_seconds),
                                 "gap_frames": gap_frames, "gap_seconds_actual": gap_frames / rate,
                                 "normalization": normalization, "display": params,
                                 "ordering": "local_fixed_linkage_order_within_subcluster" if local else "stable_item_id",
                                 "local_preset": copy.deepcopy(dataset.preset) if local else None},
                  "timing": {"total_seconds": time.monotonic() - started,
                             "clustering_seconds": cluster_seconds},
                  "coverage": {"input_count": len(ids), "page_count": len(pages),
                               "covered_count": sum(len(p["item_ids"]) for p in pages),
                               "unique_count": len(ids), "complete": True},
                  "artifacts": artifacts, "is_human_classification": False,
                  "audio_processing": ("bandpass_filtered_project_PCM_with_internal_zero_silence"
                                       if "original_audio" in dataset._items[ids[0]]
                                       else "original_float32_PCM_with_internal_zero_silence_only"),
                  "provenance": {"source_code_sha256": _sha(__file__),
                                 "acoustic_code_sha256": _sha(Path(__file__).with_name("annotation_acoustics.py"))}}
        _cancel(check_cancelled)
        if route == "family_order":
            result["parameters"]["ordering"] = FAMILY_ORDER_METHOD
            result["timing"]["ordering_seconds"] = order_seconds
            result["family_ordering"] = {
                "policy_version": FAMILY_AUDITION_POLICY, "method": FAMILY_ORDER_METHOD,
                "ordered_ids": order, "refit_representation": False, "is_human_classification": False,
            }
            result["provenance"]["family_acoustic_code_sha256"] = _sha(Path(__file__).with_name("annotation_family_acoustics.py"))
        if route in {"family_cluster", "pool_cluster"}:
            from .annotation_family_acoustics import POOL_CLUSTER_POLICY
            metadata_key = "pool_clustering" if route == "pool_cluster" else "family_clustering"
            result[metadata_key] = copy.deepcopy(local.get(metadata_key, {
                "policy_version": POOL_CLUSTER_POLICY if route == "pool_cluster" else FAMILY_AUDITION_POLICY,
                "method": "existing_fixed_local_recluster",
                "member_count": len(ids), "fixed_local_minimum": dataset.preset["local"]["minimum_recluster_rows"],
                "group_count": len(buckets), "is_human_classification": False,
                "scientific_cluster_validity_established": False}))
            result["parameters"]["ordering"] = ("frozen_global94_average_linkage_leaf_order_within_candidate"
                if metadata_key in local else "local_fixed_linkage_order_within_subcluster")
            result["provenance"]["pool_acoustic_code_sha256" if route == "pool_cluster" else "family_acoustic_code_sha256"] = _sha(Path(__file__).with_name("annotation_family_acoustics.py"))
        _write_json(out / "review.json", result)
        return result
    except Exception as exc:
        _write_json(out / "failure.json", {"status": "cancelled" if getattr(exc, "code", "") == "CANCELLED" else "failed",
                    "code": getattr(exc, "code", "PREPARATION_FAILED"), "message": str(exc),
                    "route": route, "item_ids": ids, "elapsed_seconds": time.monotonic() - started,
                    "published_review": False})
        raise
