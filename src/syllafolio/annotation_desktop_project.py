"""原生桌面技术原型的项目边界；计算、人工规则与事务仍由已有服务处理。

每个项目保存自己的冻结全局输出和独立review状态。此模块不启动HTTP、不创建
人工类型，也不把工程原型状态升级为真人实验记录。create_project会执行真正的
现行流程a；attach_prepared_dataset仅用于明确标识的既有冻结产物工程重放。
"""
from __future__ import annotations
from .annotation_english import system_error

import copy
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
from urllib.parse import urlsplit
import uuid

from . import annotation_stage_a as stage_a
from .annotation_manual_service import ManualAnnotationService
from .annotation_acoustics import AcousticDataset
from .annotation_assistant_contracts import canonical_hash
from .annotation_project_contract import (
    DEFAULT_BUDGET, build_project_contract, contract_sha256,
    validate_creation_options, validate_project_contract,
)
from .annotation_file_lock import OwnerFileLock
from .annotation_desktop_import_timing import ImportTiming, read_import_timing, verify_import_inspection, apply_inspection_integrity


LEGACY_SCHEMA_VERSION = "syllafolio-desktop-project-v1"
SCHEMA_VERSION = "syllafolio-desktop-project-v2"
RUN_RELATIVE = "runs/run-0001"
DATASET_RELATIVE = RUN_RELATIVE + "/dataset"
REVIEW_RELATIVE = "review"
PROJECT_FILENAME = "project.json"
CONTRACT_RELATIVE = "contracts/run-0001.json"
INSPECTION_FILENAME = "import_inspection.json"
ORIGINAL_SEGMENTS_FILENAME = "import_segments.original.json"
ORIGINS = {"imported_audio", "prepared_engineering_replay"}


class DesktopProjectError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _require(condition, code, message):
    if not condition:
        raise DesktopProjectError(code, message)


def _utc():
    return datetime.now(timezone.utc).isoformat()


def _sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_json(path):
    def pairs(values):
        result = {}
        for key, value in values:
            _require(key not in result, "PROJECT_JSON", 'The project manifest contains duplicate fields.')
            result[key] = value
        return result

    def invalid(_):
        raise DesktopProjectError("PROJECT_JSON", 'The project manifest contains a non-finite number.')

    try:
        return json.loads(Path(path).read_text(encoding="utf-8"),
                          object_pairs_hook=pairs, parse_constant=invalid)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DesktopProjectError("PROJECT_JSON", 'The desktop project manifest could not be read.') from exc


