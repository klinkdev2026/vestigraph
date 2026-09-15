"""Application facade: the one entry point every adapter (web, CLI, future MCP) uses.

Methods take/return plain dicts, raise ServiceError, and enforce scope
(project -> document -> checkpoint) themselves, so no adapter can reach data
by guessing ids. Heavy work is submitted to the JobRunner, never done inline.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import time
import threading
import sqlite3
from pathlib import Path

from ..preview import RENDERER_VERSION
from ..preview.budgets import DEFAULT as DEFAULT_BUDGETS
from ..preview.worker import run_diff, run_preview
from ..storage import summaries as storage_summaries
from ..storage import adaptive as compression_adaptive
from ..store import CursorError, RepositoryError
from ..presentation import Presentation, PresentationError, LabelConflict
from . import cursors, queries
from .catalog import Catalog, fingerprint
from .errors import ServiceError, bad_request, forbidden, not_found, unavailable
from .jobs import JobOutcomeUnknown, JobRunner
from .state import ServiceState, is_within
from .supervisor import Supervisor, _PRECEDENCE
from .editors import public_session, session_id as editor_session_id
from vestigraph_backends.types import BackendError, OpenRequest
from ..vesti_formats.registry import filename_suffix
from dataclasses import asdict

ASSET_TTL_S = 3600.0
PREVIEW_TTL_S = 7 * 24 * 3600.0
ASSET_BUDGET_BYTES = 2 * 1024 ** 3      # whole cache folder; oldest entries go first once exceeded
SWEEP_INTERVAL_S = 3600.0


class Application:
    def __init__(self, state: ServiceState, catalog: Catalog, runner: JobRunner, supervisor: Supervisor, *, skill_services=None):
        self.state, self.catalog, self.runner, self.supervisor = state, catalog, runner, supervisor
        self.services = catalog.services
        self.experimental_skills = os.environ.get("VESTIGRAPH_EXPERIMENTAL_SKILLS") == "1" or skill_services is not None
        self._presentation_lock = threading.RLock()
        from .legacy_imports import LegacyImports
        self.legacy_imports = LegacyImports(self)
        from .thumbnails import SavedThumbnails
        self.saved_thumbnails = SavedThumbnails(self)
        from .skills import Skills
        self.skills = Skills(self, services=skill_services)
        self.started_at = time.time()
        self.budgets = DEFAULT_BUDGETS
        runner.register("download", "heavy", self._job_download)
        runner.register("pause", "coordinate", self._job_pause)
        runner.register("resume", "coordinate", self._job_resume)
        runner.register("milestone", "coordinate", self._job_milestone)
        runner.register("preview", "preview", self._job_preview)
        runner.register("diff", "preview", self._job_diff)
        runner.register("open_in_klayout", "coordinate", self._job_open_in_klayout)
        runner.register("open_in_editor", "coordinate", self._job_open_in_klayout)
        runner.register("relocate", "coordinate", self._job_relocate)

    @classmethod
    def open(cls, state_dir, *, acquire_instance=True, registry=None, client_factory=None,
             autocapture=None, storage_delta=None, coordinator_options=None, backend_registry=None, services=None, skill_services=None):
        """registry/client_factory/coordinator_options/storage_delta exist for tests (fake KLayout,
        forcing the storage_delta capability off without uninstalling the real encoder)."""
        state = ServiceState.open(state_dir)
        if acquire_instance:
            state.acquire_instance()
        catalog = Catalog(state, services=services)
        supervisor = Supervisor(catalog, registry=registry, client_factory=client_factory,
                                autocapture=autocapture, storage_delta=storage_delta,
                                coordinator_options=coordinator_options, backend_registry=backend_registry)
        runner = JobRunner(catalog, lanes={"preview": 1, "heavy": 2, "coordinate": 1})
        return cls(state, catalog, runner, supervisor, skill_services=skill_services)

    def start(self):
        self.runner.start()
        self.supervisor.start()
        return self

    def stop(self):
        self.supervisor.stop()
        self.runner.stop()
        self.state.release_instance()

    # ----------------------------------------------------------------- status --
    def capabilities(self) -> dict:
        return dict(self.supervisor.capabilities, editor_backends=True, open_in_editor=True,
                    formats=self.services.formats.describe(), backend_errors=dict(self.supervisor.editors.errors),
                    adaptive_compression=True, gpu_compression=False,
                    capture_screenshots=True, thumbnail_status=True, checkpoint_rename=True, recorded_changes=True, legacy_import=True, generated_thumbnails=True, skill_library=self.experimental_skills)

    def status(self) -> dict:
        projects = []
        for project in self.catalog.list_projects():
            status = self.supervisor.project_status(project["id"])
            projects.append({"id": project["id"], "name": project["name"],
                             "capture_state": status["state"], "reason": status.get("reason"),
                             "document_id": status.get("document_id"),
                             "last_success_at": status.get("last_success_at"),
                             "sessions": [self._session_summary(st) for st in status.get("sessions", [])]})
        return {
            "service_instance_id": self.state.instance_id,
            "state_version": self.supervisor.version,
            "uptime_s": round(time.time() - self.started_at, 1),
            "capabilities": self.capabilities(),
            "compression": compression_adaptive.status(),
            "projects": projects,
        }

    def sessions(self) -> list:
        sessions = self.supervisor.editors.refresh()
        if not sessions and not self.supervisor.capabilities["autocapture_dependency"]:
            raise unavailable("autocapture", "The configured editor integration is unavailable.",
                              "Install or enable its adapter, then restart the service.")
        out = []
        for session in sessions:
            session_id = editor_session_id(session)
            out.append({
                **public_session(session),
                "recording": [self._session_summary(c.status(), project_id=c.project["id"])
                              for c in self.supervisor.coordinators_on(session_id)],
            })
        return out

    def _session_summary(self, status: dict, project_id=None) -> dict:
        """What the panel shows for one window: state + the layouts open in it."""
        instance = status.get("session_instance") or {}
        documents = []
        for tab in status.get("tabs") or []:
            document = None
            if tab.get("filename") and project_id is not None:
                try:
                    document = self.catalog.find_live_document(
                        project_id, {"kind": "saved", "path": str(Path(tab["filename"]).resolve())})
                except OSError:
                    document = None
            documents.append({"filename": tab.get("filename"), "active_cell": tab.get("active_cell"),
                              "is_current": bool(tab.get("is_current")),
                              "document_id": document["id"] if document else None,
                              "recording": bool(document) and document["id"] == status.get("document_id")})
        return {"session_id": instance.get("session_id"), "port": instance.get("port"), "pid": instance.get("pid"),
                "backend_id": instance.get("backend_id"), "display_name": instance.get("display_name"),
                "session_instance_id": instance.get("session_instance_id"),
                "backend_capabilities": status.get("backend_capabilities"),
                "project_id": project_id, "state": status.get("state"), "reason": status.get("reason"),
                "since": status.get("since"), "document_id": status.get("document_id"),
                "paused_local": bool(status.get("paused_local")), "last_success_at": status.get("last_success_at"),
                "last_checkpoint_id": status.get("last_checkpoint_id"), "gap": status.get("gap"),
                "diagnostic": status.get("diagnostic"), "documents": documents}

    # --------------------------------------------------------------- projects --
    def projects(self) -> list:
        return [self._project_summary(p) for p in self.catalog.list_projects()]

    def _project_summary(self, project):
        status = self.supervisor.project_status(project["id"])
        return {"id": project["id"], "name": project["name"], "created_at": project["created_at"],
                "policy": project["policy"], "policy_version": project["policy_version"],
                "catch_all": project.get("workspace") is None,
                "capture_state": status["state"], "reason": status.get("reason")}

    def project(self, project_id) -> dict:
        return self._project_summary(self.catalog.get_project(project_id))

    def project_status(self, project_id) -> dict:
        project = self.catalog.get_project(project_id)
        status = self.supervisor.project_status(project_id)
        status["sessions"] = [self._session_summary(st, project_id=project_id) for st in status.get("sessions", [])]
        status["policy_version"] = project["policy_version"]
        status["policy"] = project["policy"]
        status["history_root"] = project["history_root"]      # loopback-only UI; shown so the user can change it
        status["catch_all"] = project.get("workspace") is None
        return status

    def set_policy(self, project_id, updates, expected_version=None) -> dict:
        if _contains_path(updates):
            raise bad_request("Policy must not contain file paths; paths are registered by the local CLI.")
        project = self.catalog.set_policy(project_id, updates, expected_version)
        self.supervisor.on_policy_changed(project)
        return self._project_summary(project)

    def _require_autocapture(self, project_id):
        if not self.supervisor.capabilities["autocapture"]:
            status = self.supervisor.project_status(project_id)
            raise unavailable("autocapture", "Automatic recording is not available for this project.",
                              (status.get("diagnostic") or {}).get("next_action")
                              or "Install klayout-klink into the service's Python and restart.",
                              details={"reason": status.get("reason")})

    def _coordinators(self, project_id) -> list:
        self._require_autocapture(project_id)
        return self.supervisor.coordinators(project_id)

    def _coordinator_for_session(self, project_id, session_id):
        self._require_autocapture(project_id)
        coordinator = self.supervisor.coordinator(project_id, session_id)
        if coordinator is None:
            raise ServiceError("SESSION_OFFLINE", "That KLayout window is not online.", status=409,
                               next_action="Pick a window from the list; it must be running with klink.")
        return coordinator

    def _coordinator_for_document(self, document):
        self._require_autocapture(document["project_id"])
        matches = []
        for coordinator in self.supervisor.coordinators(document["project_id"]):
            status = coordinator.status()
            if status.get("document_id") == document["id"]:
                matches.append((status, coordinator))
        if matches:
            return min(matches, key=lambda item: _state_rank(item[0].get("state")))[1]
        raise ServiceError("NOT_RECORDING_DOCUMENT", "This document is not being recorded right now.", status=409,
                           next_action="Open it as the current document in a KLayout window and wait for recording, "
                                       "or use the CLI checkpoint command on a saved file.")

    def pause(self, project_id, reason=None, request_key=None, session_id=None):
        self.catalog.get_project(project_id)
        if session_id is not None:
            self._coordinator_for_session(project_id, session_id)
        else:
            self._require_autocapture(project_id)
        job, _ = self.runner.submit(project_id, "pause", "project", project_id,
                                    {"reason": (reason or "user")[:200], "session_id": session_id},
                                    request_key=request_key)
        return job

    def resume(self, project_id, request_key=None, session_id=None):
        self.catalog.get_project(project_id)
        if session_id is not None:
            self._coordinator_for_session(project_id, session_id)
        else:
            self._require_autocapture(project_id)
        job, _ = self.runner.submit(project_id, "resume", "project", project_id, {"session_id": session_id},
                                    request_key=request_key)
        return job

    def milestone(self, document_id, title, request_key=None):
        document = self.catalog.get_document(document_id)
        if document["read_only"] or document["origin"] != "live":
            raise ServiceError("DOCUMENT_READ_ONLY", "This history is read-only; named saves need a live document.",
                               status=409, next_action="Open the layout in KLayout inside the managed workspace; "
                                                       "the service records it as a live document.")
        self._require_autocapture(document["project_id"])    # "not recording" is reported by the job
        job, _ = self.runner.submit(document["project_id"], "milestone", "document", document_id,
                                    {"title": title}, request_key=request_key)
        return job

    def cancel_save(self, document_id):
        """Ask the recorder of this document to abandon the save in progress (synchronous).
        Only the storage part is interruptible; the KLink export RPC runs to completion."""
        document = self.catalog.get_document(document_id)
        if document["read_only"] or document["origin"] != "live":
            raise ServiceError("DOCUMENT_READ_ONLY", "This history is read-only; nothing is being saved for it.",
                               status=409, next_action="Only live documents being recorded have saves to cancel.")
        coordinator = self._coordinator_for_document(document)
        return coordinator.cancel_save(expected_document_id=document_id)  # recheck after window selection

    def capture_queue(self, document_id):
        """Metadata-only access; never reads a GDS or a layout object."""
        document, store = self._store(document_id)
        from ..capture_spool import pending_stats
        status = pending_stats(store)
        items = []
        if status["enabled"]:
            with store._connect() as db:
                items = [dict(r) for r in db.execute(
                    "SELECT id,state,size,checkpoint_id,error,created_at,sha256 IS NOT NULL AS accepted FROM capture_queue "
                    "WHERE state NOT IN ('committed','unchanged') ORDER BY ordinal LIMIT 20")]
        return {"document_id": document["id"], "status": status, "items": items}

    def discard_capture(self, document_id, capture_id=None):
        """Drop a retained copy (blocked or quarantined) so the queue can move on.

        Without ``capture_id`` the first blocked head is discarded. The copy's bytes are
        removed and its reservation released; the queue row remains as a terminal record."""
        from ..store import Repository
        from ..capture_spool import Spool, SpoolError
        document = self.catalog.get_document(document_id)
        if document["read_only"] or document["origin"] != "live":
            raise ServiceError("DOCUMENT_READ_ONLY", "Only live histories hold captured copies.", status=409)
        handle = self.supervisor.writable_handles().get(document_id)
        repo = handle or Repository(document["store_path"], services=self.services)
        # The spool only writes under the history writer lease: reuse the recorder's handle when
        # it is recording, otherwise hold our own for the duration of this call.
        own_lease = not getattr(repo, "writer_held", False)
        try:
            if own_lease:
                repo.acquire_writer("vestigraph-service discard capture")
        except RepositoryError as exc:
            raise ServiceError("HISTORY_BUSY", str(exc), status=409, retryable=True) from exc
        try:
            with repo._connect() as db:
                if not db.execute("SELECT 1 FROM sqlite_master WHERE name='capture_queue' AND type='table'").fetchone():
                    raise ServiceError("NO_RETAINED_CAPTURE", "There is no retained capture to discard.", status=409)
                if capture_id is None:
                    row = db.execute("SELECT id,state FROM capture_queue "
                                     "WHERE state NOT IN ('committed','unchanged','quarantined') ORDER BY ordinal LIMIT 1").fetchone()
                    if row is None or row["state"] != "blocked":
                        raise ServiceError("NO_RETAINED_CAPTURE", "No blocked copy is at the head of the queue.",
                                           status=409, next_action="Pass capture_id to discard a quarantined copy.")
                    capture_id = row["id"]
            try:
                item = Spool(repo).discard(capture_id)
            except SpoolError as exc:
                raise ServiceError("CAPTURE_NOT_DISCARDABLE", str(exc), status=409) from exc
        finally:
            if own_lease:
                repo.release_writer()
        self.supervisor.bump()
        return {"document_id": document_id, "capture_id": capture_id, "state": item["state"], "error": item["error"]}

    def retry_capture(self, document_id):
        """Retry the first ACCEPTED blocked copy; incomplete exports need inspection."""
        from ..store import Repository
        document = self.catalog.get_document(document_id)
        if document["read_only"] or document["origin"] != "live":
            raise ServiceError("DOCUMENT_READ_ONLY", "Only live histories can organize captured copies.", status=409)
        handle = self.supervisor.writable_handles().get(document_id)
        repo = handle or Repository(document["store_path"], services=self.services)
        try:
            with repo._writer(), repo._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                if not db.execute("SELECT 1 FROM sqlite_master WHERE name='capture_queue' AND type='table'").fetchone():
                    raise ServiceError("NO_RETAINED_CAPTURE", "There is no retained capture to retry.", status=409)
                row = db.execute("SELECT id,state,sha256 FROM capture_queue "
                                 "WHERE state NOT IN ('committed','unchanged','quarantined') ORDER BY ordinal LIMIT 1").fetchone()
                if row is None or row["state"] != "blocked" or not row["sha256"]:
                    raise ServiceError("CAPTURE_NOT_RETRYABLE",
                                       "Only a completed, accepted copy can be retried. Incomplete exports are retained for inspection.",
                                       status=409)
                db.execute("UPDATE capture_queue SET state='ready',error=NULL WHERE id=? AND state='blocked'", (row["id"],))
                capture_id = row["id"]
        except RepositoryError as exc:
            raise ServiceError("HISTORY_BUSY", str(exc), status=409, retryable=True) from exc
        self.supervisor.bump()
        return {"capture_id": capture_id, "state": "ready"}

    def _job_pause(self, job):
        reason, session_id = job["payload"].get("reason"), job["payload"].get("session_id")
        if session_id is not None:
            coordinator = self._coordinator_for_session(job["project_id"], session_id)
            return coordinator.wait_reply(coordinator.request("pause", reason=reason, scope="session"), 240), None
        self.catalog.set_paused(job["project_id"], True, reason or "user")
        results = []
        for coordinator in self._coordinators(job["project_id"]):
            results.append(coordinator.wait_reply(coordinator.request("pause", reason=reason, scope="project"), 240))
        self.supervisor.bump()
        return {"state": "paused", "reason": reason or "user"}, None

    def _job_resume(self, job):
        session_id = job["payload"].get("session_id")
        if session_id is not None:
            coordinator = self._coordinator_for_session(job["project_id"], session_id)
            return coordinator.wait_reply(coordinator.request("resume"), 60), None
        self.catalog.set_paused(job["project_id"], False, None)
        results = []
        for coordinator in self._coordinators(job["project_id"]):
            results.append(coordinator.wait_reply(coordinator.request("resume"), 60))
        self.supervisor.bump()
        return {"state": "waiting_document"}, None

    def _job_milestone(self, job):
        document = self.catalog.get_document(job["target_id"])
        coordinator = self._coordinator_for_document(document)
        result = coordinator.wait_reply(
            coordinator.request("milestone", document_id=job["target_id"], title=job["payload"]["title"]), 240)
        return result, None

    # -------------------------------------------------------------- documents --
    def documents(self, project_id) -> list:
        return [self._document_summary(d) for d in self.catalog.list_documents(project_id)]

    def _document_summary(self, document):
        capture_queue = None
        try:
            store = queries.open_store(document, self.supervisor.writable_handles(), services=self.services)
            counts = queries.counts(store)
            from ..capture_spool import pending_stats
            capture_queue = pending_stats(store)
            health = "ok"
        except ServiceError as exc:
            counts, health = {}, exc.code
        return {
            "id": document["id"], "project_id": document["project_id"], "name": document["name"],
            "origin": document["origin"], "read_only": document["read_only"],
            "recording_allowed": document["recording_allowed"],
            "classification": document.get("classification"),
            "capture_state": self.supervisor.document_state(document),
            "capture_queue": capture_queue,
            "last_checkpoint_id": counts.get("last_checkpoint_id"),
            "last_checkpoint_at": counts.get("last_checkpoint_at"),
            "checkpoint_count": counts.get("checkpoints"), "segment_count": counts.get("segments"),
            "event_count": counts.get("events"), "logical_bytes": counts.get("logical_bytes"),
            "health": health, "created_at": document["created_at"],
            "coverage": "imported_history" if document["origin"] == "imported_history" else "live",
        }

    def document(self, document_id, project_id=None) -> dict:
        return self._document_summary(self.catalog.get_document(document_id, project_id))

    def _store(self, document_id, project_id=None):
        document = self.catalog.get_document(document_id, project_id)
        return document, queries.open_store(document, self.supervisor.writable_handles(), services=self.services)

    # ---------------------------------------------------------------- history --
    def checkpoints(self, document_id, cursor=None, limit=None, segment_id=None):
        document, store = self._store(document_id)
        result = queries.list_checkpoints(store, document, cursor, limit, segment_id)
        Presentation(store.root).decorate(result["items"])
        return result

    def segments(self, document_id, cursor=None, limit=None):
        document, store = self._store(document_id)
        return queries.list_segments(store, document, cursor, limit)

    def events(self, document_id, cursor=None, limit=None, segment_id=None):
        document, store = self._store(document_id)
        return queries.list_events(store, document, cursor, limit, segment_id)

    def event(self, document_id, seq):
        _, store = self._store(document_id)
        return queries.get_event(store, seq)

    def checkpoint(self, document_id, checkpoint_id, with_manifest=False):
        _, store = self._store(document_id)
        record = queries.get_checkpoint(store, checkpoint_id, with_manifest=with_manifest)
        return Presentation(store.root).decorate([record])[0]

    def rename_checkpoint(self, document_id, checkpoint_id, title, expected_revision,
                          actor="user", request_key=None):
        with self._presentation_lock:
            return self._rename_checkpoint(document_id, checkpoint_id, title, expected_revision, actor, request_key)

    def _rename_checkpoint(self, document_id, checkpoint_id, title, expected_revision, actor, request_key):
        document, store = self._store(document_id)
        queries.get_checkpoint(store, checkpoint_id)
        if document["read_only"]:
            raise forbidden("This history is read-only.")
        try:
            result = Presentation(store.root).rename(checkpoint_id, title, expected_revision,
                                                     actor=actor, request_key=request_key)
        except LabelConflict as exc:
            raise ServiceError("TITLE_CONFLICT", str(exc), status=409) from exc
        except PresentationError as exc:
            raise bad_request(str(exc)) from exc
        except (sqlite3.Error, OSError) as exc:
            raise ServiceError("PRESENTATION_UNAVAILABLE", "Cannot save the name; layout history was not changed.",
                               status=503) from exc
        return {"checkpoint_id": checkpoint_id, "title": result["title"],
                "title_revision": result["revision"], "actor": result["actor"]}

    def thumbnail_status(self, document_id, checkpoint_id):
        _, store = self._store(document_id)
        record = queries.get_checkpoint(store, checkpoint_id)
        try:
            status = Presentation(store.root).thumbnail_status((record.get("metadata") or {}).get("capture_id"))
            if status["status"] != "stored" and self.saved_thumbnails.get(record):
                return {"status": "stored", "reason_code": None, "updated_at": None, "source": "saved_layout_render"}
            return status
        except (PresentationError, ValueError, KeyError, TypeError, sqlite3.Error, OSError) as exc:
            raise ServiceError("THUMBNAIL_UNREADABLE", "Screenshot status cannot be read.", status=503) from exc

    def thumbnail(self, document_id, checkpoint_id):
        _, store = self._store(document_id)
        record = queries.get_checkpoint(store, checkpoint_id)
        try:
            item = Presentation(store.root).thumbnail((record.get("metadata") or {}).get("capture_id"))
        except (PresentationError, ValueError, KeyError, TypeError, sqlite3.Error, OSError) as exc:
            raise ServiceError("THUMBNAIL_UNREADABLE", "The saved screenshot cannot be read.", status=503) from exc
        if item is None:
            try:
                item = self.saved_thumbnails.get(record)
            except (OSError, ValueError, PresentationError) as exc:
                raise ServiceError("THUMBNAIL_UNREADABLE", "The saved-layout image cannot be read.", status=503) from exc
        if item is None:
            return None
        png, info = item
        if info.get("raw_sha256") != record["sha256"]:
            raise ServiceError("THUMBNAIL_UNREADABLE", "Screenshot belongs to a different export.", status=503)
        return png, info

    def changes(self, document_id, checkpoint_id, limit=None, cursor=None, kind=None):
        """Recorded ChangeSet v1 entries for one version (docs/CHANGESET_V1.md §5).

        ``unavailable`` (format-1 history, no change record) is a normal result, not an
        error. The store clamps ``limit`` to its own [1, 200] range; an invalid/foreign
        cursor is the only way this raises (mapped to 400, not 503 -- it is a caller
        mistake, not a broken history)."""
        _, store = self._store(document_id)
        queries.get_checkpoint(store, checkpoint_id)   # scope check: must exist in THIS document
        try:
            return store.changes(checkpoint_id, limit=cursors.DEFAULT_LIMIT if limit is None else limit,
                                 cursor=cursor, kind=kind)
        except CursorError as exc:
            raise bad_request(f"Cannot read change record: {exc}",
                              "Reload this version and retry without a stale cursor.") from exc
        except RepositoryError as exc:
            raise ServiceError("CHANGES_UNREADABLE", f"The recorded changes of this version cannot be read: {exc}",
                               next_action="The history may be damaged; restore from a complete backup.") from exc

    def change_summaries(self, document_id, checkpoint_ids, *, _evidence=False):
        """Batch ChangeSet-root summaries for up to 100 versions in one snapshot
        (docs/SPEC_PERFORMANCE_READ_ACCESS.md §4.4): an agent reads this first, then
        decides which ids are worth a full ``changes()`` read.

        Same scope discipline as ``changes()`` -- every id must belong to THIS
        document -- but checked in one query instead of N ``get_checkpoint`` lookups.
        A malformed list (empty/too many/duplicate/non-string) is a 400; an id that
        does not exist in this document's history is a 404, same as any other
        checkpoint lookup; a damaged store is CHANGES_UNREADABLE."""
        _, store = self._store(document_id)
        try:
            ids = storage_summaries.validate_ids(checkpoint_ids)
        except CursorError as exc:
            raise bad_request(f"Cannot read change summaries: {exc}",
                              "Pass 1-100 distinct checkpoint ids that belong to this document.") from exc
        try:
            known = store.known_checkpoint_ids(ids)
        except RepositoryError as exc:
            raise ServiceError("CHANGES_UNREADABLE", f"Cannot read this document's history: {exc}",
                               next_action="The history may be damaged; restore from a complete backup.") from exc
        if any(cid not in known for cid in ids):
            raise not_found("Saved version")
        try:
            return store.evidence_package(ids) if _evidence else store.change_summaries(ids)
        except CursorError as exc:
            raise bad_request(f"Cannot read change summaries: {exc}",
                              "Pass 1-100 distinct checkpoint ids that belong to this document.") from exc
        except RepositoryError as exc:
            raise ServiceError("CHANGES_UNREADABLE", f"The recorded changes of these versions cannot be read: {exc}",
                               next_action="The history may be damaged; restore from a complete backup.") from exc

    def evidence_package(self, document_id, checkpoint_ids):
        """Same document scope/error policy as summaries, with an ordered evidence envelope."""
        return self.change_summaries(document_id, checkpoint_ids, _evidence=True)

    # ------------------------------------------------------------ annotations --
    def annotations(self, document_id, cursor=None, limit=None, target_type=None, target_id=None):
        document = self.catalog.get_document(document_id)
        scope = {"document_id": document["id"], "kind": "annotations",
                 "target_type": target_type, "target_id": target_id}
        limit = cursors.limit_of(limit)
        try:
            state = cursors.decode(cursor, scope)
        except cursors.ServiceCursorMismatch:
            raise cursors.scope_error() from None
        result = self.catalog.page_annotations(document["id"], limit, state.get("before"), state.get("upper"),
                                               target_type, target_id)
        return cursors.page(scope, result, result["items"])

    def annotate(self, document_id, target_type, target_id, text, request_key=None):
        document, store = self._store(document_id)
        try:
            if target_type == "checkpoint":
                store.get_checkpoint(target_id)
            elif target_type == "segment":
                store.get_segment(target_id)
            else:
                raise bad_request("target_type must be checkpoint or segment.")
        except RepositoryError:
            raise not_found("Annotation target") from None
        return self.catalog.add_annotation(document["id"], target_type, target_id, text, request_key=request_key)

    # ------------------------------------------------------------------- jobs --
    def request_download(self, document_id, checkpoint_id, request_key=None):
        document, store = self._store(document_id)
        queries.get_checkpoint(store, checkpoint_id)   # scope check: must exist in THIS document
        job, _ = self.runner.submit(document["project_id"], "download", "checkpoint", checkpoint_id,
                                    {"document_id": document["id"]}, request_key=request_key)
        return job

    # ---------------------------------------------------------------- preview --
    def request_preview(self, document_id, checkpoint_id, payload=None, request_key=None):
        document, store = self._store(document_id)
        if not self.supervisor.capabilities["preview"]:
            raise unavailable("preview", "The klayout Python module is not installed in the service's Python.",
                              "Install it with: python -m pip install klayout  (then restart the service). "
                              "Downloading the version still works.", details={"dependency": "klayout"})
        record = queries.get_checkpoint(store, checkpoint_id)
        options = _preview_options(payload or {})
        fmt = str(record.get("format") or "GDS2").upper()
        if not self.services.formats.has_reader(fmt, "preview"):
            raise ServiceError("PREVIEW_UNSUPPORTED_FORMAT",
                               f"Preview is unavailable for this format: {fmt}.",
                               status=409, next_action="Download it or open it in KLayout instead.")
        if record["size"] > self.budgets.max_source_bytes:
            raise ServiceError("PREVIEW_TOO_LARGE", "This version is larger than the preview limit.", status=413,
                               next_action="Download it or open it in KLayout instead.",
                               details={"size": record["size"], "limit": self.budgets.max_source_bytes})
        job, _ = self.runner.submit(document["project_id"], "preview", "checkpoint", checkpoint_id,
                                    {"document_id": document["id"], **options}, request_key=request_key)
        return job

    def _job_preview(self, job):
        document = self.catalog.get_document(job["payload"]["document_id"], job["project_id"])
        store = queries.open_store(document, self.supervisor.writable_handles(), services=self.services)
        record = queries.get_checkpoint(store, job["target_id"])
        options = {k: job["payload"].get(k) for k in ("top_cell", "viewport_dbu", "layers")}
        folder = self.state.cache_dir / "previews" / record["sha256"]
        folder.mkdir(parents=True, exist_ok=True)
        key = fingerprint({"renderer": RENDERER_VERSION, "options": options})[:24]
        cached = folder / f"{key}.json"
        summary = _read_cached_summary(cached)
        if summary is not None:
            return {**summary, "cached": True, "filename": "preview.json", "content_type": "application/json"}, str(cached)
        source = folder / "source.bin"
        if not source.is_file():
            temporary = folder / f"source-{job['id']}.tmp"
            try:
                store.export(job["target_id"], temporary)
            except RepositoryError as exc:
                raise ServiceError("EXPORT_FAILED", f"Could not rebuild the saved version: {exc}",
                                   next_action="The history may be damaged; restore from a complete backup.") from exc
            try:
                os.replace(temporary, source)
            except OSError:
                temporary.unlink(missing_ok=True)
        outcome = run_preview({"path": str(source), "format": record.get("format") or "GDS2",
                               "checkpoint_id": job["target_id"], **options}, self.budgets, formats=self.services.formats)
        if not outcome.get("ok"):
            raise ServiceError(outcome.get("code", "PREVIEW_WORKER_FAILED"), outcome.get("message", "Preview failed."),
                               status=409, next_action="Download this version or open it in KLayout instead.",
                               details={k: v for k, v in outcome.items() if k not in ("ok", "code", "message")})
        preview = outcome["preview"]
        summary = {"completeness": preview["completeness"], "warnings": preview["warnings"],
                   "bbox_dbu": preview["bbox_dbu"], "item_count": preview["counts"]["items"],
                   "top_cell": preview["top_cell"], "truncated": preview["truncated"]}
        text = json.dumps({"summary": summary, "preview": preview}, ensure_ascii=False, allow_nan=False)
        if len(text.encode("utf-8")) > self.budgets.max_response_bytes:
            raise ServiceError("PREVIEW_TOO_LARGE", "The preview result exceeds the response limit.", status=413,
                               next_action="Narrow the viewport or the layer list, or download the version.")
        _write_atomic(cached, text)
        return {**summary, "cached": False, "filename": "preview.json", "content_type": "application/json"}, str(cached)

    # ------------------------------------------------------------ relocation --
    def request_relocate(self, project_id, new_root, request_key=None, *, allow_inside_git=False):
        """Move ALL of a project's live history to a new folder (job): drain, copy, verify, switch, remove old."""
        project = self.catalog.get_project(project_id)
        self._require_autocapture(project_id)
        if not isinstance(new_root, str) or not new_root.strip():
            raise bad_request("A destination folder is required.")
        if new_root.startswith(("\\\\", "//")):
            raise bad_request("Network history destinations are not supported; choose a local folder.")
        target = Path(new_root).expanduser()
        if not target.is_absolute():
            raise bad_request("The destination must be an absolute folder path.")
        target = target.resolve()
        old_root = Path(project["history_root"]).resolve()
        if target == old_root:
            raise ServiceError("RELOCATE_SAME_FOLDER", "That is already the history folder.", status=409,
                               next_action="Choose a different folder.")
        self._check_history_overlap(project, target)
        for label, forbidden in (("the service state folder", self.state.root),
                                 ("the current history folder", old_root)):
            if is_within(target, forbidden) or is_within(forbidden, target):
                raise ServiceError("RELOCATE_BAD_DESTINATION", f"The destination overlaps {label}.", status=409,
                                   next_action="Choose a folder that is not inside or around it.")
        if project.get("workspace") and (is_within(target, Path(project["workspace"])) or is_within(Path(project["workspace"]), target)):
            raise ServiceError("RELOCATE_BAD_DESTINATION", "The destination overlaps the workspace.", status=409,
                               next_action="Keep history outside the workspace.")
        from .state import inside_git_checkout
        if inside_git_checkout(target) and not allow_inside_git:
            raise ServiceError("HISTORY_ROOT_INSIDE_GIT", "The destination is inside a Git checkout.", status=409,
                               next_action="Choose a folder outside any Git checkout.")
        if target.exists():
            if not target.is_dir():
                raise ServiceError("PATH_NOT_DIRECTORY", "The destination exists and is not a folder.", status=409,
                                   next_action="Choose a folder path.")
            from .catalog import HISTORY_ROOT_MARKER
            if any(target.iterdir()) and not (target / HISTORY_ROOT_MARKER).is_file():
                raise ServiceError("HISTORY_ROOT_NOT_EMPTY", "The destination already contains other files.",
                                   status=409, next_action="Choose an empty or new folder; nothing is merged.")
        job, _ = self.runner.submit(project_id, "relocate", "project", project_id,
                                    {"new_root": str(target), "old_root": str(old_root)}, request_key=request_key)
        return job

    def _check_history_overlap(self, project, target):
        """Check every registered history, including read-only imports and nested old stores."""
        old = Path(project["history_root"]).resolve()
        if str(target).startswith(("\\\\", "//")):
            raise bad_request("Network history destinations are not supported.")
        if os.name == "nt":
            import ctypes
            if ctypes.windll.kernel32.GetDriveTypeW(str(target.anchor)) == 4:
                raise bad_request("Network history destinations are not supported.")
        for other in self.catalog.list_projects():
            if other["id"] != project["id"]:
                root = Path(other["history_root"]).resolve()
                if any(is_within(a, b) or is_within(b, a) for a, b in ((target, root), (old, root))):
                    raise ServiceError("RELOCATE_BAD_DESTINATION", "History folders overlap another project.", status=409,
                                       next_action="Choose separate history folders; preserve existing copies.")
            for doc in self.catalog.list_documents(other["id"]):
                path = Path(doc["store_path"]).resolve()
                if is_within(target, path) or is_within(path, target):
                    raise ServiceError("RELOCATE_BAD_DESTINATION", "The destination overlaps a registered history.", status=409)
                if (other["id"] != project["id"] or doc["origin"] != "live") and is_within(path, old):
                    raise ServiceError("RELOCATE_BAD_DESTINATION", "The source contains another registered history.", status=409)

    def _job_relocate(self, job):
        # Labels must not be written into the old directory while it is copied.
        with self._presentation_lock:
            return self._relocate_with_presentation_locked(job)

    def _relocate_with_presentation_locked(self, job):
        from .catalog import HISTORY_ROOT_MARKER
        from .state import now_iso
        project = self.catalog.get_project(job["project_id"])
        old_root, new_root = Path(job["payload"]["old_root"]), Path(job["payload"]["new_root"])
        if Path(project["history_root"]).resolve() != old_root.resolve():
            raise ServiceError("RELOCATE_STALE", "The history folder changed since this move was requested.", status=409)
        self._check_history_overlap(project, new_root.resolve())
        coordinators = self._coordinators(project["id"])
        # 1. Drain every window's recorder; a failed tail save aborts before anything is touched.
        held = []
        leases = []
        topology_locked = False
        mapping, copied, switched = {}, [], False     # referenced by the rollback path below
        try:
            for coordinator in coordinators:
                coordinator.wait_reply(coordinator.request("navigation_hold"), 300)
                held.append(coordinator)
            self.catalog.history_topology_lock.acquire()
            topology_locked = True
            self._check_history_overlap(project, new_root.resolve())
            live = [d for d in self.catalog.list_documents(project["id"])
                    if d["origin"] == "live" and is_within(Path(d["store_path"]), old_root)]
            # request_relocate checked this too, but the job runs later: re-check at execution
            # time so a folder filled in the meantime is never merged into or deleted around.
            if new_root.exists() and any(new_root.iterdir()) and not (new_root / HISTORY_ROOT_MARKER).is_file():
                raise ServiceError("HISTORY_ROOT_NOT_EMPTY",
                                   "The destination received other files after the move was requested.",
                                   status=409, next_action="Choose an empty or new folder; nothing was moved.")
            new_root.mkdir(parents=True, exist_ok=True)
            if not (new_root / HISTORY_ROOT_MARKER).exists():
                (new_root / HISTORY_ROOT_MARKER).write_text(
                    json.dumps({"created_at": now_iso(), "relocated_from": str(old_root)}, ensure_ascii=False), encoding="utf-8")
            from ..filelock import FileLock, LockHeld
            from ..store import WRITER_LOCK
            for document in sorted(live, key=lambda item: item["store_path"]):
                src = Path(document["store_path"])
                lease = FileLock(src / WRITER_LOCK, {"owner": "vestigraph relocation"})
                try:
                    lease.acquire()
                except (LockHeld, OSError) as exc:
                    raise ServiceError("RELOCATE_HISTORY_LOCKED", str(exc), status=409,
                                       next_action="Stop the other writer and retry; nothing was moved.") from exc
                leases.append(lease)
            # 2. Copy every store, then 3. verify byte-for-byte before switching anything.
            for document in live:
                src = Path(document["store_path"])
                dst = new_root / src.name
                if dst.exists():
                    raise ServiceError("RELOCATE_CONFLICT", f"Destination already has a folder named {src.name}.",
                                       next_action="Choose an empty folder.")
                _settle_wal(src)
                copied.append(dst)
                shutil.copytree(src, dst, ignore=shutil.ignore_patterns("*.lock", "*-wal", "*-shm", "tmp"))
                (dst / "tmp").mkdir(exist_ok=True)
                if _content_hashes(src) != _content_hashes(dst):
                    raise ServiceError("RELOCATE_VERIFY_FAILED", f"Copy of {src.name} does not match the original.",
                                       next_action="Nothing was switched; the old folder is intact. Check the destination disk.")
                mapping[document["id"]] = dst
            # 4. Switch the catalog atomically; 5. only then remove the old copies.
            self.catalog.set_history_root(project["id"], new_root, mapping)
            switched = True                     # from here on the new copies ARE the history: never removed
            for coordinator in coordinators:
                with coordinator.lock:
                    coordinator.project = self.catalog.get_project(project["id"])
            removed = []
            for document in live:
                src = Path(document["store_path"])
                # On Windows the open lease file cannot be deleted; on POSIX deleting it
                # would let a second writer lock a different inode. Remove content first.
                for child in src.iterdir():
                    if child.name == WRITER_LOCK:
                        continue
                    if child.is_dir() and not child.is_symlink():
                        shutil.rmtree(child)
                    else:
                        child.unlink()
                removed.append(str(src))
            for lease in leases:
                lease.release()
                try:
                    lease.path.unlink(missing_ok=True)
                    lease.path.parent.rmdir()
                except OSError:
                    pass  # content is gone; a stale caller may still hold the empty lock
            try:
                stray = [p for p in old_root.iterdir() if p.name != HISTORY_ROOT_MARKER] if old_root.exists() else []
                if old_root.exists() and not stray:
                    shutil.rmtree(old_root, ignore_errors=True)
            except OSError as exc:                 # cosmetic cleanup only; the move itself is done
                stray = [f"(could not inspect the old folder: {exc})"]
            return ({"new_root": str(new_root), "old_root": str(old_root), "documents_moved": len(mapping),
                     "old_folder_removed": not old_root.exists(),
                     "left_behind": [str(p) for p in stray][:20]}, None)
        except Exception:
            if not switched:
                # A catalog write may commit before its response fails. Re-read before rollback.
                switched = Path(self.catalog.get_project(project["id"])["history_root"]).resolve() == new_root.resolve()
            if switched:
                logging.getLogger(__name__).exception("Relocation committed; source cleanup incomplete")
                return ({"new_root": str(new_root), "old_root": str(old_root), "documents_moved": len(mapping),
                         "old_folder_removed": False, "left_behind": [str(old_root)],
                         "cleanup_pending": True}, None)
            for dst in copied:
                shutil.rmtree(dst, ignore_errors=True)   # verified uncommitted copies only
            raise
        finally:
            for lease in leases:
                lease.release()
            if topology_locked:
                self.catalog.history_topology_lock.release()
            for coordinator in held:
                try:
                    coordinator.wait_reply(coordinator.request("navigation_release"), 60)
                except ServiceError:
                    pass

    # ------------------------------------------------------------- cell diff --
    def request_diff(self, document_id, from_id, to_id, request_key=None):
        """Compare two saved versions of the same document at cell level (job on the preview lane)."""
        document, store = self._store(document_id)
        if not self.supervisor.capabilities["preview"]:
            raise unavailable("preview", "Comparing versions needs the klayout Python module in the service's Python.",
                              "Install it with: python -m pip install klayout  (then restart the service).",
                              details={"dependency": "klayout"})
        if not isinstance(from_id, str) or not isinstance(to_id, str) or not from_id or not to_id:
            raise bad_request("from and to checkpoint ids are required.")
        before = queries.get_checkpoint(store, from_id)     # scope: both must live in THIS document
        after = queries.get_checkpoint(store, to_id)
        for record in (before, after):
            fmt = str(record.get("format") or "GDS2").upper()
            if not self.services.formats.has_reader(fmt, "compare"):
                raise ServiceError("PREVIEW_UNSUPPORTED_FORMAT",
                                   f"Comparison is unavailable for this format: {fmt}.",
                                   status=409, next_action="Download both versions and compare them in KLayout.")
            if record["size"] > self.budgets.max_source_bytes:
                raise ServiceError("PREVIEW_TOO_LARGE", "A version is larger than the comparison limit.", status=413,
                                   next_action="Download both versions and compare them in KLayout.")
        job, _ = self.runner.submit(document["project_id"], "diff", "checkpoint", to_id,
                                    {"document_id": document["id"], "from": from_id, "to": to_id},
                                    request_key=request_key)
        return job

    def _exported_source(self, store, checkpoint_id, record, job_id):
        """Verified export of a checkpoint into the preview cache (shared by preview and diff)."""
        folder = self.state.cache_dir / "previews" / record["sha256"]
        folder.mkdir(parents=True, exist_ok=True)
        source = folder / "source.bin"
        if not source.is_file():
            temporary = folder / f"source-{job_id}.tmp"
            try:
                store.export(checkpoint_id, temporary)
            except RepositoryError as exc:
                raise ServiceError("EXPORT_FAILED", f"Could not rebuild the saved version: {exc}",
                                   next_action="The history may be damaged; restore from a complete backup.") from exc
            try:
                os.replace(temporary, source)
            except OSError:
                temporary.unlink(missing_ok=True)
        return folder, source

    def _job_diff(self, job):
        document = self.catalog.get_document(job["payload"]["document_id"], job["project_id"])
        store = queries.open_store(document, self.supervisor.writable_handles(), services=self.services)
        from_id, to_id = job["payload"]["from"], job["payload"]["to"]
        before = queries.get_checkpoint(store, from_id)
        after = queries.get_checkpoint(store, to_id)
        folder = self.state.cache_dir / "diffs"
        folder.mkdir(parents=True, exist_ok=True)
        key = fingerprint({"renderer": RENDERER_VERSION, "from": before["sha256"], "to": after["sha256"]})[:32]
        cached = folder / f"{key}.json"
        summary = _read_cached_summary(cached)
        if summary is not None:
            return {**summary, "cached": True, "filename": "diff.json", "content_type": "application/json"}, str(cached)
        _, source_before = self._exported_source(store, from_id, before, job["id"])
        _, source_after = self._exported_source(store, to_id, after, job["id"])
        outcome = run_diff({"path_before": str(source_before), "path_after": str(source_after),
                            "format_before": before.get("format") or "GDS2", "format_after": after.get("format") or "GDS2",
                            "from_id": from_id, "to_id": to_id}, self.budgets, formats=self.services.formats)
        if not outcome.get("ok"):
            raise ServiceError(outcome.get("code", "PREVIEW_WORKER_FAILED"), outcome.get("message", "Comparison failed."),
                               status=409, next_action="Download both versions and compare them in KLayout.",
                               details={k: v for k, v in outcome.items() if k not in ("ok", "code", "message")})
        result = outcome["diff"]
        summary = {**result["summary"], "completeness": result["completeness"], "warnings": result["warnings"]}
        text = json.dumps({"summary": summary, "diff": result}, ensure_ascii=False, allow_nan=False)
        if len(text.encode("utf-8")) > self.budgets.max_response_bytes:
            raise ServiceError("PREVIEW_TOO_LARGE", "The comparison result exceeds the response limit.", status=413,
                               next_action="Download both versions and compare them in KLayout.")
        _write_atomic(cached, text)
        return {**summary, "cached": False, "filename": "diff.json", "content_type": "application/json"}, str(cached)

    # ------------------------------------------------------- open in KLayout --
    def request_open_in_klayout(self, document_id, checkpoint_id, payload=None, request_key=None):
        return self.request_open_in_editor(document_id, checkpoint_id,
                                           {**(payload or {}), "backend_id": "klayout"}, request_key,
                                           _job_kind="open_in_klayout")

    def request_open_in_editor(self, document_id, checkpoint_id, payload=None, request_key=None,
                               *, _job_kind="open_in_editor"):
        document, store = self._store(document_id)
        if not self.supervisor.capabilities["open_history"]:
            raise unavailable("open_history", "No editor integration is available for opening history.",
                              "Install or enable the intended editor adapter, then restart the service.")
        queries.get_checkpoint(store, checkpoint_id)
        payload = payload or {}
        session_id = payload.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise bad_request("session_id is required.", "Pick an editor window from the list.")
        if payload.get("confirm_new_tab") is not True:
            raise bad_request("confirm_new_tab must be true.", "This action opens a NEW tab; confirm it.")
        candidate = self._session_candidate(session_id, payload.get("expected_session_instance"),
                                            payload.get("backend_id"))
        job, _ = self.runner.submit(document["project_id"], _job_kind, "checkpoint", checkpoint_id,
                                    {"document_id": document["id"], "session_id": session_id,
                                     "backend_id": candidate.backend_id,
                                     "expected_session_instance": candidate.session_instance_id},
                                    request_key=request_key)
        return job

    def _session_candidate(self, session_id, expected_instance=None, backend_id=None):
        self.supervisor.editors.refresh()
        return self.supervisor.editors.resolve(session_id, backend_id=backend_id,
                                               expected_instance=expected_instance)

    def _job_open_in_klayout(self, job):
        payload = job["payload"]
        document = self.catalog.get_document(payload["document_id"], job["project_id"])
        store = queries.open_store(document, self.supervisor.writable_handles(), services=self.services)
        record = queries.get_checkpoint(store, job["target_id"])
        candidate = self._session_candidate(payload["session_id"], payload.get("expected_session_instance"),
                                            payload.get("backend_id"))
        # Every coordinator bound to this window must drain before we navigate it.
        held, backend = [], None
        try:
            backend = self.supervisor.editors.create(candidate)
            backend.connect(candidate)
            try:
                spec = self.services.formats.get(record.get("format") or "GDS2")
            except ValueError:
                raise BackendError("unsupported_format", outcome="not_started") from None
            backend.check_open_format(spec.format_id, spec.artifact_role)
            folder = self.state.cache_dir / "history_preview" / job["id"]
            folder.mkdir(parents=True, exist_ok=True)
            target = folder / download_name(record, formats=self.services.formats)
            try:
                store.export(job["target_id"], target)
            except RepositoryError as exc:
                raise ServiceError("EXPORT_FAILED", f"Could not rebuild the saved version: {exc}",
                                   next_action="The history may be damaged; restore from a complete backup.") from exc
            for coordinator in self._coordinators_on(editor_session_id(candidate)):
                coordinator.wait_reply(coordinator.request("navigation_hold"), 240)
                held.append(coordinator)
            response = backend.open_snapshot(OpenRequest(target.resolve(), spec.format_id, time.monotonic()+60,
                                                         artifact_role=spec.artifact_role))
            if response.outcome != "completed":
                raise BackendError(response.reason_code or "open_failed",
                                   outcome="unknown" if response.outcome == "unknown" else "failed")
        except BackendError as exc:
            if exc.outcome == "unknown":
                raise JobOutcomeUnknown(ServiceError(
                    "OPEN_UNCONFIRMED", "The editor did not confirm opening the version.",
                    status=504, next_action="Look at the editor before trying again; do not repeat blindly.")) from exc
            raise ServiceError("OPEN_UNSUPPORTED" if exc.reason_code in ("unsupported", "unsupported_format") else "OPEN_FAILED",
                               "The editor could not open this version.", status=409,
                               details={"reason_code": exc.reason_code},
                               next_action="Check the editor's supported formats and current availability.") from exc
        finally:
            if backend is not None:
                try:
                    backend.close()
                except BackendError:
                    pass
            for coordinator in held:
                try:
                    coordinator.wait_reply(coordinator.request("navigation_release"), 60)
                except ServiceError:
                    pass
        return ({"opened_path": str(target), "session_id": editor_session_id(candidate),
                 "backend_id": candidate.backend_id, "session_instance_id": candidate.session_instance_id,
                 "tab": "new", "document_ref": asdict(response.document),
                 "note": "This tab shows a saved version, not your working document; recording resumes "
                         "when you switch back to a document inside the workspace."}, None)

    def _coordinators_on(self, session_id):
        # Navigation is a lifecycle barrier, not a UI-state filter. A saving
        # recorder must drain too; a waiting/reconnecting coordinator could
        # start recording between a status read and the open RPC. Hold every
        # coordinator of this session and let it decide whether its tail is safe.
        return self.supervisor.coordinators_on(session_id)

    def job(self, job_id, project_ids=None) -> dict:
        job = self.catalog.get_job(job_id)
        if project_ids is not None and job["project_id"] not in project_ids:
            raise not_found("Job")
        return _public_job(self.runner.observed_job(job))

    def job_asset(self, job_id, project_ids=None):
        """Return (path, filename, content_type) for a succeeded job; 410 when expired."""
        job = self.catalog.get_job(job_id)
        if project_ids is not None and job["project_id"] not in project_ids:
            raise not_found("Job")
        if job["status"] != "succeeded" or not job.get("asset_path"):
            raise ServiceError("ASSET_NOT_READY", "The task has no result file yet.", status=409,
                               next_action="Wait for the task to succeed, then fetch its result.",
                               retryable=job["status"] in ("queued", "running"))
        path = Path(job["asset_path"])
        if not is_within(path, self.state.cache_dir):
            raise forbidden("Asset path is outside the service cache.", "Report this; the file was not served.")
        if not path.is_file() or time.time() - path.stat().st_mtime > _asset_ttl(path, self.state.cache_dir):
            self._discard_asset(job)
            raise ServiceError("ASSET_EXPIRED", "The prepared file expired and was removed.", status=410,
                               next_action="Request the download again.")
        result = job.get("result") or {}
        return path, result.get("filename") or path.name, result.get("content_type") or "application/octet-stream"

    def _discard_asset(self, job):
        """Downloads live in a per-job folder (removed whole); preview/diff files sit in shared
        content-addressed folders, so only the one file goes."""
        path = Path(job["asset_path"]) if job.get("asset_path") else None
        if path is not None and is_within(path, self.state.cache_dir):
            if is_within(path, self.state.cache_dir / "downloads") and path.parent != self.state.cache_dir / "downloads":
                shutil.rmtree(path.parent, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)
        self.catalog.update_job(job["id"], asset_path="")

    def sweep_assets(self):
        """Remove expired files under the service cache only (never history folders)."""
        removed = 0
        # history_preview exports may be open in KLayout for a while: they get the long TTL.
        for name, ttl in (("downloads", ASSET_TTL_S), ("previews", PREVIEW_TTL_S), ("diffs", PREVIEW_TTL_S),
                          ("history_preview", PREVIEW_TTL_S), ("thumbnails", PREVIEW_TTL_S)):
            root = self.state.cache_dir / name
            if not root.is_dir():
                continue
            for entry in root.iterdir():
                try:
                    if entry.is_dir():
                        newest = max((p.stat().st_mtime for p in entry.rglob("*") if p.is_file()), default=entry.stat().st_mtime)
                    else:
                        newest = entry.stat().st_mtime          # diffs are flat <key>.json files
                except OSError:
                    continue
                if time.time() - newest > ttl:
                    try:
                        if entry.is_dir():
                            shutil.rmtree(entry)
                        else:
                            entry.unlink()
                    except OSError:
                        continue                                 # count only what was actually removed
                    removed += 1
        removed += self._enforce_cache_budget()
        return removed

    def _enforce_cache_budget(self, budget=ASSET_BUDGET_BYTES) -> int:
        """Oldest cache entries go first until the whole cache folder fits the budget."""
        entries = []
        total = 0
        for name in ("downloads", "previews", "diffs", "history_preview", "thumbnails"):
            root = self.state.cache_dir / name
            if not root.is_dir():
                continue
            for entry in root.iterdir():
                try:
                    files = [p for p in entry.rglob("*") if p.is_file()] if entry.is_dir() else [entry]
                    size = sum(p.stat().st_size for p in files)
                    newest = max((p.stat().st_mtime for p in files), default=entry.stat().st_mtime)
                except OSError:
                    continue
                entries.append((newest, size, entry))
                total += size
        removed = 0
        for newest, size, entry in sorted(entries):
            if total <= budget:
                break
            try:
                shutil.rmtree(entry) if entry.is_dir() else entry.unlink()
            except OSError:
                continue
            total -= size
            removed += 1
        return removed

    # --------------------------------------------------------- job handlers --
    def _job_download(self, job):
        document = self.catalog.get_document(job["payload"]["document_id"], job["project_id"])
        store = queries.open_store(document, self.supervisor.writable_handles(), services=self.services)
        record = queries.get_checkpoint(store, job["target_id"])
        folder = self.state.cache_dir / "downloads" / job["id"]
        folder.mkdir(parents=True, exist_ok=True)
        filename = download_name(record, formats=self.services.formats)
        target = folder / "file.bin"
        try:
            store.export(job["target_id"], target)
        except RepositoryError as exc:
            raise ServiceError("EXPORT_FAILED", f"Could not rebuild the saved version: {exc}",
                               next_action="The history may be damaged; restore from a complete backup.") from exc
        digest = hashlib.sha256()
        with target.open("rb") as stream:
            for block in iter(lambda: stream.read(1 << 20), b""):
                digest.update(block)
        if digest.hexdigest() != record["sha256"]:
            target.unlink(missing_ok=True)
            raise ServiceError("EXPORT_FAILED", "Rebuilt file hash mismatch.",
                               next_action="The history may be damaged; restore from a complete backup.")
        return ({"filename": filename, "size": record["size"], "sha256": record["sha256"],
                 "content_type": "application/octet-stream", "format": record.get("format")}, str(target))


