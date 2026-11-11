"""安装级共享运行/独占更新租约；与项目owner锁分离，仅锁持久空文件。

Windows使用同一字节的LockFileEx共享/独占锁，句柄关闭或进程异常退出
由OS释放。锁文件不删除、不替换、不记录PID，不以陈旧PID推断运行状态。
本模块只用标准库，可复制到发行包根及独立更新器，不先导入应用代码。
LockFileEx允许GENERIC_READ共享锁；继承句柄不代替child自行取锁：
https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-lockfileex
"""
from __future__ import annotations

import ast
import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path
import stat
import threading


PROTOCOL = "syllafolio-installation-lease-v1"
LOCK_FILE = ".syllafolio-installation.lock"
PENDING_FILE = ".syllafolio-update-pending.json"
BOOTSTRAPS = ("installation_lease.py", "sitecustomize.py", "run_launcher.py", "launch.py", "check_prototype.py", "manual_feedback.py", "run_checked.py", "run_p1_checks.py")


class InstallationLeaseError(RuntimeError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(f"{code}: {message}")


def canonical_root(root):
    root = Path(root).resolve(strict=True)
    if not root.is_dir(): raise InstallationLeaseError("INSTALLATION_ROOT", "The installation root is not a directory.")
    return root


def _lock_path(root):
    path = root / LOCK_FILE
    try: info = path.lstat()
    except FileNotFoundError: return path
    if not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise InstallationLeaseError("INSTALLATION_LOCK_PATH", "The installation lease file must be a regular local file; links and reparse points are refused.")
    return path


class InstallationLease:
    """非阻塞共享app/独占updater。不同canonical安装目录互不阻塞。"""
    def __init__(self, root, *, mode="shared"):
        if mode not in {"shared", "exclusive"}: raise ValueError("Use shared or exclusive installation lease mode.")
        self.root, self.mode = canonical_root(root), mode
        self.path = _lock_path(self.root); self._guard = threading.Lock(); self._handle = None; self._fd = None
        try:
            if os.name == "nt": self._acquire_windows()
            else:
                import fcntl
                fd = os.open(self.path, (os.O_RDONLY if mode == "shared" and self.path.exists() else os.O_RDWR | os.O_CREAT)
                             | getattr(os, "O_NOFOLLOW", 0), 0o600)
                try: fcntl.flock(fd, (fcntl.LOCK_SH if mode == "shared" else fcntl.LOCK_EX) | fcntl.LOCK_NB)
                except BaseException: os.close(fd); raise
                self._fd = fd
        except OSError as exc:
            busy = (getattr(exc, "winerror", None) in {32, 33} if os.name == "nt"
                    else exc.errno in {errno.EAGAIN, errno.EACCES, errno.EDEADLK})
            if busy:
                raise InstallationLeaseError("INSTALLATION_BUSY", "This installation is running or being updated. Close its own windows/checks, or wait for its update to finish, then retry. No other application is stopped.") from exc
            raise InstallationLeaseError("INSTALLATION_LOCK_UNAVAILABLE", "The installation lease could not be established. Keep the existing installation and use a writable independent portable folder; no files were updated. " + str(exc)) from exc
        pending = self.root / PENDING_FILE
        if mode == "shared" and (pending.exists() or pending.is_symlink()):
            self.close()
            raise InstallationLeaseError("INSTALLATION_RECOVERY_REQUIRED", "An installation update did not finish publication or rollback. Application startup is blocked. Preserve the update backup and use a verified independent portable package or restore this installation before retrying; projects are unchanged.")

    def _acquire_windows(self):
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        class Overlapped(ctypes.Structure):
            _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                        ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD), ("hEvent", wintypes.HANDLE)]
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.LockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(Overlapped)]
        kernel.LockFileEx.restype = wintypes.BOOL
        kernel.UnlockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(Overlapped)]
        kernel.UnlockFileEx.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]; kernel.CloseHandle.restype = wintypes.BOOL
        # Shared read/write handles permit many readers; omitting FILE_SHARE_DELETE
        # prevents a second file identity being substituted beneath an active lease.
        read_existing = self.mode == "shared" and self.path.exists()
        handle = kernel.CreateFileW(str(self.path), 0x80000000 if read_existing else 0x80000000 | 0x40000000,
                                    1 | 2, None, 3 if read_existing else 4, 0x80 | 0x200000, None)
        if handle == ctypes.c_void_p(-1).value: raise ctypes.WinError(ctypes.get_last_error())
        try: _lock_path(self.root)
        except BaseException: kernel.CloseHandle(handle); raise
        overlap = Overlapped()
        if not kernel.LockFileEx(handle, 1 | (2 if self.mode == "exclusive" else 0), 0, 1, 0, ctypes.byref(overlap)):
            error = ctypes.get_last_error(); kernel.CloseHandle(handle); raise ctypes.WinError(error)
        self._kernel, self._overlap, self._handle = kernel, overlap, handle

    @property
    def closed(self): return self._handle is None and self._fd is None

    def close(self):
        with self._guard:
            if self._handle is not None:
                handle, self._handle = self._handle, None
                try:
                    if not self._kernel.UnlockFileEx(handle, 0, 1, 0, ctypes.byref(self._overlap)):
                        raise ctypes.WinError(ctypes.get_last_error())
                finally: self._kernel.CloseHandle(handle)
            if self._fd is not None:
                import fcntl
                fd, self._fd = self._fd, None
                try: fcntl.flock(fd, fcntl.LOCK_UN)
                finally: os.close(fd)

    def __enter__(self): return self
    def __exit__(self, *_args): self.close()
    def __del__(self):
        try: self.close()
        except Exception: pass


