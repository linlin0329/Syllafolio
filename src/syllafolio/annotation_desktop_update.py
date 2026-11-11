"""Small, checksum-guarded code updates for the private desktop prototype.

Updates never write projects, audio, reports, caches, or dependencies. Originals
are backed up before any source replacement; an exception rolls the source back.
"""
from __future__ import annotations
from .annotation_english import system_error

import ctypes
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sys
import uuid

try:
    from .annotation_desktop_installation import InstallationLease, InstallationLeaseError, PENDING_FILE, validate_installation_protocol
except ImportError:  # Standalone apply_update.py uses its own unchanged helper.
    from update_installation_lease import InstallationLease, InstallationLeaseError, PENDING_FILE, validate_installation_protocol


def digest(path):
    checksum = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(block)
    return checksum.hexdigest()


def safe_path(root, relative):
    value = PurePosixPath(relative)
    if not relative or value.is_absolute() or ".." in value.parts or "\\" in relative or ":" in relative:
        raise ValueError("Invalid update path: " + relative)
    allowed = (relative.startswith("app/") or relative in {
        "BUILD_INFO.json", "INTEGRITY.json", "Start.cmd", "Run_Checks.cmd", "Record_Feedback.cmd",
        "sitecustomize.py", "runtime_paths.py", "prototype_reports.py", "run_launcher.py",
        "launch.py", "check_prototype.py", "manual_feedback.py", "README.md",
        "README_\u5148\u8bfb.md",  # Filename used by earlier portable packages.
        "installation_lease.py", "run_checked.py",
        "run_p1_checks.py", "Run_P1_Checks.cmd", "USER_GUIDE.md",
        "P1_TEST_BASELINE.json",
    })
    if not allowed:
        raise ValueError("Update is not allowed to touch data or dependencies: " + relative)
    root = Path(root).resolve()
    path = root.joinpath(*value.parts)
    if not path.resolve().is_relative_to(root):
        raise ValueError("Update path escapes the bundle")
    parent = path
    while parent != root:
        if parent.is_symlink() or (hasattr(parent, "is_junction") and parent.is_junction()):
            raise ValueError("Update paths must not traverse links or junctions")
        parent = parent.parent
    return path


class ProcessInspectionError(RuntimeError):
    """An update must not interpret an incomplete process query as clearance."""

    def __init__(self, reason, **details):
        self.reason = reason
        self.details = dict(details, reason=reason)
        instruction = ("Close Syllafolio, then run Apply_Update.cmd directly from File Explorer "
                       "or a normal PowerShell/CMD window outside the sandbox. Administrator "
                       "rights are not required. Do not end unrelated Python sessions; if this "
                       "persists, return the PID/error details for review.")
        super().__init__("Update process check could not establish that the application is closed: "
                         + json.dumps(self.details, ensure_ascii=False) + ". " + instruction)


