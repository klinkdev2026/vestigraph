"""Standalone service/import regressions for installs without KLink."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys


def test_prepare_state_starts_without_klink_in_isolated_subprocess(tmp_path):
    root = Path(__file__).resolve().parents[1]
    script = r"""
import json
import sys

class BlockKlink:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "klink" or fullname.startswith("klink."):
            raise ImportError("klink intentionally blocked")
        return None

sys.meta_path.insert(0, BlockKlink())
from vestigraph.web.app import prepare_state

app = prepare_state(sys.argv[1])
try:
    print(json.dumps(app.status()["capabilities"], sort_keys=True))
finally:
    app.stop()
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(root) + os.pathsep + env.get("PYTHONPATH", "")
    completed = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "state")],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    capabilities = json.loads(completed.stdout)
    assert capabilities["web"] is True
    assert capabilities["autocapture_dependency"] is False
    assert capabilities["autocapture"] is False


class _BlockKlink:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "klink" or fullname.startswith("klink."):
            raise ImportError("klink intentionally blocked")
        return None


def test_http_ui_and_attached_history_work_without_klink(tmp_path, monkeypatch):
    import sys
    from tests.service_helpers import WebSession, synthetic_repo
    from vestigraph.web.app import create_app, prepare_state
    from vestigraph.web.auth import AuthManager

    for name in list(sys.modules):
        if name == "klink" or name.startswith("klink."):
            sys.modules.pop(name)
    sys.meta_path.insert(0, _BlockKlink())
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
        assert status["capabilities"]["autocapture_dependency"] is False
        docs = session.get(f"/api/v1/projects/{project['id']}/documents").json()["data"]["items"]
        assert [item["id"] for item in docs] == [document["id"]]
        checkpoints = session.get(f"/api/v1/documents/{document['id']}/checkpoints").json()["data"]["items"]
        assert len(checkpoints) == 1
    finally:
        application.stop()
        try:
            sys.meta_path.remove(next(finder for finder in sys.meta_path if isinstance(finder, _BlockKlink)))
        except StopIteration:
            pass
