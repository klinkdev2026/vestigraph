"""Service-state directory: catalog, cache, single-instance lock.

Separate from every history repository; never placed inside one. Secrets are
generated per process and never written to disk here.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ..filelock import FileLock, LockHeld
from .errors import ServiceError

STATE_FORMAT = 1
MARKER = "vestigraph-service.json"


def now_iso():
    return datetime.now(timezone.utc).isoformat()


class ServiceState:
    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()
        self.marker = self.root / MARKER
        self.catalog_path = self.root / "catalog.sqlite3"
        self.cache_dir = self.root / "cache"
        self.log_dir = self.root / "logs"
        self.lock_path = self.root / "service.lock"
        self.instance_id = uuid.uuid4().hex
        self._lock = None

    @classmethod
    def init(cls, root):
        state = cls(root)
        if state.root.exists() and not state.root.is_dir():
            raise ServiceError("STATE_DIR_INVALID", "State path exists but is not a directory.",
                               next_action="Choose an empty or new directory for --state.")
        if state.root.is_dir() and not state.marker.exists() and any(state.root.iterdir()):
            raise ServiceError("STATE_DIR_NOT_EMPTY",
                               "State directory already contains unrelated files.",
                               next_action="Choose an empty or new directory for --state; existing files are not touched.")
        if state.root.parent != state.root and _inside_history_repo(state.root):
            raise ServiceError("STATE_DIR_INSIDE_HISTORY",
                               "State directory must not live inside a history repository.",
                               next_action="Pick a separate folder for service state.")
        state.root.mkdir(parents=True, exist_ok=True)
        for sub in (state.cache_dir, state.log_dir):
            sub.mkdir(exist_ok=True)
        if not state.marker.exists():
            state.marker.write_text(json.dumps({
                "format_version": STATE_FORMAT, "created_at": now_iso(),
                "note": "Vestigraph service state. Contains no layout data; history stays in each history folder.",
            }, ensure_ascii=False, indent=2), encoding="utf-8")
        return state

    @classmethod
    def open(cls, root):
        state = cls(root)
        if not state.marker.is_file():
            raise ServiceError("STATE_NOT_INITIALIZED",
                               "Service state is not initialized.",
                               next_action="Run: python -m vestigraph service init --state DIR")
        try:
            meta = json.loads(state.marker.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ServiceError("STATE_CORRUPT", "Service state marker is unreadable.",
                               next_action="Restore the state directory from backup or init a new one.") from exc
        if meta.get("format_version") != STATE_FORMAT:
            raise ServiceError("STATE_FORMAT_UNSUPPORTED", "Service state format is not supported.",
                               next_action="Use the matching Vestigraph version.")
        state.cache_dir.mkdir(exist_ok=True)
        state.log_dir.mkdir(exist_ok=True)
        return state

    # ------------------------------------------------------ single instance --
    def acquire_instance(self, owner="vestigraph-service"):
        if self._lock is not None:
            return self
        lock = FileLock(self.lock_path, {"owner": owner, "instance_id": self.instance_id})
        try:
            lock.acquire()
        except LockHeld as exc:
            raise ServiceError(
                "SERVICE_ALREADY_RUNNING",
                f"Another Vestigraph service already uses this state directory: {exc}.",
                status=409,
                next_action="Stop the other service, or use a different --state directory.",
                details={"holder": exc.holder}) from exc
        self._lock = lock
        return self

    def release_instance(self):
        lock, self._lock = self._lock, None
        if lock is not None:
            lock.release()

    @property
    def instance_held(self):
        return self._lock is not None


def _inside_history_repo(path: Path) -> bool:
    for parent in [path, *path.parents]:
        if (parent / "index.sqlite3").is_file() and (parent / "objects").is_dir():
            return True
    return False


def resolve_existing_dir(value, what) -> Path:
    path = Path(value).expanduser()
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise ServiceError("PATH_NOT_FOUND", f"{what} does not exist: {path}",
                           next_action="Create the folder first, then register it.") from None
    if not resolved.is_dir():
        raise ServiceError("PATH_NOT_DIRECTORY", f"{what} is not a directory: {path}",
                           next_action="Point to a folder, not a file.")
    return resolved


def is_within(path: Path, root: Path) -> bool:
    """Boundary check on resolved paths (never string prefix)."""
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def inside_git_checkout(path: Path) -> bool:
    for parent in [path, *path.parents]:
        if (parent / ".git").exists():
            return True
    return False


def pid_alive(pid) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False
