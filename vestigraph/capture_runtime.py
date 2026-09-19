"""One editor-neutral recording state machine over EditorBackend.

v0.2 additions (all optional, old calls unchanged): classified ``CaptureError``
codes, a write-free ``probe_document`` so a supervisor never starts a recording
without an eligible document, ``capture_context`` written into new records,
``on_status`` progress callbacks (worker loop only), and a ``commands`` queue
for named saves while recording.
"""
from __future__ import annotations
import logging

from .capture_errors import (CaptureError, NO_DOCUMENT, DISCONNECTED, UNSUPPORTED,
    CORRUPT_STORE, EXPORT_FAILED, DOCUMENT_CHANGED, OVERFLOW, NO_BASELINE, INVALID_EVENT,
    DOCUMENT_AMBIGUOUS)
from vestigraph_backends.types import (
    BackendError,
    BackendEvent,
    EventKind,
    Availability,
    ExportRequest,
    ScreenshotRequest,
)
from vestigraph_backends.guard import GuardedBackend
from .vesti_formats.registry import select_export_format
import json
import math
import os
from pathlib import Path

from .fingerprint import content_fingerprint
from .store import SaveCancelled
import queue
import tempfile
import threading
import time

from .store import MAX_PAYLOAD, RepositoryError

QUEUE_SIZE = 1024
CONTEXT_KEY = "vestigraph_context"
BOUNDARIES = {EventKind.SELECTION_CHANGED, EventKind.OPERATION_FINISHED}




def _positive(name, value):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value <= 0):
        raise ValueError(f"{name} must be finite and positive; adjust the recording options.")
    return float(value)




def _payload(value):
    if not isinstance(value, dict):
        raise CaptureError(INVALID_EVENT, "KLink event is not an object; inspect the adapter before restarting.")
    try:
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CaptureError(INVALID_EVENT, "KLink emitted a non-JSON payload; inspect the event and restart.") from exc
    if len(raw) > MAX_PAYLOAD:
        return {
            "vestigraph_truncated": True,
            "original_json_bytes": len(raw),
            "summary": "Payload exceeded 256 KiB; the original event details were not stored.",
        }
    return json.loads(raw)




def _is_transport_error(exc):
    name = type(exc).__name__
    return isinstance(exc, (OSError, ConnectionError, TimeoutError)) or "Transport" in name or "Connection" in name


def classify(exc) -> str:
    """Stable code for any exception escaping a capture step."""
    from .capture_spool import CaptureQueueBlocked, SpoolFull
    if isinstance(exc, SpoolFull):
        return "CAPTURE_SPOOL_FULL"
    if isinstance(exc, CaptureQueueBlocked):
        return "CAPTURE_QUEUE_BLOCKED"
    if isinstance(exc, CaptureError):
        return exc.code
    if isinstance(exc, BackendError):
        return {"no_document": NO_DOCUMENT, "identity_changed": DOCUMENT_CHANGED,
                "stale_document": DOCUMENT_CHANGED, "document_ambiguous": DOCUMENT_AMBIGUOUS,
                "unsupported": UNSUPPORTED, "events_unavailable": UNSUPPORTED,
                "disconnected": DISCONNECTED, "offline": DISCONNECTED, "timeout": DISCONNECTED,
                "invalid_event": INVALID_EVENT}.get(exc.reason_code, EXPORT_FAILED)
    if isinstance(exc, RepositoryError):
        return CORRUPT_STORE
    if _is_transport_error(exc):
        return DISCONNECTED
    return EXPORT_FAILED




def _latest_content_fingerprint(repo, document=None):
    """(algorithm, value) of the newest version OF THIS DOCUMENT, so a restarted recorder
    does not re-save it. None when the newest version belongs to another file (a "Save As"
    copy must get its own baseline) or carries no comparable value.

    Format-2 histories compare the manifest's normalized hash (strict record scan);
    format-1 histories the fingerprint.py value. The two algorithms are never compared
    with each other."""
    try:
        latest = repo.history(limit=1)
    except Exception:  # noqa: BLE001 - a history read problem must not stop recording
        return None
    if not latest:
        return None
    record = latest[0]
    metadata = record.get("metadata") or {}
    if not isinstance(metadata, dict):
        return None
    if document is not None:
        stored = metadata.get("document")
        if not isinstance(stored, dict) or stored.get("filename") != document.get("filename"):
            return None
    manifest = record.get("manifest") or {}
    if isinstance(manifest, dict) and manifest.get("format") == 2 and isinstance(manifest.get("normalized_sha256"), str):
        return (manifest.get("normalized_algorithm"), manifest["normalized_sha256"])
    value = metadata.get("content_sha256")
    return (FINGERPRINT_V1, value) if isinstance(value, str) else None


