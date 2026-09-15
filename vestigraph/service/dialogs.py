"""Native folder picker for the local panel (framework-free, stdlib only).

A browser cannot hand a page the absolute path of a folder, so the picker is a
native dialog opened by the service process itself, in a short-lived subprocess
(tkinter, which ships with the python.org builds on Windows/macOS and with the
``python3-tk`` package on Linux). The subprocess isolates Tk from the service:
a hung or missing Tk never takes the service down.
"""
from __future__ import annotations

import subprocess
import sys

from .errors import ServiceError

PICK_TIMEOUT_S = 600.0
_EXIT_NO_TK = 3

_SCRIPT = r"""
import sys
try:
    import tkinter
    from tkinter import filedialog
except Exception:
    sys.exit(3)
initial, title = sys.argv[1], sys.argv[2]
root = tkinter.Tk()
root.withdraw()
try:
    root.attributes("-topmost", True)
except Exception:
    pass
kwargs = {"title": title, "mustexist": True}
if initial:
    kwargs["initialdir"] = initial
chosen = filedialog.askdirectory(**kwargs)
root.destroy()
if chosen:
    sys.stdout.write(chosen)
"""


def _no_window() -> dict:
    if sys.platform == "win32":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {}


def pick_directory(initial: str | None, title: str, *, timeout: float = PICK_TIMEOUT_S,
                   run=subprocess.run, python: str | None = None) -> str | None:
    """Open a native "choose folder" dialog on the user's desktop.

    Returns the chosen absolute path, or ``None`` when the user cancelled. Raises
    ``ServiceError(NO_NATIVE_DIALOG)`` when no dialog can be shown (no tkinter, no
    display, timeout) -- the caller then falls back to a typed path.
    """
    argv = [python or sys.executable, "-c", _SCRIPT, initial or "", title]
    try:
        result = run(argv, capture_output=True, text=True, timeout=timeout, **_no_window())
    except subprocess.TimeoutExpired as exc:
        raise ServiceError("NO_NATIVE_DIALOG", f"The folder dialog was not answered within {timeout:g} s",
                           next_action="Type the destination path instead") from exc
    except OSError as exc:
        raise ServiceError("NO_NATIVE_DIALOG", f"Cannot start the folder dialog: {exc}",
                           next_action="Type the destination path instead") from exc
    if result.returncode == _EXIT_NO_TK:
        raise ServiceError("NO_NATIVE_DIALOG", "This Python has no tkinter, so no native folder dialog is available",
                           next_action="Type the destination path instead (or install python3-tk on Linux)")
    if result.returncode != 0:
        detail = (result.stderr or "").strip().splitlines()[-1:] or ["unknown error"]
        raise ServiceError("NO_NATIVE_DIALOG", f"The folder dialog failed: {detail[0]}",
                           next_action="Type the destination path instead")
    chosen = (result.stdout or "").strip()
    return chosen or None

# Multi-file selection stays native: paths are authorized by the user's picker,
# never accepted as arbitrary paths from a browser request.
_FILES_SCRIPT = r"""
import json,sys
try:
    import tkinter
    from tkinter import filedialog
except Exception:
    sys.exit(3)
root = tkinter.Tk()
root.withdraw()
try:
    root.attributes("-topmost", True)
except Exception:
    pass
options = {"title": sys.argv[2], "filetypes": [("Layout versions", "*.gds *.gds2 *.oas *.oasis"), ("All files", "*")]}
if sys.argv[1]:
    options["initialdir"] = sys.argv[1]
chosen = filedialog.askopenfilenames(**options)
root.destroy()
sys.stdout.write(json.dumps(list(chosen), ensure_ascii=True))
"""


def pick_files(initial=None, title="Select older layout versions", *, timeout=PICK_TIMEOUT_S,
               run=subprocess.run, python=None):
    import json
    try:
        result = run([python or sys.executable, "-c", _FILES_SCRIPT, initial or "", title],
                     capture_output=True, text=True, timeout=timeout, **_no_window())
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ServiceError("NO_NATIVE_DIALOG", "Cannot open the file selection dialog.",
                           next_action="Use the local Python import API, or enable tkinter.") from exc
    if result.returncode:
        raise ServiceError("NO_NATIVE_DIALOG", "The file selection dialog is unavailable.",
                           next_action="Enable tkinter in the service Python environment.")
    try:
        paths = json.loads(result.stdout)
        if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
            raise ValueError()
        return paths
    except (ValueError, TypeError) as exc:
        raise ServiceError("NO_NATIVE_DIALOG", "Invalid file selection response.") from exc
