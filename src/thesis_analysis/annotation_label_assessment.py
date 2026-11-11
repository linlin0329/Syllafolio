"""D3 family标签的内部声学评估；纯数值模块，不修改标签或判断科学有效性。

``assess_family_labels(raw105, raw_logmel, items, ...)`` 的两矩阵与items逐行
对应。每条显式包含item_id、family_id（可空）、source_group_id（可空）、
in_scope(bool)、label_status(confirmed/unresolved/excluded)。source_group_id
是操作者登记并由上层核查的原始录音来源，不冒充独立个体。本模块不读取
历史noise/聚类结果、不猜来源，也不使用全局已拟合的表示。

固定表示借鉴prepare_label_blind_views的94D/log-mel等权融合，但所有缩放、
PCA和分类器只在训练折拟合。先按训练类覆盖确定全部分折，再开始任何拟合；
不根据性能选择分折。支持统计始终保留稀有/来源未知/未定/排除与空family。
返回可直接JSON序列化的报告；不可估计不补音节随机拆分，不返回部分成功分数。
取消抛AssessmentError(CANCELLED)，科学不可估计返回status=not_estimable。
"""
from __future__ import annotations

from collections import Counter
import copy
import hashlib
import importlib.metadata
import json
import math
from typing import Callable
import warnings

import numpy as np
from sklearn.decomposition import PCA
from sklearn.exceptions import ConvergenceWarning
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import confusion_matrix, f1_score
from sklearn.model_selection import StratifiedGroupKFold
from sklearn.preprocessing import RobustScaler, StandardScaler
from threadpoolctl import threadpool_limits

from .ei_b_assistance import EXCLUDED_INDICES, FEATURE_NAMES


SCHEMA_VERSION = "annotation-family-assessment-v1"
PRESET = {
    "id": "family_fold_local_fused_lr_v1", "version": "1.0.0",
    "level": "family", "minimum_items_per_family": 10,
    "minimum_source_groups_per_family": 3,
    "source_unit": "operator_registered_original_recording",
    "acoustic105_excluded_indices": list(EXCLUDED_INDICES),
    "acoustic_scaler": "RobustScaler_default_IQR_25_75",
    "logmel_scaler": "StandardScaler", "pca_components_max": 30,
    "pca_components_rule": "min(30, n_train - 1, n_features)",
    "pca_solver": "randomized", "pca_whiten": False,
    "pca_n_oversamples": 10, "pca_iterated_power": "auto",
    "pca_power_iteration_normalizer": "auto",
    "post_pca_scaler": "StandardScaler", "view_weight": "1/sqrt(components)",
    "fusion": "equal_dimension_normalized_concatenation",
    "classifier": {"name": "LogisticRegression", "C": 1.0,
                   "class_weight": "balanced", "solver": "lbfgs", "max_iter": 4000,
                   "tol": 0.0001, "random_state": 42, "fit_intercept": True,
                   "penalty": "l2"},
    "folds": 3, "repeats": 5, "first_split_seed": 42,
    "maximum_seed_attempts": 100, "preprocessing_seed": 42,
    "seed_acceptance": "training_class_coverage_only_before_any_fit",
    "constant_training_view": "zero_block_with_nominal_components_no_test_fitted_axes",
    "standard_deviation_ddof": 1, "numerical_threads": 1,
    "implementation_basis": (
        "ThesisAnalysis/workflows/04_syllable_typing/code/"
        "analyze_confirmed_bucket_similarity.py:prepare_label_blind_views; "
        "adapted to training-fold-only fitting, not historical whole-corpus fitting"),
}
LIMITATIONS = [
    "Internal prediction of current human family labels; not label correctness or biological validity.",
    "Registered recording groups do not establish independent birds, sessions or external validation.",
    "Five repeated partitions reuse the same syllables; n is not multiplied by five.",
    "SD describes variation over these partitions, not a confidence interval or independent replication.",
    "Source provenance is supplied by the operator and checked upstream, not inferred from file names here.",
    "Results concern eligible labelled items only; rare families and unknown-source items remain reported.",
    "No performance threshold certifies completion or the validity of a family boundary.",
    "The feature recipe is fixed; current labels may have been developed using related acoustic features.",
]