def _state_rank(state: str | None) -> int:
    return _PRECEDENCE.index(state) if state in _PRECEDENCE else 99


def _settle_wal(store_root: Path) -> None:
    """Fold SQLite write-ahead logs into their databases before the folder is copied WITHOUT them.

    The copy and its byte-for-byte check both skip ``-wal`` sidecars, so a checkpoint that
    only lives in a non-empty WAL (crash residue, or a transient writer after the drain)
    would be dropped by the move and never noticed. Truncate the WAL first; if it cannot be
    emptied (another connection is active), refuse -- nothing has been copied yet.
    """
    for database in sorted(store_root.glob("*.sqlite3")):
        try:
            connection = sqlite3.connect(str(database), timeout=30)
            try:
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                connection.close()
        except sqlite3.Error as exc:
            raise ServiceError("RELOCATE_WAL_NOT_SETTLED",
                               f"Could not fold the write-ahead log of {store_root.name}/{database.name}: {exc}",
                               status=409, next_action="Retry when nothing else writes this history; nothing was moved.") from exc
    for sidecar in sorted(store_root.glob("*-wal")):
        if sidecar.is_file() and sidecar.stat().st_size > 0:
            raise ServiceError("RELOCATE_WAL_NOT_SETTLED",
                               f"{store_root.name}/{sidecar.name} still holds changes that are not in the database file.",
                               status=409, next_action="Retry when nothing else writes this history; nothing was moved.")