FINGERPRINT_V1 = "fingerprint-v1"


def _prepared_top_cells(prepared):
    """Top cells from the strict scan (raw STRNAME bytes), in the shape file_info gives."""
    tops = getattr(prepared, "top_cells", None)
    if not isinstance(tops, list):
        return None
    return sorted(name.rstrip(b"\0").decode("ascii", "replace") for name in tops)


class Observer:
    def __init__(self, repo, backend, session, context=None, on_status=None, expected=None, cancel_event=None,
                 durable_capture=False, spool_options=None, connected=False, queue_size=QUEUE_SIZE):
        self.pipeline = None
        self.durable_capture = bool(durable_capture and getattr(repo, "format", 1) == 2)
        self.spool_options = spool_options
        self.expected = expected
        self.cancel = cancel_event if cancel_event is not None else threading.Event()
        self.lost_events = 0            # events whose payload was dropped because the queue was full
        self.lost_boundary = False
        self.pending_since_wall = None
        self.cancelled_last = False
        self.cancellable = True
        self.repo = repo
        if isinstance(backend, GuardedBackend):
            self.backend = GuardedBackend(backend.provider, formats=repo.services.formats)
            self.backend.session = backend.session
        else:
            self.backend = GuardedBackend(backend, formats=repo.services.formats)
        self.session = session
        self.connected = connected
        self.subscription = None
        self.receipt = None
        self.format = None
        self.context = dict(context) if isinstance(context, dict) else None
        self.on_status = on_status
        self.queue = queue.Queue(queue_size)
        self.ingress_lock = threading.Lock()
        self.accepting = True
        self.overflow = threading.Event()
        self.segment = None
        self.target = None
        self.count = 0
        self.dirty = self.boundary = False
        self.last_content = None       # timestamp-blind fingerprint of the last stored version
        self.baselined = False
        self.last_event = time.monotonic()
        self.dirty_since = None
        self.last_export = float("-inf")
        self.cutoff = None
        self.last_timing = None
        self.last_tops = None
        # Compact attribution for the pending mutating KLink RPC.
        self.pending_operation = None
        self.manual_dirty = False
        self.last_edit_at = None
        self.boundary_kind = None

    def _meta(self, **fields):
        meta = {"capture": "editor", "backend_id": self.backend.backend_id, "consistency": "best_effort"}
        if self.pending_operation:
            meta["operation"] = dict(self.pending_operation)
        if self.last_edit_at:
            meta["modified_at"] = self.last_edit_at
            meta["modified_at_basis"] = "observed_editor_event"
        meta.update(fields)
        if self.context:
            meta[CONTEXT_KEY] = self.context
        return meta

    def status(self, phase, **fields):
        if self.on_status is None:
            return
        report = {"phase": phase, "at": time.time(), "checkpoints": self.count,
                  "document": self.document() if self.target else None}
        report.update(fields)
        try:
            self.on_status(report)
        except Exception:
            logging.getLogger(__name__).warning("Nonfatal callback or cleanup failure", exc_info=True)

    def enqueue(self, event):
        # No editor calls or DB writes on a provider callback thread.
        if event.affects_content is False:
            return
        with self.ingress_lock:
            if not self.accepting:
                return
            try:
                self.queue.put_nowait(event)
            except queue.Full:
                self.lost_events += 1
                self.lost_boundary = self.lost_boundary or event.kind in BOUNDARIES

    def freeze(self):
        with self.ingress_lock:
            self.accepting = False
            if self.cutoff is None:
                self.cutoff = time.monotonic()

    def check_overflow(self):
        """Fold dropped event payloads into one counted marker (called from the worker loop)."""
        with self.ingress_lock:
            lost, boundary = self.lost_events, self.lost_boundary
            self.lost_events, self.lost_boundary = 0, False
        if not lost:
            return
        self.begin()
        self.repo.append_event("capture.events_coalesced", {
            "count": lost, "queue_size": self.queue.maxsize,
            "note": "The editor emitted events faster than they could be stored; their payloads were dropped, "
                    "the layout is still snapshotted."}, source="system", segment_id=self.segment)
        self._mark_dirty(boundary)
        self.status("coalesced", count=lost)

    def _mark_dirty(self, boundary, kind=None):
        if not self.dirty:
            self.dirty_since = time.monotonic()
            self.pending_since_wall = time.time()
            self.status("pending", pending_since=self.pending_since_wall)
        self.dirty = True
        self.boundary = self.boundary or boundary
        if boundary and kind is not None:
            self.boundary_kind = kind
        self.last_event = time.monotonic()

    def begin(self, title="Observed editor edits"):
        if self.segment is None:
            self.segment = self.repo.begin_segment(
                title, source="mixed", metadata=self._meta(session_instance_id=self.session.session_instance_id),
            )["id"]

    def close_segment(self, status="closed"):
        if self.segment is not None:
            self.repo.close_segment(self.segment, status=status)
            self.segment = None
        self.pending_operation = None
        self.manual_dirty = False

    def document(self):
        ref = self.target.ref
        return {"filename": self.target.source_path, "source_uri": self.target.source_uri,
                "backend_id": ref.backend_id, "session_instance_id": ref.session_instance_id,
                "document_instance_id": ref.document_instance_id,
                "vendor_evidence": json.loads(self.target.evidence_json)}

    def check_document(self):
        """The provider validates the bound document; a view name is not a revision."""
        return self.backend.inspect_document(self.target.ref).view_name

    def _binding_evidence(self, active_cell, tops):
        """What ties this export to the bound document, and how strongly.

        Continuity compares the exported top cells with the previous export of
        the same run: a complete change is recorded as a warning event, never
        silently accepted as the same design, and never a silent rejection.
        """
        evidence = {**(json.loads(self.receipt.evidence_json) if self.receipt else {}),
                    "identity": self.document(), "active_cell": active_cell,
                    "export_top_cells": tops, "confidence": "best_effort",
                    "limitation": "No atomic revision is assumed by the recorder.",
                    "artifact_role": self.format.artifact_role if self.format else "unknown"}
        if self.receipt:
            evidence["export_consistency"] = self.receipt.consistency
            evidence["observed_revision"] = self.receipt.observed_revision
            evidence["limitations"] = list(self.receipt.limitations)
        previous = getattr(self, "last_tops", None)
        if tops is not None and previous is not None:
            shared = set(tops) & set(previous)
            evidence["continuity"] = "same_top_cells" if shared or (not tops and not previous) else "top_cells_changed"
            if evidence["continuity"] == "top_cells_changed":
                self.repo.append_event("capture.binding_warning", {
                    "reason": "exported top cells share nothing with the previous export of this document",
                    "previous_top_cells": previous[:20], "export_top_cells": tops[:20]},
                    source="system", segment_id=self.segment)
        elif tops is None:
            evidence["continuity"] = "unavailable"
        return evidence

    def _export_top_cells(self, path):
        return list(self.receipt.top_cells) if self.receipt and self.receipt.top_cells is not None else None

    def _export(self, path):
        self.receipt = self.backend.export_snapshot(self.target.ref, ExportRequest(
            path.resolve(), self.format.format_id, time.monotonic()+120,
            include_structure_summary=getattr(self.repo, "format", 1) == 1))
        return self.receipt

    def _store_v1(self, path, title, source, cell_before, export_ms, layout_bytes):
        """Format-1 history: file_info top cells + fingerprint.py, unchanged from v0.2."""
        tops = self._export_top_cells(path)
        binding = self._binding_evidence(cell_before, tops)
        content = (FINGERPRINT_V1, content_fingerprint(path, formats=self.repo.services.formats))
        unchanged = content == self.last_content
        if unchanged and source != "manual":
            return None, tops, content, unchanged
        record = self.repo.checkpoint(
            path, title=title, source=source, segment_id=self.segment,
            metadata=self._meta(document=self.document(), format=self.format.format_id,
                                coverage="observed_events_only", binding=binding,
                                content_sha256=content[1], content_algorithm=content[0],
                                same_as_previous=unchanged,
                                timing={"export_ms": export_ms, "layout_bytes": layout_bytes}))
        return record, tops, content, unchanged

    def _store_v2(self, path, title, source, cell_before, export_ms, layout_bytes):
        """Format-2 history: ONE strict scan (Repository.prepare) gives the top cells, the
        timestamp-blind hash and the chunked snapshot; no file_info RPC (KLayout would read
        the whole export again) and no second fingerprint pass. The document identity checks
        around the export are untouched."""
        prepared = self.repo.prepare(path, segment_id=self.segment, progress=self._progress,
                                     cancel=self.cancel if self.cancellable else None)
        try:
            tops = _prepared_top_cells(prepared)
            if tops is None:                       # opaque, duplicate names or paged top cells
                tops = self._export_top_cells(path)
            binding = self._binding_evidence(cell_before, tops)
            binding["scan"] = prepared.format_analysis
            # No check_overflow() here: it writes an event, and the prepared snapshot holds the
            # writer lease. Dropped events are folded right after the commit instead.
            content = (prepared.manifest["normalized_algorithm"], prepared.normalized_sha256)
            unchanged = content == self.last_content
            if unchanged and source != "manual":
                prepared.discard()
                return None, tops, content, unchanged
            self.status("saving", stage="committing", fraction=1.0)
            record = self.repo.commit(
                prepared, title=title, source=source, segment_id=self.segment,
                metadata=self._meta(document=self.document(), format=self.format.format_id,
                                    coverage="observed_events_only", binding=binding,
                                    content_sha256=content[1], content_algorithm=content[0],
                                    same_as_previous=unchanged,
                                    timing={"export_ms": export_ms, "layout_bytes": layout_bytes,
                                            "prepare_ms": prepared.stats.get("timing_ms", {})},
                                    scan=prepared.stats.get("scan")))        # backend actually used
        except BaseException:
            prepared.discard()
            raise
        return record, tops, content, unchanged

    def _progress(self, info):
        """prepare() progress (worker thread): stage + fraction for the status listener."""
        self.status("saving", stage=info.get("phase"), fraction=info.get("fraction"),
                    bytes=info.get("bytes"), total=info.get("total"))

    def snapshot(self, title, source="system", cancellable=True):
        """cancellable=False for the baseline and the tail save: they are safety gates
        (a stop / navigation must not proceed with unsaved changes), so a pending cancel
        flag is ignored and cleared instead of honoured."""
        if title == "Observed editor changes" and self.pending_operation:
            title = self.pending_operation.get("title") or title
            if source == "system":
                source = "automation"
        if self.pipeline is not None:
            return self._snapshot_durable(title, source, cancellable)
        self.check_overflow()
        cell_before = self.check_document()
        self.cancellable = cancellable
        if not cancellable:
            self.cancel.clear()
        self.begin()
        directory = Path(self.repo.root) / "tmp"
        directory.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix="capture-", suffix=self.format.extensions[0], dir=directory)
        os.close(fd)
        path = Path(name)
        responded = False
        dispatched = True      # unless the backend says the export never reached the editor
        started = time.monotonic()
        self.cancelled_last = False
        self.status("saving", stage="exporting", fraction=None)
        try:
            # Only overwrite our reserved temporary file; never the user's source.
            # KLink saves "cellview N of the CURRENT tab": the tab list is checked
            # right before and right after, and the exported file's own content
            # is compared with the observed document. A switch-and-switch-back
            # inside the RPC itself cannot be excluded without a KLink document handle.
            try:
                response = self._export(path)
            except BackendError as exc:
                dispatched = exc.outcome != "not_started"
                raise
            responded = True
            export_ms = round((time.monotonic() - started) * 1000, 1)
            if not path.is_file() or path.stat().st_size == 0:
                raise CaptureError(EXPORT_FAILED, "KLink produced no layout export; check the active layout and retry.")
            layout_bytes = path.stat().st_size
            reported = response.reported_size
            if isinstance(reported, int) and reported not in (0, layout_bytes):
                raise CaptureError(EXPORT_FAILED, "Exported file size differs from what KLink reported; the export is not trusted.")
            cell_after = self.check_document()
            if cell_before != cell_after:
                raise CaptureError(DOCUMENT_CHANGED,
                                   "The active cell changed while exporting; this export is not attributed to the document.")
            ingest_started = time.monotonic()
            try:
                if getattr(self.repo, "format", 1) == 2:
                    record, tops, content, unchanged = self._store_v2(path, title, source, cell_before,
                                                                      export_ms, layout_bytes)
                else:
                    record, tops, content, unchanged = self._store_v1(path, title, source, cell_before,
                                                                      export_ms, layout_bytes)
            except SaveCancelled:
                # Nothing was published; the layout stays dirty so the next interval retries
                # (or the user pauses). The cancel flag is one-shot.
                self.cancelled_last = True
                self.last_export = time.monotonic()
                self.status("cancelled", title=title, export_ms=export_ms, layout_bytes=layout_bytes)
                self.cancel.clear()  # terminal status first: late requests cannot leak to the next save
                return None
            if record is None:
                # Nothing changed since the last stored version of this document (only KLayout's
                # save timestamps differ): an automatic save must not become a new version.
                self.last_tops = tops
                self.last_export = time.monotonic()
                self.status("unchanged", title=title, export_ms=export_ms, layout_bytes=layout_bytes)
                self.cancel.clear()
                return None
            self.last_content = content
            self.last_tops = tops
            ingest_ms = round((time.monotonic() - ingest_started) * 1000, 1)
            self.count += 1
            self.last_export = time.monotonic()
            queue_wait_ms = round((started - self.dirty_since) * 1000, 1) if self.dirty_since is not None else None
            self.pending_since_wall = None
            self.last_timing = {"export_ms": export_ms, "ingest_ms": ingest_ms, "layout_bytes": layout_bytes,
                                "queue_wait_ms": queue_wait_ms,
                                "save_total_ms": round((time.monotonic() - started) * 1000, 1),
                                "timing_ms": record.get("timing_ms") if isinstance(record, dict) else None,
                                "scan": record.get("scan") if isinstance(record, dict) else None}
            self.check_overflow()
            checkpoint_id = record.get("id") if isinstance(record, dict) else None
            self.status("saved", checkpoint_id=checkpoint_id, title=title, **self.last_timing)
            self.cancel.clear()          # state no longer says saving; consume any late cancellation
            return record
        finally:
            # A timed-out RPC may still be writing. Keep that exact temp path,
            # never race its writer or misrepresent it as a committed checkpoint.
            # An export that never reached the editor, or one that answered, has no writer:
            # remove the file now (Repository.cleanup_orphans ages out the rest).
            if responded or not dispatched:
                path.unlink(missing_ok=True)

    def _queue_status(self, report):
        report = dict(report)
        phase = report.pop("phase")
        if phase == "organized" and not report.get("unchanged"):
            self.count += 1
        self.status(phase, **report)

    def _snapshot_durable(self, title, source, cancellable):
        from .capture_spool import SpoolFull, AcceptancePending
        self.pipeline.check_health()
        self.check_overflow()
        self.cancelled_last = False
        wait = not cancellable or source == "manual"
        if wait:
            # Baseline, named save and navigation/tail remain completion barriers.
            self.pipeline.drain()
        cell_before = self.check_document()
        self.receipt = None  # A reservation must not inherit the previous export's evidence.
        self.begin()
        with self.repo._connect() as db:
            evidence = self.repo._evidence(db, self.segment)
        evidence["capture_boundary"] = "events persisted before export; not an atomic editor revision"
        metadata = {
            "spool_suffix": self.format.extensions[0],
            "segment_id": self.segment, "title": title, "source": source, "evidence": evidence,
            "checkpoint_metadata": self._meta(
                document=self.document(), format=self.format.format_id, coverage="observed_events_only",
                artifact_role=self.format.artifact_role, binding=self._binding_evidence(cell_before, None)),
        }
        try:
            item = self.pipeline.spool.reserve(metadata)
        except SpoolFull:
            self.last_export = time.monotonic()
            self.cancelled_last = True  # existing loop keeps dirty state, no successful capture
            self.status("capture_full", capture_queue=self.pipeline.spool.stats())
            raise SpoolFull("Capture queue or disk reserve is full; free capacity before resuming.")
        path = Path(item["path"])
        started = time.monotonic()
        self.status("capturing", capturing=True, capture_queue=self.pipeline.spool.stats())
        try:
            response = self._export(path)
            export_ms = round((time.monotonic() - started) * 1000, 1)
            if not path.is_file() or not path.stat().st_size:
                raise CaptureError(EXPORT_FAILED, "KLink produced no layout export.")
            size = path.stat().st_size
            reported = response.reported_size
            if isinstance(reported, int) and reported not in (0, size):
                raise CaptureError(EXPORT_FAILED, "Export size disagrees with KLink; copy not accepted.")
            if self.check_document() != cell_before:
                raise CaptureError(DOCUMENT_CHANGED, "Active cell changed during export; copy not attributed.")
            accepted = self.pipeline.spool.accept(item["id"], export_ms=export_ms,
                checkpoint_metadata=self._meta(document=self.document(), format=self.format.format_id,
                    coverage="observed_events_only", artifact_role=self.format.artifact_role,
                    binding=self._binding_evidence(cell_before, None)))
        except AcceptancePending:
            # Export and identity checks succeeded; its fsynced verification receipt
            # allows recovery without KLayout. Do not quarantine it as an unknown export.
            raise
        except BaseException as exc:
            # Unknown exports may still be writing after RPC timeout. Retain, never retry as ready.
            self.pipeline.spool.block(item["id"], "Export not accepted: " + str(exc)[:400])
            raise
        finally:
            self.status("capture_finished", capturing=False, capture_queue=self.pipeline.spool.stats())
        self.last_export = time.monotonic()
        self.pending_since_wall = None
        if accepted["state"] == "unchanged":
            self.status("captured", capturing=False, pending_since=None,
                        export_ms=export_ms, layout_bytes=size, capture_queue=self.pipeline.spool.stats())
            self.status("organized", unchanged=True, checkpoint_id=accepted["checkpoint_id"],
                        capture_id=item["id"], ingest_ms=0, export_ms=export_ms, layout_bytes=size,
                        dedupe=accepted["metadata"].get("dedupe"), capture_queue=self.pipeline.spool.stats())
            return self.repo.get_checkpoint(accepted["checkpoint_id"]) if wait else None
        self.status("captured", capturing=False, pending_since=None,
                    export_ms=export_ms, layout_bytes=size, capture_queue=self.pipeline.spool.stats())
        self.pipeline.notify()
        from .capture_thumbnail import capture_thumbnail
        capture_thumbnail(self, accepted, cell_before)
        return self.pipeline.drain(item["id"]) if wait else {"capture_id": item["id"], "accepted": True}

    def consume(self, event):
        if event.affects_content is False:
            return
        if event.kind == EventKind.CONNECTION_LOST:
            reason = json.loads(event.details_json).get("reason_code")
            raise CaptureError(INVALID_EVENT if reason == "invalid_event" else DISCONNECTED,
                               "Editor event stream is unavailable or invalid.")
        if event.ref is not None and self.target is not None and event.ref != self.target.ref:
            return  # Another document's event must never dirty this one.
        payload = json.loads(event.vendor_evidence_json or event.details_json)
        self.begin()
        self.repo.append_event(
            event.record_kind or event.kind.value, payload,
            source=event.source if event.source in ("manual", "automation", "system", "mixed", "unknown") else "unknown",
            segment_id=self.segment,
        )
        if event.kind == EventKind.OPERATION_STARTED:
            return
        cause = payload.get("caused_by") if isinstance(payload, dict) else None
        if isinstance(cause, list) and cause:
            first = cause[0] if isinstance(cause[0], dict) else {}
            method = first.get("method")
            if method:
                method = str(method)[:120]
                reason = first.get("reason")
                label = str(reason).strip()[:240] if isinstance(reason, str) and reason.strip() else method
                self.pending_operation = {
                    "method": method,
                    "trace_id": str(first.get("trace_id", ""))[:120],
                    "reason": label,
                    "title": f"AI operation: {label}",
                }
        if (not cause or payload.get("manual_changes")) and event.kind in (EventKind.CONTENT_CHANGED, EventKind.DOCUMENT_CHANGED):
            self.manual_dirty = True
        if event.kind in (EventKind.CONTENT_CHANGED, EventKind.OPERATION_FINISHED):
            self.last_edit_at = event.observed_at
        self._mark_dirty(event.kind in BOUNDARIES, event.kind)

    def _select_document(self):
        selected, _ = probe_document(self.backend)
        if self.expected is not None and selected.ref != self.expected:
            raise CaptureError(DOCUMENT_CHANGED, "Active document changed before recording started.")
        return selected

    def start(self):
        if not self.connected:
            self.backend.connect(self.session)
        self.subscription = self.backend.observe(self.enqueue)
        self.target = self._select_document()
        capabilities = self.backend.capabilities(self.target.ref)
        try:
            self.format = select_export_format(capabilities, self.target.format_hint, formats=self.repo.services.formats)
        except ValueError:
            raise BackendError("unsupported_format", outcome="not_started") from None
        self.status("baselining")
        if self.durable_capture:
            from .capture_pipeline import CapturePipeline
            self.pipeline = CapturePipeline(self.repo, on_status=self._queue_status,
                                            cancel=self.cancel, spool_options=self.spool_options).start()
            self.pipeline.drain()
        self.last_content = _latest_content_fingerprint(self.repo, self.document())
        self.begin("Editor capture baseline")
        self.repo.append_event("capture.started", {
            "coverage": "best_effort", "backend_id": self.backend.backend_id,
            "document": self.document(),
            "warning": "No replay of missing events; GUI edits may be unattributed; brief document switches may escape detection.",
        }, source="system", segment_id=self.segment)
        self.snapshot("Capture baseline", cancellable=False)
        self.baselined = True
        self.close_segment()
        self.status("recording")

    def finish(self, status="closed"):
        self.freeze()
        self.status("draining")
        if self.pipeline is not None:
            self.pipeline.drain()
        self.check_overflow()
        # Freeze+enqueue share a lock: this is a finite prefix even if editing continues.
        while True:
            try:
                event = self.queue.get_nowait()
            except queue.Empty:
                break
            self.consume(event)
        if self.target is None or not self.baselined:
            raise CaptureError(NO_BASELINE, "Capture stopped before a baseline was saved; restart on the intended layout.")
        self.check_document()
        self.begin("Editor capture stopped")
        if self.dirty:
            self.snapshot("Final observed editor changes", cancellable=False)
            if self.cancelled_last:      # cannot happen with cancellable=False; guard the gate anyway
                raise CaptureError(EXPORT_FAILED, "The final save was cancelled; the tail is not saved.")
            self.dirty = self.boundary = False
        self.repo.append_event("capture.stopped", {
            "coverage_cutoff": "callback ingress frozen and queued prefix drained",
            "warning": "Final export may include edits after event cutoff; no atomic revision binding.",
            "status": status,
        }, source="system", segment_id=self.segment)
        self.close_segment(status)
        self.status("stopped", status=status)

    def record_gap(self, error):
        self.freeze()
        self.begin("Interrupted editor capture")
        self.repo.append_event("capture.gap", {"reason": str(error)[:4096], "code": classify(error)},
                               source="system", segment_id=self.segment)
        self.close_segment("interrupted")
        self.status("gap", code=classify(error), reason=str(error)[:500])


