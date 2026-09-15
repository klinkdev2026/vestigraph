"""Organize accepted live-history copies after restart, without a KLayout connection."""
from __future__ import annotations

import logging
import threading

from ..capture_pipeline import CapturePipeline
from ..capture_spool import pending_stats
from ..store import Repository, RepositoryError

_log = logging.getLogger(__name__)


class CaptureRecovery:
    """At most one recovery worker per service; repository leases arbitrate with recorders."""

    def __init__(self, catalog, on_change=None):
        self.catalog = catalog
        self.on_change = on_change or (lambda: None)
        self.stop_event = threading.Event()
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self._run, name="vestigraph-capture-recovery", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join()  # no lease is released while its encoder still uses the repository

    def recover_document(self, document):
        if document["origin"] != "live" or document["read_only"] or not document["recording_allowed"]:
            return False
        probe = Repository(document["store_path"], readonly=True, services=self.catalog.services)
        if not pending_stats(probe)["enabled"]:
            return False
        with probe._connect() as db:
            needed = db.execute("SELECT 1 FROM capture_queue WHERE state IN ('writing','ready','processing') "
                                "OR (state IN ('committed','unchanged') AND reserved_bytes>0) OR (state='quarantined' AND reserved_bytes>size) LIMIT 1").fetchone()
        if not needed:
            # Older versions could create a spool file before INSERT committed.
            # A directory with only these orphans still needs accounting/reconciliation.
            with probe._connect() as db:
                needed = any(not db.execute("SELECT 1 FROM capture_queue WHERE id=?", (p.stem,)).fetchone()
                             for p in (probe.root / "capture-spool").glob("*")
                             if p.is_file() and any(p.suffix.lower() in spec["extensions"]
                                                    for spec in self.catalog.services.formats.describe()))
            if not needed:
                return False
        repo = Repository(document["store_path"], services=self.catalog.services)
        try:
            repo.acquire_writer("vestigraph-service capture recovery")
        except RepositoryError:
            return False  # a live recorder or external writer owns this history
        pipeline = None
        try:
            pipeline = CapturePipeline(repo, shutdown=self.stop_event,
                                       on_status=lambda _: self.on_change())
            pipeline.spool.recover()
            pipeline.spool.cleanup_done()
            while not self.stop_event.is_set() and pipeline.process_one():
                pass
            return True
        finally:
            if pipeline:
                pipeline.close()
            repo.release_writer()

    def _run(self):
        # The sweep itself is guarded: a single failed catalog read must not end this thread,
        # because nothing else would ever organize the accepted copies it exists for.
        while not self.stop_event.is_set():
            try:
                self._sweep()
            except Exception:
                _log.exception("Capture recovery sweep failed; retrying")
            self.stop_event.wait(2)

    def _sweep(self):
        for project in self.catalog.list_projects():
            if self.stop_event.is_set():
                break
            for document in self.catalog.list_documents(project["id"]):
                if self.stop_event.is_set():
                    break
                try:
                    self.recover_document(document)
                except Exception:
                    _log.exception("Capture recovery retained pending copies for %s", document["id"])