def _content_hashes(root: Path) -> dict:
    """sha256 of every history file that carries content (locks, WAL sidecars and tmp excluded)."""
    out = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name.endswith(("-wal", "-shm", ".lock")) or "tmp" in path.relative_to(root).parts:
            continue
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1 << 20), b""):
                digest.update(block)
        out[str(path.relative_to(root))] = digest.hexdigest()
    return out


def _preview_options(payload: dict) -> dict:
    top_cell = payload.get("top_cell")
    if top_cell is not None and (not isinstance(top_cell, str) or not 1 <= len(top_cell) <= 200):
        raise bad_request("top_cell must be a cell name.")
    viewport = payload.get("viewport_dbu")
    if viewport is not None:
        if (not isinstance(viewport, list) or len(viewport) != 4
                or any(isinstance(v, bool) or not isinstance(v, int) or abs(v) > 2**40 for v in viewport)):
            raise bad_request("viewport_dbu must be four integers [x1, y1, x2, y2].")
    layers = payload.get("layers")
    if layers is not None:
        if not isinstance(layers, list) or len(layers) > 64:
            raise bad_request("layers must be a list of at most 64 [layer, datatype] pairs.")
        cleaned = []
        for item in layers:
            if not (isinstance(item, list) and len(item) == 2 and all(isinstance(v, int) and not isinstance(v, bool) for v in item)):
                raise bad_request("layers must be [layer, datatype] integer pairs.")
            cleaned.append([item[0], item[1]])
        layers = cleaned
    return {"top_cell": top_cell, "viewport_dbu": viewport, "layers": layers}


