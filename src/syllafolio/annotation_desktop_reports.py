"""Per-run desktop evidence. Legacy root screenshots are never collected as new.

This standard-library module is also copied byte-for-byte to the portable root
as prototype_reports.py, so launch-time reporting need not import the app.
"""
from __future__ import annotations

from datetime import datetime, timezone
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import uuid
from zipfile import ZipFile, ZIP_DEFLATED

ARTIFACT_NAMES = {
    "automatic_checks.log", "backend_tests.log", "bundle_preflight.json",
    "runtime_config.json", "runtime.log", "startup.log", "launch_result.json",
    "desktop_1500.png", "desktop_small.png", "failure.png",
    "long_path_validation.json", "long_path_checks.log", "long_path_runtime.log", "long_path_workspace.png",
}
LAUNCH_ARTIFACT_NAMES = {
    "run_meta.json", "runtime_config.json", "startup.log", "launch_result.json",
    "runtime.log", "runtime.log.1", "runtime.log.2", "runtime.log.3",
}


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2,
                                        allow_nan=False) + "\n", encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def digest(path):
    checksum = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(block)
    return checksum.hexdigest()


def new_run_id(kind):
    return kind + "_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "_" + uuid.uuid4().hex[:8]


def _run_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
        raise ValueError("Invalid diagnostic run identity")
    return value


def _no_links(path):
    path = Path(path).absolute()
    if ".." in path.parts:
        raise ValueError("Diagnostic paths must not contain parent traversal")
    for current in (path, *path.parents):
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("Diagnostic paths must not be symbolic links or reparse points")
    return path.resolve()


def report_store_candidates(root):
    """同bundle的可发现存储位置；不从latest指针接收任意存储目录。"""
    root = Path(root).resolve()
    token = hashlib.sha256(os.path.normcase(str(root)).encode("utf-8")).hexdigest()[:16]
    values = [(root / "reports", "application_reports")]
    if os.environ.get("LOCALAPPDATA"):
        values.append((Path(os.environ["LOCALAPPDATA"]) / "Syllafolio/reports" / token, "local_app_data"))
    if os.environ.get("USERPROFILE"):
        values.append((Path(os.environ["USERPROFILE"]) / ".syllafolio/reports" / token, "user_profile"))
    if os.environ.get("XDG_STATE_HOME"):
        values.append((Path(os.environ["XDG_STATE_HOME"]) / "Syllafolio/reports" / token, "xdg_state"))
    values.append((Path(tempfile.gettempdir()) / "Syllafolio/reports" / token, "temporary_fallback"))
    found = set()
    output = []
    for value, source in values:
        value = value.absolute()
        key = os.path.normcase(str(value))
        if key not in found:
            found.add(key); output.append((value, source))
    return output


def _probe_store(path):
    path = _no_links(path)
    path.mkdir(parents=True, exist_ok=True)
    token = path / (".syllafolio-report-probe-" + uuid.uuid4().hex)
    error = None
    try:
        with token.open("xb") as stream:
            stream.write(b"Syllafolio report location probe\n")
        if token.read_bytes() != b"Syllafolio report location probe\n":
            raise OSError(errno.EIO, "Diagnostic location probe readback differs")
    except BaseException as caught:
        error = caught
        raise
    finally:
        try:
            token.unlink(missing_ok=True)
        except OSError as cleanup:
            if error is None:
                raise
            if hasattr(error, "add_note"):
                error.add_note("Diagnostic probe cleanup also failed: " + str(cleanup))


def _select_store(root):
    attempts = []
    for path, source in report_store_candidates(root):
        if os.name == "nt" and len(str(path).encode("utf-16-le")) // 2 + 90 > 248:
            attempts.append({"directory": str(path), "source": source, "accepted": False,
                             "reason": "windows_report_path_budget"})
            continue
        try:
            _probe_store(path)
        except OSError as error:
            # 只回退明确的不可写/只读位置；磁盘满、I/O损坏等真实故障必须抛出。
            if not (isinstance(error, PermissionError) or error.errno in {errno.EACCES, errno.EPERM, errno.EROFS}):
                raise
            attempts.append({"directory": str(path), "source": source, "accepted": False,
                             "reason": "report_location_not_writable", "error_type": type(error).__name__,
                             "error": str(error)})
            continue
        attempts.append({"directory": str(path), "source": source, "accepted": True,
                         "reason": "write_read_cleanup_probe_passed"})
        return path.resolve(), {"directory": str(path.resolve()), "source": source, "attempts": attempts,
                                "fallback_used": source != "application_reports"}
    raise OSError("No writable diagnostic report store: " + json.dumps(attempts, ensure_ascii=True))


