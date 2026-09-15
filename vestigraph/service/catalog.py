"""Service catalog: projects, documents, capture runs, jobs, annotations.

Its own SQLite database inside the service-state directory. It never touches
a history repository's schema; a document row only points at a v1 store path.
Public ids are UUID hex. Every method opens its own connection, so calls are
safe from worker threads.
"""
from __future__ import annotations

from contextlib import contextmanager
from functools import wraps
import threading
import hashlib
import json
import sqlite3
import uuid
from pathlib import Path

from ..store import Repository, RepositoryError
from .errors import ServiceError, bad_request, conflict, not_found
from .state import (ServiceState, inside_git_checkout, is_within, now_iso,
                    resolve_existing_dir)

CATALOG_FORMAT = 1
HISTORY_ROOT_MARKER = "vestigraph-history-root.json"
JOB_STATES = ("queued", "running", "succeeded", "failed", "interrupted", "unknown")
DEFAULT_POLICY = {
    "enabled": True,
    "session_selector": {"mode": "single_eligible"},
    "allow_unsaved": False,
    "intervals": {"idle_seconds": 5, "min_interval": 15, "max_interval": 60},
}
SCHEMA = """
CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS projects (
    ordinal INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
    name TEXT UNIQUE NOT NULL, workspace TEXT, history_root TEXT NOT NULL,
    policy TEXT NOT NULL, policy_version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS documents (
    ordinal INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id), name TEXT NOT NULL,
    store_path TEXT NOT NULL, origin TEXT NOT NULL, observed_identity TEXT NOT NULL,
    read_only INTEGER NOT NULL, recording_allowed INTEGER NOT NULL,
    predecessor_id TEXT, classification TEXT, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS documents_project ON documents(project_id, ordinal);
CREATE TABLE IF NOT EXISTS capture_runs (
    ordinal INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id),
    document_id TEXT REFERENCES documents(id), service_instance_id TEXT NOT NULL,
    session_instance TEXT NOT NULL, started_at TEXT NOT NULL, ended_at TEXT,
    status TEXT NOT NULL, coverage TEXT NOT NULL, notes TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS runs_document ON capture_runs(document_id, ordinal);
CREATE TABLE IF NOT EXISTS jobs (
    ordinal INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id), kind TEXT NOT NULL,
    target_type TEXT NOT NULL, target_id TEXT NOT NULL, payload TEXT NOT NULL,
    fingerprint TEXT NOT NULL, request_key TEXT, status TEXT NOT NULL,
    progress REAL, result TEXT, error TEXT, asset_path TEXT, external INTEGER NOT NULL,
    service_instance_id TEXT NOT NULL, created_at TEXT NOT NULL, started_at TEXT,
    ended_at TEXT);
CREATE TABLE IF NOT EXISTS request_keys (
    scope TEXT NOT NULL, request_key TEXT NOT NULL, fingerprint TEXT NOT NULL,
    job_id TEXT NOT NULL REFERENCES jobs(id), PRIMARY KEY(scope, request_key));
CREATE TABLE IF NOT EXISTS annotations (
    ordinal INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
    document_id TEXT NOT NULL REFERENCES documents(id), target_type TEXT NOT NULL,
    target_id TEXT NOT NULL, text TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS annotations_target ON annotations(document_id, target_type, target_id, ordinal);
CREATE TABLE IF NOT EXISTS annotation_keys (
    document_id TEXT NOT NULL, request_key TEXT NOT NULL, annotation_id TEXT NOT NULL REFERENCES annotations(id),
    PRIMARY KEY(document_id, request_key));
"""


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def fingerprint(payload) -> str:
    return hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()


def _row(row):
    if row is None:
        return None
    out = dict(row)
    out.pop("ordinal", None)
    for key in ("policy", "observed_identity", "session_instance", "coverage", "payload",
                "result", "error", "notes"):
        if key in out and out[key] is not None:
            out[key] = json.loads(out[key])
    for key in ("read_only", "recording_allowed", "external"):
        if key in out:
            out[key] = bool(out[key])
    return out