def _write_atomic(path: Path, text: str) -> None:
    """A crash mid-write must not leave a half JSON that poisons the cache key forever."""
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _read_cached_summary(path: Path):
    """Cached summary or None; an unreadable/corrupt cache file is a miss and is removed."""
    if not path.is_file():
        return None
    try:
        summary = json.loads(path.read_text(encoding="utf-8")).get("summary", {})
        path.touch()
        return summary
    except (OSError, ValueError, AttributeError):
        try:
            path.unlink()
        except OSError:
            pass
        return None


def _asset_ttl(path: Path, cache_dir: Path) -> float:
    return ASSET_TTL_S if is_within(path, cache_dir / "downloads") else PREVIEW_TTL_S


def _contains_path(value) -> bool:
    """True when any string inside looks like an absolute/UNC path (client must send ids only)."""
    if isinstance(value, str):
        return ":\\" in value or ":/" in value or value.startswith(("/", "\\\\", "\\"))
    if isinstance(value, dict):
        return any(_contains_path(v) for v in value.values())
    if isinstance(value, list):
        return any(_contains_path(v) for v in value)
    return False


def download_name(record: dict, *, formats=None) -> str:
    """Suggested filename from the stored record only: real stem + suffix of the stored format."""
    source = record.get("document_filename") or record.get("filename") or "version"
    stem = Path(source).stem or "version"
    fmt = record.get("format")
    suffix = filename_suffix(fmt, record.get("filename"), formats=formats)
    safe = "".join(ch for ch in stem if ch not in '\\/:*?"<>|\r\n\t').strip() or "version"
    return safe[:120] + suffix


def _public_job(job: dict) -> dict:
    return {key: job.get(key) for key in (
        "id", "project_id", "kind", "target_type", "target_id", "status", "progress",
        "result", "error", "created_at", "started_at", "ended_at", "external", "persistence_pending")}
