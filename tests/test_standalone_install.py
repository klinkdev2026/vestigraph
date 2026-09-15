"""Installed default package contract and service/import regressions."""
from __future__ import annotations

import tomllib
from pathlib import Path


def _pyproject() -> dict:
    return tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8"))


def test_default_install_requires_klink_and_keeps_extra_compatibility():
    project = _pyproject()["project"]
    dependencies = project["dependencies"]
    assert any(req == "klayout-klink>=0.6.0,<0.7" for req in dependencies)
    assert any(req == "klayout>=0.30,<0.31" for req in dependencies)

    optional = _pyproject()["project"]["optional-dependencies"]
    assert optional["klink"] == ["klayout-klink>=0.6.0,<0.7"]


def test_http_ui_and_attached_history_work_in_default_install(tmp_path, monkeypatch):
    from tests.service_helpers import WebSession, synthetic_repo
    from vestigraph.web.app import create_app, prepare_state
    from vestigraph.web.auth import AuthManager

    monkeypatch.setenv("VESTIGRAPH_HOME", str(tmp_path / "home"))

    application = prepare_state(tmp_path / "state")
    try:
        project = application.catalog.list_projects()[0]
        repo = synthetic_repo(tmp_path / "legacy", checkpoints=1, events=1)
        document = application.catalog.add_history(project["id"], repo.root)
        auth = AuthManager()
        session = WebSession(create_app(application, port=8787, auth=auth), auth)

        assert session.get("/").status_code == 200
        session.login()
        status = session.get("/api/v1/status").json()["data"]
        assert status["capabilities"]["web"] is True
        docs = session.get(f"/api/v1/projects/{project['id']}/documents").json()["data"]["items"]
        assert [item["id"] for item in docs] == [document["id"]]
        checkpoints = session.get(f"/api/v1/documents/{document['id']}/checkpoints").json()["data"]["items"]
        assert len(checkpoints) == 1
    finally:
        application.stop()
