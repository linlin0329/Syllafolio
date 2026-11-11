"""协议助手P1的共用声学后端；不读取旧类别、不定类、不连接模型。

输入JSON包含版本、完整数值行身份``feature_item_ids``、显式
``global_fit_ids``及有媒体的``items``。每个item包含匿名item_id、
source_clip_id、父wav_path、整数帧区间、sample_rate_hz、feature_row_index；
可用feature_id将匿名媒体ID连接完整数值行。相对音频路径以manifest为基准。
全局缩放按声明的完整数值总体拟合一次，媒体子集不冒充全局拟合总体。

局部配方适配既有recluster_merged_macro_08_10.prepare_multiview_features，
仅拟合fit_ids；comparison_ids始终只供真人比较。调用方负责可信任务角色、
材料允许集、目录版本与计时。本模块只进行数值和媒体身份检查。
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import math
from pathlib import Path
import re
from typing import Callable

import numpy as np
import soundfile as sf
from scipy.cluster.hierarchy import leaves_list, linkage
from scipy.spatial.distance import cdist, squareform
from sklearn.decomposition import PCA
from sklearn.preprocessing import RobustScaler, StandardScaler

from .ei_b_assistance import EXCLUDED_INDICES, FEATURE_NAMES


DEFAULT_PRESET = (Path(__file__).resolve().parents[2] /
                  "configs/annotation_assistant/p1/preset.json")
VERSION_FIELDS = ("material_version", "boundary_version", "feature_version",
                  "global_representation_version")
ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$")
CancelCheck = Callable[[], bool | None] | None


class AcousticError(ValueError):
    """稳定错误码；失败不静默换算法、不输出人工判断。"""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


def _require(condition, code, message):
    if not condition:
        raise AcousticError(code, message)


def _cancel(check: CancelCheck):
    # 长数值库调用无法逐迭代抢占；阶段间及媒体逐项检查，外层仍保留取消请求状态。
    if check is not None and check():
        raise AcousticError("CANCELLED", "Cancellation requested")


def _sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _hash(value):
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode()).hexdigest()


def _read_json(path):
    def pairs(items):
        result = {}
        for key, value in items:
            _require(key not in result, "INVALID_INPUT", f"Duplicate JSON field: {key}")
            result[key] = value
        return result

    def constant(value):
        raise AcousticError("INVALID_INPUT", f"Non-finite JSON number: {value}")

    return json.loads(Path(path).read_text(encoding="utf-8"),
                      object_pairs_hook=pairs, parse_constant=constant)


def _write_json(path, value):
    Path(path).write_text(json.dumps(
        value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False,
    ) + "\n", encoding="utf-8")


def _ids(values, label, *, nonempty=False):
    _require(isinstance(values, list) and
             all(isinstance(v, str) and ID_PATTERN.fullmatch(v) for v in values),
             "INVALID_INPUT", f"{label} must be a list of path-free identifiers")
    _require(len(values) == len(set(values)), "DUPLICATE_ID", label)
    _require(not nonempty or bool(values), "INSUFFICIENT_INPUT", label)
    return list(values)


def _integer(value, label):
    _require(type(value) is int and value >= 0, "INVALID_INPUT",
             f"{label} must be a non-negative integer")
    return value


def _packages():
    names = ("numpy", "scipy", "scikit-learn", "soundfile", "Pillow",
             "umap-learn", "hdbscan")
    result = {}
    for name in names:
        try:
            result[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            result[name] = None
    return result


def _new_output(output_dir):
    path = Path(output_dir).absolute()
    _require(not path.exists() and not path.is_symlink(), "OUTPUT_EXISTS",
             "Choose a new output location; existing jobs cannot be overwritten.")
    path.mkdir(parents=True, exist_ok=False)
    return path


def _failure(path, error):
    _write_json(path / "failure.json", {
        "status": "cancelled" if getattr(error, "code", "") == "CANCELLED" else "failed",
        "error_code": getattr(error, "code", "COMPUTATION_FAILED"),
        "message": str(error),
        "result_usable": False,
    })


def _ordered_neighbors(values, query_ids, scope_ids, k):
    distances = cdist(values(query_ids), values(scope_ids))
    rows = []
    for i, ident in enumerate(query_ids):
        choices = sorted(
            ((float(distances[i, j]), other) for j, other in enumerate(scope_ids)
             if other != ident), key=lambda pair: (pair[0], pair[1]),
        )[:k]
        rows.append({"item_id": ident, "neighbors": [
            {"item_id": other, "distance": distance} for distance, other in choices
        ]})
    return rows


class AcousticDataset:
    """只读输入容器；服务层可在两个条件中复用同一实例。"""

    @classmethod
    def from_files(cls, manifest_path, features_path, logmel_path, preset_path=None):
        instance = cls()
        instance._load(manifest_path, features_path, logmel_path,
                       preset_path or DEFAULT_PRESET)
        return instance

    def _load(self, manifest_path, features_path, logmel_path, preset_path):
        manifest_path = Path(manifest_path).resolve()
        preset_path = Path(preset_path).resolve()
        inputs_before = {
            "manifest_sha256": _sha(manifest_path),
            "features_sha256": _sha(features_path),
            "logmel_sha256": _sha(logmel_path),
            "preset_sha256": _sha(preset_path),
        }
        data = _read_json(manifest_path)
        preset = _read_json(preset_path)
        _require(data.get("schema_version") == "annotation-acoustics-input-v1",
                 "INVALID_INPUT", "Unsupported media or feature manifest version.")
        _require(preset.get("schema_version") == "annotation-acoustics-preset-v1",
                 "INVALID_PRESET", "Unsupported acoustic preset version.")
        _require(data.get("feature_names") == FEATURE_NAMES, "FEATURE_LAYOUT",
                 "The 105D field names or order do not match; the 94D layout cannot be inferred from dimensions alone.")
        _require(preset["global"]["excluded_indices"] == list(EXCLUDED_INDICES),
                 "INVALID_PRESET", "The selected 94D columns do not match the authoritative 105D layout.")
        self.preset = preset
        self.preset_id = preset["preset_id"]
        self.input_versions = {key: data[key] for key in VERSION_FIELDS}
        _require(all(isinstance(v, str) and v for v in self.input_versions.values()),
                 "INVALID_INPUT", "Input versions must be specified explicitly.")
        # 保存数值快照而非mmap；外部改写文件不能改变已登记输入的运行时数组。
        self.features = np.load(features_path, allow_pickle=False)
        self.logmel = np.load(logmel_path, allow_pickle=False)
        _require(self.features.ndim == 2 and self.features.shape[1] == 105,
                 "FEATURE_LAYOUT", "The acoustic matrix must have shape N × 105.")
        if self.logmel.ndim == 3 and self.logmel.shape[1:] == (64, 32):
            self.logmel = self.logmel.reshape(len(self.logmel), -1)
        _require(self.logmel.ndim == 2 and self.logmel.shape[1] > 0 and
                 len(self.logmel) == len(self.features), "FEATURE_LAYOUT",
                 "The log-mel matrix must match the 105D matrix row for row.")
        for matrix in (self.features, self.logmel):
            _require(matrix.dtype.kind in "fiu" and np.isfinite(matrix).all(),
                     "INVALID_FEATURE", "All features must be finite real numbers.")
            matrix.setflags(write=False)
        rows = data.get("items", [])
        _require(isinstance(rows, list) and bool(rows), "INSUFFICIENT_INPUT",
                 "The media item list cannot be empty.")
        self.item_ids = _ids([r.get("item_id") for r in rows], "items", nonempty=True)
        if "feature_item_ids" in data:
            feature_ids = _ids(data["feature_item_ids"], "feature_item_ids", nonempty=True)
        else:
            _require(len(rows) == len(self.features), "FEATURE_IDENTITY",
                     "A media subset does not identify all numeric rows; declare feature_item_ids.")
            indexed = {r["feature_row_index"]: r.get("feature_id", r["item_id"])
                       for r in rows}
            _require(set(indexed) == set(range(len(self.features))), "FEATURE_IDENTITY",
                     "The complete numeric row identities are missing.")
            feature_ids = _ids([indexed[i] for i in range(len(self.features))],
                               "feature_item_ids", nonempty=True)
        _require(len(feature_ids) == len(self.features), "FEATURE_IDENTITY",
                 "The number of complete numeric row IDs does not match.")
        self._feature_index = {ident: i for i, ident in enumerate(feature_ids)}
        self.global_fit_ids = _ids(data.get("global_fit_ids", []), "global_fit_ids",
                                   nonempty=True)
        _require(set(self.global_fit_ids) <= set(feature_ids), "FEATURE_IDENTITY",
                 "A global fit ID has no numeric row.")
        self._items = {}
        self._parents = {}
        media_feature_rows = set()
        sample_rates = set()
        for original in rows:
            row = dict(original)
            for key in ("source_frame_start", "source_frame_stop",
                        "sample_rate_hz", "feature_row_index"):
                _integer(row.get(key), key)
            _ids([row.get("source_clip_id")], "source_clip_id", nonempty=True)
            index = row["feature_row_index"]
            feature_id = row.get("feature_id", row["item_id"])
            _require(index < len(feature_ids) and feature_ids[index] == feature_id,
                     "FEATURE_IDENTITY", "The anonymous syllable ID, feature_id and row index do not match.")
            _require(index not in media_feature_rows, "FEATURE_IDENTITY",
                     "Different media items cannot share a numeric row without an explicit record.")
            media_feature_rows.add(index)
            path = Path(row["wav_path"])
            path = (manifest_path.parent / path).resolve() if not path.is_absolute() else path.resolve()
            _require(path.is_file(), "MISSING_MEDIA", "The parent audio file is missing.")
            parent_key = row["source_clip_id"]
            if parent_key not in self._parents:
                info = sf.info(path)
                _require(info.channels == 1 and info.samplerate > 0, "INVALID_MEDIA",
                         "Only mono parent audio is supported.")
                self._parents[parent_key] = {
                    "path": path, "sha256": _sha(path), "frames": info.frames,
                    "sample_rate_hz": info.samplerate,
                }
            parent = self._parents[parent_key]
            _require(path == parent["path"], "MEDIA_IDENTITY",
                     "The same anonymous parent-source ID refers to different WAV files.")
            _require(0 <= row["source_frame_start"] < row["source_frame_stop"] <=
                     parent["frames"], "INVALID_BOUNDARY", "The integer frame interval exceeds the parent audio.")
            _require(row["sample_rate_hz"] == parent["sample_rate_hz"],
                     "INVALID_BOUNDARY", "The syllable sample rate does not match the parent audio.")
            if "parent_audio_sha256" in row:
                _require(row["parent_audio_sha256"] == parent["sha256"],
                         "MEDIA_IDENTITY", "The parent audio fingerprint has changed.")
            row["_path"] = path
            self._items[row["item_id"]] = row
            sample_rates.add(row["sample_rate_hz"])
        _require(len(sample_rates) == 1, "MIXED_SAMPLE_RATE",
                 "Grouped audition requires one sample rate; audio is not resampled automatically.")
        self.sample_rate_hz = sample_rates.pop()
        self._retained = [i for i in range(105) if i not in EXCLUDED_INDICES]
        fit_rows = [self._feature_index[i] for i in self.global_fit_ids]
        self._global_scaler = RobustScaler(
            quantile_range=tuple(preset["global"]["quantile_range"])
        ).fit(np.asarray(self.features[fit_rows], dtype=np.float64))
        media_rows = [self._items[i]["feature_row_index"] for i in self.item_ids]
        transformed = self._global_scaler.transform(np.asarray(
            self.features[media_rows], dtype=np.float64
        ))[:, self._retained]
        _require(np.isfinite(transformed).all(), "INVALID_FEATURE",
                 "The fixed global representation contains non-finite values.")
        self._global94 = dict(zip(self.item_ids, transformed))
        self._provenance = {
            "manifest_sha256": _sha(manifest_path),
            "features_sha256": _sha(features_path),
            "logmel_sha256": _sha(logmel_path),
            "preset_sha256": _sha(preset_path),
            "source_code_sha256": _sha(Path(__file__)),
            "feature_definition_code_sha256": _sha(Path(__file__).with_name("ei_b_assistance.py")),
            "input_versions": self.input_versions,
            "features_shape": list(self.features.shape),
            "logmel_shape": list(self.logmel.shape),
            "global_fit_count": len(self.global_fit_ids),
            "global_fit_ids_sha256": _hash(self.global_fit_ids),
            "parent_audio_sha256": {k: p["sha256"] for k, p in self._parents.items()},
            "packages": _packages(),
        }
        _require(all(self._provenance[key] == value
                     for key, value in inputs_before.items()),
                 "INPUT_CHANGED", "The manifest, numeric input or preset changed during loading.")
        self.input_fingerprint = _hash(self._provenance)

    def public_items(self):
        return [{
            "item_id": ident, "source_clip_id": self._items[ident]["source_clip_id"],
            "source_frame_start": self._items[ident]["source_frame_start"],
            "source_frame_stop": self._items[ident]["source_frame_stop"],
            "sample_rate_hz": self.sample_rate_hz,
            "duration_s": ((self._items[ident]["source_frame_stop"] -
                            self._items[ident]["source_frame_start"]) /
                           self.sample_rate_hz),
        } for ident in self.item_ids]

    def _select(self, values, label, *, nonempty=False):
        ids = _ids(values, label, nonempty=nonempty)
        _require(set(ids) <= set(self._items), "OUT_OF_SCOPE",
                 f"{label} contains an unregistered media item")
        return ids

    def neighbors(self, item_ids, scope_ids, k=5):
        item_ids = self._select(item_ids, "item_ids", nonempty=True)
        scope_ids = self._select(scope_ids, "scope_ids", nonempty=True)
        _require(type(k) is int and k >= 1, "INVALID_INPUT", "k must be a positive integer.")
        rows = _ordered_neighbors(
            lambda ids: np.asarray([self._global94[i] for i in ids]),
            item_ids, scope_ids, k,
        )
        return {
            "status": "succeeded", "operation": "fixed_global_neighbors",
            "items": rows, "query_ids": item_ids, "scope_ids": scope_ids,
            "distance_definition": "euclidean_RobustScaler105_retained94",
            "input_versions": self.input_versions,
            "input_fingerprint": self.input_fingerprint,
            "global_fit_count": len(self.global_fit_ids),
            "global_fit_ids_sha256": _hash(self.global_fit_ids),
            "new_fit": False,
            "parameters": self.preset["global"],
            "provenance": self._provenance,
        }

    def _verify_parent(self, row):
        parent = self._parents[row["source_clip_id"]]
        _require(_sha(parent["path"]) == parent["sha256"], "MEDIA_CHANGED",
                 "The parent audio changed after loading; create a new input version.")
        return parent

    def read_audio(self, item_id, *, context=False):
        self._select([item_id], "item_id")
        row = self._items[item_id]
        parent = self._verify_parent(row)
        start, stop = row["source_frame_start"], row["source_frame_stop"]
        if context:
            pad = int(round(self.preset["display"]["context_padding_s"] *
                            row["sample_rate_hz"]))
            start, stop = max(0, start - pad), min(parent["frames"], stop + pad)
        samples, rate = sf.read(parent["path"], start=start, stop=stop,
                                dtype="float32", always_2d=False)
        _require(_sha(parent["path"]) == parent["sha256"], "MEDIA_CHANGED",
                 "The parent audio changed during reading.")
        _require(len(samples) == stop - start and np.isfinite(samples).all(),
                 "INVALID_MEDIA", "The audio read is incomplete or contains non-finite samples.")
        return samples, rate, {
            "source_clip_id": row["source_clip_id"],
            "source_frame_start": start, "source_frame_stop": stop,
            "target_start_frame_in_context": row["source_frame_start"] - start,
            "target_stop_frame_in_context": row["source_frame_stop"] - start,
        }

    def build_comparison_bundle(self, item_ids, comparison_ids, output_dir,
                                check_cancelled: CancelCheck = None):
        item_ids = self._select(item_ids, "item_ids", nonempty=True)
        comparison_ids = self._select(comparison_ids, "comparison_ids")
        combined = list(dict.fromkeys(item_ids + comparison_ids))
        _require(len(combined) <= self.preset["display"]["maximum_bundle_items"],
                 "BUNDLE_TOO_LARGE", "The comparison package exceeds the fixed preset size.")
        _cancel(check_cancelled)
        out = _new_output(output_dir)
        try:
            (out / "media").mkdir()
            (out / "spectrograms").mkdir()
            max_duration = max((self._items[i]["source_frame_stop"] -
                                self._items[i]["source_frame_start"]) /
                               self.sample_rate_hz for i in combined)
            items, group_parts, group_index = [], [], []
            cursor = 0
            for index, ident in enumerate(combined):
                _cancel(check_cancelled)
                samples, rate, _ = self.read_audio(ident)
                context_samples, _, context = self.read_audio(ident, context=True)
                key = hashlib.sha256(ident.encode()).hexdigest()[:20]
                audio_name = f"media/{key}.wav"
                context_name = f"media/{key}_context.wav"
                image_name = f"spectrograms/{key}.png"
                sf.write(out / audio_name, samples, rate, subtype="FLOAT")
                sf.write(out / context_name, context_samples, rate, subtype="FLOAT")
                _plot_spectrogram(samples, rate, out / image_name,
                                  self.preset["display"], max_duration)
                group_parts.append(samples)
                group_index.append({
                    "item_id": ident, "start_frame": cursor,
                    "stop_frame": cursor + len(samples),
                })
                cursor += len(samples)
                if index < len(combined) - 1:
                    gap = np.zeros(int(round(rate * self.preset["display"]["group_gap_s"])),
                                   dtype=np.float32)
                    group_parts.append(gap)
                    cursor += len(gap)
                reloaded, _ = sf.read(out / audio_name, dtype="float32")
                _require(np.array_equal(samples, reloaded), "AUDIO_EXPORT_MISMATCH",
                         "The output clip does not match its parent-source interval.")
                items.append({
                    "item_id": ident,
                    "role": "target" if ident in item_ids else "comparison",
                    "audio_path": audio_name, "context_audio_path": context_name,
                    "image_path": image_name, "duration_s": len(samples) / rate,
                    "context": context, "audio_sha256": _sha(out / audio_name),
                    "context_audio_sha256": _sha(out / context_name),
                    "image_sha256": _sha(out / image_name),
                })
            _cancel(check_cancelled)
            sf.write(out / "media/group.wav", np.concatenate(group_parts),
                     self.sample_rate_hz, subtype="FLOAT")
            receipt = {
                "schema_version": "annotation-comparison-bundle-v1",
                "status": "succeeded", "bundle_id": "bundle-" + _hash({
                    "input": self.input_fingerprint, "items": combined,
                    "targets": item_ids, "comparisons": comparison_ids,
                })[:24],
                "item_ids": item_ids, "comparison_ids": comparison_ids, "items": items,
                "group_audio_path": "media/group.wav", "group_index": group_index,
                "sample_rate_hz": self.sample_rate_hz,
                "display": dict(self.preset["display"],
                                time_max_s=max_duration,
                                frequency_max_hz=min(
                                    self.preset["display"]["frequency_max_hz"],
                                    self.sample_rate_hz / 2)),
                "input_versions": self.input_versions,
                "input_fingerprint": self.input_fingerprint,
                "provenance": self._provenance,
                "media_for": "human_only_no_machine_semantic_interpretation",
            }
            _write_json(out / "bundle.json", receipt)
            return receipt
        except Exception as error:
            _failure(out, error)
            raise

    def _multiview(self, fit_ids, check_cancelled):
        rows = [self._items[i]["feature_row_index"] for i in fit_ids]
        raw94 = np.asarray(self.features[rows][:, self._retained], dtype=np.float64)
        patch = np.asarray(self.logmel[rows], dtype=np.float64)
        local = self.preset["local"]
        views, models = [], {}
        for name, raw, scaler in (
                ("acoustic94", raw94, RobustScaler()),
                ("logmel", patch, StandardScaler())):
            _cancel(check_cancelled)
            scaled = scaler.fit_transform(raw)
            # 明示数值秩上限，避免对白化的零方差方向除零；不增补样本或换模型。
            rank = int(np.linalg.matrix_rank(scaled - scaled.mean(axis=0)))
            count = min(local["pca_components_max"], len(fit_ids) - 1,
                        scaled.shape[1], rank)
            _require(count >= 1, "INSUFFICIENT_VARIATION",
                     f"{name}: no usable local variation is available.")
            pca = PCA(n_components=count, whiten=local["pca_whiten"],
                      svd_solver=local["pca_svd_solver"], random_state=local["seed"])
            reduced = pca.fit_transform(scaled)
            post = StandardScaler()
            view = post.fit_transform(reduced) / math.sqrt(count)
            _require(np.isfinite(view).all(), "INVALID_RESULT",
                     "The local representation contains non-finite values.")
            views.append(view.astype(np.float32))
            models[name] = {
                "scaler_center": np.asarray(getattr(scaler, "center_",
                                                     getattr(scaler, "mean_", []))).tolist(),
                "scaler_scale": scaler.scale_.tolist(),
                "pca_components": pca.components_.tolist(),
                "pca_mean": pca.mean_.tolist(),
                "pca_explained_variance": pca.explained_variance_.tolist(),
                "post_pca_mean": post.mean_.tolist(),
                "post_pca_scale": post.scale_.tolist(),
                "components": count, "numeric_rank": rank,
                "fit_ids": list(fit_ids),
            }
        return np.hstack(views).astype(np.float32), models

    def run_local(self, action, fit_ids, comparison_ids, output_dir,
                  check_cancelled: CancelCheck = None):
        _require(action in {"local_reorder", "local_recluster"}, "WRONG_ACTION",
                 "Choose an explicit local reordering or reclustering action.")
        fit_ids = self._select(fit_ids, "fit_ids", nonempty=True)
        comparison_ids = self._select(comparison_ids, "comparison_ids")
        minimum = self.preset["local"][
            "minimum_reorder_rows" if action == "local_reorder"
            else "minimum_recluster_rows"
        ]
        _require(len(fit_ids) >= minimum, "INSUFFICIENT_INPUT",
                 f"{action}: the fixed preset requires at least {minimum} items; use direct comparison when it does not apply.")
        _cancel(check_cancelled)
        out = _new_output(output_dir)
        try:
            fused, models = self._multiview(fit_ids, check_cancelled)
            _cancel(check_cancelled)
            distances = cdist(fused, fused)
            tree = linkage(squareform(distances, checks=True),
                           method=self.preset["local"]["ordering_linkage"],
                           optimal_ordering=self.preset["local"]["optimal_ordering"])
            order = leaves_list(tree).tolist()
            labels = np.full(len(fit_ids), -1, dtype=int)
            strengths = np.zeros(len(fit_ids))
            embedding = np.empty((len(fit_ids), 0))
            if action == "local_recluster":
                _cancel(check_cancelled)
                try:
                    import umap
                    import hdbscan
                except (ImportError, RuntimeError) as error:
                    raise AcousticError(
                        "DEPENDENCY_UNAVAILABLE",
                        "Fixed clustering requires umap-learn, hdbscan and a writable NUMBA_CACHE_DIR; the algorithm is not replaced automatically.",
                    ) from error
                reducer = umap.UMAP(**self.preset["umap"])
                embedding = reducer.fit_transform(fused)
                _cancel(check_cancelled)
                clusterer = hdbscan.HDBSCAN(**self.preset["hdbscan"])
                labels = clusterer.fit_predict(embedding)
                strengths = clusterer.probabilities_
            _cancel(check_cancelled)
            nonnoise = sorted(set(int(v) for v in labels if v >= 0),
                              key=lambda label: min(
                                  fit_ids[i] for i in np.flatnonzero(labels == label)))
            cluster_ids = {label: f"candidate_{i + 1:03d}"
                           for i, label in enumerate(nonnoise)}
            value_by_id = dict(zip(fit_ids, fused))
            neighbors = _ordered_neighbors(
                lambda ids: np.asarray([value_by_id[i] for i in ids]),
                fit_ids, fit_ids, 5,
            )
            np.save(out / "fused_features.npy", fused, allow_pickle=False)
            np.save(out / "distances.npy", distances, allow_pickle=False)
            np.save(out / "ordering_linkage.npy", tree, allow_pickle=False)
            np.save(out / "embedding.npy", embedding, allow_pickle=False)
            _write_json(out / "representation_models.json", models)
            receipt = {
                "schema_version": "annotation-local-result-v1", "status": "succeeded",
                "action": action, "fit_ids": fit_ids, "comparison_ids": comparison_ids,
                "fit_ids_sha256": _hash(fit_ids),
                "comparison_ids_sha256": _hash(comparison_ids),
                "ordered_ids": [fit_ids[i] for i in order], "neighbors": neighbors,
                "assignments": [{
                    "item_id": ident, "candidate_cluster_id": cluster_ids.get(int(labels[i])),
                    "membership_strength": (float(strengths[i])
                                            if action == "local_recluster" else None),
                    "state": ("not_clustered_reorder_only" if action == "local_reorder"
                              else "candidate_requires_human_review" if labels[i] >= 0
                              else "unclustered_not_noise_judgment"),
                } for i, ident in enumerate(fit_ids)],
                "input_versions": self.input_versions,
                "input_fingerprint": self.input_fingerprint,
                "preset_id": self.preset_id, "parameters": self.preset,
                "provenance": self._provenance,
                "refitted_steps": ["acoustic94_scaler", "logmel_scaler", "separate_pca",
                                  "post_pca_scaling"] + (
                                      ["umap", "hdbscan"] if action == "local_recluster" else []),
                "comparison_used_for_fitting": False,
                "score_semantics": "candidate distances/strengths; not calibrated type probabilities",
                "artifacts": {
                    name: {"path": name, "sha256": _sha(out / name)}
                    for name in ("fused_features.npy", "distances.npy",
                                 "ordering_linkage.npy", "embedding.npy",
                                 "representation_models.json")
                },
            }
            receipt["result_id"] = "local-" + _hash({
                "input": self.input_fingerprint, "action": action, "fit_ids": fit_ids,
                "comparison_ids": comparison_ids, "preset": self.preset_id,
            })[:24]
            _write_json(out / "result.json", receipt)
            return receipt
        except Exception as error:
            _failure(out, error)
            raise


def _plot_spectrogram(samples, rate, path, display, common_duration):
    """固定时频/幅度标尺；短音节右侧灰色表示无数据，不把填充画成已观察静音。"""
    from PIL import Image, ImageDraw

    nfft, hop = display["n_fft"], display["hop"]
    window = np.hanning(nfft)
    padded = np.pad(np.asarray(samples, dtype=np.float64),
                    (nfft // 2, nfft // 2 + hop))
    frames = np.lib.stride_tricks.sliding_window_view(padded, nfft)[::hop]
    amplitude = np.abs(np.fft.rfft(frames * window, axis=1)) / (window.sum() / 2)
    frequencies = np.fft.rfftfreq(nfft, 1 / rate)
    max_hz = min(display["frequency_max_hz"], rate / 2)
    select = frequencies <= max_hz
    db = 20 * np.log10(np.maximum(amplitude[:, select], 1e-12))
    width, height = display["image_width"], display["image_height"]
    left, right, top, bottom = 62, 15, 14, 40
    plot_width, plot_height = width - left - right, height - top - bottom
    duration = len(samples) / rate
    active_width = max(1, min(plot_width, int(round(plot_width * duration / common_duration))))
    times = np.linspace(0, max(0, len(samples) - 1), active_width)
    centers = np.arange(len(frames)) * hop
    sampled = np.stack([np.interp(times, centers, db[:, j])
                        for j in range(db.shape[1])])
    intensity = np.clip((sampled - display["db_min"]) /
                        (display["db_max"] - display["db_min"]), 0, 1)
    # frequency rows (high at top), time columns; common colour scale for every item.
    gray = np.uint8(255 * (1 - intensity[::-1]))
    patch = Image.fromarray(gray, mode="L").resize(
        (active_width, plot_height), Image.Resampling.BILINEAR
    ).convert("RGB")
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((left, top, left + plot_width, top + plot_height), fill="#dddddd")
    image.paste(patch, (left, top))
    draw.rectangle((left, top, left + plot_width, top + plot_height), outline="#333333")
    for fraction in (0, 0.5, 1):
        x = left + int(fraction * plot_width)
        draw.text((x - 8, top + plot_height + 5), f"{fraction * common_duration:.2f}", fill="black")
    draw.text((left + plot_width // 2 - 20, height - 14), "Time (s)", fill="black")
    draw.text((3, top), f"{max_hz / 1000:g} kHz", fill="black")
    draw.text((14, top + plot_height - 12), "0", fill="black")
    draw.text((left + 4, top + 3), f"{display['db_min']} to {display['db_max']} dBFS", fill="#777777")
    image.save(path)
