"""Shared synthetic fixtures for service/web tests. No real layouts, no network."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

from vestigraph.service.application import Application
from vestigraph.service.catalog import Catalog
from vestigraph.service.state import ServiceState
from vestigraph.store import Repository


def init_state(root: Path) -> ServiceState:
    state = ServiceState.init(root)
    Catalog.init(state)
    return state


def synthetic_repo(root: Path, *, checkpoints=2, events=3, document_name="缂栧彿 test.gds", capture=True) -> Repository:
    """A v1 history with segments, events and checkpoints made from synthetic bytes."""
    repo = Repository.init(root)
    source = root.parent / f"src-{root.name}.bin"
    document_path = root.parent / "workspace" / document_name
    for index in range(checkpoints):
        segment = repo.begin_segment(f"娈?{index}", source="mixed", metadata={"capture": "klink"} if capture else None)
        for e in range(events):
            repo.append_event("shapes_changed", {"count": e, "cell": "TOP"}, source="unknown", segment_id=segment["id"])
        source.write_bytes(f"synthetic layout {index}\n".encode() * (10 + index))
        metadata = {"capture": "klink", "format": "GDS2", "document": {"filename": str(document_path)}} if capture else None
        repo.checkpoint(source, title=f"鐗堟湰 {index}", source="system" if capture else "manual",
                        segment_id=segment["id"], metadata=metadata)
        repo.close_segment(segment["id"])
    source.unlink(missing_ok=True)
    return repo


def bulk_events(repo: Repository, segment_id: str, count: int, kind="e") -> None:
    """Fixture-only bulk insert (one transaction) so 10k-row read tests stay fast.

    Uses the store's own schema through its writer lease; the read path under
    test is the real Repository/queries code.
    """
    import sqlite3
    from vestigraph.store import _now
    repo.acquire_writer("test-fixture")
    try:
        with sqlite3.connect(repo.database, timeout=30) as db:
            db.executemany("INSERT INTO events(created_at,kind,source,segment_id,payload) VALUES(?,?,?,?,?)",
                           [(_now(), kind, "unknown", segment_id, '{"n":%d}' % n) for n in range(count)])
        db.close()
    finally:
        repo.release_writer()


def tree_hashes(root: Path) -> dict:
    """sha256 of every file except SQLite's own transient -wal/-shm sidecars."""
    out = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.name.endswith(("-wal", "-shm")):
            out[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def project_dirs(tmp_path: Path, name="p"):
    workspace = tmp_path / f"ws {name}"
    history = tmp_path / f"hist {name}"
    workspace.mkdir(parents=True, exist_ok=True)
    return workspace, history


def open_application(state_root: Path, *, acquire_instance=True, **options) -> Application:
    """Tests never touch a real KLayout: autocapture is off unless a fake registry is given."""
    options.setdefault("autocapture", "registry" in options)
    return Application.open(state_root, acquire_instance=acquire_instance, **options).start()


class WebSession:
    """TestClient wrapper that performs bootstrap and carries CSRF/Origin for writes."""

    def __init__(self, app, auth, base="http://127.0.0.1:8787"):
        from fastapi.testclient import TestClient
        self.base = base
        self.origin = base
        self.client = TestClient(app, base_url=base)
        self.auth = auth
        self.csrf = None

    def login(self):
        token = self.auth.issue_bootstrap()
        response = self.client.post("/api/v1/auth/bootstrap",
                                    headers={"Authorization": f"Bearer {token}", "Origin": self.origin})
        assert response.status_code == 200, response.text
        self.csrf = response.json()["data"]["csrf_token"]
        return response.json()["data"]

    def get(self, path, **kw):
        return self.client.get(path, **kw)

    def write_headers(self, request_id=None, **extra):
        headers = {"Origin": self.origin, "X-CSRF-Token": self.csrf or ""}
        if request_id:
            headers["X-Request-ID"] = request_id
        headers.update(extra)
        return headers

    def post(self, path, json=None, request_id=None, **kw):
        return self.client.post(path, json=json, headers=self.write_headers(request_id, **kw.pop("headers", {})), **kw)

    def put(self, path, json=None, **kw):
        return self.client.put(path, json=json, headers=self.write_headers(**kw.pop("headers", {})), **kw)


def env_without_pytest():
    env = dict(os.environ)
    env.pop("PYTEST_CURRENT_TEST", None)
    return env
