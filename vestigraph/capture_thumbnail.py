"""Best-effort capture-time view, not atomic geometry evidence.

The temporary compatibility call delegates vendor RPC/image decoding to the
provider. Diagnostics and persistence stay here; optional image failures never
invalidate an accepted export. Generic observer wiring remains the next step.
"""
import time
import uuid
from datetime import datetime, timezone
from .presentation import Presentation, PresentationError, MAX_PNG, CAPTURE_IMAGE_LOCK, validate_png
from .thumbnail_diagnostics import DIAGNOSTICS
from .store import SaveCancelled
from vestigraph_backends.types import BackendError, ScreenshotRequest


def _scope(observer, accepted):
    if not getattr(observer, "_thumbnail_session_id", None):
        observer._thumbnail_session_id = uuid.uuid4().hex
    return dict(backend=observer.backend.backend_id, session=observer._thumbnail_session_id,
                document=getattr(observer, "target", None), capture=accepted.get("id"))


def _still_needed(observer, capture_id):
    pipeline = getattr(observer, "pipeline", None)
    if pipeline is None:
        return True  # compatibility callers without the durable queue
    current = pipeline.spool.get(capture_id)
    return current and current["state"] in ("ready", "processing", "committed", "blocked")


def _report_failure(observer, accepted, reason, started, exception=None):
    scope = _scope(observer, accepted)
    try:
        with CAPTURE_IMAGE_LOCK:
            if not _still_needed(observer, accepted["id"]):
                return
            DIAGNOSTICS.report(**scope, reason=reason, duration_ms=(time.monotonic()-started)*1000, exception=exception)
            Presentation(observer.repo.root).record_thumbnail_failure(accepted["id"], reason)
    except Exception as exc:
        DIAGNOSTICS.report(**scope, reason="storage_failed", exception=exc)


def capture_thumbnail(observer, accepted, cell_before):
    if accepted["state"] != "ready":
        return
    started, stage = time.monotonic(), "identity"
    try:
        with CAPTURE_IMAGE_LOCK:
            if not _still_needed(observer, accepted["id"]):
                return
        if getattr(observer, "cancel", None) is not None and observer.cancel.is_set():
            _report_failure(observer, accepted, "cancelled", started)
            return
        if observer.check_document() != cell_before:
            _report_failure(observer, accepted, "identity_changed", started)
            return
        stage = "request"
        revision = observer.receipt.observed_revision if getattr(observer, "receipt", None) else None
        result = observer.backend.screenshot(observer.target.ref, ScreenshotRequest(
            time.monotonic()+3, expected_revision=revision))
        if result.status != "success":
            reason = result.reason_code
            if reason not in ("identity_changed", "timeout", "invalid_image", "unsupported", "cancelled"):
                reason = "unexpected"
            _report_failure(observer, accepted, reason, started)
            return
        png = result.png
        stage = "identity"
        if observer.check_document() != cell_before:
            _report_failure(observer, accepted, "identity_changed", started)
            return
        stage = "image"
        validate_png(png)
        info = {
            "source": observer.backend.backend_id + ".screenshot",
            "captured_at": result.captured_at,
            "binding": result.binding,
            "raw_sha256": accepted["sha256"],
            "document": observer.document(),
            "active_cell": cell_before,
            "scope": "current_viewport",
            "atomic_revision": result.binding == "atomic" and revision is not None and result.observed_revision == revision,
        }
        stage = "storage"
        with CAPTURE_IMAGE_LOCK:
            if not _still_needed(observer, accepted["id"]):
                return
            Presentation(observer.repo.root).put_thumbnail(accepted["id"], png, info)
        DIAGNOSTICS.report(**_scope(observer, accepted), reason=None,
                           duration_ms=(time.monotonic()-started)*1000)
    except Exception as exc:
        # Do not parse vendor exception messages. Typed vendor error mapping is B2.
        code = getattr(exc, "code", None)
        reason = (exc.reason_code if isinstance(exc, BackendError) and exc.reason_code in (
                      "identity_changed", "timeout", "invalid_image", "unsupported", "cancelled")
                  else "identity_changed" if code in ("DOCUMENT_CHANGED", "NO_DOCUMENT", "DOCUMENT_AMBIGUOUS")
                  else "cancelled" if isinstance(exc, SaveCancelled)
                  else "timeout" if isinstance(exc, TimeoutError)
                  else "storage_failed" if stage == "storage"
                  else "invalid_image" if stage == "image" and isinstance(exc, (PresentationError, ValueError))
                  else "unexpected")
        _report_failure(observer, accepted, reason, started, exc)