def _history_topology(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self.history_topology_lock:
            return method(self, *args, **kwargs)
    return call


class Catalog:
    def __init__(self, state: ServiceState, *, services=None):
        from ..vesti_runtime.services import VestiRepositoryServices
        self.services = services if services is not None else VestiRepositoryServices()
        self.state = state
        self.history_topology_lock = threading.RLock()
        self.path = state.catalog_path
        if not self.path.is_file():
            raise ServiceError("STATE_NOT_INITIALIZED", "Catalog database is missing.",
                               next_action="Run: python -m vestigraph service init --state DIR")
        with sqlite3.connect(self.path, timeout=30) as db:
            db.executescript(SCHEMA)          # additive migrations: every statement is IF NOT EXISTS
        db.close()

    @classmethod
    def init(cls, state: ServiceState, *, services=None):
        with sqlite3.connect(state.catalog_path, timeout=30) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)
            db.execute("INSERT OR IGNORE INTO config VALUES('format_version', ?)", (str(CATALOG_FORMAT),))
        db.close()
        return cls(state, services=services)

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    # --------------------------------------------------------------- projects --
    @_history_topology
    def add_project(self, name, workspace, history_root, *, allow_unsaved=False,
                    allow_inside_git=False):
        """Register a project. ``workspace=None`` means: every saved layout on this machine
        (minus exclusions) belongs here -- the zero-configuration default."""
        if not isinstance(name, str) or not name.strip() or len(name) > 100:
            raise bad_request("Project name must be 1-100 characters.")
        name = name.strip()
        ws = resolve_existing_dir(workspace, "Workspace") if workspace is not None else None
        hr = Path(history_root).expanduser()
        if hr.exists():
            hr = hr.resolve()
            if not hr.is_dir():
                raise ServiceError("PATH_NOT_DIRECTORY", "History root is not a directory.",
                                   next_action="Choose a folder for --history-root.")
            if (hr / "index.sqlite3").exists():
                raise ServiceError("HISTORY_ROOT_IS_REPOSITORY",
                                   "History root is itself a v1 history repository.",
                                   next_action="Use service add-history to attach an existing repository read-only, "
                                               "and choose an empty folder as --history-root.")
            if any(hr.iterdir()) and not (hr / HISTORY_ROOT_MARKER).is_file():
                raise ServiceError("HISTORY_ROOT_NOT_EMPTY",
                                   "History root already contains files that are not Vestigraph's.",
                                   next_action="Choose an empty or new folder; existing files are never converted.")
        else:
            hr = hr.resolve()
        if ws is not None and (is_within(hr, ws) or is_within(ws, hr)):
            raise ServiceError("HISTORY_ROOT_OVERLAPS_WORKSPACE",
                               "History root and workspace must not contain each other.",
                               next_action="Keep history outside the workspace so exports are never recorded.")
        if is_within(hr, self.state.root) or is_within(self.state.root, hr):
            raise ServiceError("HISTORY_ROOT_OVERLAPS_STATE",
                               "History root and the service state directory must not overlap.",
                               next_action="Choose a separate folder for --history-root.")
        if inside_git_checkout(hr) and not allow_inside_git:
            raise ServiceError("HISTORY_ROOT_INSIDE_GIT",
                               "History root is inside a Git checkout and could be synced/committed.",
                               next_action="Choose a folder outside any Git checkout, or pass --allow-inside-git.")
        hr.mkdir(parents=True, exist_ok=True)
        marker = hr / HISTORY_ROOT_MARKER
        if not marker.exists():
            marker.write_text(_json({"created_at": now_iso(), "note": "Vestigraph per-document history stores live here."}),
                              encoding="utf-8")
        policy = dict(DEFAULT_POLICY, allow_unsaved=bool(allow_unsaved))
        project_id = uuid.uuid4().hex
        with self._db() as db:
            if db.execute("SELECT 1 FROM projects WHERE name=?", (name,)).fetchone():
                raise conflict("PROJECT_NAME_TAKEN", f"A project named '{name}' already exists.",
                               "Pick another name or reuse the existing project id.")
            for row in db.execute("SELECT id, workspace FROM projects"):
                if row["workspace"] is None:
                    if ws is None:
                        raise conflict("DEFAULT_PROJECT_EXISTS",
                                       f"Project {row['id']} already records everything on this machine.",
                                       "Reuse that project; only one catch-all project is allowed.")
                    continue          # a catch-all project coexists with workspace projects (workspace wins)
                other = Path(row["workspace"])
                if ws is not None and (is_within(ws, other) or is_within(other, ws)):
                    raise conflict("WORKSPACE_OVERLAP",
                                   f"Workspace overlaps project {row['id']}.",
                                   "Workspaces must not nest; adjust the folder or reuse that project.")
            db.execute("INSERT INTO projects(id,name,workspace,history_root,policy,policy_version,created_at)"
                       " VALUES(?,?,?,?,?,1,?)",
                       (project_id, name, None if ws is None else str(ws), str(hr), _json(policy), now_iso()))
        return self.get_project(project_id)

    def default_project(self):
        """The catch-all project (workspace NULL) if registered."""
        with self._db() as db:
            row = db.execute("SELECT * FROM projects WHERE workspace IS NULL ORDER BY ordinal LIMIT 1").fetchone()
        return _row(row)

    def ensure_default_project(self, history_root, *, name="Vestigraph", allow_unsaved=True):
        """Zero-config path used by ``serve`` without registered projects."""
        existing = self.default_project()
        if existing is not None:
            return existing
        return self.add_project(name, None, history_root, allow_unsaved=allow_unsaved, allow_inside_git=True)

    @_history_topology
    def set_history_root(self, project_id, new_root, store_paths: dict):
        """Point a project and its live documents at a relocated history root (after a verified copy)."""
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
            if row is None:
                raise not_found("Project")
            db.execute("UPDATE projects SET history_root=? WHERE id=?", (str(new_root), project_id))
            for document_id, store_path in store_paths.items():
                db.execute("UPDATE documents SET store_path=? WHERE id=? AND project_id=?",
                           (str(store_path), document_id, project_id))
            result = _row(db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone())
        return result

    def get_project(self, project_id):
        with self._db() as db:
            row = db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if row is None:
            raise not_found("Project")
        return _row(row)

    def list_projects(self):
        with self._db() as db:
            rows = db.execute("SELECT * FROM projects ORDER BY ordinal").fetchall()
        return [_row(r) for r in rows]

    def set_policy(self, project_id, updates: dict, expected_version=None):
        if not isinstance(updates, dict):
            raise bad_request("Policy must be an object.")
        allowed = {"enabled", "session_selector", "allow_unsaved", "intervals"}
        unknown = set(updates) - allowed
        if unknown:
            raise bad_request(f"Unknown policy fields: {sorted(unknown)}")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
            if row is None:
                raise not_found("Project")
            if expected_version is not None and expected_version != row["policy_version"]:
                raise conflict("POLICY_VERSION_CONFLICT",
                               "Policy was changed by someone else.",
                               "Reload the policy and apply your change again.")
            policy = dict(json.loads(row["policy"]))
            policy.update(_validate_policy(updates))
            version = row["policy_version"] + 1
            db.execute("UPDATE projects SET policy=?, policy_version=? WHERE id=?",
                       (_json(policy), version, project_id))
        return self.get_project(project_id)

    def set_paused(self, project_id, paused: bool, reason=None):
        """Persist the user's pause (survives service restarts). Not a client-settable policy key."""
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
            if row is None:
                raise not_found("Project")
            policy = dict(json.loads(row["policy"]))
            if paused:
                policy["paused"] = {"reason": str(reason or "user")[:200], "at": now_iso()}
            else:
                policy.pop("paused", None)
            db.execute("UPDATE projects SET policy=?, policy_version=policy_version+1 WHERE id=?",
                       (_json(policy), project_id))
        return self.get_project(project_id)

    # -------------------------------------------------------------- documents --
    @_history_topology
    def add_history(self, project_id, path, *, read_only=True):
        """Attach an existing v1 repository as an imported_history document."""
        project = self.get_project(project_id)
        repo_root = resolve_existing_dir(path, "History repository")
        if not (repo_root / "index.sqlite3").is_file():
            raise ServiceError("NOT_A_HISTORY_REPOSITORY",
                               "Folder is not an initialized Vestigraph history (no index.sqlite3).",
                               next_action="Point to an existing history folder; folders are never auto-initialized.")
        try:
            repo = Repository.open_readonly(repo_root, services=self.services)
            counts = repo.counts()
            names = _observed_filenames(repo)
        except RepositoryError as exc:
            raise ServiceError("HISTORY_UNREADABLE", f"Cannot read the history: {exc}",
                               next_action="Check the folder or restore it from a complete backup.") from exc
        if len(names) == 1:
            name, classification = Path(next(iter(names))).name or next(iter(names)), "single_document"
        elif not names:
            name, classification = repo_root.name, "no_checkpoints" if counts["checkpoints"] == 0 else "unnamed"
        else:
            name, classification = repo_root.name, "legacy_unclassified"
        document_id = uuid.uuid4().hex
        with self._db() as db:
            for row in db.execute("SELECT id, store_path FROM documents WHERE project_id=?", (project_id,)):
                if Path(row["store_path"]) == repo_root:
                    raise conflict("HISTORY_ALREADY_ATTACHED",
                                   f"This history is already attached as document {row['id']}.",
                                   "Use that document id.")
            db.execute("INSERT INTO documents(id,project_id,name,store_path,origin,observed_identity,"
                       "read_only,recording_allowed,predecessor_id,classification,created_at)"
                       " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       (document_id, project["id"], name, str(repo_root), "imported_history",
                        _json({"filenames": sorted(names)}), int(read_only), int(not read_only),
                        None, classification, now_iso()))
        return self.get_document(document_id)

    @_history_topology
    def add_live_document(self, project_id, name, observed_identity: dict, *, predecessor_id=None):
        """Create a fresh per-document v1 store under the project's history root (M2)."""
        project = self.get_project(project_id)
        if not isinstance(name, str) or not name.strip():
            raise bad_request("Document name is empty.")
        document_id = uuid.uuid4().hex
        store = Path(project["history_root"]) / document_id
        Repository.init(store, services=self.services)
        with self._db() as db:
            db.execute("INSERT INTO documents(id,project_id,name,store_path,origin,observed_identity,"
                       "read_only,recording_allowed,predecessor_id,classification,created_at)"
                       " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       (document_id, project["id"], name.strip(), str(store), "live",
                        _json(observed_identity), 0, 1, predecessor_id, None, now_iso()))
        return self.get_document(document_id)

    def get_document(self, document_id, project_id=None):
        with self._db() as db:
            row = db.execute("SELECT * FROM documents WHERE id=?", (document_id,)).fetchone()
        doc = _row(row)
        if doc is None or (project_id is not None and doc["project_id"] != project_id):
            raise not_found("Document")
        return doc

    def list_documents(self, project_id):
        self.get_project(project_id)
        with self._db() as db:
            rows = db.execute("SELECT * FROM documents WHERE project_id=? ORDER BY ordinal", (project_id,)).fetchall()
        return [_row(r) for r in rows]

    def find_live_document(self, project_id, observed_identity: dict):
        key = _json(observed_identity)
        with self._db() as db:
            row = db.execute("SELECT * FROM documents WHERE project_id=? AND origin='live' AND observed_identity=?"
                             " ORDER BY ordinal DESC LIMIT 1", (project_id, key)).fetchone()
        return _row(row)

    # ----------------------------------------------------------- capture runs --
    def start_run(self, project_id, document_id, session_instance: dict, coverage: dict):
        run_id = uuid.uuid4().hex
        with self._db() as db:
            db.execute("INSERT INTO capture_runs(id,project_id,document_id,service_instance_id,session_instance,"
                       "started_at,ended_at,status,coverage,notes) VALUES(?,?,?,?,?,?,NULL,'running',?,'[]')",
                       (run_id, project_id, document_id, self.state.instance_id, _json(session_instance),
                        now_iso(), _json(coverage)))
        return self.get_run(run_id)

    def end_run(self, run_id, status, note=None, coverage=None):
        with self._db() as db:
            row = db.execute("SELECT * FROM capture_runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise not_found("Capture run")
            notes = json.loads(row["notes"])
            if note:
                notes.append({"at": now_iso(), "note": str(note)[:2000]})
            db.execute("UPDATE capture_runs SET status=?, ended_at=coalesce(ended_at,?), notes=?, coverage=?"
                       " WHERE id=?",
                       (status, now_iso(), _json(notes),
                        _json(coverage) if coverage is not None else row["coverage"], run_id))
        return self.get_run(run_id)

    def get_run(self, run_id):
        with self._db() as db:
            row = db.execute("SELECT * FROM capture_runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise not_found("Capture run")
        return _row(row)

    def list_runs(self, document_id, limit=20):
        with self._db() as db:
            rows = db.execute("SELECT * FROM capture_runs WHERE document_id=? ORDER BY ordinal DESC LIMIT ?",
                              (document_id, limit)).fetchall()
        return [_row(r) for r in rows]

    def orphan_runs(self):
        """Runs left 'running' by a previous service instance: ours to close, honestly."""
        with self._db() as db:
            rows = db.execute("SELECT * FROM capture_runs WHERE status='running' AND service_instance_id<>?",
                              (self.state.instance_id,)).fetchall()
        return [_row(r) for r in rows]

    # ------------------------------------------------------------------- jobs --
    def create_job(self, project_id, kind, target_type, target_id, payload: dict, *,
                   request_key=None, external=False):
        """Idempotent creation: same (project, key, payload) returns the existing job."""
        if request_key is not None and (not isinstance(request_key, str) or not 1 <= len(request_key) <= 128):
            raise bad_request("X-Request-ID must be 1-128 characters.")
        digest = fingerprint({"kind": kind, "target_type": target_type, "target_id": target_id, "payload": payload})
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            if request_key is not None:
                existing = db.execute("SELECT * FROM request_keys WHERE scope=? AND request_key=?",
                                      (project_id, request_key)).fetchone()
                if existing is not None:
                    if existing["fingerprint"] != digest:
                        raise conflict("IDEMPOTENCY_CONFLICT",
                                       "This request id was already used with a different request.",
                                       "Generate a new request id for a new action.")
                    row = db.execute("SELECT * FROM jobs WHERE id=?", (existing["job_id"],)).fetchone()
                    return _row(row), False
            job_id = uuid.uuid4().hex
            db.execute("INSERT INTO jobs(id,project_id,kind,target_type,target_id,payload,fingerprint,request_key,"
                       "status,progress,result,error,asset_path,external,service_instance_id,created_at)"
                       " VALUES(?,?,?,?,?,?,?,?,'queued',NULL,NULL,NULL,NULL,?,?,?)",
                       (job_id, project_id, kind, target_type, target_id, _json(payload), digest,
                        request_key, int(external), self.state.instance_id, now_iso()))
            if request_key is not None:
                db.execute("INSERT INTO request_keys(scope,request_key,fingerprint,job_id) VALUES(?,?,?,?)",
                           (project_id, request_key, digest, job_id))
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            return _row(row), True

    def get_job(self, job_id, project_id=None):
        with self._db() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        job = _row(row)
        if job is None or (project_id is not None and job["project_id"] != project_id):
            raise not_found("Job")
        return job

    def update_job(self, job_id, status=None, *, progress=None, result=None, error=None, asset_path=None):
        if status is not None and status not in JOB_STATES:
            raise bad_request(f"Invalid job status {status}.")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise not_found("Job")
            fields, values = [], []
            if status is not None:
                fields.append("status=?"); values.append(status)
                if status == "running":
                    fields.append("started_at=coalesce(started_at,?)"); values.append(now_iso())
                elif status in ("succeeded", "failed", "interrupted", "unknown"):
                    fields.append("ended_at=coalesce(ended_at,?)"); values.append(now_iso())
            if progress is not None:
                fields.append("progress=?"); values.append(float(progress))
            if result is not None:
                fields.append("result=?"); values.append(_json(result))
            if error is not None:
                fields.append("error=?"); values.append(_json(error))
            if asset_path is not None:
                fields.append("asset_path=?"); values.append(str(asset_path))
            if fields:
                values.append(job_id)
                db.execute(f"UPDATE jobs SET {', '.join(fields)} WHERE id=?", values)
        return self.get_job(job_id)

    def settle_orphan_jobs(self):
        """Jobs from a previous instance: external ones become unknown, the rest interrupted."""
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT id, external, status FROM jobs WHERE status IN ('queued','running')"
                              " AND service_instance_id<>?", (self.state.instance_id,)).fetchall()
            for row in rows:
                status = "unknown" if (row["external"] and row["status"] == "running") else "interrupted"
                db.execute("UPDATE jobs SET status=?, ended_at=?, error=? WHERE id=?",
                           (status, now_iso(),
                            _json({"code": "SERVICE_RESTARTED",
                                   "message": "The service restarted before this task finished."}), row["id"]))
        return len(rows)

    def list_jobs(self, project_id, limit=50):
        with self._db() as db:
            rows = db.execute("SELECT * FROM jobs WHERE project_id=? ORDER BY ordinal DESC LIMIT ?",
                              (project_id, limit)).fetchall()
        return [_row(r) for r in rows]

    # ------------------------------------------------------------ annotations --
    def add_annotation(self, document_id, target_type, target_id, text, request_key=None):
        """Idempotent per (document, request_key): a retried or double-clicked note is stored once."""
        if request_key is not None and (not isinstance(request_key, str) or not 1 <= len(request_key) <= 128):
            raise bad_request("X-Request-ID must be 1-128 characters.")
        if target_type not in ("checkpoint", "segment"):
            raise bad_request("target_type must be checkpoint or segment.")
        if not isinstance(text, str) or not text.strip():
            raise bad_request("Annotation text is empty.")
        if len(text) > 4000:
            raise bad_request("Annotation text exceeds 4000 characters.")
        if not isinstance(target_id, str) or not target_id:
            raise bad_request("target_id is required.")
        annotation_id = uuid.uuid4().hex
        with self._db() as db:
            # One writer at a time for the key check + insert: two concurrent retries with the same
            # key otherwise race into a bare IntegrityError (500) instead of the idempotent answer.
            db.execute("BEGIN IMMEDIATE")
            if request_key is not None:
                existing = db.execute("SELECT annotation_id FROM annotation_keys WHERE document_id=? AND request_key=?",
                                      (document_id, request_key)).fetchone()
                if existing is not None:
                    row = db.execute("SELECT * FROM annotations WHERE id=?", (existing["annotation_id"],)).fetchone()
                    if row is None or (row["target_type"], row["target_id"], row["text"]) != (target_type, target_id, text):
                        from .errors import ServiceError
                        raise ServiceError("REQUEST_KEY_REUSED",
                                           "This request id was already used for a different annotation.",
                                           status=409, next_action="Send the new annotation with a new X-Request-ID.")
                    return _row(row)
            db.execute("INSERT INTO annotations(id,document_id,target_type,target_id,text,created_at)"
                       " VALUES(?,?,?,?,?,?)",
                       (annotation_id, document_id, target_type, target_id, text, now_iso()))
            if request_key is not None:
                db.execute("INSERT INTO annotation_keys(document_id,request_key,annotation_id) VALUES(?,?,?)",
                           (document_id, request_key, annotation_id))
            row = db.execute("SELECT * FROM annotations WHERE id=?", (annotation_id,)).fetchone()
        return _row(row)

    def page_annotations(self, document_id, limit, before=None, upper=None, target_type=None, target_id=None):
        clauses, args = ["document_id=?"], [document_id]
        if target_type is not None:
            clauses.append("target_type=?"); args.append(target_type)
        if target_id is not None:
            clauses.append("target_id=?"); args.append(target_id)
        with self._db() as db:
            if upper is None:
                upper = db.execute("SELECT coalesce(max(ordinal),0) FROM annotations").fetchone()[0]
            clauses.append("ordinal<=?"); args.append(upper)
            if before is not None:
                clauses.append("ordinal<?"); args.append(before)
            rows = db.execute(f"SELECT * FROM annotations WHERE {' AND '.join(clauses)}"
                              " ORDER BY ordinal DESC LIMIT ?", (*args, limit + 1)).fetchall()
        more = len(rows) > limit
        rows = rows[:limit]
        return {"items": [_row(r) for r in rows], "upper": upper,
                "next_before": rows[-1]["ordinal"] if more else None}


def _validate_policy(updates: dict) -> dict:
    out = {}
    if "enabled" in updates:
        if not isinstance(updates["enabled"], bool):
            raise bad_request("enabled must be true or false.")
        out["enabled"] = updates["enabled"]
    if "allow_unsaved" in updates:
        if not isinstance(updates["allow_unsaved"], bool):
            raise bad_request("allow_unsaved must be true or false.")
        out["allow_unsaved"] = updates["allow_unsaved"]
    if "session_selector" in updates:
        sel = updates["session_selector"]
        if not isinstance(sel, dict) or sel.get("mode") not in ("single_eligible", "pinned"):
            raise bad_request("session_selector.mode must be single_eligible or pinned.")
        if sel["mode"] == "pinned":
            sid = sel.get("session_id")
            if not isinstance(sid, str) or not sid or len(sid) > 200:
                raise bad_request("pinned session_selector needs a session_id from /sessions.")
            out["session_selector"] = {"mode": "pinned", "session_id": sid}
        else:
            out["session_selector"] = {"mode": "single_eligible"}
    if "intervals" in updates:
        iv = updates["intervals"]
        if not isinstance(iv, dict):
            raise bad_request("intervals must be an object.")
        merged = dict(DEFAULT_POLICY["intervals"])
        for key in ("idle_seconds", "min_interval", "max_interval"):
            if key in iv:
                value = iv[key]
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not 1 <= value <= 3600:
                    raise bad_request(f"intervals.{key} must be a number between 1 and 3600 seconds.")
                merged[key] = float(value)
        if merged["max_interval"] < merged["min_interval"]:
            raise bad_request("intervals.max_interval must be at least min_interval.")
        out["intervals"] = merged
    return out


def _observed_filenames(repo: Repository) -> set:
    """Distinct document filenames recorded by the observer, from a bounded scan."""
    names, before = set(), None
    for _ in range(50):  # at most 50 pages x 200 rows; enough to classify
        page = repo.page_checkpoints(limit=200, before=before)
        for item in page["items"]:
            document = (item.get("metadata") or {}).get("document")
            if isinstance(document, dict) and isinstance(document.get("filename"), str) and document["filename"]:
                names.add(document["filename"])
            elif not isinstance(document, dict) and item.get("source") == "manual":
                names.add(item.get("filename") or "")
        before = page["next_before"]
        if before is None:
            break
    names.discard("")
    return names
