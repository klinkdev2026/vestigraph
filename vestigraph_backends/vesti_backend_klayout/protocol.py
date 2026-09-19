"""KLink protocol decoding and endpoint validation for the KLayout provider."""
import logging
import ipaddress
from vestigraph.capture_errors import (CaptureError, NO_DOCUMENT, DISCONNECTED, UNSUPPORTED,
    DOCUMENT_CHANGED, DOCUMENT_AMBIGUOUS)

CHANNELS = (
    "shapes_changed", "cells_changed", "instances_changed", "layer_list_changed",
    "cellview_changed", "selection_sent", "job_progress", "job_started", "job_done",
)
BOUNDARIES = {"selection_sent", "job_done"}
def _identity(response):
    """No stable document UUID is available. Do not use active_cell as identity."""
    if not isinstance(response, dict) or not isinstance(response.get("tabs"), list):
        raise CaptureError(NO_DOCUMENT, "Cannot identify the document; open one layout and retry.")
    index = response.get("current_index")
    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
        raise CaptureError(NO_DOCUMENT, "No active document; open one layout and restart recording.")
    tabs = [tab for tab in response["tabs"] if isinstance(tab, dict) and tab.get("index") == index]
    views = [] if len(tabs) != 1 else [
        view for view in tabs[0].get("cellviews", [])
        if isinstance(view, dict) and view.get("is_active") is True
    ]
    if len(views) != 1:
        raise CaptureError(NO_DOCUMENT, "Active cellview is missing or ambiguous; select one document and retry.")
    cellview, filename = views[0].get("index"), views[0].get("filename")
    if isinstance(cellview, bool) or not isinstance(cellview, int) or cellview < 0:
        raise CaptureError(UNSUPPORTED, "Invalid active cellview index; reconnect to a compatible KLink.")
    if filename is not None and not isinstance(filename, str):
        raise CaptureError(UNSUPPORTED, "Invalid document filename; reconnect to a compatible KLink.")
    return index, cellview, filename


def _active_cell(response, identity):
    """Name of the active cell of the identified cellview, if reported."""
    tab_index, cellview_index, _ = identity
    for tab in response.get("tabs", []):
        if isinstance(tab, dict) and tab.get("index") == tab_index:
            for view in tab.get("cellviews", []):
                if isinstance(view, dict) and view.get("index") == cellview_index:
                    cell = view.get("active_cell")
                    return cell if isinstance(cell, str) else None
    return None


def _all_tabs(response, identity):
    """Every cellview open in this window (what the panel lists under the window)."""
    tab_index, cellview_index, _ = identity
    out = []
    for tab in response.get("tabs", []):
        if not isinstance(tab, dict):
            continue
        for view in tab.get("cellviews", []):
            if not isinstance(view, dict):
                continue
            filename = view.get("filename")
            out.append({"tab_index": tab.get("index"), "cellview_index": view.get("index"),
                        "filename": filename if isinstance(filename, str) and filename else None,
                        "active_cell": view.get("active_cell") if isinstance(view.get("active_cell"), str) else None,
                        "is_current": tab.get("index") == tab_index and view.get("index") == cellview_index})
    return out


def _same_file_openings(response, filename):
    """How many cellviews across all tabs show this saved file (copies make ownership ambiguous)."""
    if not filename:
        return 1
    count = 0
    for tab in response.get("tabs", []):
        if not isinstance(tab, dict):
            continue
        for view in tab.get("cellviews", []):
            if isinstance(view, dict) and view.get("filename") == filename:
                count += 1
    return count


def _ensure_unambiguous(response, identity):
    if _same_file_openings(response, identity[2]) > 1:
        raise CaptureError(DOCUMENT_AMBIGUOUS,
                           "The same file is open in more than one tab, so the history owner is ambiguous; "
                           "close the extra copies to record.")


def _save_only(value):
    """Export-only RPC causes are not geometry edits, including our own exports."""
    causes = value.get("caused_by") if isinstance(value, dict) else None
    return bool(isinstance(causes, list) and causes and all(
        isinstance(cause, dict) and cause.get("method") == "layout.save_file"
        for cause in causes
    ))


def _validate_endpoint(host, port):
    if not isinstance(host, str) or not host.strip():
        raise ValueError("Host must be loopback; use 127.0.0.1.")
    host = host.strip().lower()
    if host == "localhost":
        host = "127.0.0.1"  # Do not trust hosts-file/DNS resolution for local-only policy.
    try:
        if not ipaddress.ip_address(host).is_loopback:
            raise ValueError
    except ValueError as exc:
        raise ValueError("Host must be numeric loopback or localhost; use 127.0.0.1.") from exc
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("Port must be 1..65535; use the port shown by your KLink session.")
    return host, port