class AssessmentError(ValueError):
    """输入或取消的稳定错误；不得以异常为理由静默换方法。"""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


def get_preset() -> dict:
    return copy.deepcopy(PRESET)


def _require(condition, code, message):
    if not condition:
        raise AssessmentError(code, message)


def _reason(code, message, **details):
    return {"code": code, "message": message, **details}


def _check(check_cancelled):
    if check_cancelled is not None and check_cancelled():
        raise AssessmentError("CANCELLED", "Internal assessment cancelled; incomplete reports do not publish scores.")


def _emit(progress, check_cancelled, stage, completed_folds=0, **details):
    _check(check_cancelled)
    if progress is not None:
        progress({"stage": stage, "completed_folds": completed_folds,
                  "total_folds": PRESET["folds"] * PRESET["repeats"], **details})
    _check(check_cancelled)


def _identifier(value, label, *, optional=False):
    _require((optional and value is None) or
             (isinstance(value, str) and bool(value.strip()) and value == value.strip()),
             "INVALID_INPUT", f"{label} must be a non-empty identifier or explicit null")


def _validated_items(items, family_ids=None):
    _require(isinstance(items, (list, tuple)), "INVALID_INPUT", "items must be a list of row records.")
    rows, seen = [], set()
    for item in items:
        _require(isinstance(item, dict), "INVALID_INPUT", "Each entry in items must be an object.")
        _require(all(k in item for k in ("item_id", "family_id", "source_group_id",
                                        "in_scope", "label_status")),
                 "INVALID_INPUT", "Each item must explicitly include its identity, source, scope and label status.")
        for key in ("item_id", "family_id", "source_group_id"):
            _identifier(item[key], key, optional=key != "item_id")
        _require(item["item_id"] not in seen, "DUPLICATE_ITEM", "item_id values must be unique.")
        seen.add(item["item_id"])
        _require(type(item["in_scope"]) is bool, "INVALID_INPUT", "in_scope must be a boolean.")
        _require(item["label_status"] in {"confirmed", "unresolved", "excluded"},
                 "INVALID_INPUT", "label_status must be specified explicitly.")
        _require(item["label_status"] != "confirmed" or item["family_id"] is not None,
                 "INVALID_INPUT", "A confirmed item must have a human-confirmed family_id.")
        rows.append({key: item[key] for key in ("item_id", "family_id", "source_group_id",
                                              "in_scope", "label_status")})
    families = [] if family_ids is None else list(family_ids)
    _require(family_ids is None or isinstance(family_ids, (list, tuple)),
             "INVALID_INPUT", "family_ids must be a list of identifiers.")
    for family in families:
        _identifier(family, "family_ids")
    _require(len(families) == len(set(families)), "INVALID_INPUT", "family_ids must be unique.")
    return rows, sorted(set(families) | {r["family_id"] for r in rows if r["family_id"]})


