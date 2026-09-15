"""Neutral editor discovery and public identities. No vendor SDK/transport knowledge."""
import hashlib
import json
import threading
from vestigraph_backends.guard import GuardedBackend
from vestigraph_backends.types import Availability, BackendError
from .errors import ServiceError


def session_id(session):
    return session.alias or session.backend_id + "-" + hashlib.sha256(
        session.session_instance_id.encode()).hexdigest()[:24]


def public_session(session):
    # Provider metadata is decoration, never routing/authority. Core fields win.
    return {**json.loads(session.public_metadata_json),
            "session_id": session_id(session), "backend_id": session.backend_id,
            "session_instance_id": session.session_instance_id,
            "display_name": session.display_name + " · " + (session.alias or session.session_instance_id[:12]),
            "online": session.availability == Availability.ONLINE}


def document_identity(document, session):
    from pathlib import Path
    if document.source_path:
        # Preserve existing saved-file identities. Ports/editors never own history.
        return {"kind": "saved", "path": str(Path(document.source_path).resolve())}
    if document.source_uri:
        return {"kind": "native", "backend_id": document.ref.backend_id, "uri": document.source_uri}
    return {"kind": "unsaved", "backend_id": document.ref.backend_id,
            "session_instance_id": document.ref.session_instance_id,
            "document_instance_id": document.ownership_key or (
                document.ref.document_instance_id if document.identity_strength == "stable" else "unresolved")}


class EditorDirectory:
    def __init__(self, registry, *, formats=None):
        self.formats = formats
        self.registry = registry
        self.lock = threading.RLock()
        self._sessions = {}
        self.errors = {}

    def refresh(self):
        with self.lock:
            found = {}
            errors = {}
            for backend_id in self.registry.backend_ids:
                try:
                    provider = GuardedBackend(self.registry.create(backend_id), formats=self.formats)
                    sessions = provider.discover({"include_stale": True})
                    found.update({session.key: session for session in sessions})
                except BackendError as exc:
                    errors[backend_id] = exc.reason_code
                    # A discovery read failure is not proof the user's editor exited.
                    found.update({key: value for key, value in self._sessions.items() if key[0] == backend_id})
            self._sessions, self.errors = found, errors
            return tuple(found.values())

    def sessions(self):
        with self.lock:
            return tuple(self._sessions.values())

    def create(self, session):
        return GuardedBackend(self.registry.create(session.backend_id), formats=self.formats)

    def resolve(self, public_id, *, backend_id=None, expected_instance=None):
        matches = [session for session in self.sessions()
                   if session_id(session) == public_id
                   and (backend_id is None or session.backend_id == backend_id)]
        if len(matches) > 1:
            raise ServiceError("SESSION_AMBIGUOUS", "Select the editor as well as its session.", status=409)
        if not matches or matches[0].availability != Availability.ONLINE:
            raise ServiceError("SESSION_NOT_FOUND", "That editor session is not online.", status=404)
        session = matches[0]
        if expected_instance is not None and str(expected_instance) not in (
                session.session_instance_id, *session.legacy_instance_aliases):
            raise ServiceError("SESSION_CHANGED", "The editor session has changed; refresh and choose again.", status=409)
        return session
