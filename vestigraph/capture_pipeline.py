"""Durable capture -> serial background history organization.

Only the capture thread talks to KLink. The worker consumes accepted, immutable
copies and frozen evidence. Checkpoint publication and queue acknowledgement
share one SQLite transaction; source cleanup happens afterwards.
"""
from __future__ import annotations
import logging

import json
from pathlib import Path
import threading
import time

from .capture_spool import Spool, CaptureQueueBlocked, capture_suffix
from . import history_order
from .store import RepositoryError, SaveCancelled, _now

TERMINAL = ("committed", "unchanged")


def validate_capture(db, repo, capture_id, segment_id, path=None, digest=None):
    """Internal transaction guard. Closed segments are allowed ONLY for accepted work."""
    row = db.execute("SELECT * FROM capture_queue WHERE id=?", (capture_id,)).fetchone()
    if row is None or row["state"] != "processing" or not row["sha256"]:
        raise RepositoryError("Capture is not an accepted processing item.")
    row = dict(row)
    meta = json.loads(row["metadata"])
    if meta.get("segment_id") != segment_id:
        raise RepositoryError("Capture segment does not match its frozen context.")
    if segment_id is not None and db.execute(
            "SELECT 1 FROM segments WHERE id=?", (segment_id,)).fetchone() is None:
        raise RepositoryError("Captured segment is missing.")
    expected_path = (repo.root / "capture-spool" / (capture_id + capture_suffix(meta, formats=repo.services.formats))).resolve()
    if path is not None and Path(path).resolve() != expected_path:
        raise RepositoryError("Capture source is not its reserved copy.")
    if digest is not None and digest != row["sha256"]:
        raise RepositoryError("Accepted capture bytes changed; original copy retained.")
    first = db.execute("SELECT id FROM capture_queue WHERE state NOT IN ('committed','unchanged','quarantined') "
                       "ORDER BY ordinal LIMIT 1").fetchone()
    if first is None or first[0] != capture_id:
        raise RepositoryError("Captured copies must be organized in acceptance order.")
    predecessor = meta.get("predecessor_capture_id")
    expected = meta.get("expected_head_id")
    ordinal = row["ordinal"]
    while predecessor:
        previous = db.execute("SELECT state,checkpoint_id,metadata,ordinal FROM capture_queue WHERE id=?",
                              (predecessor,)).fetchone()
        if previous is None or previous[3] >= ordinal:
            raise RepositoryError("Capture predecessor is missing or cyclic.")
        ordinal = previous[3]
        if previous[0] == "quarantined":
            context = json.loads(previous[2])
            expected = context.get("expected_head_id")
            predecessor = context.get("predecessor_capture_id")
            continue
        if previous[0] not in TERMINAL:
            raise RepositoryError("Capture predecessor is not completed.")
        expected = previous[1]
        break
    head = history_order.head(db)
    if (head["id"] if head else None) != expected:
        raise RepositoryError("History changed outside the capture queue; retained copies need reconciliation.")
    row["metadata"] = meta
    return row


def acknowledge_capture(db, capture_id, checkpoint_id, state="committed", checkpoint_metadata=None, dedupe=None):
    """Called INSIDE the checkpoint transaction (or validated exact-duplicate transaction)."""
    if state not in TERMINAL:
        raise RepositoryError("Invalid capture acknowledgement.")
    row = db.execute("SELECT metadata FROM capture_queue WHERE id=?", (capture_id,)).fetchone()
    meta = json.loads(row[0])
    meta["completed_at"] = _now()
    if dedupe is not None:
        meta["dedupe"] = dedupe
    binding = (checkpoint_metadata or {}).get("binding") or {}
    if binding.get("continuity") == "top_cells_changed":
        db.execute("INSERT INTO events(created_at,kind,source,segment_id,payload) VALUES(?,?,?,?,?)",
                   (_now(), "capture.binding_warning", "system", meta.get("segment_id"),
                    json.dumps({"capture_id": capture_id, "checkpoint_id": checkpoint_id,
                                "reason": "Exported top cells share nothing with the previous export.",
                                "export_top_cells": (binding.get("export_top_cells") or [])[:20]})))
    db.execute("UPDATE capture_queue SET state=?,checkpoint_id=?,metadata=?,error=NULL WHERE id=?",
               (state, checkpoint_id, json.dumps(meta, ensure_ascii=False), capture_id))


class _Cancel:
    def __init__(self, pipeline):
        self.pipeline = pipeline

    def is_set(self):
        p = self.pipeline
        return (p.stopping.is_set() or (p.shutdown is not None and p.shutdown.is_set())
                or (not p.gates and p.cancel.is_set()))