def probe_document(backend):
    documents = backend.list_documents()
    active = [document for document in documents if document.active]
    if len(active) != 1:
        raise CaptureError(NO_DOCUMENT, "No unique active document; select one in the editor.")
    selected = active[0]
    if selected.source_path and sum(d.source_path == selected.source_path for d in documents) > 1:
        raise CaptureError(DOCUMENT_AMBIGUOUS, "The same file is open more than once; ownership is ambiguous.")
    backend.inspect_document(selected.ref)
    return selected, documents


def observe(repo, backend, session, *, capture_context=None, on_status=None, commands=None,
            expected_document=None, cancel_event=None, durable_capture=False, spool_options=None,
            connected=False, **options):
    observer = Observer(repo, backend, session, context=capture_context, on_status=on_status,
                        expected=expected_document, cancel_event=cancel_event,
                        durable_capture=durable_capture, spool_options=spool_options, connected=connected)
    return run_observer(observer, commands=commands, **options)


def run_observer(observer, *, idle_seconds=5, min_interval=15, max_interval=60,
                 duration=None, stop_event=None, commands=None):
    """Record one current document; return checkpoint count, or raise on a gap.

    Call from a worker thread for a native UI or a service. Set stop_event for a
    graceful stop (drains and saves the tail). Duration/stop cannot preempt a
    running RPC/export. No HTTP server is started.

    capture_context: dict copied into every new segment/checkpoint metadata
    under "vestigraph_context" (old records are never rewritten).
    on_status: callable(dict) invoked from the worker loop only. Phases: baselining,
    pending (changes waiting for the next save), saving (stage/fraction), saved,
    unchanged, cancelled, coalesced (dropped event payloads counted), draining, stopped, gap.
    cancel_event: threading.Event; setting it aborts the CURRENT save between chunk batches
    (the KLink export RPC itself cannot be interrupted); nothing is published.
    commands: queue.Queue of ("snapshot", title, reply) tuples; reply is a
    dict filled with {"ok": bool, "record"/"error"} and reply["event"] is set.
    """
    idle = _positive("idle_seconds", idle_seconds)
    minimum = _positive("min_interval", min_interval)
    maximum = _positive("max_interval", max_interval)
    if maximum < minimum:
        raise ValueError("max_interval must be at least min_interval; adjust recording options.")
    duration = None if duration is None else _positive("duration", duration)
    if stop_event is not None and not callable(getattr(stop_event, "is_set", None)):
        raise ValueError("stop_event must provide is_set(); pass threading.Event.")
    start, next_poll = time.monotonic(), time.monotonic()
    try:
        try:
            observer.start()
            while True:
                if observer.pipeline is not None:
                    observer.pipeline.check_health()
                now = time.monotonic()
                if ((duration is not None and now - start >= duration)
                        or (stop_event is not None and stop_event.is_set())):
                    observer.finish()
                    break
                observer.check_overflow()
                try:
                    event = observer.queue.get(timeout=min(0.05, max(0., next_poll - now)))
                    observer.consume(event)
                except queue.Empty:
                    pass
                if commands is not None:
                    _serve_commands(observer, commands)
                now = time.monotonic()
                if now >= next_poll:
                    if observer.backend.health() != Availability.ONLINE:
                        raise CaptureError(DISCONNECTED, "Editor connection is unavailable.")
                    observer.check_document()
                    next_poll = now + max(.01, min(1., idle, minimum, maximum))
                age = now - observer.last_export
                operation_boundary = (observer.boundary
                                      and observer.boundary_kind == EventKind.OPERATION_FINISHED)
                # A mutating AI RPC marks the end of one low-level operation,
                # not the end of the conversation. Coalesce the burst and
                # checkpoint once the interaction has been idle for the quiet window.
                interaction_quiet = (operation_boundary
                                     and now - observer.last_event >= idle)
                if (observer.dirty
                        and (interaction_quiet
                             or (not operation_boundary and age >= minimum and (observer.boundary
                                 or now - observer.last_event >= idle
                                 or now - observer.dirty_since >= maximum)))):
                    observer.snapshot("Observed editor changes")
                    if not observer.cancelled_last:            # a cancelled save keeps the changes pending
                        observer.close_segment()
                        observer.dirty = observer.boundary = False
                        observer.boundary_kind = None
                        observer.pending_operation = None
        except KeyboardInterrupt:
            observer.finish("interrupted")
    except Exception as exc:
        # Distinguish loss of the editor from an actually unsaved observed tail.
        exc.tail_unsaved = bool(observer.dirty or not observer.queue.empty() or not observer.baselined)
        if observer.pipeline is not None:
            exc.tail_unsaved = exc.tail_unsaved or bool(observer.pipeline.spool.stats()["pending_count"])
        try:
            observer.record_gap(exc)
        except Exception as diagnostic:
            raise CaptureError(CORRUPT_STORE,
                f"Capture failed ({exc}); gap could not be persisted ({diagnostic}). "
                "Inspect disk space/permissions and incomplete segments before restarting."
            ) from exc
        raise
    finally:
        observer.freeze()
        if observer.pipeline is not None:
            observer.pipeline.close()
        if commands is not None:
            _reject_commands(commands)
        try:
            if observer.subscription is not None:
                observer.subscription.close()
            observer.backend.close()
        except Exception:
            logging.getLogger(__name__).warning("Nonfatal callback or cleanup failure", exc_info=True)
    return observer.count


