"""EditorBackend v1 for KLayout, over local KLink only.

No SDK imports at construction. Opaque references are connection-scoped, but
the editor has no stable document handle: before/after checks are best effort.
"""
from __future__ import annotations
import logging

import base64
import hashlib
import json
from pathlib import Path
import threading
import time
import uuid
from datetime import datetime, timezone

from vestigraph_backends.base import EditorBackend, Subscription
from vestigraph_backends.types import (
    Availability,
    BackendCapabilities,
    BackendError,
    BackendEvent,
    Capability,
    DocumentDescriptor,
    DocumentRef,
    DocumentState,
    EventKind,
    ExportReceipt,
    OpenReceipt,
    ScreenshotResult,
    SessionDescriptor,
    MAX_EVENT_BYTES,
)
from vestigraph.capture_errors import CaptureError
from vestigraph_backends.vesti_backend_klayout import BACKEND_ID, DISPLAY_NAME
from vestigraph_backends.vesti_backend_klayout import discovery as discovery
from vestigraph_backends.vesti_backend_klayout.protocol import (
    CHANNELS,
    _identity,
    _active_cell,
    _ensure_unambiguous,
    _save_only,
    _validate_endpoint,
)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise BackendError("timeout", retryable=True, outcome="not_started")
    return remaining


def _mapped(exc, *, outcome="failed"):
    if isinstance(exc, BackendError):
        return exc
    code = getattr(exc, "code", None)
    if code in ("ERR_UNKNOWN_METHOD", -32601):
        return BackendError("unsupported", outcome="not_started")
    if code in ("ERR_NO_VIEW", "ERR_NO_LAYOUT"):
        return BackendError("no_document", outcome="not_started")
    if code in ("ERR_CONN_CLOSED", "ERR_TIMEOUT"):
        return BackendError("timeout" if code == "ERR_TIMEOUT" else "disconnected",
                            retryable=True, outcome=outcome)
    if isinstance(exc, CaptureError):
        reason = {"NO_DOCUMENT": "no_document", "DOCUMENT_CHANGED": "identity_changed",
                  "DOCUMENT_AMBIGUOUS": "document_ambiguous", "UNSUPPORTED": "unsupported"}.get(
                      exc.code, "unexpected")
    elif isinstance(exc, TimeoutError):
        reason = "timeout"
    elif isinstance(exc, (ConnectionError, OSError)):
        reason = "disconnected"
    else:
        reason = "unexpected"
    return BackendError(reason, retryable=reason in ("timeout", "disconnected"), outcome=outcome)


class _Subscription(Subscription):
    def __init__(self, callback, client):
        self._lock = threading.RLock()
        self._callback = callback
        self._client = client
        self.handlers = []
        self.detached = False

    def deliver(self, event):
        with self._lock:
            if self._callback is not None:
                self._callback(event)

    def close(self):
        with self._lock:
            if self._callback is None:
                return
            self._callback = None
            handlers, self.handlers = self.handlers, []
            client, self._client = self._client, None
        # KLink.on appends (it does not replace). Remove only our own callbacks
        # using its local off(), never a blocking unsubscribe RPC on a reader thread.
        off = getattr(client, "off", None)
        detached = callable(off) or not handlers
        for channel, handler in handlers:
            if callable(off):
                try:
                    off(channel, handler)
                except Exception:
                    detached = False
        self.detached = detached


_EVENTS = {
    "cellview_changed": EventKind.DOCUMENT_CHANGED,
    "selection_sent": EventKind.SELECTION_CHANGED,
    "job_progress": EventKind.PROGRESS,
    "job_started": EventKind.OPERATION_STARTED,
    "job_done": EventKind.OPERATION_FINISHED,
}


def _event(channel, payload):
    kind = EventKind.SAVE_NOTICE if _save_only(payload) else _EVENTS.get(channel, EventKind.CONTENT_CHANGED)
    source = "automation" if isinstance(payload, dict) and payload.get("caused_by") else "unknown"
    details = json.dumps({"channel": channel}, separators=(",", ":"))
    try:
        evidence = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        if not isinstance(payload, dict) or len(evidence.encode("utf-8")) + len(details) > MAX_EVENT_BYTES:
            evidence = json.dumps({"truncated": True, "vestigraph_truncated": True,
                                   "original_json_bytes": len(evidence.encode("utf-8")),
                                   "summary": "Event details exceeded the backend byte budget."})
    except (TypeError, ValueError, RecursionError):
        evidence = '{"invalid_payload":true}'
    if evidence == '{"invalid_payload":true}' or not isinstance(payload, dict):
        kind = EventKind.CONNECTION_LOST
        details = '{"reason_code":"invalid_event"}'
    # No document attribution from an unbound asynchronous vendor event.
    return BackendEvent(kind, source, _now(), details_json=details, vendor_evidence_json=evidence,
                        record_kind=channel, affects_content=False if kind == EventKind.SAVE_NOTICE else None)