def summarize_support(items, family_ids=None) -> dict:
    """不读取特征、不拟合的支持度预览；max_source_share以已知来源确认为分母。"""
    rows, families = _validated_items(items, family_ids)
    scoped = [r for r in rows if r["in_scope"]]
    confirmed = [r for r in scoped if r["label_status"] == "confirmed"]
    support, eligible_families = [], []
    for family in families:
        members = [r for r in scoped if r["family_id"] == family]
        labelled = [r for r in members if r["label_status"] == "confirmed"]
        known = [r for r in labelled if r["source_group_id"] is not None]
        counts = Counter(r["source_group_id"] for r in known)
        reasons = []
        if len(known) < PRESET["minimum_items_per_family"]:
            reasons.append(_reason("INSUFFICIENT_ITEMS", "Fewer than 10 confirmed syllables have known original-recording sources."))
        if len(counts) < PRESET["minimum_source_groups_per_family"]:
            reasons.append(_reason("INSUFFICIENT_SOURCE_GROUPS", "Fewer than 3 original-recording sources are known."))
        eligible = not reasons
        if eligible:
            eligible_families.append(family)
        support.append({
            "family_id": family, "n_in_scope": len(members), "n_confirmed": len(labelled),
            "n_known_source": len(known), "n_unknown_source": len(labelled) - len(known),
            "n_source_groups": len(counts),
            "max_source_share": max(counts.values()) / len(known) if known else None,
            "max_source_share_denominator": "known_source_confirmed_items",
            "source_counts": dict(sorted(counts.items())), "eligible": eligible,
            "reasons": reasons,
        })
    eligible_set = set(eligible_families)
    eligible_ids = [r["item_id"] for r in confirmed
                    if r["family_id"] in eligible_set and r["source_group_id"] is not None]
    coverage = {
        "n_total": len(rows), "n_scope": len(scoped), "n_out_of_scope": len(rows) - len(scoped),
        "n_confirmed": len(confirmed),
        "n_unknown_source": sum(r["source_group_id"] is None for r in confirmed),
        "n_scope_unknown_source": sum(r["source_group_id"] is None for r in scoped),
        "n_unresolved": sum(r["label_status"] == "unresolved" for r in scoped),
        "n_excluded": sum(r["label_status"] == "excluded" for r in scoped),
        "n_eligible": len(eligible_ids), "n_families": len(families),
        "n_eligible_families": len(eligible_families),
        "item_fraction": len(eligible_ids) / len(scoped) if scoped else None,
        "family_fraction": len(eligible_families) / len(families) if families else None,
        "item_fraction_denominator": "all_items_in_declared_scope",
        "family_fraction_denominator": "all_declared_or_referenced_families",
    }
    return {"support": support, "coverage": coverage, "eligible_item_ids": eligible_ids,
            "eligible_family_ids": eligible_families}


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False, separators=(",", ":")).encode()).hexdigest()


def _array_hash(value):
    array = np.asarray(value, dtype="<f8", order="C")
    return hashlib.sha256(str(array.shape).encode() + array.tobytes()).hexdigest()


def _base_report(summary):
    return {
        "schema_version": SCHEMA_VERSION, "preset": get_preset(),
        "status": "not_estimable", "reasons": [],
        **copy.deepcopy(summary), "plan": None, "repeats": [], "metrics": None,
        "per_family": [], "confusion": None, "warnings": [],
        "audit": {"packages": {name: importlib.metadata.version(name)
                              for name in ("numpy", "scipy", "scikit-learn")},
                  "folds": [], "fit_scope": "training_fold_only", "fits_started": 0},
        "limitations": list(LIMITATIONS), "scientific_validity_certified": False,
    }