def _serve_commands(observer, commands):
    from .capture_spool import AcceptancePending
    while True:
        try:
            command = commands.get_nowait()
        except queue.Empty:
            return
        kind, title, reply = command
        try:
            if kind not in ("snapshot", "prepare_edit"):
                raise CaptureError(UNSUPPORTED, f"Unknown capture command {kind}.")
            if kind == "prepare_edit":
                # The MCP caller has not sent the mutation yet. Drain editor
                # events first; if no AI burst is pending, compare actual
                # content even when GUI events were delayed or missed.
                observer.backend.flush_observed_changes(observer.target.ref)
                while True:
                    try:
                        observer.consume(observer.queue.get_nowait())
                    except queue.Empty:
                        break
                if observer.pending_operation and not observer.manual_dirty:
                    reply.update(ok=True, record=None)
                    continue
                observer.pending_operation = None
                record = observer.snapshot(title, source="system", cancellable=False)
            else:
                record = observer.snapshot(title, source="manual")
            observer.close_segment()
            if observer.cancelled_last:
                reply.update(ok=False, error="Save cancelled by the user.", code="SAVE_CANCELLED")
            else:
                observer.dirty = observer.boundary = False
                reply.update(ok=True, record=record)
        except Exception as exc:
            reply.update(ok=False, error=str(exc), code=classify(exc))
            # Release the recorder lease so offline recovery can retry its verified receipt.
            if isinstance(exc, AcceptancePending) or classify(exc) in (DOCUMENT_CHANGED, OVERFLOW, DISCONNECTED, CORRUPT_STORE):
                raise
        finally:
            reply["event"].set()         # the caller's wait contract: ALWAYS answered, cancel included


def _reject_commands(commands):
    while True:
        try:
            _, _, reply = commands.get_nowait()
        except queue.Empty:
            return
        reply.update(ok=False, error="Recording stopped before this save ran.", code=NO_BASELINE)
        reply["event"].set()
