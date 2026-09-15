"""Per-project capture coordinator: the single serial owner of one endpoint.

State machine (one coordinator per KLayout window): waiting_session -> waiting_document ->
baselining -> recording -> draining -> paused | reconnecting | blocked.
Everything that touches the recorder, the writer lease or navigation for a
project goes through this thread; HTTP handlers only enqueue requests.

Time is injectable (``clock``/``wait``) and the loop wakes on an Event, so
tests never sleep-and-hope.
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
from pathlib import Path

from .. import capture_runtime as capture
from vestigraph_backends.types import BackendError
from dataclasses import asdict
from ..store import Repository, RepositoryError
from .editors import public_session, document_identity
from .errors import ServiceError

_log = logging.getLogger("vestigraph.coordinator")
from .paths import temp_roots
from .state import is_within, now_iso

BACKOFF_S = (1, 2, 4, 8, 15)


class ClaimRegistry:
    """Which project currently owns which (window, document identity). Thread-safe, in-memory."""

    def __init__(self):
        self._lock = threading.Lock()
        self._owners = {}

    def claim(self, key, owner) -> bool:
        with self._lock:
            current = self._owners.get(key)
            if current is None or current == owner:
                self._owners[key] = owner
                return True
            return False

    def release(self, key, owner):
        with self._lock:
            if self._owners.get(key) == owner:
                del self._owners[key]

    def owner(self, key):
        with self._lock:
            return self._owners.get(key)
DOCUMENT_POLL_S = 2.0
SESSION_POLL_S = 3.0


class Coordinator:
    """One recorder for ONE KLayout window (session) of ONE project.

    The Supervisor creates a coordinator per (project, online session) and removes it when
    the window goes away; there is no session selection or binding step any more."""

    def __init__(self, project: dict, catalog, *, session, directory,
                 clock=time.monotonic, service_instance_id="", on_change=None,
                 document_poll_s=DOCUMENT_POLL_S, session_poll_s=SESSION_POLL_S,
                 exclusions=None, claims=None, durable_capture=True, spool_options=None):
        self.durable_capture = durable_capture
        self.blocked_queue_poll_s = 30.0   # a blocked durable queue is cleared by a person, not by retrying
        self.spool_options = spool_options
        self.session_descriptor = session
        self.session = public_session(session)  # Compatibility display envelope, never transport parameters.
        self.directory = directory
        self.backend = None
        self.owner = f"{project['id']}@{session.backend_id}:{session.session_instance_id}"
        self.claims = claims if claims is not None else ClaimRegistry()
        self.claim_key = None
        self.last_end_error = None
        self.project = dict(project)
        self.catalog = catalog
        self.clock = clock
        self.service_instance_id = service_instance_id
        self.on_change = on_change or (lambda: None)
        self.document_poll_s = document_poll_s
        self.session_poll_s = session_poll_s
        self.exclusions = [Path(p) for p in (exclusions or [])]
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.stopping = threading.Event()
        self.requests = queue.Queue()
        self.thread = None
        # Recorder ownership
        self.recorder = None           # thread running capture.observe
        self._stopping_recorder = False  # re-entrancy guard: requests handled during a drain
        self.run_stop = None
        self.commands = None
        self.repo = None               # writable Repository holding the lease while recording
        self.run = None                # catalog capture_run dict
        self.document = None           # catalog document dict
        self.candidate = self.session
        self.backoff_index = 0
        self.paused_by_navigation = False
        self.paused_local = False      # paused for this window only (project policy pause is global)
        self.state = {
            "state": "waiting_session", "reason": "starting", "since": now_iso(),
            "session_instance": public_session(session), "document_id": None,
            "capture_run_id": None, "tabs": [], "paused_local": False,
            "last_success_at": None, "last_checkpoint_id": None,
            "export_ms": None, "ingest_ms": None, "layout_bytes": None,
            "gap": None, "policy_version": self.project["policy_version"],
            "diagnostic": None,
        }

    # ------------------------------------------------------------- lifecycle --
    def start(self):
        self.thread = threading.Thread(target=self._loop, daemon=True,
                                       name=f"vestigraph-coordinator-{self.project['id'][:8]}-{self.session['session_id']}")
        self.thread.start()
        return self

    def stop(self, timeout=30.0):
        self.stopping.set()
        self.wake.set()
        if self.thread is not None:
            self.thread.join(timeout)

    def notify_policy(self, project: dict):
        with self.lock:
            self.project = dict(project)
        self.request("policy_changed")

    def request(self, kind, **payload):
        """Ask the coordinator to do something; returns a reply dict with an Event."""
        reply = {"event": threading.Event(), "ok": None}
        self.requests.put((kind, payload, reply))
        self.wake.set()
        return reply

    def wait_reply(self, reply, timeout):
        if not reply["event"].wait(timeout):
            raise ServiceError("COORDINATOR_TIMEOUT", "The recorder did not answer in time.", status=503,
                               retryable=True, next_action="Check that KLayout is responsive, then retry.")
        if not reply["ok"]:
            raise ServiceError(reply.get("code", "RECORDER_ERROR"), reply.get("error", "Recorder error."),
                               status=409, next_action=reply.get("next_action", "Check the recording status."),
                               details=reply.get("details"))
        return reply.get("result")

    # ----------------------------------------------------------------- state --
    def status(self) -> dict:
        with self.lock:
            out = dict(self.state)
            out["policy_version"] = self.project["policy_version"]
        return out

    def _set(self, state=None, reason=None, **fields):
        with self.lock:
            changed = state is not None and state != self.state["state"]
            if state is not None:
                self.state["state"] = state
            if reason is not None:
                self.state["reason"] = reason
            if changed:
                self.state["since"] = now_iso()
            self.state.update(fields)
        self.on_change()

    def writable_handle(self):
        with self.lock:
            return (self.document["id"], self.repo) if (self.repo is not None and self.document) else None

    # ------------------------------------------------------------------ loop --
    def _loop(self):
        try:
            while not self.stopping.is_set():
                self._drain_requests()
                if self.stopping.is_set():
                    break
                try:
                    delay = self._step()
                except Exception as exc:  # noqa: BLE001 - the loop must survive: a dead thread is a silent window
                    _log.exception("coordinator step failed for %s", self.session.get("session_id"))
                    delay = self._backoff("blocked", "coordinator_error",
                                          "Unexpected recorder error; inspect the local service log.")
                    try:
                        self._stop_recorder("interrupted", note=f"Coordinator error: {type(exc).__name__}: {exc}")
                    except Exception:  # noqa: BLE001
                        logging.getLogger(__name__).warning("Nonfatal callback or cleanup failure", exc_info=True)
                self.wake.wait(delay)
                self.wake.clear()
        finally:
            self._stop_recorder("closed", note=self.stop_note or "Service stopping.")
            if self.recorder is None:
                self._close_backend()
            self._set("disabled" if not self._policy().get("enabled", True) else "waiting_session",
                      self.stop_reason or "service_stopped")

    stop_note = None
    stop_reason = None

    def session_gone(self, timeout=30.0):
        """The window left the registry: drain and stop (called by the Supervisor)."""
        self.stop_note, self.stop_reason = "KLayout window closed.", "session_offline"
        self.stop(timeout)

    def _policy(self):
        with self.lock:
            return dict(self.project["policy"])

    def _drain_requests(self):
        while True:
            try:
                kind, payload, reply = self.requests.get_nowait()
            except queue.Empty:
                return
            try:
                result = self._handle(kind, payload)
                reply.update(ok=True, result=result)
            except ServiceError as exc:
                reply.update(ok=False, error=exc.message, code=exc.code, next_action=exc.next_action,
                             details=exc.details)
            except Exception as exc:  # noqa: BLE001
                reply.update(ok=False, error=f"{type(exc).__name__}: {exc}", code="RECORDER_ERROR")
            finally:
                reply["event"].set()

    def _handle(self, kind, payload):
        if kind == "policy_changed":
            policy = self._policy()
            if not policy.get("enabled", True) and self.recorder is not None:
                self._stop_recorder("closed", note="Recording disabled by policy.")
            return {"policy_version": self.project["policy_version"]}
        if kind == "pause":
            # scope "project": the Application already set the catalog flag; every window's
            # coordinator gets this request. scope "session": this window only.
            reason = payload.get("reason") or "user"
            self._stop_recorder("closed", note=f"Paused: {reason}")
            if payload.get("scope") == "session":
                self.paused_local = True
            with self.lock:
                self.project = self.catalog.get_project(self.project["id"])
            self._set("paused", f"paused:{reason}", paused_local=self.paused_local)
            return {"state": "paused", "reason": reason, "scope": payload.get("scope", "project")}
        if kind == "resume":
            self.paused_local = False
            with self.lock:
                self.project = self.catalog.get_project(self.project["id"])
            self.backoff_index = 0
            self._set("waiting_document", "resumed", paused_local=False)
            return {"state": "waiting_document"}
        if kind == "milestone":
            return self._milestone(payload["document_id"], payload["title"])
        if kind == "navigation_hold":
            # M4: an open-in-klayout job asks the recorder to drain first. If the tail could
            # not be saved (export failed, timed out, document lost), navigation must NOT proceed.
            if not self._stop_recorder("closed", note="Paused for history navigation."):
                raise ServiceError("TAIL_NOT_SAVED",
                                   "The latest changes could not be saved before opening the history version, "
                                   "so the tab was not opened.", status=409,
                                   next_action="Check KLayout and the recording status, then try again.",
                                   details={"recorder_error": self.last_end_error})
            self.paused_by_navigation = True
            self._set("paused", "history_navigation")
            return {"state": "paused"}
        if kind == "navigation_release":
            self.paused_by_navigation = False
            self._set("waiting_document", "navigation_released")
            return {"state": "waiting_document"}
        if kind == "cancel_save":
            return self.cancel_save()
        raise ServiceError("BAD_REQUEST", f"Unknown coordinator request {kind}.")

    def _step(self) -> float:
        """One scheduling decision; returns how long to wait before the next."""
        policy = self._policy()
        if not policy.get("enabled", True):
            self._stop_recorder("closed", note="Recording disabled by policy.")
            self._set("disabled", "policy_disabled")
            return 5.0
        if policy.get("paused") or self.paused_local or self.paused_by_navigation:
            if self.recorder is not None:
                self._stop_recorder("closed", note="Paused.")
            self._set("paused", self.state.get("reason") if self.state["state"] == "paused" else "paused")
            return 5.0
        if self.recorder is not None:
            if self.recorder.is_alive():
                return 1.0
            return self._recorder_ended()
        # No recorder: look at this window's current document.
        candidate = self.session
        try:
            if self.backend is None:
                self.backend = self.directory.create(self.session_descriptor)
                self.backend.connect(self.session_descriptor)
            probe, documents = capture.probe_document(self.backend)
            caps = self.backend.capabilities(probe.ref)
            if not caps.events.supported or not caps.events.available or not caps.export.available:
                raise BackendError("unsupported", outcome="not_started")
        except (capture.CaptureError, BackendError) as exc:
            code = capture.classify(exc)
            if code in (capture.NO_DOCUMENT, capture.DOCUMENT_AMBIGUOUS):
                reason = "document_ambiguous" if code == capture.DOCUMENT_AMBIGUOUS else "no_document"
                self._set("waiting_document", reason, tabs=[], diagnostic=None)
                return self.document_poll_s
            self._close_backend()
            if code == capture.DISCONNECTED:
                return self._backoff("reconnecting", "endpoint_unreachable", str(exc))
            self._set("blocked", f"endpoint_{code.lower()}", diagnostic={"error": str(exc)})
            return 10.0
        views = [{"filename": d.source_path, "source_uri": d.source_uri, "name": d.display_name,
                  "active_cell": d.view_name, "is_current": d.active, "document_ref": asdict(d.ref)}
                 for d in documents]
        verdict, why = self._eligible(probe, policy)
        if not verdict:
            self._set("waiting_document", why, tabs=views,
                      diagnostic={"filename": probe.source_path})
            return self.document_poll_s
        self._set(None, None, tabs=views, backend_capabilities=asdict(caps))
        delay = self._start_recorder(candidate, probe)
        return 0.5 if delay is None else delay

    def _close_backend(self):
        backend, self.backend = self.backend, None
        if backend is not None:
            try:
                backend.close()
            except BackendError:
                pass

    # ------------------------------------------------------------ documents --
    def _eligible(self, probe, policy: dict):
        filename = probe.source_path
        if not filename and probe.source_uri:
            return False, "native_document_binding_required"
        if filename is None or filename == "":
            return (True, "unsaved_document") if policy.get("allow_unsaved") else (False, "unsaved_document_not_allowed")
        path = Path(filename)
        try:
            resolved = path.resolve()
        except OSError:
            return False, "document_path_unresolvable"
        if is_within(resolved, Path(self.project["history_root"])):
            return False, "document_inside_history_root"
        for excluded in self.exclusions:
            if is_within(resolved, excluded):
                return False, "document_is_service_output"
        workspace = self.project.get("workspace")
        if workspace is None:
            # Catch-all project: any saved layout except temp folders and other projects' workspaces.
            for temp in temp_roots():
                if is_within(resolved, temp):
                    return False, "document_in_temp_folder"
            for other in self.foreign_workspaces():
                if is_within(resolved, other):
                    return False, "document_belongs_to_workspace_project"
            return True, "managed_document"
        if not is_within(resolved, Path(workspace)):
            return False, "document_outside_workspace"
        return True, "managed_document"

    def foreign_workspaces(self):
        """Workspaces of the other projects (a catch-all project must not claim their files)."""
        out = []
        for project in self.catalog.list_projects():
            if project["id"] != self.project["id"] and project.get("workspace"):
                out.append(Path(project["workspace"]))
        return out

    def _identity_for(self, probe, candidate) -> dict:
        return document_identity(probe, self.session_descriptor)

    # ------------------------------------------------------------- recorder --
    def _start_recorder(self, candidate: dict, probe: dict):
        identity = self._identity_for(probe, candidate)
        # One document has ONE recorder at a time across all projects and all windows: history
        # ownership is by document, not by port. A saved file open in two windows is recorded
        # from whichever window claimed it first; the other takes over when that one lets go.
        key = json.dumps(identity, sort_keys=True)
        if not probe.source_path:
            # Anonymous references are connection scoped. All project coordinators
            # still arbitrate one anonymous recorder per physical editor session.
            key = json.dumps({"backend": probe.ref.backend_id, "session": probe.ref.session_instance_id,
                              "anonymous": True}, sort_keys=True)
        owner = self.owner
        if not self.claims.claim(key, owner):
            holder = str(self.claims.owner(key) or "")
            other_project = holder.split("@")[0] != self.project["id"]
            self._set("waiting_document",
                      "document_claimed_by_other_project" if other_project else "document_open_in_other_window",
                      diagnostic={"owner": holder})
            return
        self.claim_key = key
        # From here until the recorder thread owns them, the claim and the writer lease are
        # released on EVERY failure: a claim left behind makes this document unrecordable by
        # any window for the life of the process, and a leaked lease keeps its history read-only.
        repo = None
        try:
            document = self.catalog.find_live_document(self.project["id"], identity)
            if document is None:
                name = probe.display_name
                document = self.catalog.add_live_document(self.project["id"], name, identity)
        except Exception as exc:  # noqa: BLE001 - catalog failure: release, report, retry later
            self.claims.release(key, owner)
            self.claim_key = None
            self._set("blocked", "catalog_error", diagnostic={"error": f"{type(exc).__name__}: {exc}"})
            return
        try:
            repo = Repository(document["store_path"], services=self.catalog.services)
            repo.acquire_writer(f"vestigraph-service run for {document['name']}")
            try:
                from ..baseline_duplicates import reconcile
                reconcile(repo)
            except Exception:
                # Presentation cleanup must not prevent recording an otherwise writable history.
                logging.getLogger(__name__).warning("Nonfatal callback or cleanup failure", exc_info=True)
            if self.durable_capture and getattr(repo, "format", 1) == 2:
                # Pre-flight: a retained copy at the head of the durable queue stops the organizer
                # before the first save. Starting a recorder anyway would fail at once, and doing
                # that every few seconds writes an interrupted run + gap each time, forever, while
                # the state still read "recording". Report it once here; nothing is started.
                from ..capture_spool import blocked_head, GiB
                import shutil
                options = self.spool_options or {}
                reserve = options.get("min_free_bytes", GiB) + options.get("max_file_bytes", 2 * GiB)
                if shutil.disk_usage(repo.root).free < reserve:
                    repo.release_writer()
                    self.claims.release(key, owner)
                    self.claim_key = None
                    self._set("blocked", "capture_capacity_low", document_id=document["id"],
                              diagnostic={"next_action": "Free disk capacity before recording can resume."})
                    return self.blocked_queue_poll_s
                head = blocked_head(repo)
                if head is not None:
                    repo.release_writer()
                    self.claims.release(key, owner)
                    self.claim_key = None
                    self._set("blocked", "capture_queue_blocked", document_id=document["id"],
                              diagnostic={"capture_id": head["id"], "error": head.get("error"),
                                          "next_action": "Retry the retained copy (POST /documents/{id}/capture/retry) "
                                                         "or discard it (POST /documents/{id}/capture/discard)."})
                    return self.blocked_queue_poll_s
        except Exception as exc:
            if repo is not None:
                repo.release_writer()
            self.claims.release(key, owner)          # nothing started: do not hold the document
            self.claim_key = None
            self._set("blocked", "history_locked_or_unreadable", document_id=document["id"],
                      diagnostic={"error": str(exc)})
            return
        try:
            run = self.catalog.start_run(self.project["id"], document["id"], public_session(self.session_descriptor),
                                         {"backend_id": probe.ref.backend_id, "consistency": "best_effort"})
        except Exception as exc:  # noqa: BLE001
            repo.release_writer()
            self.claims.release(key, owner)
            self.claim_key = None
            self._set("blocked", "catalog_error", document_id=document["id"],
                      diagnostic={"error": f"{type(exc).__name__}: {exc}"})
            return
        stop = threading.Event()
        cancel = threading.Event()
        commands = queue.Queue()
        intervals = self._policy().get("intervals", {})
        context = {"project_id": self.project["id"], "document_id": document["id"], "capture_run_id": run["id"],
                   "service_instance_id": self.service_instance_id}
        with self.lock:
            self.repo, self.run, self.document = repo, run, document
            self.run_stop, self.commands = stop, commands
            self.cancel_save_event = cancel
        self._set("baselining", "saving_starting_version", document_id=document["id"], capture_run_id=run["id"],
                  session_instance=public_session(self.session_descriptor), gap=None, diagnostic=None)
        outcome = {}

        def target():
            try:
                outcome["count"] = capture.observe(
                    repo, self.backend, self.session_descriptor, connected=True,
                    idle_seconds=intervals.get("idle_seconds", 5), min_interval=intervals.get("min_interval", 15),
                    max_interval=intervals.get("max_interval", 60), stop_event=stop,
                    capture_context=context,
                    on_status=self._on_status, commands=commands, cancel_event=cancel,
                    durable_capture=self.durable_capture, spool_options=self.spool_options,
                    expected_document=probe.ref)
            except BaseException as exc:  # noqa: BLE001 - reported through the state machine
                outcome["error"] = exc
            finally:
                self.wake.set()

        self.recorder = threading.Thread(target=target, daemon=True, name=f"vestigraph-recorder-{run['id'][:8]}")
        self.recorder_outcome = outcome
        self.recorder.start()

    def cancel_save(self, expected_document_id=None) -> dict:
        """Interrupt the automatic save in progress. Thread-safe and immediate: it must NOT
        go through the serial request queue, because that queue is exactly what a long
        named save or a drain is blocking. Honest about what cannot be cancelled."""
        with self.lock:
            state = self.state["state"]
            if expected_document_id is not None and self.state.get("document_id") != expected_document_id:
                return {"cancelled": False, "state": state, "note": "the window is no longer recording this document"}
            event = getattr(self, "cancel_save_event", None)
            repo = self.repo
            if event is None or repo is None:
                return {"cancelled": False, "state": state, "note": "no recording is running"}
            if getattr(repo, "format", 1) != 2:
                return {"cancelled": False, "state": state,
                        "note": "this history is format 1; its saves cannot be interrupted"}
            if state in ("baselining", "draining"):
                return {"cancelled": False, "state": state,
                        "note": "the starting version and the final save are safety gates and cannot be cancelled"}
            capture_queue = self.state.get("capture_queue")
            if capture_queue and (not capture_queue.get("processing") or self.state.get("capturing")):
                return {"cancelled": False, "state": state,
                        "note": "no cancellable organization is running; captured copies are retained"}
            if state != "saving":
                return {"cancelled": False, "state": state,
                        "note": "no save is in progress; a running KLink export cannot be interrupted"}
            event.set()
            return {"cancelled": True, "state": "saving",
                    "note": "the storage part stops at the next batch; a running KLink export finishes first"}

    def _on_status(self, report: dict):
        with self.lock:
            self._apply_status(report)

    def _apply_status(self, report: dict):
        phase = report.get("phase")
        if "capture_queue" in report:
            self._set(None, None, capture_queue=report["capture_queue"])
        if phase in ("organization_blocked", "capture_full"):
            self._set("blocked", "capture_queue_blocked" if phase == "organization_blocked" else "capture_capacity_low",
                      organizing_progress=None, diagnostic={"error": report.get("error"),
                      "next_action": "Resolve the retained copy or free disk capacity before resuming."})
            return
        if self.state["state"] == "blocked" and phase in ("capture_finished", "captured", "organizing"):
            return
        if phase in ("capturing", "capture_finished", "captured", "capture_full", "organizing",
                     "organized", "organization_cancelled", "organization_blocked"):
            fields = {}
            if "capturing" in report:
                fields["capturing"] = report["capturing"]
            if phase == "organizing":
                fields["organizing_progress"] = {"stage": report.get("stage"), "fraction": report.get("fraction"),
                                                 "bytes": report.get("bytes"), "total": report.get("total")}
            elif phase in ("organized", "organization_cancelled", "organization_blocked"):
                fields["organizing_progress"] = None
            if phase == "captured":
                fields["pending_since"] = None
                for key in ("export_ms", "layout_bytes"):
                    if key in report:
                        fields[key] = report[key]
            if phase == "organized":
                fields.update(last_success_at=now_iso(), last_checkpoint_id=report.get("checkpoint_id"),
                              ingest_ms=report.get("ingest_ms"), timing_ms=report.get("timing_ms"),
                              scan=report.get("scan"))
            self._set(None, None, **fields)
            if self.state["state"] not in ("baselining", "draining"):
                busy = self.state.get("capturing") or (self.state.get("capture_queue") or {}).get("processing")
                self._set("saving" if busy else "recording", "recording")
            return
        if phase == "baselining":
            self._set("baselining", "saving_starting_version")
        elif phase == "recording":
            self.last_end_error = None
            self._set("recording", "recording")
        elif phase == "draining":
            self._set("draining", "saving_tail")
        elif phase == "pending":
            if self.state["state"] in ("recording", "saving"):
                self._set(None, "changes_pending", pending_since=report.get("pending_since"))
        elif phase == "saving":
            stage = report.get("stage") or "scanning"
            if self.state["state"] not in ("baselining", "draining"):
                self._set("saving", f"saving_{stage}")
            self._set(None, None, save_progress={"stage": stage, "fraction": report.get("fraction"),
                                                 "bytes": report.get("bytes"), "total": report.get("total")})
        elif phase == "cancelled":
            self._set("recording", "save_cancelled", save_progress=None, pending_since=None)
        elif phase == "coalesced":
            self._set(None, None, events_coalesced=(self.state.get("events_coalesced") or 0) + int(report.get("count") or 0))
        elif phase == "unchanged":
            if self.state["state"] == "saving":
                self._set("recording", "recording", save_progress=None, pending_since=None)
        elif phase == "saved":
            self._set(None, None, last_success_at=now_iso(), last_checkpoint_id=report.get("checkpoint_id"),
                      export_ms=report.get("export_ms"), ingest_ms=report.get("ingest_ms"),
                      layout_bytes=report.get("layout_bytes"), timing_ms=report.get("timing_ms"),
                      scan=report.get("scan"), save_progress=None, pending_since=None)
            if self.state["state"] in ("baselining", "saving"):
                self._set("recording", "recording")

    def _stop_recorder(self, status, note=None, timeout=180.0) -> bool:
        """Drain the recorder; True only if it ended cleanly (tail saved or nothing to save)."""
        if self.recorder is None:
            return self.last_end_error is None  # a previous failed tail is not repaired by thread exit
        if self._stopping_recorder:
            # A request handled DURING the drain asked to stop again: the recorder is already
            # being stopped; the outer call reports the outcome. Never wait twice.
            return False
        self._stopping_recorder = True
        try:
            self._set("draining", "saving_tail")
            self.run_stop.set()
            self.wake.set()
            deadline = time.monotonic() + timeout
            while self.recorder.is_alive() and time.monotonic() < deadline:
                # Wait in short slices and keep answering the control queue in between: a tail
                # save (or a hung editor RPC) must not freeze status/pause/cancel for this window
                # for the whole timeout.
                self.recorder.join(1.0)
                if self.recorder.is_alive():
                    self._drain_requests()
            if self.recorder.is_alive():
                # Cannot preempt an RPC/export. Leave the run open; the next step reports it.
                self._set("blocked", "recorder_unresponsive", diagnostic={"note": "stop requested; export still running"})
                self.last_end_error = "RECORDER_UNRESPONSIVE"
                return False
            self._recorder_ended(final_status=status, note=note)
            return self.last_end_error is None
        finally:
            self._stopping_recorder = False

    def _recorder_ended(self, final_status=None, note=None) -> float:
        outcome = getattr(self, "recorder_outcome", {}) or {}
        error = outcome.get("error")
        run, repo, document = self.run, self.repo, self.document
        with self.lock:
            self.recorder = None
            self.recorder_outcome = None
            self.repo = self.run = self.document = None
            self.run_stop = self.commands = None
            self.cancel_save_event = None
            # Clear routing identity atomically with the released handle, even if
            # end_run/backoff below raises or follows a disconnected path.
            self.state["document_id"] = None
            self.state["capture_run_id"] = None
            self.state["capture_queue"] = None
            self.state["capturing"] = False
            self.state["organizing_progress"] = None
        if repo is not None:
            repo.release_writer()
        self._close_backend()
        if self.claim_key is not None:
            self.claims.release(self.claim_key, self.owner)
            self.claim_key = None
        self.last_end_error = (capture.classify(error) if error is not None
                               and getattr(error, "tail_unsaved", True) else None)
        if error is None:
            if run is not None:
                self.catalog.end_run(run["id"], final_status or "closed", note=note or "Recorder stopped.")
            self.backoff_index = 0
            self._set("waiting_document", "recorder_stopped", document_id=None, capture_run_id=None)
            return 0.1
        code = capture.classify(error)
        reason = str(error)[:500]
        if run is not None:
            self.catalog.end_run(run["id"], "interrupted", note=f"{code}: {reason}",
                                 coverage={"gap": code, "tail_confirmed": False})
        gap = {"code": code, "reason": reason, "at": now_iso(), "capture_run_id": run["id"] if run else None}
        if code in (capture.DOCUMENT_CHANGED, capture.DOCUMENT_AMBIGUOUS):
            # Not a failure of the service: the user moved on or opened a copy. Re-evaluate.
            self.backoff_index = 0
            reason = "document_changed" if code == capture.DOCUMENT_CHANGED else "document_ambiguous"
            self._set("waiting_document", reason, gap=gap, document_id=None, capture_run_id=None)
            return 0.1
        if code == capture.DISCONNECTED:
            return self._backoff("reconnecting", "connection_lost", reason, gap=gap)
        if code in (capture.OVERFLOW, capture.NO_BASELINE, capture.EXPORT_FAILED, capture.INVALID_EVENT):
            self._set("reconnecting", f"restart_after_{code.lower()}", gap=gap, document_id=None, capture_run_id=None)
            return self._backoff_delay()
        if code in ("CAPTURE_QUEUE_BLOCKED", "CAPTURE_SPOOL_FULL"):
            # Only a person can clear this (retry/discard the retained copy): poll slowly, and
            # the pre-flight in _start_recorder keeps the next attempts from opening new runs.
            self._set("blocked", "capture_queue_blocked", gap=gap, document_id=None, capture_run_id=None,
                      diagnostic={"error": reason,
                                  "next_action": "Retry or discard the retained copy "
                                                 "(POST /documents/{id}/capture/retry or /capture/discard)."})
            return self.blocked_queue_poll_s
        self._set("blocked", f"recorder_{code.lower()}", gap=gap, document_id=None, capture_run_id=None,
                  diagnostic={"error": reason})
        return 10.0

    def _backoff(self, state, reason, detail, gap=None) -> float:
        delay = self._backoff_delay()
        fields = {"diagnostic": {"error": detail, "retry_in_s": delay}, "capture_run_id": None}
        if gap is not None:
            fields["gap"] = gap
        self._set(state, reason, **fields)
        return delay

    def _backoff_delay(self) -> float:
        delay = BACKOFF_S[min(self.backoff_index, len(BACKOFF_S) - 1)]
        self.backoff_index += 1
        return float(delay)

    # ------------------------------------------------------------ milestone --
    def _milestone(self, document_id, title):
        with self.lock:
            active = self.document["id"] if self.document else None
            commands = self.commands
            recording = self.recorder is not None and self.recorder.is_alive()
        if not recording or active != document_id:
            raise ServiceError("NOT_RECORDING_DOCUMENT",
                               "This document is not being recorded right now.", status=409,
                               next_action="Open it as the current KLayout document and wait for recording, "
                                           "or use the CLI checkpoint command on a saved file.")
        reply = {"event": threading.Event()}
        commands.put(("snapshot", title, reply))
        self.wake.set()
        if not reply["event"].wait(180):
            raise ServiceError("COORDINATOR_TIMEOUT", "The named save did not finish in time.", status=503,
                               retryable=True, next_action="Check that KLayout is responsive, then retry.")
        if not reply.get("ok"):
            raise ServiceError(reply.get("code", "EXPORT_FAILED"), reply.get("error", "Named save failed."),
                               status=409, next_action="Check the recording status and retry.")
        record = reply["record"]
        return {"checkpoint_id": record["id"], "title": record["title"], "created_at": record["created_at"]}