class KLayoutBackend(EditorBackend):
    backend_id = BACKEND_ID
    display_name = DISPLAY_NAME
    api_version = 1

    def __init__(self, *, client_factory=None, session_registry=None):
        self._factory = client_factory
        self._registry = session_registry
        self._client = None
        self._session = None
        self._documents = {}
        self._subscriptions = []
        self._lock = threading.RLock()

    def discover(self, config):
        try:
            registry = self._registry if self._registry is not None else discovery.default_registry()
            records = discovery.candidates(registry, include_stale=bool(config.get("include_stale")))
        except Exception as exc:
            raise _mapped(exc) from None
        sessions = []
        for record in records:
            identity = json.dumps(discovery.session_instance(record), sort_keys=True).encode()
            sid = hashlib.sha256(identity).hexdigest()
            sessions.append(SessionDescriptor(self.backend_id, sid, self.display_name,
                                             Availability.OFFLINE if record.get("stale") else Availability.ONLINE,
                                             private_handle=dict(record), alias=record["session_id"],
                                             public_metadata_json=json.dumps(record),
                                             legacy_instance_aliases=tuple(str(value) for value in
                                                 (record.get("pid"), record["session_id"]) if value is not None)))
        return tuple(sessions)

    def connect(self, session):
        if session.backend_id != self.backend_id or not isinstance(session.private_handle, dict):
            raise BackendError("invalid_session", outcome="not_started")
        try:
            host, port = _validate_endpoint(session.private_handle.get("host"), session.private_handle.get("port"))
        except ValueError:
            raise BackendError("invalid_session", outcome="not_started") from None
        self.close()
        with self._lock:
            try:
                factory = self._factory
                if factory is None:
                    from klink import KLinkClient
                    factory = KLinkClient
                self._client = factory(host=host, port=port)
                self._client.connect()
                self._session = session
            except ImportError:
                self.close()
                raise BackendError("dependency_unavailable", outcome="not_started") from None
            except Exception as exc:
                self.close()
                raise _mapped(exc) from None

    def close(self):
        # Do not hold the operation lock while waiting for a callback to finish.
        with self._lock:
            subscriptions, self._subscriptions = self._subscriptions, []
            client, self._client = self._client, None
            self._session = None
            self._documents.clear()
        for subscription in subscriptions:
            subscription.close()
        if client is not None:
            try:
                client.close()
            except Exception:
                logging.getLogger(__name__).warning("Nonfatal callback or cleanup failure", exc_info=True)

    def _rpc(self, method, params=None, *, timeout=10, outcome="failed"):
        if self._client is None:
            raise BackendError("disconnected", retryable=True, outcome="not_started")
        try:
            return self._client.call(method, params or {}, timeout=timeout)
        except Exception as exc:
            raise _mapped(exc, outcome=outcome) from None

    def health(self):
        with self._lock:
            try:
                self._rpc("meta.ping")
                return Availability.ONLINE
            except BackendError:
                return Availability.OFFLINE

    def capabilities(self, document=None):
        with self._lock:
            online = self._client is not None
            if document is not None:
                self._lookup(document)
            yes = Capability(True, online, None if online else "disconnected")
            return BackendCapabilities(events=yes, export_formats=("GDS2",), export=yes,
                                       screenshot=yes, open_snapshot=yes, open_formats=("GDS2", "OASIS"))

    def _scan_documents(self, response):
        if not isinstance(response, dict) or not isinstance(response.get("tabs"), list):
            raise BackendError("invalid_response")
        if any(not isinstance(tab, dict) or not isinstance(tab.get("cellviews"), list) for tab in response["tabs"]):
            raise BackendError("invalid_response")
        anonymous = [view for tab in response["tabs"] if isinstance(tab, dict)
                     for view in tab.get("cellviews", []) if isinstance(view, dict) and not view.get("filename")]
        if len(anonymous) > 1:
            # This protocol version has no persistent editor-side document IDs.
            # Slot renumbering cannot safely identify multiple untitled layouts.
            self._documents.clear()
            raise BackendError("document_ambiguous", outcome="not_started")
        old = {identity: ref for ref, identity in self._documents.items()}
        current, descriptions, seen = {}, [], set()
        for tab in response["tabs"]:
            if (not isinstance(tab, dict) or type(tab.get("index")) is not int
                    or tab["index"] < 0 or not isinstance(tab.get("cellviews"), list)):
                raise BackendError("invalid_response")
            for view in tab.get("cellviews", []):
                if (not isinstance(view, dict) or type(view.get("index")) is not int or view["index"] < 0):
                    raise BackendError("invalid_response")
                filename = view.get("filename")
                if filename is not None and not isinstance(filename, str):
                    raise BackendError("invalid_response")
                identity = (tab["index"], view["index"], filename)
                if identity[:2] in seen:
                    raise BackendError("document_ambiguous")
                seen.add(identity[:2])
                ref = old.get(identity) or DocumentRef(self.backend_id, self._session.session_instance_id, uuid.uuid4().hex)
                current[ref] = identity
                active = tab["index"] == response.get("current_index") and view.get("is_active") is True
                descriptions.append(DocumentDescriptor(ref, Path(filename).name if filename else "Untitled",
                                    active, source_path=filename or None, format_hint=None,
                                    identity_strength="best_effort", view_name=view.get("active_cell"),
                                    evidence_json=json.dumps({"tab_index": tab["index"], "cellview_index": view["index"]}),
                                    ownership_key=(hashlib.sha256(json.dumps(identity).encode()).hexdigest()
                                                   if filename else ref.document_instance_id)))
        self._documents = current
        return tuple(descriptions)

    def list_documents(self):
        with self._lock:
            response = self._rpc("view.list_tabs")
            if not isinstance(response, dict) or not isinstance(response.get("tabs"), list):
                raise BackendError("invalid_response")
            return self._scan_documents(response)

    def _lookup(self, document):
        if self._session is None or document not in self._documents:
            raise BackendError("identity_changed", outcome="not_started")
        return self._documents[document]

    def _inspect(self, document, timeout=10):
        identity = self._lookup(document)
        response = self._rpc("view.list_tabs", timeout=timeout)
        self._scan_documents(response)
        self._lookup(document)
        try:
            observed = _identity(response)
            _ensure_unambiguous(response, observed)
            if observed != identity:
                # Evict disappeared documents; a newly observed reopening gets a new ref.
                self._scan_documents(response)
                raise BackendError("identity_changed", outcome="not_started")
            return _active_cell(response, observed)
        except Exception as exc:
            raise _mapped(exc) from None

    def inspect_document(self, document):
        with self._lock:
            cell = self._inspect(document)
            return DocumentState(document, identity_strength="best_effort",
                                 diagnostics=("switch_and_back_undetectable",), view_name=cell)

    def observe(self, callback):
        if not callable(callback):
            raise TypeError("Callback must be callable.")
        with self._lock:
            response = self._rpc("events.channels")
            available = response.get("channels", []) if isinstance(response, dict) else []
            selected = [channel for channel in CHANNELS if channel in available]
            if not (set(CHANNELS) - {"job_progress", "job_started"}).issubset(selected):
                raise BackendError("events_unavailable", outcome="not_started")
            if any(sub._callback is not None for sub in self._subscriptions):
                raise BackendError("already_observing", outcome="not_started")
            if any(not sub.detached for sub in self._subscriptions):
                # Old clients without off() may still be used once, but must
                # reconnect before another subscription to keep retention bounded.
                raise BackendError("reconnect_required", outcome="not_started")
            subscription = _Subscription(callback, self._client)
            self._subscriptions = [subscription]
            try:
                for channel in selected:
                    handler = lambda payload, name=channel: subscription.deliver(_event(name, payload))
                    subscription.handlers.append((channel, handler))
                    self._client.on(channel, handler)
                response = self._client.subscribe(selected)
                if not isinstance(response, dict) or not set(selected).issubset(response.get("accepted", [])):
                    raise BackendError("events_unavailable")
            except Exception as exc:
                subscription.close()
                raise _mapped(exc) from None
            return subscription

    def flush_observed_changes(self, document):
        with self._lock:
            self._inspect(document)
            try:
                self._rpc("events.flush", {})
            except BackendError as exc:
                if exc.reason_code != "unsupported":
                    raise

    def export_snapshot(self, document, request):
        if request.format.upper() != "GDS2" or request.destination.suffix.lower() not in (".gds", ".gds2"):
            raise BackendError("unsupported_format", outcome="not_started")
        if request.cancel is not None and request.cancel.is_set():
            raise BackendError("cancelled", outcome="not_started")
        with self._lock:
            started = _now()
            cell = self._inspect(document, _remaining(request.deadline))
            identity = self._lookup(document)
            if identity[2] and Path(identity[2]).resolve() == request.destination.resolve():
                raise BackendError("source_overwrite_refused", outcome="not_started")
            response = self._rpc("layout.save_file",
                                 {"path": str(request.destination), "cellview_index": identity[1]},
                                 timeout=_remaining(request.deadline), outcome="unknown")
            if isinstance(response, dict) and (response.get("success") is False or response.get("ok") is False):
                raise BackendError("export_failed")
            try:
                if self._inspect(document, _remaining(request.deadline)) != cell:
                    raise BackendError("identity_changed")
            except BackendError as exc:
                # A deadline/connection failure AFTER dispatch must not claim no action ran.
                if exc.reason_code in ("timeout", "disconnected"):
                    raise BackendError(exc.reason_code, retryable=exc.retryable, outcome="unknown") from None
                raise
            reported = response.get("file_size") if isinstance(response, dict) else None
            if type(reported) is not int or reported < 0:
                reported = None
            tops = None
            if request.include_structure_summary:
                try:
                    info = self._rpc("layout.file_info", {"path": str(request.destination)},
                                     timeout=_remaining(request.deadline))
                    values = info.get("top_cells", info.get("tops")) if isinstance(info, dict) else None
                    if isinstance(values, list):
                        tops = tuple(sorted(str(value) for value in values))
                except BackendError:
                    pass  # Optional structure summary, never a second format-2 scan.
            # Core owns file existence/size/hash verification and acceptance.
            return ExportReceipt(document, "GDS2", "exchange_snapshot", True, reported, None,
                                 started, _now(), limitations=("switch_and_back_undetectable",),
                                 evidence_json=json.dumps({"tabs_checked": "before_and_after_export",
                                                          "active_cell": cell}), top_cells=tops)

    def screenshot(self, document, request):
        with self._lock:
            try:
                if request.expected_revision is not None:
                    raise BackendError("revision_unavailable", outcome="not_started")
                cell = self._inspect(document, _remaining(request.deadline))
                response = self._rpc("view.screenshot", {"mode": "base64",
                    "width_px": request.width, "height_px": request.height}, timeout=_remaining(request.deadline))
                if self._inspect(document, _remaining(request.deadline)) != cell:
                    raise BackendError("identity_changed")
                png = decode_screenshot(response, request.max_bytes,
                                        max_width=request.width, max_height=request.height)
                return ScreenshotResult("success", document, _now(), png=png)
            except Exception as exc:
                reason = _mapped(exc).reason_code
                return ScreenshotResult("unavailable" if reason in ("unsupported", "revision_unavailable") else "failed",
                                        document, _now(), reason_code=reason)

    def open_snapshot(self, request):
        if request.format.upper() not in ("GDS2", "GDS", "OASIS", "OAS"):
            raise BackendError("unsupported_format", outcome="not_started")
        with self._lock:
            dispatched = False
            try:
                timeout = _remaining(request.deadline)
                if self._client is None:
                    raise BackendError("disconnected", outcome="not_started")
                dispatched = True
                response = self._rpc("layout.show_file", {"path": str(request.path), "mode": request.mode,
                    "keep_position": False}, timeout=timeout, outcome="unknown")
                if isinstance(response, dict) and (response.get("success") is False or response.get("ok") is False):
                    return OpenReceipt("failed", reason_code="open_failed")
                documents = self._scan_documents(self._rpc("view.list_tabs", timeout=_remaining(request.deadline)))
                matches = [d for d in documents if d.active and d.source_path
                           and Path(d.source_path).resolve() == request.path.resolve()]
                if len(matches) == 1:
                    return OpenReceipt("completed", document=matches[0].ref)
                return OpenReceipt("unknown", reason_code="open_unconfirmed")
            except Exception as raw:
                exc = _mapped(raw)
                if not dispatched or exc.outcome == "not_started":
                    raise exc from None
                return OpenReceipt("unknown", reason_code=exc.reason_code)


def decode_screenshot(response, max_bytes=2 * 1024 * 1024, *, max_width=1920, max_height=1080):
    from vestigraph.presentation import validate_png, PresentationError
    try:
        url = response.get("data_url") if isinstance(response, dict) else None
        prefix = "data:image/png;base64,"
        if not isinstance(url, str) or not url.startswith(prefix) or len(url) > max_bytes * 4 // 3 + 100:
            raise ValueError("Invalid screenshot envelope.")
        png = base64.b64decode(url[len(prefix):], validate=True)
        if len(png) > max_bytes:
            raise ValueError("Image budget exceeded.")
        width, height = validate_png(png)
        if width > max_width or height > max_height:
            raise ValueError("Image dimensions exceed requested budget.")
        return png
    except (ValueError, PresentationError):
        raise BackendError("invalid_image") from None