class _WindowsProcessApi:
    """Minimal read-only Win32 adapter; no command lines, termination or elevation.

    TokenIsAppContainer=29 follows Microsoft's AppContainer example. Process32*
    returns FALSE at normal exhaustion only with ERROR_NO_MORE_FILES (18).
    https://learn.microsoft.com/windows/win32/secauthz/appcontainer-for-legacy-applications-
    https://learn.microsoft.com/windows/win32/api/tlhelp32/nf-tlhelp32-process32next
    """

    def __init__(self, kernel=None, security=None, last_error=None):
        from ctypes import wintypes
        self.types = wintypes
        self.kernel = kernel if kernel is not None else ctypes.WinDLL("kernel32", use_last_error=True)
        self.security = security if security is not None else ctypes.WinDLL("advapi32", use_last_error=True)
        self.last_error = last_error if last_error is not None else ctypes.get_last_error
        class Entry(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                        ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                        ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                        ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
                        ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]
        self.Entry = Entry
        self._signature(self.kernel, "CreateToolhelp32Snapshot", [wintypes.DWORD, wintypes.DWORD], wintypes.HANDLE)
        for name in ("Process32FirstW", "Process32NextW"):
            self._signature(self.kernel, name, [wintypes.HANDLE, ctypes.POINTER(Entry)], wintypes.BOOL)
        self._signature(self.kernel, "OpenProcess", [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE)
        self._signature(self.kernel, "QueryFullProcessImageNameW", [wintypes.HANDLE, wintypes.DWORD,
                       wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL)
        self._signature(self.kernel, "CloseHandle", [wintypes.HANDLE], wintypes.BOOL)
        self._signature(self.kernel, "GetCurrentProcess", [], wintypes.HANDLE)
        self._signature(self.security, "OpenProcessToken", [wintypes.HANDLE, wintypes.DWORD,
                       ctypes.POINTER(wintypes.HANDLE)], wintypes.BOOL)
        self._signature(self.security, "GetTokenInformation", [wintypes.HANDLE, wintypes.DWORD,
                       wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL)

    @staticmethod
    def _signature(library, name, arguments, result):
        function = getattr(library, name)
        function.argtypes, function.restype = arguments, result

    def _error(self, operation):
        error = OSError(self.last_error(), operation)
        error.operation = operation
        return error

    def is_appcontainer(self):
        token, flag, returned = self.types.HANDLE(), self.types.DWORD(), self.types.DWORD()
        if not self.security.OpenProcessToken(self.kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
            raise self._error("OpenProcessToken(current process)")
        try:
            if not self.security.GetTokenInformation(token, 29, ctypes.byref(flag), ctypes.sizeof(flag),
                                                     ctypes.byref(returned)):
                raise self._error("GetTokenInformation(TokenIsAppContainer)")
            if returned.value != ctypes.sizeof(flag):
                raise OSError("TokenIsAppContainer returned an unexpected buffer size")
            return bool(flag.value)
        finally:
            self.kernel.CloseHandle(token)

    def processes(self):
        handle = self.kernel.CreateToolhelp32Snapshot(2, 0)
        if handle == ctypes.c_void_p(-1).value:
            raise self._error("CreateToolhelp32Snapshot")
        try:
            entry = self.Entry()
            entry.dwSize = ctypes.sizeof(entry)
            step = self.kernel.Process32FirstW
            while True:
                if not step(handle, ctypes.byref(entry)):
                    if self.last_error() != 18:
                        raise self._error("Process32FirstW" if step is self.kernel.Process32FirstW else "Process32NextW")
                    break
                yield int(entry.th32ProcessID), entry.szExeFile
                step = self.kernel.Process32NextW
        finally:
            self.kernel.CloseHandle(handle)

    def image_path(self, pid):
        process = self.kernel.OpenProcess(0x1000, False, pid)
        if not process:
            raise self._error("OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)")
        try:
            buffer = ctypes.create_unicode_buffer(32768)
            size = self.types.DWORD(len(buffer))
            if not self.kernel.QueryFullProcessImageNameW(process, 0, buffer, ctypes.byref(size)):
                raise self._error("QueryFullProcessImageNameW")
            import ntpath
            if not buffer.value or not ntpath.isabs(buffer.value):
                raise OSError("The executable-image query returned no absolute path")
            try:
                # QueryFullProcessImageName can preserve a directory alias;
                # compare its resolved target just as we resolve bundle_root.
                return str(Path(buffer.value).resolve())
            except OSError as error:
                error.operation = "ResolveProcessImagePath"
                raise
        finally:
            self.kernel.CloseHandle(process)


def _image_identity(path):
    import ntpath
    value = str(path)
    if value.lower().startswith("\\\\?\\unc\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    return ntpath.normcase(ntpath.normpath(value))


def _inspection_detail(error, *, pid=None):
    return {"pid": pid, "operation": getattr(error, "operation", "process inspection"),
            "winerror": getattr(error, "winerror", None) or getattr(error, "errno", None),
            "error": system_error(error)}


def _inspect_bundle_processes(bundle, api):
    """Separate known unrelated interpreters from an uninspectable candidate.

    An AppContainer may have a restricted view of external processes. Rejecting
    updates there is an application policy, not a claim that a particular GUI
    was proved invisible by TokenIsAppContainer alone.
    """
    try:
        contained = api.is_appcontainer()
    except OSError as error:
        raise ProcessInspectionError("execution_context_unavailable", **_inspection_detail(error)) from error
    if contained is not False:
        raise ProcessInspectionError("appcontainer_process_visibility_unverified")
    expected = {_image_identity(Path(bundle).resolve() / "_runtime" / name)
                for name in ("python.exe", "pythonw.exe")}
    found, unknown, own_seen = [], [], False
    try:
        for pid, name in api.processes():
            if pid == os.getpid():
                own_seen = True
                continue
            if name.lower() not in {"python.exe", "pythonw.exe"}:
                continue
            try:
                image = api.image_path(pid)
                import ntpath
                if not image or not ntpath.isabs(image):
                    raise OSError("The executable-image query returned no absolute path")
            except OSError as error:
                unknown.append(_inspection_detail(error, pid=pid))
                continue
            if _image_identity(image) in expected:
                found.append(int(pid))
    except OSError as error:
        raise ProcessInspectionError("process_enumeration_incomplete", **_inspection_detail(error),
                                     active_process_ids=found) from error
    if not own_seen:
        raise ProcessInspectionError("process_snapshot_missing_updater", active_process_ids=found)
    if unknown:
        raise ProcessInspectionError("python_process_identity_unavailable", processes=unknown,
                                     active_process_ids=found)
    return found


def running_bundle_processes(bundle):
    """Return same-bundle processes, or refuse an unverifiable Windows scan."""
    if os.name != "nt":
        return []
    return _inspect_bundle_processes(bundle, _WindowsProcessApi())


def apply_update(bundle, update_dir, *, dry_run=False, process_query=running_bundle_processes):
    # Acquire before inspecting manifests or scanning processes. Every supported
    # launcher takes the opposing shared lease before reading application code.
    with InstallationLease(bundle, mode="exclusive"):
        return _apply_update_locked(bundle, update_dir, dry_run=dry_run, process_query=process_query)


def _apply_update_locked(bundle, update_dir, *, dry_run, process_query):
    bundle, update_dir = Path(bundle).resolve(), Path(update_dir).resolve()
    pending = bundle / PENDING_FILE
    if pending.exists() or pending.is_symlink():
        raise InstallationLeaseError("INSTALLATION_RECOVERY_REQUIRED", "An earlier update did not complete or roll back. Preserve its backup and restore the installation, or use a verified independent portable package; no new update was started.")
    manifest = json.loads((update_dir / "patch_manifest.json").read_text(encoding="utf-8"))
    if manifest.get("schema") != "syllafolio-prototype-patch-v1":
        raise ValueError("Unsupported patch schema")
    build = json.loads((bundle / "BUILD_INFO.json").read_text(encoding="utf-8"))
    if build.get("schema") == "syllafolio-windows-prototype-build-v1" or "installation_lease" in build:
        validate_installation_protocol(bundle, build)
    active = process_query(bundle)
    if active:
        raise RuntimeError("Close all Syllafolio windows and running checks before applying. Active process IDs: " + str(active))
    files = manifest["files"]
    if build.get("version") == manifest["target_version"]:
        if all(safe_path(bundle, row["path"]).is_file() and
               digest(safe_path(bundle, row["path"])) == row["sha256"] for row in files):
            return {"status": "already_applied", "version": build["version"], "data_changed": False}
        raise ValueError("This version has local source changes; no files were overwritten")
    if build.get("version") != manifest["base_version"]:
        raise ValueError("This patch is for " + manifest["base_version"])
    if digest(bundle / "INTEGRITY.json") != manifest["base_integrity_sha256"]:
        raise ValueError("The base integrity manifest differs; no files were overwritten")
    paths, seen = [], set()
    for row in files:
        if row["path"] in seen:
            raise ValueError("Duplicate patch path")
        seen.add(row["path"])
        target = safe_path(bundle, row["path"])
        source = update_dir / "payload" / row["path"]
        if source.is_symlink() or not source.is_file() or digest(source) != row["sha256"]:
            raise ValueError("Patch payload hash mismatch: " + row["path"])
        before = row.get("before_sha256")
        if before is None and target.exists():
            raise ValueError("A new update path already contains a local file: " + row["path"])
        if before is not None and (not target.is_file() or digest(target) != before):
            raise ValueError("Local source differs from the tested base: " + row["path"])
        paths.append((row, source, target))
    if dry_run:
        return {"status": "ready", "files": len(paths), "data_changed": False}
    backup = bundle / "updates" / ("u" + uuid.uuid4().hex[:8])
    backup.mkdir(parents=True, exist_ok=False)
    originals = {}
    for row, _, target in paths:
        if target.exists():
            saved = backup / "before" / row["path"]
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, saved)
            originals[row["path"]] = saved
    replaced = []
    journal = {"schema": "syllafolio-update-journal-v1", "backup": backup.relative_to(bundle).as_posix(),
               "base_version": manifest["base_version"], "target_version": manifest["target_version"],
               "before": {row["path"]: row.get("before_sha256") for row, _source, _target in paths}, "projects_changed": False}
    temporary_journal = pending.with_suffix(".writing")
    try:
        with temporary_journal.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(journal, ensure_ascii=False, indent=2) + "\n"); stream.flush(); os.fsync(stream.fileno())
        temporary_journal.replace(pending)
    finally: temporary_journal.unlink(missing_ok=True)
    try:
        # Identity files publish last. A caught IO failure restores every replaced source.
        ordered = sorted(paths, key=lambda x: (x[0]["path"] in {"BUILD_INFO.json", "INTEGRITY.json"}, x[0]["path"]))
        for row, source, target in ordered:
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(".u" + uuid.uuid4().hex[:6] + ".tmp")
            try:
                shutil.copyfile(source, temporary)
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
            replaced.append(row["path"])
        for row, _, target in paths:
            if digest(target) != row["sha256"]:
                raise OSError("Post-update hash mismatch: " + row["path"])
        result = {"status": "applied", "base_version": manifest["base_version"],
                  "version": manifest["target_version"], "files": len(paths),
                  "backup": str(backup), "data_changed": False,
                  "applied_at": datetime.now(timezone.utc).isoformat(), "windows_acceptance_complete": False}
        (backup / "update_result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        pending.unlink()
    except BaseException as original_error:
        rollback_errors = []
        for relative in reversed(replaced):
            try:
                target = safe_path(bundle, relative)
                if relative in originals: shutil.copy2(originals[relative], target)
                else: target.unlink(missing_ok=True)
            except Exception as exc: rollback_errors.append(relative + ": " + system_error(exc))
        for row, _source, target in paths:
            before = row.get("before_sha256")
            try:
                if (before is None and target.exists()) or (before is not None and (not target.is_file() or digest(target) != before)):
                    rollback_errors.append("Original byte identity not restored: " + row["path"])
            except OSError as exc: rollback_errors.append(row["path"] + ": " + system_error(exc))
        if not rollback_errors:
            try: pending.unlink(missing_ok=True)
            except OSError as exc: rollback_errors.append(system_error(exc))
        if rollback_errors:
            raise InstallationLeaseError("INSTALLATION_RECOVERY_REQUIRED", "Update rollback was incomplete. Startup remains blocked; preserve backup " + str(backup) + ". " + "; ".join(rollback_errors)) from original_error
        raise
    return result


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        result = apply_update(args.bundle, Path(__file__).resolve().parent, dry_run=args.dry_run)
    except ProcessInspectionError as error:
        print(json.dumps({"status": "blocked_process_inspection", "details": error.details,
                          "message": system_error(error), "data_changed": False}, ensure_ascii=False, indent=2),
              file=sys.stderr)
        return 2
    except InstallationLeaseError as error:
        print(json.dumps({"status": "blocked_installation", "code": error.code,
                          "message": system_error(error), "data_changed": False}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
