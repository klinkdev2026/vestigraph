"""Bounded background job runner with persisted state.

Lanes keep heavy work away from each other: one preview worker, two heavy
file workers. HTTP handlers only enqueue; the catalog is the source of truth
for status and results, so a restart keeps finished jobs and settles the rest.
"""
from __future__ import annotations

import queue
import logging
import threading

from .catalog import Catalog
from .errors import ServiceError, queue_full

LANES = {"preview": 1, "heavy": 2}
QUEUE_LIMIT = 32


class JobRunner:
    def __init__(self, catalog: Catalog, queue_limit=QUEUE_LIMIT, lanes=None):
        self.catalog = catalog
        self.queue_limit = queue_limit
        self.lanes = dict(lanes or LANES)
        self.handlers = {}          # kind -> (lane, callable(job) -> (result, asset_path))
        self.queues = {lane: queue.Queue() for lane in self.lanes}
        self.threads = []
        self.pending = 0
        self.deferred = {}  # terminal outcomes awaiting durable catalog storage
        self.lock = threading.Lock()
        self.stopping = threading.Event()
        self.idle = threading.Condition(self.lock)

    def register(self, kind, lane, handler):
        if lane not in self.lanes:
            raise ValueError(f"unknown lane {lane}")
        self.handlers[kind] = (lane, handler)

    def start(self):
        self.catalog.settle_orphan_jobs()
        for lane, count in self.lanes.items():
            for index in range(count):
                thread = threading.Thread(target=self._worker, args=(lane,), daemon=True,
                                          name=f"vestigraph-job-{lane}-{index}")
                thread.start()
                self.threads.append(thread)
        return self

    def stop(self, timeout=10.0):
        self.stopping.set()
        for lane in self.queues:
            for _ in range(self.lanes[lane]):
                self.queues[lane].put(None)
        for thread in self.threads:
            thread.join(timeout)
        # Whatever is still queued/running is ours: report it honestly.
        for lane in self.queues:
            while True:
                try:
                    job_id = self.queues[lane].get_nowait()
                except queue.Empty:
                    break
                if job_id is not None:
                    self.catalog.update_job(job_id, "interrupted", error={
                        "code": "SERVICE_STOPPED", "message": "The service stopped before this task ran."})

    def submit(self, project_id, kind, target_type, target_id, payload, *, request_key=None):
        """Create (idempotently) and enqueue a job. Returns (job, created)."""
        if kind not in self.handlers:
            raise ServiceError("UNKNOWN_JOB_KIND", f"No handler for job kind {kind}.", status=503,
                               next_action="This capability is not available in this build.")
        lane, _ = self.handlers[kind]
        with self.lock:
            if self.pending >= self.queue_limit:
                raise queue_full()
            job, created = self.catalog.create_job(project_id, kind, target_type, target_id, payload,
                                                   request_key=request_key,
                                                   external=kind in EXTERNAL_KINDS)
            if created:
                self.pending += 1
                self.queues[lane].put(job["id"])
        return job, created

    def _finish(self, job_id, status, **fields):
        try:
            self.catalog.update_job(job_id, status, **fields)
        except Exception:
            logging.getLogger("vestigraph.jobs").exception("terminal outcome not persisted for %s", job_id)
            with self.lock:
                self.deferred[job_id] = {"status": status, **fields}

    def observed_job(self, job):
        with self.lock:
            outcome = self.deferred.get(job["id"])
            return {**job, **outcome, "persistence_pending": True} if outcome else job

    def _flush_outcomes(self):
        with self.lock:
            items = list(self.deferred.items())
        for job_id, outcome in items:
            try:
                self.catalog.update_job(job_id, **outcome)
            except Exception:
                continue  # retained in memory, exposed to clients, retry next poll
            with self.lock:
                self.deferred.pop(job_id, None)
                self.idle.notify_all()

    def _worker(self, lane):
        while not self.stopping.is_set():
            self._flush_outcomes()
            try:
                job_id = self.queues[lane].get(timeout=1.0)
            except queue.Empty:
                continue
            if job_id is None:
                return
            try:
                self._run(job_id)
            except Exception:  # noqa: BLE001 - the lane must survive a catalog/disk failure
                logging.getLogger("vestigraph.jobs").exception("job %s could not be run", job_id)
                self._finish(job_id, "failed", error={
                    "code": "JOB_CRASHED", "message": "The task could not be started or recorded.",
                    "next_action": "Check the service log and disk before retrying."})
            finally:
                with self.lock:
                    self.pending -= 1
                    self.idle.notify_all()

    def _run(self, job_id):
        job = self.catalog.get_job(job_id)
        if job["status"] != "queued":
            return
        handler = self.handlers[job["kind"]][1]
        self.catalog.update_job(job_id, "running")
        try:
            result, asset_path = handler(job)
        except JobOutcomeUnknown as exc:
            # An external action (e.g. a navigation RPC) timed out: it may or may
            # not have happened. Never retry blindly; the client must check.
            self._finish(job_id, "unknown", error=exc.error.to_dict())
        except ServiceError as exc:
            self._finish(job_id, "failed", error=exc.to_dict())
        except Exception as exc:  # noqa: BLE001 - job failures must be recorded, not lost
            logging.getLogger("vestigraph.jobs").exception("job %s failed", job_id)
            self._finish(job_id, "failed", error={
                "code": "JOB_CRASHED", "message": "The task failed; see the local service log for details.",
                "next_action": "Check the service log using this job id before retrying."})
        else:
            self._finish(job_id, "succeeded", result=result, asset_path=asset_path, progress=1.0)

    def wait_idle(self, timeout=30.0):
        """Test/shutdown helper: block until no job is pending."""
        with self.idle:
            return self.idle.wait_for(lambda: self.pending == 0 and not self.deferred, timeout=timeout)


EXTERNAL_KINDS = {"open_in_klayout", "open_in_editor", "restore_in_editor"}


class JobOutcomeUnknown(Exception):
    """Raised by a handler when an external side effect cannot be confirmed."""

    def __init__(self, error: ServiceError):
        super().__init__(error.message)
        self.error = error