def _registered_run(root, run_id):
    _run_id(run_id)
    root = Path(root).resolve()
    matches = []
    for store, _source in report_store_candidates(root):
        location = store / "index" / (run_id + ".json")
        _no_links(location)
        if not location.is_file():
            continue
        registered = read_json(location)
        if registered.get("bundle_root") != str(root) or registered.get("run_id") != run_id:
            raise ValueError("Diagnostic location belongs to a different bundle or run")
        declared = Path(registered["folder"])
        if not declared.is_absolute() or ".." in declared.parts or declared.name != run_id:
            raise ValueError("Invalid registered diagnostic location")
        matches.append((_no_links(declared), store.resolve()))
    if matches:
        if len({str(folder) for folder, _store in matches}) != 1:
            raise ValueError("Diagnostic run has conflicting registered locations")
        folder, store = matches[0]
    else:
        store = root / "reports"
        folder = _no_links(store / "runs" / run_id)
    meta_path = folder / "run_meta.json"
    _no_links(meta_path)
    if meta_path.is_file():
        meta = read_json(meta_path)
        if meta.get("bundle_root") != str(root) or meta.get("run_id") != run_id:
            raise ValueError("Diagnostic metadata belongs to a different bundle or run")
        bound = meta.get("report_storage", {}).get("directory")
        if bound is not None and Path(bound).resolve() != store:
            raise ValueError("Diagnostic run report store differs from its registration")
    return folder, store


def _safe_run(root, run_id):
    return _registered_run(root, run_id)[0]


def _location_label(root, folder):
    return folder.relative_to(root).as_posix() if folder.is_relative_to(root) else str(folder)


def begin_run(root, kind, run_id=None, config=None):
    root = Path(root).resolve()
    build = read_json(root / "BUILD_INFO.json")
    run_id = run_id or new_run_id(kind)
    _run_id(run_id)
    for candidate, _source in report_store_candidates(root):
        if (candidate / "index" / (run_id + ".json")).exists():
            raise ValueError("Diagnostic run identity is already registered")
    store, storage = _select_store(root)
    if config is not None:
        folder = Path(config["diagnostics_dir"]).resolve()
        if Path(config["bundle_root"]).resolve() != root or folder.name != run_id:
            raise ValueError("Runtime configuration does not match this bundle and run")
        folder = _no_links(folder)
    else:
        folder = _no_links(store / "runs" / run_id)
    folder.mkdir(parents=True, exist_ok=True)
    if (folder / "run_meta.json").exists():
        raise ValueError("A diagnostic run identity must not be reused")
    meta = {"schema": "syllafolio-diagnostic-run-v1", "run_id": run_id,
            "kind": kind, "bundle_version": build["version"], "bundle_root": str(root),
            "created_at": datetime.now(timezone.utc).isoformat(), "report_storage": storage}
    atomic_json(folder / "run_meta.json", meta)
    if config is not None:
        atomic_json(folder / "runtime_config.json", config)
    # 实际store在run元数据中绑定；发布后写入故障仍会抛出，不静默换store。
    atomic_json(store / "index" / (run_id + ".json"),
                {"bundle_root": str(root), "run_id": run_id, "folder": str(folder)})
    if kind == "check":
        old = store / "desktop_validation.json"
        if old.is_file():
            original = old.read_bytes()
            history = store / "history" / ("desktop_validation_" + hashlib.sha256(original).hexdigest()[:12] + ".json")
            history.parent.mkdir(parents=True, exist_ok=True)
            if not history.exists():
                history.write_bytes(original)
        publish_report(root, run_id, {"status": "starting", "automatic_checks_passed": False,
                       "windows_acceptance_complete": False, "errors": []})
    return folder