def _plan_splits(rows, labels, groups, progress, check_cancelled):
    accepted, attempts = [], []
    all_classes = set(labels.tolist())
    for seed in range(PRESET["first_split_seed"],
                      PRESET["first_split_seed"] + PRESET["maximum_seed_attempts"]):
        _emit(progress, check_cancelled, "planning", message="Selecting feasible folds that cover all training classes", seed=seed)
        splitter = StratifiedGroupKFold(n_splits=PRESET["folds"], shuffle=True, random_state=seed)
        split = list(splitter.split(np.zeros((len(rows), 1)), labels, groups))
        feasible = len(split) == PRESET["folds"]
        coverage = np.zeros(len(rows), dtype=int)
        missing, folds = [], []
        for fold, (train, test) in enumerate(split, start=1):
            absent = sorted(all_classes - set(labels[train].tolist()))
            train_groups, test_groups = set(groups[train].tolist()), set(groups[test].tolist())
            valid = (len(train) > 1 and len(test) > 0 and not absent and
                     not train_groups.intersection(test_groups) and
                     not set(train).intersection(test) and len(train) + len(test) == len(rows))
            feasible = feasible and valid
            coverage[test] += 1
            missing.append({"fold": fold, "missing_training_classes": absent})
            folds.append((train, test))
        feasible = feasible and bool(np.all(coverage == 1))
        attempts.append({"seed": seed, "accepted": feasible,
                         "training_class_coverage": missing})
        if feasible:
            accepted.append({"seed": seed, "folds": folds})
        if len(accepted) == PRESET["repeats"]:
            break
    public = {
        "selected_seeds": [a["seed"] for a in accepted], "seed_attempts": attempts,
        "all_splits_fixed_before_fit": True, "selection_uses_scores": False,
        "n_unique_partitions": len({_hash(sorted([sorted(groups[t].tolist())
                                                   for _, t in a["folds"]]))
                                     for a in accepted}),
        "folds": [],
    }
    for repeat, candidate in enumerate(accepted, start=1):
        for fold, (train, test) in enumerate(candidate["folds"], start=1):
            public["folds"].append({
                "repeat": repeat, "fold": fold, "seed": candidate["seed"],
                "train_item_ids": [rows[i]["item_id"] for i in train],
                "test_item_ids": [rows[i]["item_id"] for i in test],
                "train_group_ids": sorted(set(groups[train].tolist())),
                "test_group_ids": sorted(set(groups[test].tolist())),
                "train_class_counts": dict(sorted(Counter(labels[train].tolist()).items())),
                "test_class_counts": dict(sorted(Counter(labels[test].tolist()).items())),
            })
    return accepted, public


def _fit_view(train, test, name, progress, check_cancelled, completed, repeat, fold):
    _emit(progress, check_cancelled, "preprocessing", completed,
          repeat=repeat, fold=fold, view=name, message="Fitting scaling and PCA on the training fold only")
    scaler = RobustScaler() if name == "acoustic94" else StandardScaler()
    scaled_train = scaler.fit_transform(train)
    scaled_test = scaler.transform(test)
    components = min(PRESET["pca_components_max"], len(train) - 1, train.shape[1])
    if not np.any(np.ptp(scaled_train, axis=0) > 0):
        # 训练样本没有可学习的轴；测试集不能决定保留哪些方向。无信息亦是结果。
        summary = {
            "name": name, "n_fit_items": len(train), "n_input_features": train.shape[1],
            "components": components, "dimension_normalizer": math.sqrt(components),
            "no_variance": True, "transform": "training_constant_zero_block",
            "scaler_center": np.asarray(getattr(scaler, "center_", getattr(scaler, "mean_", []))).tolist(),
            "scaler_scale": scaler.scale_.tolist(), "pca_components_sha256": None,
            "pca_mean_sha256": None, "pca_explained_variance": [0.0] * components,
            "pca_explained_variance_ratio_sum": None,
            "post_pca_mean": [0.0] * components, "post_pca_scale": [1.0] * components,
        }
        return np.zeros((len(train), components)), np.zeros((len(test), components)), summary
    _check(check_cancelled)
    pca = PCA(n_components=components, svd_solver=PRESET["pca_solver"],
              whiten=PRESET["pca_whiten"], random_state=PRESET["preprocessing_seed"],
              n_oversamples=PRESET["pca_n_oversamples"],
              iterated_power=PRESET["pca_iterated_power"],
              power_iteration_normalizer=PRESET["pca_power_iteration_normalizer"])
    reduced_train = pca.fit_transform(scaled_train)
    reduced_test = pca.transform(scaled_test)
    _check(check_cancelled)
    post = StandardScaler()
    view_train = post.fit_transform(reduced_train) / math.sqrt(components)
    view_test = post.transform(reduced_test) / math.sqrt(components)
    _require(np.isfinite(view_train).all() and np.isfinite(view_test).all(),
             "NUMERICAL_FAILURE", "The fold representation contains non-finite values.")
    summary = {
        "name": name, "n_fit_items": len(train), "n_input_features": train.shape[1],
        "components": components, "dimension_normalizer": math.sqrt(components),
        "no_variance": False, "transform": "training_fitted_pca_and_scalers",
        "scaler_center": np.asarray(getattr(scaler, "center_", getattr(scaler, "mean_", []))).tolist(),
        "scaler_scale": scaler.scale_.tolist(), "pca_components_sha256": _array_hash(pca.components_),
        "pca_mean_sha256": _array_hash(pca.mean_),
        "pca_explained_variance": pca.explained_variance_.tolist(),
        "pca_explained_variance_ratio_sum": float(pca.explained_variance_ratio_.sum()),
        "post_pca_mean": post.mean_.tolist(), "post_pca_scale": post.scale_.tolist(),
    }
    return view_train, view_test, summary


