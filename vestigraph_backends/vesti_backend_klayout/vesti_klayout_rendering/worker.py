"""Killable preview subprocess: JSON request on stdin, JSON result on stdout.

Usage from the service: ``run_preview(request, budgets)`` spawns
``python -m vestigraph_backends.vesti_backend_klayout.vesti_klayout_rendering.worker`` with a hard timeout. Never import
klayout in the service process for rendering.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys

from vestigraph.preview.budgets import DEFAULT, Budgets, SUPPORTED_FORMATS


def klayout_available() -> bool:
    from vestigraph_backends.vesti_backend_klayout.capabilities import has_module
    return has_module("klayout")


def run_preview(request: dict, budgets: Budgets = DEFAULT) -> dict:
    """Return {"ok": True, "preview": {...}} or {"ok": False, "code", "message", ...}."""
    fmt = str(request.get("format") or "").upper()
    from .thumbnail import FORMATS as IMAGE_FORMATS
    supported = IMAGE_FORMATS if request.get("op") == "thumbnail" else SUPPORTED_FORMATS
    if fmt and fmt not in supported:
        return {"ok": False, "code": "PREVIEW_UNSUPPORTED_FORMAT",
                "message": f"Preview is only available for {sorted(SUPPORTED_FORMATS)} in this version; "
                           f"this version is {fmt}. Download it or open it in KLayout instead."}
    if not klayout_available():
        return {"ok": False, "code": "PREVIEW_UNAVAILABLE",
                "message": "The klayout Python module is not installed in the service's interpreter."}
    payload = json.dumps({"request": request, "budgets": budgets.to_dict()}).encode("utf-8")
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "vestigraph_backends.vesti_backend_klayout.vesti_klayout_rendering.worker"], input=payload,
            capture_output=True, timeout=budgets.timeout_s, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        return {"ok": False, "code": "PREVIEW_TIMEOUT",
                "message": f"Rendering took longer than {budgets.timeout_s:g} s and was stopped."}
    if completed.returncode != 0 or not completed.stdout:
        tail = completed.stderr.decode("utf-8", "replace")[-2000:]
        return {"ok": False, "code": "PREVIEW_WORKER_FAILED",
                "message": "The preview process failed.", "details": tail}
    if len(completed.stdout) > budgets.max_response_bytes:
        return {"ok": False, "code": "PREVIEW_TOO_LARGE",
                "message": "The preview result exceeds the response limit; narrow the viewport or layers."}
    try:
        return json.loads(completed.stdout.decode("utf-8"))
    except ValueError:
        return {"ok": False, "code": "PREVIEW_WORKER_FAILED", "message": "The preview process returned invalid JSON."}


def run_diff(request: dict, budgets: Budgets = DEFAULT) -> dict:
    """Cell-level comparison of two exported files; same process isolation and budgets as previews."""
    for key in ("format_before", "format_after"):
        fmt = str(request.get(key) or "").upper()
        if fmt and fmt not in SUPPORTED_FORMATS:
            return {"ok": False, "code": "PREVIEW_UNSUPPORTED_FORMAT",
                    "message": f"Comparison is only available for {sorted(SUPPORTED_FORMATS)} in this version."}
    return run_preview({**request, "op": "diff"}, budgets)


def main() -> int:
    from vestigraph_backends.vesti_backend_klayout.vesti_klayout_rendering.geometry import (
        PreviewRefused,
        render,
    )
    try:
        message = json.loads(sys.stdin.buffer.read().decode("utf-8"))
        request, budgets = message["request"], Budgets(**message["budgets"])
        if request.get("op") == "thumbnail":
            from .thumbnail import render as render_image
            out = {"ok": True, "thumbnail": render_image(request["path"], budgets)}
        elif request.get("op") == "diff":
            from vestigraph_backends.vesti_backend_klayout.vesti_klayout_rendering.celldiff import diff
            result = diff(request["path_before"], request["path_after"], budgets=budgets,
                          from_id=request.get("from_id"), to_id=request.get("to_id"))
            out = {"ok": True, "diff": result}
        else:
            preview = render(request["path"], top_cell=request.get("top_cell"),
                             viewport_dbu=request.get("viewport_dbu"), layers=request.get("layers"),
                             budgets=budgets, checkpoint_id=request.get("checkpoint_id"))
            out = {"ok": True, "preview": preview}
    except PreviewRefused as exc:
        out = {"ok": False, "code": exc.code, "message": str(exc), **exc.extra}
    except Exception as exc:  # noqa: BLE001 - report, never hang
        out = {"ok": False, "code": "PREVIEW_WORKER_FAILED", "message": f"{type(exc).__name__}: {exc}"}
    sys.stdout.buffer.write(json.dumps(out, allow_nan=False).encode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