def _write_marker(root, marker):
    value = copy.deepcopy(marker)
    value["updated_at"] = _utc()
    target = root / PROJECT_FILENAME
    temporary = root / (".project-" + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                        allow_nan=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    marker.update(value)


def _relative_path(root, relative, *, must_exist=False):
    _require(isinstance(relative, str) and relative
             and not Path(relative).is_absolute() and ".." not in Path(relative).parts,
             "PROJECT_PATH", 'Project assets must use relative paths inside the project.')
    path = root / relative
    _require(path.resolve().is_relative_to(root), "PROJECT_PATH", 'A resource path points outside the project.')
    cursor = path
    while cursor != root:
        _require(not _is_link(cursor), "PROJECT_PATH", 'Project resources cannot be symbolic links or directory junctions.')
        cursor = cursor.parent
    _require(not must_exist or path.exists(), "PROJECT_PATH", 'A project resource is missing.')
    return path


def _is_link(path):
    path = Path(path)
    try:
        return path.is_symlink() or bool(getattr(path.lstat(), "st_file_attributes", 0) & 0x400)
    except FileNotFoundError:
        return False


def _reject_link_ancestors(path):
    path = Path(path).absolute()
    for part in (path, *path.parents):
        _require(not _is_link(part), "PROJECT_PATH", 'The project path cannot pass through a symbolic link or directory junction.')


def _new_project(project_dir, *, origin, source_root=None, policy):
    requested = Path(project_dir).expanduser()
    _reject_link_ancestors(requested)
    _require(not requested.exists() and not requested.is_symlink(),
             "PROJECT_EXISTS", 'Choose a new project directory that does not already exist.')
    root = requested.resolve()
    if source_root is not None:
        _require(not root.is_relative_to(Path(source_root).resolve()),
                 "PROJECT_LOCATION", 'Choose a project location outside the source directory.')
    try:
        root.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise DesktopProjectError("PROJECT_EXISTS", 'Another operation created the project directory.') from exc
    marker = {"schema_version": SCHEMA_VERSION, "project_id": "desktop-" + uuid.uuid4().hex,
              "created_at": _utc(), "status": "creating", "origin": origin,
              "source": policy["evidence_source"], "purpose": policy["purpose"],
              "operator_id": policy["operator_id"], "budget_seconds": policy["budget_seconds"],
              "experiment_enabled": False, "model_calls_allowed": False,
              "run_relative": RUN_RELATIVE, "dataset_relative": DATASET_RELATIVE,
              "review_relative": REVIEW_RELATIVE}
    marker["run_id"] = marker["project_id"]
    _write_marker(root, marker)
    return root, marker


def _failure(root, marker, error):
    marker.update(status="cancelled" if getattr(error, "code", "") == "CANCELLED" else "failed",
                  error={"code": getattr(error, "code", "PROJECT_PREPARATION_FAILED"),
                         "message": system_error(error)})
    _write_marker(root, marker)


def _ready(root, marker, result):
    marker.update(status="ready", source_fingerprint=result["source_fingerprint"],
                  manifest_sha256=_sha(root / DATASET_RELATIVE / "manifest.json"),
                  preparation_summary=_preparation_summary(result))
    marker.pop("error", None)
    _write_marker(root, marker)


def _preparation_summary(result):
    return {key: copy.deepcopy(value) for key, value in result.items()
            if key not in {"dataset_dir", "suggestions_path"}}


def _finish(root, marker, result):
    dataset = AcousticDataset.from_files(root / DATASET_RELATIVE / "manifest.json",
                                         root / DATASET_RELATIVE / "features.npy",
                                         root / DATASET_RELATIVE / "logmel.npy")
    contract = build_project_contract(
        project_id=marker["project_id"], run_id=marker["run_id"], purpose=marker["purpose"],
        operator_id=marker["operator_id"], budget_seconds=marker["budget_seconds"],
        evidence_source=marker["source"], input_fingerprint=dataset.input_fingerprint,
        input_versions=dataset.input_versions, allowed_item_ids=dataset.item_ids,
        preset_id=dataset.preset["preset_id"], preset_sha256=canonical_hash(dataset.preset))
    contract_path = root / CONTRACT_RELATIVE
    contract_path.parent.mkdir(parents=True, exist_ok=True)
    _require(not contract_path.exists(), "PROJECT_CONTRACT", 'The immutable run contract already exists.')
    contract_path.write_text(json.dumps(contract, ensure_ascii=False, sort_keys=True,
                                        allow_nan=False, indent=2) + "\n", encoding="utf-8")
    marker.update(contract_relative=CONTRACT_RELATIVE, contract_version=contract["schema_version"],
                  contract_sha256=contract_sha256(contract), contract_file_sha256=_sha(contract_path),
                  active_run_id=marker["run_id"])
    service = ManualAnnotationService(root / DATASET_RELATIVE, root / REVIEW_RELATIVE,
                                      run_id=marker["run_id"], source=marker["source"],
                                      project_contract=contract)
    try:
        _ready(root, marker, result)
    except Exception:
        service.close()
        raise
    return DesktopProject(root, marker, service)


def _inspection_snapshot(input_dir, plan):
    """原始描述字节/名称映射的审阅身份与数值source fingerprint分别保存。"""
    from .annotation_desktop_stage_a import input_inspection_identity
    source = Path(input_dir).expanduser().resolve()
    identity = input_inspection_identity(source, plan)
    raw = (source / "segments.json").read_bytes() if identity["has_sidecar"] else None
    if raw is not None:
        _require(hashlib.sha256(raw).hexdigest() == identity["segments_json_sha256"],
                 "INPUT_CHANGED", 'The original segment description changed while it was being read. Inspect the input again.')
        entries = json.loads(raw.decode("utf-8"))["items"]
    else:
        entries = [{"file": row["filename"]} for row in plan["items"]]
    _require(len(entries) == len(plan["items"]), "INPUT_CHANGED", 'The original mapping does not match the inspected scope.')
    mapping = [{"item_id": row["item_id"], "original_item_id": entry.get("item_id", f"input-{index + 1:04d}"),
                "original_file": (source / entry["file"]).resolve().relative_to(source).as_posix()}
               for index, (row, entry) in enumerate(zip(plan["items"], entries))]
    return identity, raw, mapping


def _write_inspection(root, marker, identity, raw, mapping):
    if raw is not None:
        (root / ORIGINAL_SEGMENTS_FILENAME).write_bytes(raw)
    value = {"schema_version": "syllafolio-desktop-import-inspection-v1",
             "project_id": marker["project_id"], "run_id": marker["run_id"],
             "inspection_fingerprint": canonical_hash(identity), "inspection_identity": identity,
             "original_mapping": mapping,
             "original_segments_relative": ORIGINAL_SEGMENTS_FILENAME if raw is not None else None}
    (root / INSPECTION_FILENAME).write_text(json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + "\n", encoding="utf-8")


def _verify_inspected_import(root, input_dir, plan, identity, mapping):
    from .annotation_desktop_stage_a import input_inspection_identity
    _require(input_inspection_identity(input_dir, plan) == identity,
             "INPUT_CHANGED", 'The input description or a name changed during import. Inspect the input again.')
    receipt = _read_json(root / RUN_RELATIVE / "coordinator/source/import_receipt.json")
    frozen_mapping = [{key: row.get(key) for key in ("item_id", "original_item_id", "original_file")}
                      for row in receipt.get("original_mapping", [])]
    _require(frozen_mapping == mapping, "INPUT_CHANGED", 'The frozen mapping of original IDs to files differs from the one you reviewed.')
    if identity["has_sidecar"]:
        _require(_sha(root / ORIGINAL_SEGMENTS_FILENAME) == identity["segments_json_sha256"],
                 "INPUT_CHANGED", 'The reviewed copy of the original segment description has changed.')


def create_project(input_dir, project_dir, progress=None, cancelled=None, *,
                   purpose="engineering", operator_id=None, budget_seconds=DEFAULT_BUDGET,
                   expected_source_fingerprint=None, expected_inspection_fingerprint=None,
                   _annotation_evidence=None, bandpass=None):
    """冻结真实已分割输入并运行现行流程a；仅新项目，失败留诊断清单。

    此调用包含特征提取和聚类，桌面界面应在工作线程调用。progress接收原流程a
    字典，cancelled为可选的无参数布尔回调。完成后返回持有服务owner的项目。
    """
    policy = validate_creation_options(purpose=purpose, operator_id=operator_id,
                                       budget_seconds=budget_seconds)
    _require(expected_source_fingerprint is None or isinstance(expected_source_fingerprint, str)
             and bool(expected_source_fingerprint), "INPUT_CHANGED", 'Invalid input inspection identity.')
    _require(expected_inspection_fingerprint is None or isinstance(expected_inspection_fingerprint, str)
             and bool(expected_inspection_fingerprint), "INPUT_CHANGED", 'Invalid reviewed input identity.')
    timing, root, marker, project = ImportTiming("imported_audio"), None, None, None
    try:
        timing.progress({"stage": "starting", "message": "Inspecting the complete input before creating a project.",
                         "completed": 0, "total": 0}, progress)
        plan = stage_a.inspect_source(input_dir, cancelled=cancelled)
        from .acoustics.bandpass import validate_bandpass
        bandpass = validate_bandpass(bandpass, plan["sample_rate_hz"])
        _require(expected_source_fingerprint is None or plan["source_fingerprint"] == expected_source_fingerprint,
                 "INPUT_CHANGED", 'The input changed after the summary inspection. Check the full scope again.')
        inspection, original_segments, original_mapping = _inspection_snapshot(input_dir, plan)
        inspection_fingerprint = canonical_hash(inspection)
        _require(expected_inspection_fingerprint is None or inspection_fingerprint == expected_inspection_fingerprint,
                 "INPUT_CHANGED", 'The original description, IDs or filenames changed after review. Inspect the input again.')
        timing.set_input(plan["source_fingerprint"], summary={
            **{key: copy.deepcopy(plan[key]) for key in ("item_count", "sample_rate_hz", "total_duration_s", "context_available_count")},
            "segment_only_count": plan["item_count"] - plan["context_available_count"],
            "has_sidecar": inspection["has_sidecar"], "segments_json_sha256": inspection["segments_json_sha256"]})
        timing.input_identity["inspection_fingerprint"] = inspection_fingerprint
        if cancelled and cancelled():
            raise stage_a.StageAError("CANCELLED", 'Project creation was cancelled before any output was written.')
        root, marker = _new_project(project_dir, origin="imported_audio", source_root=input_dir, policy=policy)
        timing.bind(root, marker)
        if _annotation_evidence is not None:
            marker["source_annotations_sha256"] = _annotation_evidence.write(root)
        _write_inspection(root, marker, inspection, original_segments, original_mapping)
        current_inspection, _raw, _mapping = _inspection_snapshot(input_dir, plan)
        _require(current_inspection == inspection, "INPUT_CHANGED", 'The reviewed input identity changed before the calculation started.')
        result = stage_a.prepare_stage_a(input_dir, root / RUN_RELATIVE,
                                       progress=lambda event: timing.progress(event, progress), cancelled=cancelled,
                                       bandpass=bandpass)
        _require(result["source_fingerprint"] == plan["source_fingerprint"],
                 "INPUT_CHANGED", 'The frozen scope differs from the first inspection. The new project was not published.')
        _verify_inspected_import(root, input_dir, plan, inspection, original_mapping)
        timing.transition("validate")
        project = _finish(root, marker, result)
        timing.set_input(result["source_fingerprint"], manifest_sha256=marker["manifest_sha256"],
                         input_fingerprint=project.dataset.input_fingerprint)
        timing.finish("succeeded")
        project.import_timing_diagnostics = copy.deepcopy(timing.diagnostics)
        return project
    except Exception as exc:
        if project is not None:
            project.close()
        recorded = timing.finish("cancelled" if getattr(exc, "code", "") == "CANCELLED" else "failed")
        if root is not None:
            _failure(root, marker, exc)
        if progress:
            try:
                progress({"stage": recorded["state"], "message": system_error(exc), "timing": recorded})
            except Exception:
                pass
        raise


def attach_prepared_dataset(dataset_dir, project_dir, *, progress=None, cancelled=None):
    """复制已完整验收的流程a输出作独立工程重放，不冒充新输入计算。

    dataset_dir须是完整流程a输出中的dataset目录，不能是任意旧人工工作台或只有
    特征数组的目录。冻结源只读，原review数据库不会复制到新桌面项目。
    """
    policy = validate_creation_options(purpose="engineering")
    timing, root, marker, project = ImportTiming("prepared_engineering_replay"), None, None, None
    try:
        timing.progress({"stage": "replay_validate", "message": "Validating prepared engineering source; no features are being extracted."}, progress)
        dataset = Path(dataset_dir).expanduser().resolve()
        _require(dataset.name == "dataset" and dataset.is_dir(),
                 "PREPARED_DATASET", 'Choose the dataset directory from a complete candidate preparation run.')
        source_root = dataset.parent
        original = stage_a.validate_stage_a(source_root)
        original_manifest = _sha(dataset / "manifest.json")
        original_provenance = _sha(source_root / "global/provenance.json")
        timing.set_input(original["source_fingerprint"])
        if cancelled and cancelled():
            raise stage_a.StageAError("CANCELLED", 'Replay of the prepared engineering data was cancelled before any output was written.')
        root, marker = _new_project(project_dir, origin="prepared_engineering_replay",
                                   source_root=source_root, policy=policy)
        timing.bind(root, marker)
        for path in source_root.rglob("*"):
            _require(not _is_link(path), "PROJECT_PATH", 'The source of a prepared engineering project cannot contain symbolic links or directory junctions.')
        timing.progress({"stage": "replay_copy", "message": "Copying the frozen engineering output; not a new import fit."}, progress)
        shutil.copytree(source_root, root / RUN_RELATIVE)
        if cancelled and cancelled():
            raise stage_a.StageAError("CANCELLED", 'Replay was cancelled. Diagnostics for the incomplete run were retained.')
        timing.progress({"stage": "validate", "message": "Validating the copied engineering replay and project state."}, progress)
        result = stage_a.validate_stage_a(root / RUN_RELATIVE)
        _require(result["source_fingerprint"] == original["source_fingerprint"]
                 and _sha(root / DATASET_RELATIVE / "manifest.json") == original_manifest
                 and _sha(root / RUN_RELATIVE / "global/provenance.json") == original_provenance,
                 "INPUT_CHANGED", 'The inputs or source records of the prepared project changed during copying.')
        project = _finish(root, marker, result)
        timing.set_input(result["source_fingerprint"], manifest_sha256=marker["manifest_sha256"],
                         input_fingerprint=project.dataset.input_fingerprint)
        timing.finish("succeeded")
        project.import_timing_diagnostics = copy.deepcopy(timing.diagnostics)
        return project
    except Exception as exc:
        if project is not None:
            project.close()
        recorded = timing.finish("cancelled" if getattr(exc, "code", "") == "CANCELLED" else "failed")
        if root is not None:
            _failure(root, marker, exc)
        if progress:
            try:
                progress({"stage": recorded["state"], "message": system_error(exc), "timing": recorded})
            except Exception:
                pass
        raise


def _validate_marker(root):
    path = _relative_path(root, PROJECT_FILENAME, must_exist=True)
    marker = _read_json(path)
    _require(isinstance(marker, dict) and marker.get("schema_version") in {SCHEMA_VERSION, LEGACY_SCHEMA_VERSION},
             "PROJECT_SCHEMA", 'This is not a supported Syllafolio desktop project.')
    _require(marker.get("status") == "ready", "PROJECT_NOT_READY",
             'Project creation did not finish. Check the failure or cancellation record in project.json, then create a new project.')
    legacy = marker["schema_version"] == LEGACY_SCHEMA_VERSION
    _require(marker.get("origin") in ORIGINS and marker.get("experiment_enabled") is False,
             "PROJECT_SCOPE", 'Unsupported project purpose or material origin.')
    if legacy:
        _require(marker.get("source") == "engineering_user" and marker.get("purpose", "engineering") == "engineering",
                 "PROJECT_SCOPE", 'A P5 project must retain its original engineering purpose.')
    else:
        _require(marker.get("purpose") in {"engineering", "daily_annotation"}
                 and marker.get("model_calls_allowed") is False,
                 "PROJECT_SCOPE", 'Unsupported project purpose or model permissions.')
        _require(marker.get("origin") != "prepared_engineering_replay" or marker["purpose"] == "engineering",
                 "PROJECT_SCOPE", 'Prepared replay is available only for engineering projects.')
    _require(isinstance(marker.get("project_id"), str)
             and re.fullmatch(r"desktop-[a-f0-9]{32}", marker["project_id"])
             and marker.get("run_id") == marker["project_id"],
             "PROJECT_IDENTITY", 'Invalid project run identity.')
    for field, expected in [("run_relative", RUN_RELATIVE), ("dataset_relative", DATASET_RELATIVE),
                            ("review_relative", REVIEW_RELATIVE)]:
        _require(marker.get(field) == expected, "PROJECT_PATH", 'The internal project paths do not match.')
        _relative_path(root, expected, must_exist=field != "review_relative")
    if not legacy:
        _require(marker.get("active_run_id") == marker["run_id"]
                 and marker.get("contract_relative") == CONTRACT_RELATIVE,
                 "PROJECT_CONTRACT", 'The active run or contract reference does not match.')
        contract_path = _relative_path(root, CONTRACT_RELATIVE, must_exist=True)
        contract = validate_project_contract(_read_json(contract_path), run_id=marker["run_id"])
        _require(contract_sha256(contract) == marker.get("contract_sha256")
                 and _sha(contract_path) == marker.get("contract_file_sha256")
                 and contract.get("schema_version") == marker.get("contract_version")
                 and contract.get("project_id") == marker["project_id"]
                 and all(contract.get(key) == marker.get(key) for key in ("purpose", "operator_id", "budget_seconds"))
                 and contract.get("evidence_source") == marker.get("source"),
                 "PROJECT_CONTRACT", 'The project identity does not match its immutable run contract.')
    _require(_sha(root / DATASET_RELATIVE / "manifest.json") == marker.get("manifest_sha256"),
             "PROJECT_IDENTITY", 'The project input manifest has changed.')
    if "source_annotations_sha256" in marker:
        source_manifest = _relative_path(root, "source_annotations/import.json", must_exist=True)
        _require(_sha(source_manifest) == marker["source_annotations_sha256"], "PROJECT_IDENTITY",
                 "The recording interval manifest has changed.")
        recorded = _read_json(source_manifest)
        _require(recorded.get("annotation_file") in {"boundaries.txt", "boundaries.TextGrid"},
                 "PROJECT_PATH", "The project annotation table path is invalid.")
        original_table = _relative_path(root, "source_annotations/" + recorded["annotation_file"], must_exist=True)
        _require(_sha(original_table) == recorded.get("annotation_sha256"), "PROJECT_IDENTITY",
                 "The saved recording annotation file has changed.")
    result = stage_a.validate_stage_a(root / RUN_RELATIVE)
    _require(result["source_fingerprint"] == marker.get("source_fingerprint"),
             "PROJECT_IDENTITY", "The project's frozen source identity has changed.")
    # An older marker stores this generated capacity explanation in its original
    # language. Compare its English equivalent, keeping all identity fields exact.
    from .annotation_english import system_text
    saved_summary = copy.deepcopy(marker.get("preparation_summary"))
    requirements = saved_summary.get("input_requirements") if isinstance(saved_summary, dict) else None
    if isinstance(requirements, dict) and "capacity_note" in requirements:
        requirements["capacity_note"] = system_text(requirements["capacity_note"])
    _require(_preparation_summary(result) == saved_summary,
             "PROJECT_IDENTITY", 'The project summary differs from the verified frozen candidates.')
    review = root / REVIEW_RELATIVE
    if review.exists():
        _require(review.is_dir() and not any(_is_link(p) for p in review.rglob("*")),
                 "PROJECT_PATH", 'Manual project records cannot point outside the project directory.')
    _require((review / "state.sqlite3").is_file(), "PROJECT_STATE_MISSING", "The project's manual review state is missing. Its history cannot be replaced with an empty run.")
    return marker


def _persisted_contract(root, marker):
    """Opening must never silently replace a missing run with an empty one."""
    database = root / REVIEW_RELATIVE / "state.sqlite3"
    try:
        # Even mode=ro can create/change WAL shared-memory files. Inspect a
        # detached copy while the caller holds the existing source-owner lock.
        with tempfile.TemporaryDirectory(prefix="desktop-contract-check-") as temporary:
            copied = Path(temporary) / "state.sqlite3"
            shutil.copy2(database, copied)
            for suffix in ("-wal", "-shm"):
                if database.with_name(database.name + suffix).exists():
                    shutil.copy2(database.with_name(database.name + suffix), copied.with_name(copied.name + suffix))
            with closing(sqlite3.connect(copied.as_uri() + "?mode=ro", uri=True)) as connection:
                row = connection.execute("SELECT contract_json,contract_sha256,state_json FROM runs WHERE run_id=?",
                                         (marker["run_id"],)).fetchone()
        _require(row is not None, "PROJECT_STATE_MISSING", 'The original run is missing. Its history cannot be replaced with an empty run.')
        contract, digest, state = json.loads(row[0]), row[1], json.loads(row[2])
        _require(contract_sha256(contract) == digest and state.get("contract") == contract
                 and state.get("run_id") == marker["run_id"],
                 "PROJECT_CONTRACT", 'The database contract, identity or contract hash does not match.')
        return contract
    except (sqlite3.Error, OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        if isinstance(exc, DesktopProjectError):
            raise
        raise DesktopProjectError("PROJECT_STATE", 'Could not read the original project run and contract.') from exc


def open_project(project_dir):
    """只读重验冻结输入，再用原手动服务恢复同一run与SQLite历史。"""
    requested = Path(project_dir).expanduser()
    _reject_link_ancestors(requested)
    _require(requested.is_dir() and not _is_link(requested),
             "PROJECT_DIRECTORY", 'The desktop project directory is missing or is a symbolic link.')
    root = requested.resolve()
    if not (root / PROJECT_FILENAME).exists():
        raise DesktopProjectError("PROJECT_SCHEMA", 'Open a complete Syllafolio desktop project. A web workspace cannot be opened here.')
    marker = _validate_marker(root)
    contract = _read_json(root / CONTRACT_RELATIVE) if marker["schema_version"] == SCHEMA_VERSION else None
    lock_path = root / REVIEW_RELATIVE / "state.sqlite3.owner.lock"
    _require(lock_path.is_file() and not _is_link(lock_path), "PROJECT_OWNER_LOCK", 'The original project owner lock is missing.')
    try:
        with OwnerFileLock(lock_path):
            stored_before = _persisted_contract(root, marker)
    except BlockingIOError as exc:
        raise DesktopProjectError("STORE_IN_USE", 'This project is already open.') from exc
    _require(contract is None or stored_before == contract,
             "PROJECT_CONTRACT", 'The database disagrees with the immutable project contract.')
    if contract is None:
        _require(stored_before.get("schema_version") == "0.2.0"
                 and stored_before.get("mode") == "engineering"
                 and stored_before.get("experiment_enabled") is False,
                 "PROJECT_CONTRACT", 'A P5 project cannot be converted to a different purpose.')
    service = ManualAnnotationService(root / DATASET_RELATIVE, root / REVIEW_RELATIVE,
                                      run_id=marker["run_id"], source=marker["source"],
                                      project_contract=contract)
    try:
        stored = service.store.snapshot(marker["run_id"])["contract"]
        if contract is not None:
            _require(stored == contract, "PROJECT_CONTRACT", 'The database state does not match the project contract.')
        else:
            _require(stored.get("schema_version") == "0.2.0" and stored.get("mode") == "engineering"
                     and stored.get("experiment_enabled") is False,
                     "PROJECT_CONTRACT", 'The P5 project contract has a different purpose or version.')
    except Exception:
        service.close()
        raise
    return DesktopProject(root, marker, service)


def backup_project(source_dir, destination_dir):
    """持原项目owner锁离线备份；不打开/恢复源服务，不升级容器或用途。"""
    from .annotation_desktop_backup import backup_project as copy_project
    return copy_project(source_dir, destination_dir)


class DesktopProject:
    """桌面适配层：UI只收集人工输入，数据写入仍经过原服务守卫。"""

    def __init__(self, project_dir, marker, service):
        self.project_dir = Path(project_dir).resolve()
        self.metadata = copy.deepcopy(marker)
        self.service = service
        self.dataset = service.dataset
        self.import_timing_diagnostics = []
        self._import_inspection_integrity = None

    def snapshot(self):
        # The DB copy, child runtime progress and worker maps are one sample.
        # Worker callbacks publish/remove their runtime values under this lock.
        with self.service._lock:
            result = self._snapshot_core()
        return self._finish_snapshot(result)

    def try_snapshot(self):
        """GUI entry: never wait behind a long owner operation; None is pending."""
        if not self.service._lock.acquire(blocking=False):
            return None
        try:
            result = self._snapshot_core()
        finally:
            self.service._lock.release()
        return self._finish_snapshot(result)

    def _snapshot_core(self):
        result = self.service.snapshot()
        # Legacy completed exports may have had their top-level mode replaced
        # by project metadata. Preserve raw history and expose the frozen mode.
        for job in result.get("final_review", {}).get("publication_report", {}).get("jobs", []):
            frozen_mode = (job.get("metadata") or {}).get("mode")
            job["delivery_mode"] = (frozen_mode if frozen_mode in {"draft", "complete"} else
                                    job.get("mode") if job.get("mode") in {"draft", "complete"} else None)
        result["desktop_project"] = copy.deepcopy(self.metadata)
        from .annotation_desktop_jobs import project_job_view
        result["desktop_jobs"] = project_job_view(result, self.service,
                                                  project_id=self.metadata["project_id"])
        return result

    def _finish_snapshot(self, result):
        # Sidecar validation and display work must not extend the owner lock.
        result["fine_submission"] = {"policy": "ordinary_closed_first",
                                     "blocked_items": self._fine_blocked_items(result)}
        result["desktop_view"] = self._desktop_view(result)
        from .annotation_desktop_stage_a import project_stage_a_view
        timing = read_import_timing(self.project_dir, self.metadata)
        if self._import_inspection_integrity is None:
            self._import_inspection_integrity = verify_import_inspection(self.project_dir, self.metadata)
        timing = apply_inspection_integrity(timing, self._import_inspection_integrity)
        for diagnostic in self.import_timing_diagnostics:
            if diagnostic not in timing["diagnostics"]:
                timing["diagnostics"].append(copy.deepcopy(diagnostic))
        result["stage_a"] = project_stage_a_view(self, result, timing=timing)
        return result

    def _desktop_view(self, state):
        """Pure projection of the one owner's snapshot, without extra reads."""
        contract, batch = state.get("contract", {}), state.get("batch_review", {})
        pool = batch.get("pool", {})
        organized = bool(batch.get("organization"))
        first_done, pool_done = bool(pool.get("first_pass_complete")), bool(pool.get("stage_complete"))
        final = state.get("final_review", {})
        final_pool = final.get("stages", {}).get("pool", {})
        returned_to_d = bool(final_pool.get("coverage_complete") and
            (final_pool.get("next_view") == "final" or final_pool.get("blocked_by_directed_ids")))
        current = "A" if not organized else "B" if not first_done else "C" if not (pool_done or returned_to_d) else "D"
        can_enter_pool = first_done or bool(organized and pool.get("active_ids"))
        blocked = {"A": [], "B": [] if organized else ["ORGANIZATION_REQUIRED"],
                   "C": [] if can_enter_pool else ["BATCH_FIRST_PASS_PENDING"],
                   "D": [] if pool_done or returned_to_d else ["POOL_REVIEW_PENDING"]}
        summary = {key: copy.deepcopy(contract.get(key)) for key in
                   ("schema_version", "purpose", "mode", "operator_id", "evidence_source", "record_kind", "budget_seconds")}
        summary.update(purpose=contract.get("purpose", "engineering"),
                       evidence_source=contract.get("evidence_source", self.metadata.get("source")),
                       record_kind=contract.get("record_kind", "engineering_runtime"))
        checkpoints = state.get("checkpoints", {})
        if isinstance(checkpoints, dict):
            checkpoint_rows = [{"checkpoint_id": key, **{field: row[field] for field in ("reason", "created_at") if field in row}}
                               for key, row in checkpoints.items() if isinstance(row, dict)]
        else:
            checkpoint_rows = [{field: copy.deepcopy(row[field]) for field in ("checkpoint_id", "reason", "created_at") if field in row}
                               for row in checkpoints if isinstance(row, dict)] if isinstance(checkpoints, list) else []
        return {
            "identity": {"project_id": self.metadata["project_id"], "run_id": self.service.run_id,
                         "purpose": contract.get("purpose", "engineering"), "origin": self.metadata.get("origin"),
                         "container_version": self.metadata.get("schema_version"), "experiment_enabled": False},
            "contract_summary": summary,
            "navigation": {"current_stage": current, "stages": [{"stage": stage,
                "current": stage == current, "blockers": blocked[stage],
                "native_business_complete": False, "native_scope": "d1_d2_d3_d4" if stage == "D" else "native_review"}
                for stage in ("A", "B", "C", "D")]},
            "completion": copy.deepcopy(state.get("completion", {})),
            "blockers": copy.deepcopy(state.get("completion", {}).get("missing", [])),
            "jobs": copy.deepcopy(state.get("desktop_jobs", {})),
            "results": {"assessment": copy.deepcopy(final.get("assessment", {})),
                        "catalogue_export": copy.deepcopy(final.get("publication_report", {}))},
            "drafts": {"managed_by": "desktop_ui", "submission_state": "unsubmitted",
                       "contents_in_service_projection": False, "restore_requires_confirmation": True},
            "checkpoints": {"items": checkpoint_rows, "new_native_ui_available": True},
        }

    @staticmethod
    def _fine_blocked_items(state):
        from .annotation_batch_review import _current_review
        blocked = {}
        batch = state.get("batch_review", {})
        for review in batch.get("reviews", {}).values():
            if review.get("closed") or review.get("archived") or review.get("directed_issue_id"):
                continue
            try:
                _current_review(batch, review["review_id"])
            except ValueError:
                continue  # Superseded/retired pages are historical, not closable current rounds.
            for item in review.get("item_ids", []):
                blocked.setdefault(item, []).append(review["review_id"])
        return {item: sorted(ids) for item, ids in blocked.items()}

    def execute(self, action, payload, *, expected_revision, idempotency_key):
        try:
            with self.service._lock:
                if (action == "final_item_review" and isinstance(payload, dict) and not payload.get("directed_review_id")
                        and self.service.store.lookup_event(self.service.run_id, idempotency_key) is None):
                    state = self.service.store.snapshot(self.service.run_id)
                    _require(payload.get("item_id") not in self._fine_blocked_items(state),
                             "FINE_REVIEW_PENDING", 'Save all family outcomes and finish the ordinary round before submitting a fine decision. Your fine review is still a draft.')
                result = self.service.execute(action, payload, expected_revision=expected_revision,
                                              idempotency_key=idempotency_key)
        except Exception as exc:
            exc.desktop_receipt = self._command_receipt(action, payload, expected_revision, idempotency_key, error=exc)
            raise
        if isinstance(result, dict):
            result = dict(result)
            result["desktop_receipt"] = self._command_receipt(action, payload, expected_revision, idempotency_key, result=result)
        return result

    def _command_receipt(self, action, payload, expected_revision, idempotency_key, *, result=None, error=None):
        try:
            request_hash = canonical_hash({"action": action, "payload": payload, "expected_revision": expected_revision})
        except (TypeError, ValueError):
            request_hash = None
        result = result or {}
        receipt = result.get("receipt") or {}
        code = getattr(error, "code", "COMMAND_FAILED") if error else None
        next_action = {"REVISION_CONFLICT": "refresh_and_reconfirm", "IDEMPOTENCY_CONFLICT": "check_original_request",
                       "FINE_REVIEW_PENDING": "close_current_ordinary_review", "BATCH_SCOPE_STALE": "inspect_changed_scope"}.get(code)
        return {"project_id": self.metadata["project_id"], "run_id": self.service.run_id,
                "action": action, "target": payload.get("item_id", payload.get("job_id", payload.get("review_id"))) if isinstance(payload, dict) else None,
                "request_key": idempotency_key, "request_hash": request_hash, "submitted_revision": expected_revision,
                "applied_revision": receipt.get("revision_after", receipt.get("revision")),
                "replayed": bool(result.get("replayed")), "status": "rejected" if error else "accepted",
                "error_code": code, "message": system_error(error) if error else "",
                "next_action": next_action or ("inspect_error_and_retry_explicitly" if error else None)}

    def read_audio(self, item_id, *, context=False):
        return self.dataset.read_audio(item_id, context=context)

    def media_path(self, url_or_id):
        _require(isinstance(url_or_id, str), "ASSET_SCOPE", 'Select a media asset from this project.')
        parsed = urlsplit(url_or_id)
        _require(not parsed.scheme and not parsed.netloc and not parsed.fragment,
                 "ASSET_SCOPE", 'This desktop project cannot load external media URLs.')
        ident = parsed.path.removeprefix("/api/media/")
        _require(re.fullmatch(r"asset-[a-f0-9]{24}", ident),
                 "ASSET_SCOPE", 'Invalid media reference.')
        return self.service.media_path(ident)

    def review_page(self, review_id, page_id):
        review = self.snapshot()["batch_review"]["reviews"].get(review_id)
        _require(review is not None, "REVIEW_NOT_FOUND", 'The review round does not exist in this project.')
        page = next((row for row in review.get("pages", []) if row["page_id"] == page_id), None)
        _require(page is not None, "PAGE_NOT_FOUND", 'The specified audition page does not exist in this round.')
        return copy.deepcopy(page)

    def close(self):
        self.service.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
