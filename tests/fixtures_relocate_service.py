"""Project history relocation: refusals, a full relocate-while-recording job,
a verify-failure rollback, and the HTTP PUT endpoint.

Modeled on scratch/relocate_smoke.py (a working manual script) turned into
assertions; real Coordinator/JobRunner/catalog, fake KLink endpoint only.
"""


from __future__ import annotations


from pathlib import Path


import pytest


from tests.autocapture_fakes import FakeEndpoint, FakeRegistry, make_client_factory, wait_for


from tests.service_helpers import init_state, project_dirs


from vestigraph.service import application as application_module


from vestigraph.service.application import Application


from vestigraph.service.errors import ServiceError


from vestigraph.store import Repository


FAST = {"document_poll_s": 0.05, "session_poll_s": 0.05}


def _wait_status(app, project_id, predicate, timeout=20.0):
    holder = {}

    def check():
        holder["last"] = status = app.supervisor.project_status(project_id)
        return status if predicate(status) else None

    return wait_for(check, timeout=timeout, status_fn=lambda: holder.get("last"))


@pytest.fixture
def recording_stack(tmp_path):
    """A project recording a live document; the fixture leaves it in state 'recording'."""
    registry, endpoint = FakeRegistry(), FakeEndpoint("127.0.0.1", 8765)
    ws, hist = project_dirs(tmp_path)
    layout = ws / "chip.gds"
    layout.write_bytes(b"x")
    endpoint.filename = str(layout)
    registry.add("klayout-8765", port=8765)
    init_state(tmp_path / "state")
    app = Application.open(tmp_path / "state", registry=registry, client_factory=make_client_factory({8765: endpoint}),
                           autocapture=True, coordinator_options=FAST).start()
    project = app.catalog.add_project("p", ws, hist, allow_inside_git=True)
    app.catalog.set_policy(project["id"], {"intervals": {"idle_seconds": 1, "min_interval": 1, "max_interval": 2}})
    app.supervisor.on_project_added(app.catalog.get_project(project["id"]))
    _wait_status(app, project["id"], lambda s: s["state"] == "recording", timeout=15)
    try:
        yield {"app": app, "project": project, "endpoint": endpoint, "workspace": ws, "history": hist,
              "state_dir": tmp_path / "state", "tmp_path": tmp_path}
    finally:
        app.stop()


__all__ = [name for name in globals() if not name.startswith("__")]