def _client_factory(client_factory):
    if client_factory is not None:
        return client_factory
    try:
        from klink import KLinkClient
    except ImportError as exc:
        raise CaptureError(UNSUPPORTED, "Install a compatible KLink client in this Python environment, then retry observe.") from exc
    return KLinkClient


def probe_document(host="127.0.0.1", port=8765, client_factory=None, timeout=10):
    """Look at the endpoint's active document without writing anything anywhere.

    Returns {"tab_index", "cellview_index", "filename", "channels"}; raises
    CaptureError(DISCONNECTED / NO_DOCUMENT / UNSUPPORTED).
    """
    host, port = _validate_endpoint(host, port)
    client = _client_factory(client_factory)(host=host, port=port)
    try:
        try:
            client.connect()
            response = client.call("events.channels", {}, timeout=timeout)
            tabs = client.call("view.list_tabs", {}, timeout=timeout)
        except CaptureError:
            raise
        except Exception as exc:
            raise CaptureError(DISCONNECTED, f"Cannot reach KLink at {host}:{port}: {exc}") from exc
        available = response.get("channels", []) if isinstance(response, dict) else []
        if not (set(CHANNELS) - {"job_progress"}).issubset(available):
            raise CaptureError(UNSUPPORTED, "Endpoint lacks required channels; install/enable a compatible KLink.")
        identity = _identity(tabs)
        tab_index, cellview_index, filename = identity
        return {"tab_index": tab_index, "cellview_index": cellview_index, "filename": filename,
                "active_cell": _active_cell(tabs, identity),
                "openings": _same_file_openings(tabs, filename),
                "tabs": _all_tabs(tabs, identity),
                "channels": [c for c in CHANNELS if c in available]}
    finally:
        try:
            client.close()
        except Exception:
            logging.getLogger(__name__).warning("Nonfatal callback or cleanup failure", exc_info=True)


def check_document(client, target):
    response = client.call("view.list_tabs", {}, timeout=10)
    observed = _identity(response)
    if observed != target:
        raise CaptureError(DOCUMENT_CHANGED, "Active document changed; return to the intended document and restart recording.")
    _ensure_unambiguous(response, observed)
    return _active_cell(response, observed)


def export_file(client, path, target):
    # Caller owns before/after binding checks and verifies the reserved copy.
    return client.call("layout.save_file", {
        "path": str(path.resolve()), "cellview_index": target[1],
    }, timeout=120)


def file_info(client, path):
    return client.call("layout.file_info", {"path": str(path)}, timeout=30)


def start_observation(client, callback, host, port):
    try:
        client.connect()
        response = client.call("events.channels", {}, timeout=10)
    except Exception as exc:
        raise CaptureError(DISCONNECTED, f"Cannot reach KLink at {host}:{port}: {exc}") from exc
    available = response.get("channels", []) if isinstance(response, dict) else []
    selected = [channel for channel in CHANNELS if channel in available]
    if not (set(CHANNELS) - {"job_progress"}).issubset(selected):
        raise CaptureError(UNSUPPORTED, "Endpoint lacks required channels; install/enable a compatible KLink.")
    for channel in selected:
        client.on(channel, callback(channel))
    response = client.subscribe(selected)
    accepted = response.get("accepted", []) if isinstance(response, dict) else []
    if not set(selected).issubset(accepted):
        raise CaptureError(UNSUPPORTED, "Required subscriptions rejected; inspect KLink capabilities and reconnect.")
    return client.call("view.list_tabs", {}, timeout=10), accepted


def heartbeat(client):
    return client.call("meta.ping", {}, timeout=10)


def close_connection(client):
    client.close()


def screenshot(client):
    from vestigraph_backends.vesti_backend_klayout.adapter import decode_screenshot
    return decode_screenshot(client.call("view.screenshot", {
        "mode": "base64", "width_px": 1280, "height_px": 720,
    }, timeout=3))


def open_history(candidate, path, client_factory=None):
    """Compatibility response for the old HTTP job; timeout never means not opened."""
    from vestigraph_backends.types import BackendError
    client = _client_factory(client_factory)(host=candidate["host"], port=candidate["port"])
    try:
        try:
            client.connect()
        except Exception:
            raise BackendError("open_failed", outcome="not_started") from None
        try:
            return client.call("layout.show_file", {"path": str(path), "mode": "new",
                                                   "keep_position": False}, timeout=60)
        except Exception as exc:
            # The request was dispatched: the editor may well have opened the file before the
            # transport failed (a timeout is only the most common case). Reporting "failed"
            # here makes the user open the same file again; "unknown" is the honest answer.
            timeout = "Timeout" in type(exc).__name__ or "timed out" in str(exc).lower()
            raise BackendError("timeout" if timeout else "open_failed", outcome="unknown") from None
    finally:
        try:
            client.close()
        except Exception:
            logging.getLogger(__name__).warning("Nonfatal callback or cleanup failure", exc_info=True)