def publish_report(root, run_id, report):
    root = Path(root).resolve()
    folder, store = _registered_run(root, run_id)
    meta = read_json(folder / "run_meta.json")
    if report.get("run_id", run_id) != run_id:
        raise ValueError("Check report belongs to a different run")
    result = dict(report, run_id=run_id, bundle_version=meta["bundle_version"],
                  run_dir=_location_label(root, folder))
    atomic_json(folder / "desktop_validation.json", result)
    pointer = {"schema": "syllafolio-check-pointer-v1", "run_id": run_id,
               "bundle_version": meta["bundle_version"], "status": result["status"],
               "run_dir": result["run_dir"], "report_sha256": digest(folder / "desktop_validation.json"),
               "updated_at": datetime.now(timezone.utc).isoformat()}
    atomic_json(store / "latest_check.json", pointer)
    # Compatibility view only. Artifacts are always read from the bound run.
    atomic_json(store / "desktop_validation.json", result)
    return result


def publish_launch(root, run_id, report):
    """launcher唯一latest入口；返回真实pointer位置，不对失败写入报成功。"""
    root = Path(root).resolve()
    folder, store = _registered_run(root, run_id)
    meta = read_json(folder / "run_meta.json")
    if meta.get("kind") != "launch" or report.get("run_id", run_id) != run_id:
        raise ValueError("Launch report belongs to another run or kind")
    if report.get("diagnostics_dir", str(folder)) != str(folder):
        raise ValueError("Launch report has an invalid diagnostic directory")
    pointer = store / "latest_launch.json"
    result = dict(report, run_id=run_id, bundle_version=meta["bundle_version"], diagnostics_dir=str(folder),
                  pointer_path=str(pointer), updated_at=datetime.now(timezone.utc).isoformat())
    atomic_json(pointer, result)
    return result


def _latest_pointer(root, name):
    found = []
    for store, _source in report_store_candidates(root):
        path = store / name
        _no_links(path)
        if path.is_file():
            found.append((path.stat().st_mtime_ns, path, read_json(path)))
    return max(found, key=lambda row: row[0]) if found else None


def seal_artifacts(root, run_id, report):
    folder = _safe_run(root, run_id)
    artifacts = {}
    for path in folder.iterdir():
        allowed = path.name in ARTIFACT_NAMES or re.fullmatch(r"runtime\.log\.\d+", path.name)
        if not allowed or path.is_symlink() or not path.is_file():
            continue
        if path.name == "failure.png" and report.get("automatic_checks_passed") is True:
            continue
        artifacts[path.name] = {"sha256": digest(path), "bytes": path.stat().st_size}
    return publish_report(root, run_id, dict(report, artifacts=artifacts))


def latest_check(root):
    root = Path(root).resolve()
    selected = _latest_pointer(root, "latest_check.json")
    if selected is None:
        return None
    _mtime, pointer, value = selected
    build = read_json(root / "BUILD_INFO.json")
    if value.get("bundle_version") != build["version"]:
        return None
    folder = _safe_run(root, value["run_id"])
    if value.get("run_dir") != _location_label(root, folder):
        raise ValueError("Check pointer has an invalid run directory")
    path = folder / "desktop_validation.json"
    if digest(path) != value.get("report_sha256"):
        raise ValueError("Latest check report changed after it was recorded")
    result = read_json(path)
    if result.get("run_id") != value["run_id"]:
        raise ValueError("Check identity does not match its directory")
    return folder, result


def latest_launch(root):
    """Select only this bundle's latest launch; its live logs are not sealed."""
    root = Path(root).resolve()
    selected = _latest_pointer(root, "latest_launch.json")
    if selected is None:
        return None
    _mtime, pointer, value = selected
    folder = _safe_run(root, value["run_id"])
    declared = Path(value.get("diagnostics_dir", ""))
    if (not declared.is_absolute() or ".." in declared.parts or declared != folder):
        raise ValueError("Launch pointer has an invalid diagnostic directory")
    # External short directories must still be explicitly registered to the
    # same bundle/run. Never follow a pointer directly into an arbitrary folder.
    meta_path = folder / "run_meta.json"
    if meta_path.is_symlink() or not meta_path.is_file():
        raise ValueError("Launch metadata must be a regular file in its run")
    meta = read_json(meta_path)
    if (meta.get("bundle_root") != str(root) or meta.get("run_id") != value["run_id"]
            or meta.get("kind") != "launch"):
        raise ValueError("Launch metadata belongs to a different bundle, run or kind")
    build = read_json(root / "BUILD_INFO.json")
    if meta.get("bundle_version") != build["version"]:
        return None
    return folder, meta


