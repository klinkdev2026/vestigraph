"""Process-level exclusive file locks (OS-enforced; released when the holder dies).

Used for the history writer lease and the service single-instance lock. A lock
is an open file descriptor holding an exclusive byte-range/flock lock; the file
body carries owner diagnostics only and is never the source of truth.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

# The lock byte sits far past the diagnostics text so other processes can
# still read the holder info (Windows refuses reads of a locked byte range).
LOCK_OFFSET = 1 << 30

if os.name == "nt":
    import msvcrt

    def _try_lock(fd):
        try:
            os.lseek(fd, LOCK_OFFSET, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    def _unlock(fd):
        try:
            os.lseek(fd, LOCK_OFFSET, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
else:
    import fcntl

    def _try_lock(fd):
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def _unlock(fd):
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass


class LockHeld(RuntimeError):
    """Another live process (or another handle in this process) holds the lock."""

    def __init__(self, path, holder):
        self.path, self.holder = Path(path), holder or {}
        owner = self.holder.get("owner") or "unknown"
        pid = self.holder.get("pid")
        super().__init__(f"{owner} (pid {pid}) holds {self.path.name}")


class FileLock:
    """Exclusive lock on ``path``; ``info`` is written into the file for diagnostics."""

    def __init__(self, path, info=None):
        self.path = Path(path)
        self.info = dict(info or {})
        self.fd = None

    @property
    def held(self):
        return self.fd is not None

    def acquire(self):
        if self.held:
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            if not _try_lock(fd):
                holder = read_holder(self.path)
                os.close(fd)
                raise LockHeld(self.path, holder)
            payload = dict(self.info, pid=os.getpid(), acquired_at=time.time())
            data = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, data)
            os.ftruncate(fd, len(data))       # drop any longer content left by a previous holder
        except LockHeld:
            raise
        except OSError:
            os.close(fd)
            raise
        self.fd = fd
        return self

    def release(self):
        fd, self.fd = self.fd, None
        if fd is None:
            return
        try:
            os.ftruncate(fd, 0)
        except OSError:
            pass
        _unlock(fd)
        os.close(fd)

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.release()


def read_holder(path):
    """Best-effort diagnostics written by the current holder (may be stale/empty)."""
    try:
        text = Path(path).read_text(encoding="utf-8").strip()
        value = json.loads(text) if text else {}
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}
