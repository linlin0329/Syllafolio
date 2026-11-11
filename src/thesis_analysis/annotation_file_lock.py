"""跨平台的标注存储owner锁；不修改锁文件内容或人工状态。

POSIX继续采用flock，Windows使用CRT固定字节的非阻塞锁。Windows允许
锁定EOF之外的字节，因此无需先写入占位字节，避免并发初始化时写入已被锁住
的区域。锁文件不得删除或替换，否则不同owner可能锁到不同文件身份。
"""
from __future__ import annotations

import errno
import os
from pathlib import Path
import threading


_WINDOWS = os.name == "nt"
_BUSY_ERRORS = {errno.EACCES, errno.EAGAIN, errno.EDEADLK}


def _set_lock(fd: int, *, release: bool = False) -> None:
    if _WINDOWS:
        import msvcrt

        # CRT locking starts at the descriptor's current position, on acquire
        # and release alike. Never let append offsets select another byte.
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK if release else msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN if release else fcntl.LOCK_EX | fcntl.LOCK_NB)


class OwnerFileLock:
    """构造时获取独占锁；close幂等释放，竞争抛BlockingIOError。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._guard = threading.Lock()
        self._fd: int | None = None
        # O_APPEND/O_TRUNC are intentionally absent; existing contents and the
        # fixed locked byte remain unchanged even when the file is initially empty.
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
        try:
            _set_lock(fd)
        except BaseException as exc:
            os.close(fd)
            if isinstance(exc, OSError) and exc.errno in _BUSY_ERRORS:
                raise BlockingIOError(exc.errno, "Owner lock is already held", str(self.path)) from exc
            raise
        self._fd = fd

    @property
    def closed(self) -> bool:
        return self._fd is None

    def fileno(self) -> int:
        if self._fd is None:
            raise ValueError("Owner lock is closed")
        return self._fd

    def close(self) -> None:
        with self._guard:
            if self._fd is None:
                return
            fd, self._fd = self._fd, None
            try:
                _set_lock(fd, release=True)
            finally:
                os.close(fd)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def __del__(self):
        # Explicit close/context management is preferred; retain the previous
        # file object's last-resort descriptor cleanup when an owner is dropped.
        try:
            self.close()
        except Exception:
            pass
