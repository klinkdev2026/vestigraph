"""Runtime conformance boundary: provider declarations are not successful results."""
import time
from vestigraph_backends.base import EditorBackend, Subscription
from vestigraph_backends.types import (
    API_VERSION,
    Availability,
    BackendCapabilities,
    BackendError,
    BackendEvent,
    DocumentDescriptor,
    DocumentState,
    ExportReceipt,
    OpenReceipt,
    ScreenshotResult,
)
from vestigraph.vesti_formats.registry import FORMATS


class GuardedBackend(EditorBackend):
    def __init__(self, provider, *, formats=None):
        if not isinstance(provider, EditorBackend) or provider.api_version != API_VERSION:
            raise BackendError("incompatible_backend", outcome="not_started")
        self.formats = formats if formats is not None else FORMATS.copy(frozen=True)
        self.provider = provider
        self.backend_id = provider.backend_id
        self.display_name = getattr(provider, "display_name", self.backend_id)
        self.session = None

    def _invoke(self, method, *args):
        function = getattr(self.provider, method, None)
        if not callable(function):
            raise BackendError("unsupported", outcome="not_started")
        try:
            return function(*args)
        except BackendError:
            raise
        except NotImplementedError:
            raise BackendError("unsupported",
                               outcome="unknown" if method in ("export_snapshot", "open_snapshot") else "not_started") from None
        except TimeoutError:
            raise BackendError("timeout", retryable=True,
                               outcome="unknown" if method in ("export_snapshot", "open_snapshot") else "failed") from None
        except Exception:
            raise BackendError("provider_failed",
                               outcome="unknown" if method in ("export_snapshot", "open_snapshot") else "failed") from None

    def _ref(self, ref):
        from vestigraph_backends.types import DocumentRef
        if not isinstance(ref, DocumentRef) or self.session is None or (ref.backend_id, ref.session_instance_id) != self.session.key:
            raise BackendError("identity_changed", outcome="not_started")

    def _require(self, name, ref=None):
        if ref is not None:
            self._ref(ref)
        capability = getattr(self.capabilities(ref), name)
        if not capability.supported:
            raise BackendError("unsupported", outcome="not_started")
        if not capability.available:
            raise BackendError(capability.reason_code or "unavailable", retryable=True, outcome="not_started")

    def _deadline(self, request, *, dispatched=False):
        if time.monotonic() >= request.deadline:
            raise BackendError("timeout", retryable=True, outcome="unknown" if dispatched else "not_started")

    def discover(self, config):
        from vestigraph_backends.types import SessionDescriptor
        result = self._invoke("discover", config)
        if not isinstance(result, tuple) or any(
                not isinstance(item, SessionDescriptor) or item.backend_id != self.backend_id for item in result):
            raise BackendError("invalid_response")
        if len({item.key for item in result}) != len(result):
            raise BackendError("invalid_response")
        return result

    def connect(self, session):
        if session.backend_id != self.backend_id:
            raise BackendError("invalid_session", outcome="not_started")
        self._invoke("connect", session)
        self.session = session

    def close(self):
        try:
            self._invoke("close")
        finally:
            self.session = None

    def health(self):
        result = self._invoke("health")
        if not isinstance(result, Availability):
            raise BackendError("invalid_response")
        return result

    def capabilities(self, document=None):
        result = self._invoke("capabilities", document)
        if not isinstance(result, BackendCapabilities):
            raise BackendError("invalid_response")
        return result

    def list_documents(self):
        result = self._invoke("list_documents")
        if not isinstance(result, tuple) or any(not isinstance(item, DocumentDescriptor) for item in result):
            raise BackendError("invalid_response")
        for item in result:
            self._ref(item.ref)
        if len({item.ref for item in result}) != len(result):
            raise BackendError("document_ambiguous")
        return result

    def inspect_document(self, document):
        self._ref(document)
        result = self._invoke("inspect_document", document)
        if not isinstance(result, DocumentState) or result.ref != document:
            raise BackendError("identity_changed")
        return result

    def observe(self, callback):
        self._require("events")
        def checked(event):
            if not isinstance(event, BackendEvent) or (event.ref is not None and (
                    self.session is None or (event.ref.backend_id, event.ref.session_instance_id) != self.session.key)):
                from vestigraph_backends.types import EventKind
                from datetime import datetime, timezone
                event = BackendEvent(EventKind.CONNECTION_LOST, "system", datetime.now(timezone.utc).isoformat(),
                                     details_json='{"reason_code":"invalid_event"}')
            callback(event)
        result = self._invoke("observe", checked)
        if not isinstance(result, Subscription):
            raise BackendError("invalid_response")
        return result

    def export_snapshot(self, document, request):
        self._ref(document)
        self._require("export", document)
        try:
            spec = self.formats.get(request.format)
            supported = self._formats(self.capabilities(document).export_formats)
        except ValueError:
            raise BackendError("unsupported_format", outcome="not_started") from None
        if spec.format_id not in supported or request.destination.suffix.lower() not in spec.extensions:
            raise BackendError("unsupported_format", outcome="not_started")
        if request.cancel is not None and request.cancel.is_set():
            raise BackendError("cancelled", outcome="not_started")
        self._deadline(request)
        result = self._invoke("export_snapshot", document, request)
        self._deadline(request, dispatched=True)
        if (not isinstance(result, ExportReceipt) or result.document != document
                or result.completed is not True or result.artifact_role != spec.artifact_role):
            raise BackendError("invalid_export_receipt", outcome="unknown")
        try:
            actual = self.formats.get(result.actual_format)
        except ValueError:
            raise BackendError("format_mismatch", outcome="unknown") from None
        if actual != spec:
            raise BackendError("format_mismatch", outcome="unknown")
        return result

    def screenshot(self, document, request):
        from vestigraph.presentation import validate_png
        try:
            self._ref(document)
            self._require("screenshot", document)
            self._deadline(request)
            result = self._invoke("screenshot", document, request)
            if not isinstance(result, ScreenshotResult) or result.document != document:
                raise BackendError("identity_changed")
            if result.status == "success":
                self._deadline(request, dispatched=True)
                try:
                    width, height = validate_png(result.png)
                    if len(result.png) > request.max_bytes or width > request.width or height > request.height:
                        raise ValueError()
                except (ValueError, RuntimeError):
                    raise BackendError("invalid_image") from None
                if request.expected_revision is not None and result.observed_revision != request.expected_revision:
                    raise BackendError("identity_changed")
            return result
        except BackendError as exc:
            from datetime import datetime, timezone
            return ScreenshotResult("unavailable" if exc.reason_code == "unsupported" else "failed",
                                    document, datetime.now(timezone.utc).isoformat(), reason_code=exc.reason_code)

    def open_snapshot(self, request):
        self.check_open_format(request.format, request.artifact_role)
        self._deadline(request)
        result = self._invoke("open_snapshot", request)
        self._deadline(request, dispatched=True)
        if not isinstance(result, OpenReceipt):
            return OpenReceipt("unknown", reason_code="invalid_response")
        if result.outcome == "completed":
            if result.document is None or self.session is None or (
                    result.document.backend_id, result.document.session_instance_id) != self.session.key:
                return OpenReceipt("unknown", reason_code="identity_changed")
        return result

    def check_open_format(self, format_id, artifact_role):
        """Read-only preflight before core spends time reconstructing a large file."""
        self._require("open_snapshot")
        try:
            spec = self.formats.get(format_id)
        except ValueError:
            raise BackendError("unsupported_format", outcome="not_started") from None
        if artifact_role != spec.artifact_role:
            raise BackendError("native_project_restore_unsupported", outcome="not_started")
        if spec.format_id not in self._formats(self.capabilities().open_formats):
            raise BackendError("unsupported_format", outcome="not_started")
        return spec

    def _formats(self, values):
        result = set()
        for value in values:
            try:
                result.add(self.formats.get(value).format_id)
            except ValueError:
                continue
        return result

    def flush_observed_changes(self, document):
        self._ref(document)
        self._invoke("flush_observed_changes", document)
