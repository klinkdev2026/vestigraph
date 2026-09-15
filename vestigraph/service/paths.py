"""Per-user default locations (no configuration needed to start recording).

Windows: %LOCALAPPDATA%\\Vestigraph\\{state,history}
macOS:   ~/Library/Application Support/Vestigraph/{state,history}
Linux:   $XDG_DATA_HOME/vestigraph/{state,history}  (default ~/.local/share)
Override everything with VESTIGRAPH_HOME=<dir>.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path


def user_home() -> Path:
    configured = os.environ.get("VESTIGRAPH_HOME")
    if configured:
        return Path(configured).expanduser()
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "Vestigraph"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Vestigraph"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "vestigraph"


def default_state_dir() -> Path:
    return user_home() / "state"


def default_history_root() -> Path:
    return user_home() / "history"


def default_port() -> int:
    return 8787


def temp_roots() -> list:
    """Directories whose files are never treated as user documents."""
    roots = [Path(tempfile.gettempdir())]
    for name in ("TEMP", "TMP", "TMPDIR"):
        value = os.environ.get(name)
        if value:
            roots.append(Path(value))
    return roots
