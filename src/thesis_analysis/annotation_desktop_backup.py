"""离线桌面项目备份：持原owner锁复制全部字节，不构造或恢复服务。"""
from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import shutil
import uuid

from .annotation_file_lock import OwnerFileLock
from .annotation_desktop_project import (
    DesktopProjectError, REVIEW_RELATIVE, _is_link, _require, _reject_link_ancestors,
    _sha, _utc, _validate_marker, _persisted_contract, _read_json,
    SCHEMA_VERSION, CONTRACT_RELATIVE,
)


RECEIPT_FILENAME = ".syllafolio-backup.json"


def _locked_bytes(owner):
    descriptor = owner.fileno()
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks = []
    while True:
        block = os.read(descriptor, 1024 * 1024)
        if not block:
            break
        chunks.append(block)
    return b"".join(chunks)


def _tree_identity(root, *, owner=None, paths_only=False):
    """Reject reparse points before descending, including Windows junctions."""
    files, directories = {}, []

    def visit(folder):
        for child in sorted(folder.iterdir(), key=lambda item: item.name):
            _require(not _is_link(child), "PROJECT_PATH", 'The backup source cannot contain symbolic links or directory junctions.')
            relative = child.relative_to(root).as_posix()
            if child.is_dir():
                directories.append(relative)
                visit(child)
            else:
                _require(child.is_file(), "PROJECT_PATH", 'Only regular files and directories can be backed up.')
                if paths_only:
                    continue
                if owner is not None and child == owner.path:
                    data = _locked_bytes(owner)
                    files[relative] = {"sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data)}
                else:
                    files[relative] = {"sha256": _sha(child), "size_bytes": child.stat().st_size}
    visit(root)
    return {"files": files, "directories": sorted(directories)}


def _copy_file(source, destination):
    _require(not _is_link(source), "PROJECT_PATH", 'A source path became a link during copying. The backup was stopped.')
    result = shutil.copy2(source, destination, follow_symlinks=False)
    _require(not _is_link(source) and not _is_link(destination), "PROJECT_PATH", 'A link appeared during copying. The backup was stopped.')
    return result


def backup_project(source_dir, destination_dir):
    """Publish a byte-identical offline copy plus a SHA receipt in a new directory.

    Existing lock files are opened without writing bytes. The lock remains held
    through source-before/source-after verification and final directory rename.
    SQLite main, WAL, SHM, assets, drafts and all other files are copied together.
    No SQLite connection or annotation service is created against the source.
    """
    source_requested, target_requested = Path(source_dir).expanduser(), Path(destination_dir).expanduser()
    _reject_link_ancestors(source_requested)
    _reject_link_ancestors(target_requested)
    _require(source_requested.is_dir(), "PROJECT_DIRECTORY", 'The source project directory does not exist.')
    source, target = source_requested.resolve(), target_requested.resolve()
    _require(not target.exists() and not target.is_symlink(), "PROJECT_EXISTS", 'The backup destination already exists. Choose a new destination.')
    _require(not target.is_relative_to(source) and not source.is_relative_to(target),
             "PROJECT_LOCATION", 'Choose a backup destination outside the original project; neither directory may contain the other.')
    _tree_identity(source, paths_only=True)  # Inspect before opening any nested owner-lock path.
    lock_path = source / REVIEW_RELATIVE / "state.sqlite3.owner.lock"
    _require(lock_path.is_file() and not _is_link(lock_path), "PROJECT_OWNER_LOCK", 'The source project is missing its owner lock. It cannot be copied safely.')
    try:
        owner = OwnerFileLock(lock_path)
    except BlockingIOError as exc:
        raise DesktopProjectError("STORE_IN_USE", 'The source project is still open. Close it normally before backing it up.') from exc
    staging = None
    try:
        with owner:
            marker = _validate_marker(source)
            before = _tree_identity(source, owner=owner)
            target.parent.mkdir(parents=True, exist_ok=True)
            _reject_link_ancestors(target.parent)
            staging = target.parent / ("." + target.name + ".backup-" + uuid.uuid4().hex)
            def copy_under_lock(src, dst):
                if Path(src) == owner.path:
                    Path(dst).write_bytes(_locked_bytes(owner))
                    shutil.copystat(src, dst, follow_symlinks=False)
                    return str(dst)
                return _copy_file(src, dst)
            shutil.copytree(source, staging, copy_function=copy_under_lock)
            _require(_tree_identity(staging) == before, "BACKUP_COPY_MISMATCH", 'The backup files or directory structure differ from the source.')
            _validate_marker(staging)
            # Contract/state verification reads another detached SQLite copy;
            # it neither recovers a service nor changes source/staging WAL.
            stored = _persisted_contract(staging, marker)
            if marker["schema_version"] == SCHEMA_VERSION:
                _require(stored == _read_json(staging / CONTRACT_RELATIVE),
                         "PROJECT_CONTRACT", 'The backup database does not match the immutable project contract.')
            else:
                _require(stored.get("schema_version") == "0.2.0" and stored.get("mode") == "engineering"
                         and stored.get("experiment_enabled") is False,
                         "PROJECT_CONTRACT", 'The backed-up P5 contract has a different purpose.')
            _require(_tree_identity(source, owner=owner) == before, "INPUT_CHANGED", 'The source project changed during copying. The backup was not published.')
            receipt = {"schema_version": "syllafolio-offline-backup-v1", "created_at": _utc(),
                       "project_id": marker["project_id"], "run_id": marker["run_id"],
                       "source_project": str(source), "destination_project": str(target),
                       "owner_lock_held": True, "service_constructed": False,
                       "source_files_changed": False, "includes_sqlite_wal_and_shm": True,
                       "identity": before}
            receipt_name = (RECEIPT_FILENAME if not (staging / RECEIPT_FILENAME).exists()
                            else ".syllafolio-backup-" + uuid.uuid4().hex + ".json")
            (staging / receipt_name).write_text(json.dumps(receipt, ensure_ascii=False, sort_keys=True,
                allow_nan=False, indent=2) + "\n", encoding="utf-8")
            _require(not target.exists() and not target.is_symlink(), "PROJECT_EXISTS", 'The backup destination appeared before publication.')
            # os.rename refuses an existing directory on Windows. Never replace it.
            os.rename(staging, target)
            staging = None
            return {**receipt, "receipt_path": str(target / receipt_name), "project_dir": str(target)}
    finally:
        if staging is not None and staging.exists():
            resolved = staging.resolve()
            # Cleanup is confined to this generated staging sibling, never source/target.
            _require(resolved.parent == target.parent and resolved.name.startswith("." + target.name + ".backup-")
                     and resolved != source and resolved != target and not _is_link(staging),
                     "BACKUP_CLEANUP_PATH", 'The temporary backup directory did not match its recorded identity. It was preserved for inspection.')
            shutil.rmtree(resolved)