def validate_installation_protocol(root, build):
    """核实已冻结协议与真实bootstrap字节；不靠版本banner声称旧启动器有锁。"""
    root = canonical_root(root)
    claim = build.get("installation_lease")
    if not isinstance(claim, dict) or claim.get("schema") != PROTOCOL or claim.get("lock_file") != LOCK_FILE:
        raise InstallationLeaseError("INSTALLATION_LEGACY_UNSUPPORTED", "This older installation has no shared startup/update lease protocol. In-place hot update is refused. Keep it intact, extract a separate current portable package, and migrate only a verified offline project copy after closing the old project owner.")
    declared = claim.get("bootstrap_sha256")
    if not isinstance(declared, dict) or set(declared) != set(BOOTSTRAPS):
        raise InstallationLeaseError("INSTALLATION_PROTOCOL_CHANGED", "The installation lease bootstrap inventory is incomplete; no update was applied.")
    integrity = json.loads((root / "INTEGRITY.json").read_text(encoding="utf-8"))
    for name in BOOTSTRAPS:
        path = root / name
        if path.is_symlink() or not path.is_file() or getattr(path.stat(), "st_file_attributes", 0) & 0x400:
            raise InstallationLeaseError("INSTALLATION_PROTOCOL_CHANGED", "An installation bootstrap is missing or redirected: " + name)
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != declared[name] or actual != integrity.get(name):
            raise InstallationLeaseError("INSTALLATION_PROTOCOL_CHANGED", "An installation bootstrap differs from its frozen protocol inventory: " + name)
        if name == "installation_lease.py":
            if actual != hashlib.sha256(Path(__file__).read_bytes()).hexdigest():
                raise InstallationLeaseError("INSTALLATION_PROTOCOL_CHANGED", "This updater does not implement the installation's exact lease protocol; use a compatible full portable package.")
            continue
        # Every supported executable entry must hold the shared lease around main.
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if name == "sitecustomize.py":
            imports = [node for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))]
            if any((isinstance(node, ast.ImportFrom) and node.module not in {"pathlib", "__future__"})
                   or (isinstance(node, ast.Import) and any(alias.name != "os" for alias in node.names)) for node in imports):
                raise InstallationLeaseError("INSTALLATION_PROTOCOL_CHANGED", "Pre-entry sitecustomize must not import runtime paths or application modules before the lease.")
            continue
        expected = ast.parse('if __name__ == "__main__":\n from installation_lease import InstallationLease\n with InstallationLease(ROOT, mode="shared"):\n  raise SystemExit(main())').body[0]
        if not any(ast.dump(node, include_attributes=False) == ast.dump(expected, include_attributes=False) for node in tree.body):
            raise InstallationLeaseError("INSTALLATION_PROTOCOL_CHANGED", "The installation entry point has no audited shared-lease wrapper: " + name)
    return claim
