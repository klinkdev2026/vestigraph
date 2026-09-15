"""Neutral test providers: no transport client, endpoint, channels or editor SDK."""
from datetime import datetime, timezone
from pathlib import Path
import struct
import threading
import time
import zlib

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
)
from vestigraph.preview.base import Renderer, RendererCapabilities


def now():
    return datetime.now(timezone.utc).isoformat()


def image():
    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind+body))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\0\xff\0\0")) + chunk(b"IEND", b""))


class _Subscription(Subscription):
    def __init__(self, provider, callback):
        self.provider, self.callback = provider, callback

    def close(self):
        with self.provider.lock:
            if self in self.provider.subscriptions:
                self.provider.subscriptions.remove(self)


class FakeBackend(EditorBackend):
    def __init__(self, root, *, backend_id="test_editor", data=b"example"):
        self.backend_id = backend_id
        self.root = Path(root)
        self.data = data
        self.session = None
        self.documents = {}
        self.revisions = {}
        self.subscriptions = []
        self.calls = []
        self.lock = threading.RLock()

    def discover(self, config):
        return tuple(SessionDescriptor(self.backend_id, name, "Test editor", Availability.ONLINE)
                     for name in ("session-one", "session-two"))

    def connect(self, session):
        if session not in self.discover({}):
            raise BackendError("unknown_session", outcome="not_started")
        self.close()
        self.session = session
        self.documents = {DocumentRef(*session.key, name): self.data for name in ("design-one", "design-two")}
        self.revisions = dict.fromkeys(self.documents, 0)

    def close(self):
        with self.lock:
            self.subscriptions.clear()
            self.session = None

    def health(self):
        return Availability.ONLINE if self.session else Availability.OFFLINE

    def _check(self, ref):
        if self.session is None:
            raise BackendError("offline", retryable=True, outcome="not_started")
        if (ref.backend_id, ref.session_instance_id) != self.session.key or ref not in self.documents:
            raise BackendError("stale_document", outcome="not_started")

    def capabilities(self, document=None):
        if document is not None:
            self._check(document)
        enabled = Capability(True, self.session is not None, None if self.session else "offline")
        return BackendCapabilities(events=enabled, export_formats=("GDS2",), export=enabled,
                                   screenshot=enabled, open_snapshot=enabled, stable_document_handle=enabled,
                                   atomic_export=enabled, atomic_screenshot_binding=enabled, cancel_export=enabled,
                                   open_formats=("GDS2",))

    def list_documents(self):
        if not self.session:
            return ()
        return tuple(DocumentDescriptor(ref, ref.document_instance_id, i == 0,
                      source_path=str(self.root / (ref.document_instance_id + ".gds")), format_hint="GDS2",
                      identity_strength="stable") for i, ref in enumerate(self.documents))

    def inspect_document(self, document):
        self._check(document)
        return DocumentState(document, str(self.revisions[document]), "stable")

    def observe(self, callback):
        if self.session is None:
            raise BackendError("offline", outcome="not_started")
        item = _Subscription(self, callback)
        with self.lock:
            self.subscriptions.append(item)
        return item

    def edit(self, document, data, *, source="manual"):
        self._check(document)
        with self.lock:
            self.documents[document] = data
            self.revisions[document] += 1
            event = BackendEvent(EventKind.CONTENT_CHANGED, source, now(), document,
                                 str(self.revisions[document]), "observed")
            for subscription in tuple(self.subscriptions):
                if subscription in self.subscriptions:
                    subscription.callback(event)

    def _deadline(self, deadline):
        if time.monotonic() >= deadline:
            raise BackendError("timeout", retryable=True, outcome="not_started")

    def export_snapshot(self, document, request):
        self._check(document)
        self._deadline(request.deadline)
        if request.cancel is not None and request.cancel.is_set():
            raise BackendError("cancelled", outcome="not_started")
        if request.format != "GDS2":
            raise BackendError("unsupported_format", outcome="not_started")
        started = now()
        with self.lock:
            request.destination.write_bytes(self.documents[document])
            self.calls.append(("export", document))
            return ExportReceipt(document, "GDS2", "exchange_snapshot", True,
                                 len(self.documents[document]), str(self.revisions[document]), started, now(), "atomic")

    def screenshot(self, document, request):
        self._check(document)
        self._deadline(request.deadline)
        if request.expected_revision is not None and request.expected_revision != str(self.revisions[document]):
            return ScreenshotResult("failed", document, now(), reason_code="identity_changed")
        png = image()
        if len(png) > request.max_bytes:
            return ScreenshotResult("failed", document, now(), reason_code="invalid_image")
        self.calls.append(("screenshot", document))
        return ScreenshotResult("success", document, now(), png, str(self.revisions[document]), "atomic")

    def open_snapshot(self, request):
        self._deadline(request.deadline)
        if self.session is None:
            raise BackendError("offline", outcome="not_started")
        if request.format != "GDS2":
            raise BackendError("unsupported_format", outcome="not_started")
        ref = DocumentRef(*self.session.key, "opened-" + str(len(self.documents)))
        self.documents[ref] = request.path.read_bytes()
        self.revisions[ref] = 0
        self.calls.append(("open", ref))
        return OpenReceipt("completed", ref)


class FakeRenderer(Renderer):
    def capabilities(self):
        return RendererCapabilities(True, ("GDS2",))

    def render(self, request, budgets):
        return {"ok": True, "preview": {"items": [], "completeness": "partial",
                                       "omissions": ["test_renderer_no_geometry"]}}

    def compare(self, request, budgets):
        return {"ok": True, "diff": {"coverage": "unavailable", "reason": "test_renderer_no_geometry"}}
