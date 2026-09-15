"""Capture supervisor: owns one Coordinator per project, independent of any client.

Nothing here is started by an HTTP request; the Application starts it. The
browser, the CLI or a future agent adapter only send requests to coordinators.
"""
from __future__ import annotations
import logging

import importlib.util
import threading

from .catalog import Catalog
from .editors import EditorDirectory, session_id as public_session_id
from vestigraph_backends.registry import configured_registry
from vestigraph_backends.types import Availability
from .coordinator import SESSION_POLL_S, ClaimRegistry, Coordinator
from .state import now_iso


# Project summary = the most "alive" of its windows' states.
# Aggregation order for a project with several windows (one coordinator per KLink session):
# the window doing the most is the one the project reports. EVERY state a coordinator can
# emit must be listed -- a missing state sorts last, so a window in that state is hidden
# behind idle windows (found live with 8 sessions online: "saving" was missing, the project
# showed "waiting_document" during every save and the cancel button never appeared).
_PRECEDENCE = ("saving", "recording", "baselining", "draining", "waiting_document", "reconnecting",
               "blocked", "paused", "waiting_session", "disabled")


def _has_module(name: str) -> bool:
    """find_spec raises (not returns None) when a parent package exists without the child."""
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError, AttributeError):
        return False


def _storage_delta_available() -> bool:
    try:
        from ..vesti_codecs.defaults import vesti_default_codecs
        codec = vesti_default_codecs().delta_codec
        return bool(codec is not None and codec.encoder_available())
    except Exception:  # noqa: BLE001
        return False


def _scan_native_available() -> bool:
    try:
        from vestigraph.vesti_formats.vesti_format_gds import native as scan_backend
        return bool(scan_backend.capability()["available"])
    except Exception:  # noqa: BLE001
        return False


def probe_capabilities(*, generic=False) -> dict:
    """Which optional pieces exist in THIS interpreter (never assumed)."""
    from vestigraph_backends.registry import legacy_capabilities
    return {
        "web": True,
        **({"autocapture": True, "autocapture_dependency": True, "session_registry": True,
            "open_history": True, "preview": False, "preview_dependency": False}
           if generic else legacy_capabilities()),
        "storage_delta": _storage_delta_available(),  # optional bsdiff4 encoder (pip install vestigraph[storage-delta])
        "scan_native": _scan_native_available(),      # optional Rust scanner (vestigraph_scan_core); python is the default
        "remote_access": False,
        "remote_edit": False,
    }