class CapturePipeline:
    """One worker, one lease-owning Repository, a disk-bounded persistent FIFO."""

    def __init__(self, repo, *, on_status=None, cancel=None, spool_options=None, shutdown=None):
        self.repo = repo
        self.spool = Spool(repo, **(spool_options or {}))
        self.on_status = on_status
        self.cancel = cancel if cancel is not None else threading.Event()
        self.shutdown = shutdown
        self.stopping = threading.Event()
        self.wake = threading.Event()
        self.condition = threading.Condition()
        self.gates = 0
        self.thread = None
        self.retry_at = 0.0
        self.last_progress = 0.0
        self.failure = None
        self._active = None

    def status(self, phase, **fields):
        report = {"phase": phase, "capture_queue": self.spool.stats(), **fields}
        if self.on_status:
            try:
                self.on_status(report)
            except Exception:
                logging.getLogger(__name__).warning("Nonfatal callback or cleanup failure", exc_info=True)

    def start(self):
        self.spool.recover()
        self.spool.cleanup_done()
        self.thread = threading.Thread(target=self._run, name="vestigraph-organizer", daemon=True)
        self.thread.start()
        return self

    def check_health(self):
        if self.failure is not None:
            raise CaptureQueueBlocked("Capture organizer stopped; retained copies require recovery.") from self.failure
        first = self.spool.ready_item()
        if first and first["state"] == "blocked":
            raise CaptureQueueBlocked("Capture organization is blocked; retry or discard the retained copy.")

    def notify(self):
        self.wake.set()

    def _progress(self, info):
        now = time.monotonic()
        if now - self.last_progress < .1 and info.get("fraction") != 1:
            return
        self.last_progress = now
        self.status("organizing", stage=info.get("phase"), fraction=info.get("fraction"),
                    bytes=info.get("bytes"), total=info.get("total"))

    def process_one(self):
        """Also usable by restart recovery; never reads editor/session state."""
        row = self.spool.ready_item()
        if not row or row["state"] != "ready":
            return False
        capture_id, meta = row["id"], row["metadata"]
        self.spool.mark_processing(capture_id)
        self._active = capture_id
        self.status("organizing", stage="starting", fraction=None)
        prepared = None
        started = time.monotonic()
        try:
            path = Path(row["path"])
            # Accepted raw duplicates queued behind another save need no second scan.
            if meta.get("source", "system") != "manual" and meta.get("accepted_stat"):
                from .capture_noop import same_document
                with self.repo._connect() as db:
                    db.execute("BEGIN IMMEDIATE")
                    validate_capture(db, self.repo, capture_id, meta.get("segment_id"), path)
                    head = history_order.head(db)
                    parent = self.repo._row(head) if head else None
                    stat = path.stat()
                    stable = [stat.st_size, stat.st_mtime_ns, stat.st_ino, stat.st_ctime_ns] == meta["accepted_stat"]
                    raw_duplicate = bool(stable and parent and parent["sha256"] == row["sha256"]
                                         and same_document(parent, meta))
                    if raw_duplicate:
                        acknowledge_capture(db, capture_id, parent["id"], "unchanged",
                                            dedupe={"basis": "accepted_raw_sha256", "checkpoint_id": parent["id"]})
                if raw_duplicate:
                    self._completed(row, parent, True, started)
                    return True
            prepared = self.repo.prepare(path, segment_id=meta.get("segment_id"),
                                         progress=self._progress, cancel=_Cancel(self),
                                         _capture_id=capture_id)
            if prepared.raw_sha256 != row["sha256"]:
                raise RepositoryError("Accepted copy hash changed before organization.")
            # Automatic no-op receipts ignore verified GDS timestamps; named versions do not.
            parent = prepared.parent
            unchanged = bool(parent and parent["sha256"] == prepared.raw_sha256)
            from .capture_noop import same_document
            pm = (parent or {}).get("manifest") or {}
            normalized_equal = (pm.get("normalized_algorithm") == "gds-timestamp-zero-v1"
                                and prepared.manifest.get("normalized_algorithm") == pm["normalized_algorithm"]
                                and prepared.normalized_sha256 == pm.get("normalized_sha256"))
            if parent and pm.get("format") == 1 and same_document(parent, meta) and meta.get("source", "system") != "manual":
                handler = self.repo.services.formats.storage_for_manifest(prepared.manifest)
                compare = getattr(handler, "normalized_legacy", None)
                if compare is not None:
                    try:
                        normalized_equal = compare(self.repo, parent) == (prepared.manifest["normalized_algorithm"], prepared.normalized_sha256)
                    except Exception:
                        normalized_equal = False  # no proof: keep the newly captured version
            unchanged = bool(parent and same_document(parent, meta) and (unchanged or normalized_equal))
            skipped = (unchanged and meta.get("source", "system") != "manual"
                       and not getattr(prepared, "recovery", None))
            if skipped:
                with self.repo._connect() as db:
                    db.execute("BEGIN IMMEDIATE")
                    validate_capture(db, self.repo, capture_id, meta.get("segment_id"),
                                     path, prepared.raw_sha256)
                    acknowledge_capture(db, capture_id, parent["id"], "unchanged",
                                        dedupe={"basis": "prepared_normalized_hash", "checkpoint_id": parent["id"],
                                                "raw_bytes_equal": parent["sha256"] == prepared.raw_sha256})
                prepared.discard()
                record = parent
            else:
                metadata = dict(meta.get("checkpoint_metadata") or {})
                binding = dict(metadata.get("binding") or {})
                tops = prepared.top_cells
                tops = sorted(n.rstrip(b"\0").decode("ascii", "replace") for n in tops) if isinstance(tops, list) else None
                binding.update(export_top_cells=tops, scan=prepared.format_analysis)
                previous = ((parent or {}).get("metadata") or {}).get("binding") or {}
                prior_tops = previous.get("export_top_cells")
                binding["continuity"] = "unavailable"
                if tops is not None and isinstance(prior_tops, list):
                    binding["continuity"] = ("same_top_cells" if set(tops) & set(prior_tops)
                                              or (not tops and not prior_tops) else "top_cells_changed")
                metadata.update(binding=binding, capture_id=capture_id,
                                captured_at=meta.get("captured_at", row.get("created_at")),
                                content_sha256=prepared.normalized_sha256,
                                content_algorithm=prepared.manifest["normalized_algorithm"],
                                same_as_previous=unchanged, scan=prepared.stats.get("scan"),
                                timing={"export_ms": meta.get("export_ms"), "layout_bytes": row["size"],
                                        "prepare_ms": prepared.stats.get("timing_ms", {})})
                record = self.repo.commit(prepared, title=meta.get("title", "Captured layout"),
                                          source=meta.get("source", "system"),
                                          segment_id=meta.get("segment_id"), metadata=metadata)
            self._completed(row, record, skipped, started)
            return True
        except SaveCancelled:
            self.spool.retry(capture_id, error="Organization cancelled; accepted copy retained.")
            self.retry_at = time.monotonic() + 5
            self.status("organization_cancelled", capture_id=capture_id)
            return False
        except Exception as exc:
            current = self.spool.get(capture_id)
            if current["state"] not in TERMINAL:
                self.spool.block(capture_id, str(exc)[:500])
            self.status("organization_blocked", error=str(exc)[:500])
            return False
        finally:
            if prepared is not None:
                prepared.discard()
            self._active = None
            self.cancel.clear()
            with self.condition:
                self.condition.notify_all()

    def _completed(self, row, record, skipped, started):
        # A cleanup failure must never turn a committed checkpoint back into ready.
        if skipped:
            try:
                from .presentation import Presentation
                Presentation(self.repo.root).discard_thumbnail(row["id"])
            except Exception:
                logging.getLogger(__name__).warning("Nonfatal callback or cleanup failure", exc_info=True)
        try:
            self.spool.cleanup_done()
        except OSError:
            pass
        self.status("organized", checkpoint_id=record["id"], capture_id=row["id"],
                    unchanged=skipped, ingest_ms=round((time.monotonic()-started)*1000, 1),
                    export_ms=row["metadata"].get("export_ms"), layout_bytes=row["size"],
                    timing_ms=record.get("timing_ms"), scan=record.get("scan"))

    def _run(self):
        try:
            while not self.stopping.is_set():
                if (self.gates or time.monotonic() >= self.retry_at) and self.process_one():
                    continue
                self.wake.wait(.2)
                self.wake.clear()
        except BaseException as exc:
            self.failure = exc
            with self.condition:
                self.condition.notify_all()

    def drain(self, capture_id=None):
        """Baseline/manual/tail barrier. Never falsely report completion of retained work."""
        with self.condition:
            self.gates += 1
        self.wake.set()
        try:
            while True:
                if self.failure:
                    raise RepositoryError(f"Capture organizer stopped: {self.failure}")
                if self.stopping.is_set():
                    raise RepositoryError("Capture organizer is stopping; accepted copies retained.")
                if capture_id is not None:
                    row = self.spool.get(capture_id)
                    if row["state"] == "quarantined":
                        raise CaptureQueueBlocked("The requested capture was discarded.")
                    if row["state"] in TERMINAL:
                        # Publication precedes cleanup and the organized callback.
                        # A baseline must not advertise recording before that callback
                        # has populated last_checkpoint_id in the coordinator.
                        with self.condition:
                            if self._active != capture_id:
                                return self.repo.get_checkpoint(row["checkpoint_id"])
                first = self.spool.ready_item()
                if first is None:
                    with self.condition:
                        if capture_id is None and self._active is None:
                            return None
                elif first["state"] in ("blocked", "writing"):
                    raise CaptureQueueBlocked("Captured copy is blocked; retained for inspection: " + str(first.get("error")))
                with self.condition:
                    self.condition.wait(.1)
        finally:
            with self.condition:
                self.gates -= 1

    def close(self):
        """Join before the caller releases its writer lease, even on recorder failure."""
        self.stopping.set()
        self.wake.set()
        if self.thread:
            self.thread.join()