def _snapshot_launch(selected):
    """Read bounded named files once; a running process may append afterwards."""
    if selected is None:
        return {}, None
    folder, meta = selected
    files = {}
    prefix = "launch/" + meta["run_id"] + "/"
    for name in sorted(LAUNCH_ARTIFACT_NAMES):
        path = folder / name
        if path.is_symlink():
            raise ValueError("Launch attachments must not be symbolic links")
        if path.exists():
            if not path.is_file():
                raise ValueError("Launch attachments must be regular files")
            # Read a single byte snapshot, not a hash followed by a second read.
            # Logging can rotate/append while feedback is being collected.
            try:
                with path.open("rb") as stream:
                    data = stream.read(32 * 1024 * 1024 + 1)
            except FileNotFoundError:
                if name == "run_meta.json":
                    raise ValueError("Launch metadata disappeared during collection")
                continue  # A rotating log disappeared before it could be opened.
            if len(data) > 32 * 1024 * 1024:
                raise ValueError("Launch diagnostic exceeds the snapshot size limit: " + name)
            files[prefix + name] = data
    recorded = json.loads(files[prefix + "run_meta.json"])
    if recorded != meta:
        raise ValueError("Launch metadata changed during collection")
    result_name = prefix + "launch_result.json"
    if result_name in files and json.loads(files[result_name]).get("run_id") != meta["run_id"]:
        raise ValueError("Launch result belongs to another run")
    return files, {"run_id": meta["run_id"], "bundle_version": meta["bundle_version"],
                   "snapshot": True, "consistency": "per_file_byte_snapshot",
                   "snapshot_at": datetime.now(timezone.utc).isoformat(),
                   "process_may_still_be_running": True}


def collect_feedback(root, feedback, *, manual_run_id=None):
    root = Path(root).resolve()
    build = read_json(root / "BUILD_INFO.json")
    selected = latest_check(root)
    launch_files, launch_snapshot = _snapshot_launch(latest_launch(root))
    if selected is None:
        manual_run_id = manual_run_id or new_run_id("manual")
        folder = begin_run(root, "manual", manual_run_id)
        report = {"run_id": manual_run_id, "status": "manual_only_no_current_automatic_check",
                  "bundle_version": build["version"], "artifacts": {}}
    else:
        folder, report = selected
    run_id = report["run_id"]
    fields = feedback.get("checks", {})
    if not isinstance(fields, dict) or any(v not in {"not_tested", "pass", "problem"} for v in fields.values()):
        raise ValueError("Manual checks must explicitly record pass, problem or not_tested")
    manual = dict(feedback, schema="syllafolio-windows-human-feedback-v2", bundle_version=build["version"],
                  automatic_run_id=run_id if selected else None,
                  launch_run_id=launch_snapshot["run_id"] if launch_snapshot else None,
                  collected_at=datetime.now(timezone.utc).isoformat(),
                  windows_acceptance_complete=False)
    atomic_json(folder / "manual_feedback.json", manual)
    files = {"manual_feedback.json": (folder / "manual_feedback.json").read_bytes(),
             "run_meta.json": (folder / "run_meta.json").read_bytes()}
    if selected:
        files["desktop_validation.json"] = (folder / "desktop_validation.json").read_bytes()
    for name, identity in report.get("artifacts", {}).items():
        if name not in ARTIFACT_NAMES and not re.fullmatch(r"runtime\.log\.\d+", name):
            raise ValueError("Unexpected attachment in the check report")
        if name == "failure.png" and report.get("automatic_checks_passed") is True:
            continue
        path = folder / name
        if path.is_symlink() or path.parent != folder:
            raise ValueError("Attachment must be a regular file in the selected run")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != identity["sha256"]:
            raise ValueError("Diagnostic attachment changed after the check: " + name)
        files[name] = data
    files.update(launch_files)
    summary = {"schema": "syllafolio-feedback-archive-v2", "run_id": run_id,
               "bundle_version": build["version"], "automatic_status": report["status"],
               "launch_snapshot": launch_snapshot,
               "files": {name: {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                         for name, data in files.items()}}
    for name in launch_files:
        summary["files"][name]["snapshot"] = True
    files["feedback_manifest.json"] = (json.dumps(summary, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    _folder, store = _registered_run(root, run_id)
    path = store / ("feedback_" + run_id + "_" + uuid.uuid4().hex[:6] + ".zip")
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        for name, data in sorted(files.items()):
            archive.writestr(name, data)
    return path