def assess_family_labels(raw105, raw_logmel, items, *, family_ids=None,
                         blocking_reasons=None, progress: Callable | None = None,
                         check_cancelled: Callable | None = None) -> dict:
    """计算固定的5×3折来源分组评估；回调不写状态，取消在数值阶段间检查。

    blocking_reasons为{code,message,...}列表，也接受文字理由；来源冲突等
    上游阻断必须先返回无评分报告。形状/输入合同错误抛AssessmentError；
    特征缺失可由调用方直接加入blocking_reasons并传None矩阵。
    """
    _emit(progress, check_cancelled, "support", message="Summarizing scope, source and class support")
    summary = summarize_support(items, family_ids)
    rows, _ = _validated_items(items, family_ids)
    report = _base_report(summary)
    _require(blocking_reasons is None or isinstance(blocking_reasons, (list, tuple)),
             "INVALID_INPUT", "blocking_reasons must be a list.")
    for block in blocking_reasons or []:
        if isinstance(block, str) and block.strip():
            report["reasons"].append(_reason("UPSTREAM_BLOCK", block))
        elif isinstance(block, dict) and isinstance(block.get("code"), str) and isinstance(block.get("message"), str):
            # 保留来源冲突ID等证据，但拒绝不能JSON表示的字段。
            _hash(block)
            report["reasons"].append(copy.deepcopy(block))
        else:
            raise AssessmentError("INVALID_INPUT", "Each blocking reason must contain code and message.")
    if len(summary["eligible_family_ids"]) < 2:
        report["reasons"].append(_reason("INSUFFICIENT_ELIGIBLE_FAMILIES", "Fewer than 2 families meet the source and sample requirements."))
    if report["reasons"]:
        return report
    try:
        matrix105 = np.asarray(raw105, dtype=np.float64)
        logmel = np.asarray(raw_logmel, dtype=np.float64)
    except (TypeError, ValueError) as error:
        raise AssessmentError("INVALID_FEATURES", "Raw features must be numeric matrices.") from error
    if logmel.ndim == 3 and logmel.shape[1:] == (64, 32):
        logmel = logmel.reshape(len(logmel), 2048)
    _require(matrix105.shape == (len(rows), 105) and logmel.shape == (len(rows), 2048),
             "INVALID_FEATURES", "raw105 and raw2048 log-mel matrices must use the same row order as items.")
    eligible_set = set(summary["eligible_item_ids"])
    selected = [i for i, row in enumerate(rows) if row["item_id"] in eligible_set]
    # 固定音节身份排序，使导入表的物理行次序不成为隐藏的分折参数。
    selected.sort(key=lambda index: rows[index]["item_id"])
    cohort = [rows[i] for i in selected]
    raw94 = matrix105[np.ix_(selected, [i for i in range(105) if i not in EXCLUDED_INDICES])]
    patches = logmel[selected]
    if not np.isfinite(raw94).all() or not np.isfinite(patches).all():
        report["reasons"].append(_reason("NONFINITE_FEATURES", "Raw features in the scoring scope contain non-finite values; labels were not deleted and values were not imputed."))
        return report
    labels = np.asarray([row["family_id"] for row in cohort])
    groups = np.asarray([row["source_group_id"] for row in cohort])
    classes = summary["eligible_family_ids"]
    report["audit"]["cohort_item_ids"] = [r["item_id"] for r in cohort]
    report["audit"]["input_sha256"] = {"raw94": _array_hash(raw94), "raw_logmel": _array_hash(patches),
                                           "items": _hash(cohort)}
    report["audit"]["retained_feature_names"] = [name for i, name in enumerate(FEATURE_NAMES)
                                                   if i not in EXCLUDED_INDICES]
    try:
        accepted, plan = _plan_splits(cohort, labels, groups, progress, check_cancelled)
    except (ValueError, FloatingPointError) as error:
        if isinstance(error, AssessmentError) and error.code == "CANCELLED":
            raise
        report["reasons"].append(_reason("SPLIT_FAILURE", str(error)))
        return report
    report["plan"] = plan
    if len(accepted) < PRESET["repeats"]:
        report["reasons"].append(_reason("NO_FEASIBLE_GROUPED_PLAN", "The fixed 100 seeds did not yield 5 grouped three-fold schemes with complete training-class coverage."))
        return report
    _emit(progress, check_cancelled, "plan_fixed", message="All 5 fold schemes are frozen; starting within-fold fitting")
    repeats, completed = [], 0
    # 一线程避免本地标注工作台数值库过量占用；不改变预设或按结果重试。
    try:
        with threadpool_limits(limits=PRESET["numerical_threads"]), warnings.catch_warnings():
            warnings.simplefilter("error", ConvergenceWarning)
            warnings.simplefilter("error", RuntimeWarning)
            for repeat, candidate in enumerate(accepted, start=1):
                predictions = [None] * len(cohort)
                counts = np.zeros(len(cohort), dtype=int)
                fold_for_item = np.zeros(len(cohort), dtype=int)
                for fold, (train, test) in enumerate(candidate["folds"], start=1):
                    report["audit"]["fits_started"] += 1
                    train_views, test_views, view_summaries = [], [], []
                    for name, raw in (("acoustic94", raw94), ("logmel", patches)):
                        left, right, fitted = _fit_view(raw[train], raw[test], name,
                                                       progress, check_cancelled, completed, repeat, fold)
                        train_views.append(left)
                        test_views.append(right)
                        view_summaries.append(fitted)
                    x_train, x_test = np.hstack(train_views), np.hstack(test_views)
                    _emit(progress, check_cancelled, "classification", completed,
                          repeat=repeat, fold=fold, message="Fitting the fixed classifier and predicting held-out sources")
                    arguments = {k: v for k, v in PRESET["classifier"].items() if k != "name"}
                    model = LogisticRegression(**arguments)
                    model.fit(x_train, labels[train])
                    _check(check_cancelled)
                    predicted = model.predict(x_test)
                    _require(np.isfinite(model.coef_).all() and np.isfinite(model.intercept_).all(),
                             "NUMERICAL_FAILURE", "The classifier parameters contain non-finite values.")
                    for row_index, label in zip(test, predicted):
                        predictions[row_index] = str(label)
                    counts[test] += 1
                    fold_for_item[test] = fold
                    report["audit"]["folds"].append({
                        "repeat": repeat, "fold": fold, "seed": candidate["seed"],
                        "fit_item_ids": [cohort[i]["item_id"] for i in train],
                        "n_train": len(train), "n_test": len(test), "views": view_summaries,
                        "fused_components": x_train.shape[1], "classifier_n_iter": model.n_iter_.tolist(),
                        "classifier_class_order": model.classes_.tolist(),
                    })
                    constant_views = [v["name"] for v in view_summaries if v["no_variance"]]
                    if constant_views:
                        report["warnings"].append(_reason(
                            "CONSTANT_TRAINING_VIEW", "A training-fold view is constant; its zero representation and actual uninformative predictions are retained.",
                            repeat=repeat, fold=fold, views=constant_views,
                            all_views_constant=len(constant_views) == 2))
                    completed += 1
                    _emit(progress, check_cancelled, "fold_completed", completed,
                          repeat=repeat, fold=fold, message="One source-isolated fold completed")
                _require(bool(np.all(counts == 1)) and all(p is not None for p in predictions),
                         "OOF_COVERAGE_FAILURE", "Each syllable must have exactly one out-of-fold prediction per repetition.")
                confusion = confusion_matrix(labels, predictions, labels=classes)
                recalls = np.diag(confusion) / confusion.sum(axis=1)
                repeats.append({
                    "repeat": repeat, "seed": candidate["seed"], "n": len(cohort),
                    "balanced_accuracy": float(recalls.mean()),
                    "macro_f1": float(f1_score(labels, predictions, labels=classes,
                                               average="macro", zero_division=0)),
                    "recall": dict(zip(classes, recalls.tolist())), "confusion_counts": confusion.tolist(),
                    "predictions": [{"item_id": row["item_id"], "source_group_id": row["source_group_id"],
                                     "true_family_id": row["family_id"], "predicted_family_id": predicted,
                                     "fold": int(fold_for_item[i])}
                                    for i, (row, predicted) in enumerate(zip(cohort, predictions))],
                })
    except (ConvergenceWarning, RuntimeWarning, ValueError, FloatingPointError, np.linalg.LinAlgError) as error:
        if isinstance(error, AssessmentError) and error.code == "CANCELLED":
            raise
        code = ("CLASSIFIER_NONCONVERGENCE" if isinstance(error, ConvergenceWarning)
                else error.code if isinstance(error, AssessmentError) else "NUMERICAL_FAILURE")
        report["reasons"].append(_reason(code, str(error), completed_folds=completed))
        # 前面已完成的折仅保留拟合审计，不发布选择性部分得分。
        return report
    _emit(progress, check_cancelled, "summarizing", completed, message="Summarizing out-of-fold predictions across repeated splits")
    report["status"] = "completed"
    report["repeats"] = repeats
    report["metrics"] = {
        key: {"mean": float(np.mean([r[key] for r in repeats])),
              "sd": float(np.std([r[key] for r in repeats], ddof=PRESET["standard_deviation_ddof"]))}
        for key in ("balanced_accuracy", "macro_f1")
    }
    report["metrics"]["sd_description"] = "Sample SD across five repeated grouped partitions; not CI or independent replication."
    normalized = np.asarray([np.asarray(r["confusion_counts"], dtype=float) /
                             np.asarray(r["confusion_counts"], dtype=float).sum(axis=1, keepdims=True)
                             for r in repeats])
    mean, deviation = normalized.mean(axis=0), normalized.std(axis=0, ddof=1)
    report["per_family"] = [
        {"family_id": family, "n": int(np.sum(labels == family)),
         "recall_mean": float(mean[i, i]), "recall_sd": float(deviation[i, i]),
         "per_repeat_recall": [r["recall"][family] for r in repeats]}
        for i, family in enumerate(classes)]
    pairs = [{"actual_family_id": actual, "predicted_family_id": predicted,
              "rate_mean": float(mean[i, j]), "rate_sd": float(deviation[i, j])}
             for i, actual in enumerate(classes) for j, predicted in enumerate(classes)
             if i != j and mean[i, j] > 0]
    pairs.sort(key=lambda p: (-p["rate_mean"], p["actual_family_id"], p["predicted_family_id"]))
    report["confusion"] = {"family_ids": classes, "matrix_mean_row_normalized": mean.tolist(),
                           "top_pairs": pairs[:20], "top_pair_count_limit": 20,
                           "orientation": "rows=true_current_human_family; columns=predicted_family"}
    # 最后一道JSON有限性检查；不让NaN成为API成功结果。
    try:
        _hash(report)
    except (ValueError, TypeError) as error:
        report.update(status="not_estimable", metrics=None, per_family=[], confusion=None, repeats=[])
        report["reasons"].append(_reason("NONFINITE_REPORT", str(error)))
        # 此分支审计亦可能含非有限参数，明确移除其数值而不冒充成功。
        report["audit"]["folds"] = []
    return report