class Supervisor:
    """One Coordinator per (project, online KLayout window). A watcher thread reads klink's
    session registry; windows appearing get a recorder, windows leaving get drained."""

    def __init__(self, catalog: Catalog, *, registry=None, client_factory=None, autocapture=None,
                 storage_delta=None, coordinator_options=None, backend_registry=None):
        self.catalog = catalog
        self.services = catalog.services
        self.capabilities = probe_capabilities(generic=backend_registry is not None)
        self.editors = EditorDirectory(configured_registry(
            backend_registry=backend_registry, session_registry=registry, client_factory=client_factory),
            formats=catalog.services.formats)
        if backend_registry is not None:
            self.capabilities.update(autocapture=True, open_history=True,
                                     autocapture_dependency=True, session_registry=True)
        if autocapture is not None:
            self.capabilities["autocapture"] = bool(autocapture)
            self.capabilities["open_history"] = bool(autocapture)
        if storage_delta is not None:
            self.capabilities["storage_delta"] = bool(storage_delta)
        codec = self.services.codecs.delta_codec
        if storage_delta is None:
            self.capabilities["storage_delta"] = bool(codec is not None and codec.encoder_available())
        preview = False
        for fmt in self.services.formats.reader_formats("preview"):
            try:
                reader = self.services.formats.reader(fmt, "preview")
                preview = preview or not hasattr(reader, "capabilities") or reader.capabilities().available
            except Exception:
                continue  # an unavailable optional reader must not prevent recording
        self.capabilities.update(preview=preview, preview_dependency=preview)
        self.coordinator_options = dict(coordinator_options or {})
        self.session_poll_s = float(self.coordinator_options.get("session_poll_s", SESSION_POLL_S))
        self.lock = threading.Lock()
        self._coordinators = {}        # (project_id, session_id) -> Coordinator
        self.claims = ClaimRegistry()  # (window, document identity) -> owning project
        self._placeholders = {}        # project_id -> state dict when no coordinator can run
        self.version = 0
        self.started = False
        self.stopping = threading.Event()
        self.rescan_event = threading.Event()
        self.thread = None
        from .capture_recovery import CaptureRecovery
        self.capture_recovery = CaptureRecovery(catalog, self.bump)

    # ------------------------------------------------------------- lifecycle --
    def start(self):
        self.started = True
        self.capture_recovery.start()
        # A previous instance's runs are not ours to declare successful.
        for run in self.catalog.orphan_runs():
            self.catalog.end_run(run["id"], "interrupted",
                                 note="Service restarted while this run was active; tail changes may be missing.")
        self.editors.refresh()
        for project in self.catalog.list_projects():
            self._ensure(project)
        if self.capabilities["autocapture"]:
            self.rescan()
            self.thread = threading.Thread(target=self._watch_loop, daemon=True, name="vestigraph-supervisor")
            self.thread.start()
        return self

    def stop(self):
        self.started = False
        self.capture_recovery.stop()
        self.stopping.set()
        self.rescan_event.set()
        if self.thread is not None:
            self.thread.join(10)
        with self.lock:
            coordinators = list(self._coordinators.values())
            self._coordinators.clear()
        for coordinator in coordinators:
            coordinator.stop()

    def bump(self):
        with self.lock:
            self.version += 1
            return self.version

    def rescan(self):
        """Reconcile coordinators with the registry now (the watcher does this periodically)."""
        if not self.capabilities["autocapture"]:
            return
        online = {session.key: session for session in self.editors.refresh()
                  if session.availability == Availability.ONLINE}
        projects = {p["id"]: p for p in self.catalog.list_projects()}
        gone, created = [], []
        with self.lock:
            for key, coordinator in list(self._coordinators.items()):
                project_id, session_id = key
                live = online.get(session_id)
                if project_id not in projects or live is None:
                    gone.append(self._coordinators.pop(key))
        for coordinator in gone:
            coordinator.session_gone()
        for project in projects.values():
            for session in online.values():
                key = (project["id"], session.key)
                with self.lock:
                    if key in self._coordinators:
                        continue
                    coordinator = self._make(project, session)
                    self._coordinators[key] = coordinator
                created.append(coordinator)
        for coordinator in created:
            if self.started:
                coordinator.start()
        if gone or created:
            self.bump()

    def _watch_loop(self):
        while not self.stopping.is_set():
            try:
                self.rescan()
            except Exception:      # a registry hiccup must not kill the watcher
                logging.getLogger(__name__).warning("Nonfatal callback or cleanup failure", exc_info=True)
            self.rescan_event.wait(self.session_poll_s)
            self.rescan_event.clear()

    def _make(self, project, session):
        exclusions = [self.catalog.state.root, project["history_root"]]
        options = {k: v for k, v in self.coordinator_options.items() if k != "session_poll_s"}
        return Coordinator(project, self.catalog, session=session, directory=self.editors,
                           service_instance_id=self.catalog.state.instance_id,
                           on_change=self.bump, exclusions=exclusions, claims=self.claims, **options)

    def _ensure(self, project):
        if not self.capabilities["autocapture"]:
            reason = "klink_missing" if not self.capabilities["autocapture_dependency"] else "session_registry_missing"
            state = "blocked"
            if not project["policy"].get("enabled", True):
                state, reason = "disabled", "policy_disabled"
            elif project["policy"].get("paused"):
                state, reason = "paused", "paused"
            with self.lock:
                self._placeholders[project["id"]] = {
                    "state": state, "reason": reason, "since": now_iso(),
                    "session_instance": None, "document_id": None, "capture_run_id": None,
                    "last_success_at": None, "last_checkpoint_id": None,
                    "export_ms": None, "ingest_ms": None, "layout_bytes": None, "gap": None,
                    "diagnostic": {"next_action": "Install klayout-klink into the service's Python and restart."},
                    "sessions": [],
                }
            self.bump()
            return
        if self.started:
            self.rescan()

    def coordinators(self, project_id) -> list:
        with self.lock:
            return [c for (pid, _), c in self._coordinators.items() if pid == project_id]

    def coordinator(self, project_id, session_id=None):
        """One window's coordinator; without session_id the first one (None when no window)."""
        with self.lock:
            if session_id is not None:
                matches = [c for (pid, _), c in self._coordinators.items()
                           if pid == project_id and public_session_id(c.session_descriptor) == session_id]
                return matches[0] if len(matches) == 1 else None
            for (pid, _), coordinator in self._coordinators.items():
                if pid == project_id:
                    return coordinator
        return None

    def coordinators_on(self, session_id) -> list:
        with self.lock:
            matches = [c for c in self._coordinators.values()
                       if public_session_id(c.session_descriptor) == session_id]
            if len({c.session_descriptor.key for c in matches}) > 1:
                from .errors import ServiceError
                raise ServiceError("SESSION_AMBIGUOUS", "Select an unambiguous editor session.", status=409)
            return matches

    def on_policy_changed(self, project):
        for coordinator in self.coordinators(project["id"]):
            coordinator.notify_policy(project)
        if not self.coordinators(project["id"]):
            self._ensure(project)
        self.bump()

    def on_project_added(self, project):
        self._ensure(project)

    # ------------------------------------------------------------- state view --
    def project_status(self, project_id) -> dict:
        """Aggregate over the project's windows + the per-window list under "sessions"."""
        with self.lock:
            placeholder = self._placeholders.get(project_id)
        if placeholder is not None:
            return dict(placeholder)
        sessions = [c.status() for c in self.coordinators(project_id)]
        if not sessions:
            project = self.catalog.get_project(project_id)
            if not project["policy"].get("enabled", True):
                state, reason = "disabled", "policy_disabled"
            elif project["policy"].get("paused"):
                state, reason = "paused", "paused"
            else:
                state, reason = "waiting_session", "no_eligible_session"
            return {"state": state, "reason": reason, "since": now_iso(), "session_instance": None,
                    "document_id": None, "capture_run_id": None, "last_success_at": None,
                    "last_checkpoint_id": None, "export_ms": None, "ingest_ms": None, "layout_bytes": None,
                    "gap": None, "diagnostic": None, "sessions": []}
        best = min(sessions, key=lambda st: _PRECEDENCE.index(st["state"]) if st["state"] in _PRECEDENCE else 99)
        out = dict(best)
        out["sessions"] = sessions
        out["last_success_at"] = max((st.get("last_success_at") or "" for st in sessions), default=None) or None
        return out

    def document_state(self, document) -> str:
        """Capture state as seen from a document row (any window recording it)."""
        if document["origin"] == "imported_history" or document["read_only"]:
            return "read_only"
        matches = []
        for coordinator in self.coordinators(document["project_id"]):
            status = coordinator.status()
            if status.get("document_id") == document["id"]:
                matches.append(status)
        if matches:
            best = min(matches, key=lambda st: _PRECEDENCE.index(st["state"]) if st["state"] in _PRECEDENCE else 99)
            return best.get("state", "blocked")
        return "idle"

    def writable_handles(self) -> dict:
        out = {}
        with self.lock:
            coordinators = list(self._coordinators.values())
        for coordinator in coordinators:
            handle = coordinator.writable_handle()
            if handle is not None:
                out[handle[0]] = handle[1]
        return out
