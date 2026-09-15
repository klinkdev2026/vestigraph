"""Synthetic local skill workflow, including real loopback HTTP through KLink."""
import hashlib
import json
from pathlib import Path
import socket
import types
import threading
import time
import zipfile
import pytest
from tests.fixtures_legacy_imports import service, history, source, put
from vestigraph.agent_client import call
from vestigraph.klink_extension import register
from vestigraph.vesti_skills.catalog import SkillCatalog


def request_for(service, tmp_path):
    app, session, doc, repo, final = service
    older = put(repo, source(tmp_path, "older.gds", 1), final["id"])
    return dict(document_id=doc["id"], from_id=older["id"], to_id=final["id"],
                history_revision=repo.history_revision(), title="Synthetic procedure", goal="Preserve the source hierarchy")


def invoke(session, name, arguments):
    return session.post("/api/v1/agent/invoke", json={"name": name, "arguments": arguments})


def test_guide_never_returns_done_for_ambiguous_projects(service, tmp_path):
    app, session, doc, repo, final = service
    workspace = tmp_path / "second-workspace"
    workspace.mkdir()
    app.catalog.add_project("Second synthetic project", workspace, tmp_path / "second-history", allow_inside_git=True)
    guide = invoke(session, "guide", {}).json()["data"]
    assert guide["next_action"] != "done"
    assert guide["problems"] == ["Choose the user's project from projects; do not guess."]


def test_local_skill_workflow_atomic_retries_export_restart(service, tmp_path):
    app, session, doc, repo, final = service
    args = request_for(service, tmp_path)
    original = repo.counts()
    guide = invoke(session, "guide", {}).json()["data"]
    assert guide["next_action"]["tool"] == "vestigraph.guide"
    versions = invoke(session, "history", {"document_id": doc["id"]})
    assert versions.status_code == 200, versions.text
    history_action = versions.json()["data"]["next_action"]
    assert history_action["history_revision_source"] == "history.history_revision"
    assert "do not invent" in history_action["required_before_call"]
    for _ in range(2):
        response = invoke(session, "refine", args)
        assert response.status_code == 200, response.text
        saved = response.json()["data"]["skill"]
        assert saved["revision"] == 1
    sid = saved["id"]
    assert len(app.skills.list(doc["project_id"])["items"]) == 1
    pending = invoke(session, "guide", {"project_id": doc["project_id"]}).json()["data"]
    assert pending["next_action"] == {"tool": "vestigraph.skill", "arguments": {"skill_id": sid}}
    selected = invoke(session, "skill", {"skill_id": sid}).json()["data"]
    assert selected["next_action"]["tool"] == "vestigraph.submit"
    assert "body" not in selected["next_action"]["arguments"]
    placeholder = invoke(session, "submit", {"skill_id": sid, "expected_revision": 1,
                                             "body": "Replace with instructions derived from the selected evidence and user explanation."})
    assert placeholder.status_code == 400
    assert placeholder.json()["error"]["next_action"]["tool"] == "vestigraph.submit"
    assert app.skills.get(sid) == saved
    body = dict(skill_id=sid, expected_revision=1, body="# Procedure\nPreserve source and insert references.",
                files={"scripts/replay.py": "raise RuntimeError('must never run')"}, validation_note="Only synthetic structure reviewed.")
    invalid = invoke(session, "submit", {**body, "files": {"scripts/../private": "invalid"}})
    assert invalid.status_code == 400
    assert app.skills.get(sid) == saved
    result = invoke(session, "submit", body)
    assert result.status_code == 200, result.text
    checked = result.json()["data"]
    assert checked["state"] == "draft" and checked["revision"] == 2
    assert checked["verification"]["reports"][0]["scope"] == "document_structure"
    assert "No execution, replay, domain-rule or outcome validation." in checked["verification"]["reports"][0]["limitations"]
    assert checked["verification"]["reports"][1]["scope"] == "author_statement"
    conflict = invoke(session, "submit", body)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["next_action"]["tool"] == "vestigraph.skill"
    assert app.skills.get(sid)["revision"] == 2
    assert invoke(session, "submit", {**body, "expected_revision": 2}).status_code == 200
    data, _, _ = app.skills.export(sid, "agent-skill", 3)
    assert data == app.skills.export(sid, "agent-skill", 3)[0]
    digest = hashlib.sha256(data).hexdigest()
    folder = app.state.root / "exports"
    folder.mkdir(parents=True, exist_ok=True)
    collision = folder / (sid + "-r3-" + digest + ".zip")
    collision.write_bytes(b"wrong")
    failed_export = invoke(session, "export", {"skill_id": sid, "expected_revision": 3})
    assert failed_export.status_code == 400
    assert not list(folder.glob("export-*"))
    collision.unlink()
    paths = []
    for _ in range(2):
        exported = invoke(session, "export", {"skill_id": sid, "expected_revision": 3})
        assert exported.status_code == 200, exported.text
        artifact = exported.json()["data"]
        paths.append(artifact["path"])
        assert hashlib.sha256(Path(artifact["path"]).read_bytes()).hexdigest() == artifact["sha256"]
    assert paths[0] == paths[1]
    with zipfile.ZipFile(paths[0]) as archive:
        assert any(n.endswith("/SKILL.md") for n in archive.namelist())
    assert SkillCatalog(app.catalog).get(sid)["sources"] == saved["sources"]
    assert repo.counts() == original


def test_skill_auth_disabled_scope_and_typed_inputs(service, tmp_path, monkeypatch):
    app, session, doc, repo, final = service
    args = request_for(service, tmp_path)
    assert session.client.post("/api/v1/agent/invoke", json={"name": "refine", "arguments": args}).status_code == 403
    assert invoke(session, "refine", {**args, "history_revision": True}).status_code == 400
    assert app.skills.list(doc["project_id"])["items"] == []
    app.experimental_skills = False
    assert invoke(session, "refine", args).status_code == 409
    assert invoke(session, "guide", {}).status_code == 200
    app.experimental_skills = True
    monkeypatch.setattr("vestigraph.web.routes.scope", lambda request: set())
    assert invoke(session, "history", {"document_id": doc["id"]}).status_code == 404


def test_skill_real_http_extension_discovery_and_call(service, tmp_path, monkeypatch):
    import uvicorn
    from klink.mcp.bridge import KLinkMCPBridge
    from klink import ext
    from vestigraph.web.app import create_app
    app, session, doc, repo, final = service
    args = request_for(service, tmp_path)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    web = create_app(app, port=port)
    web.state.control_secret = "synthetic-test-secret"
    control = tmp_path / "control.json"
    control.write_text(json.dumps({"port": port, "secret": web.state.control_secret}), encoding="utf-8")
    monkeypatch.setenv("VESTIGRAPH_CONTROL_FILE", str(control))
    monkeypatch.setenv("HTTP_PROXY", "http://invalid.invalid:1")
    server = uvicorn.Server(uvicorn.Config(web, log_level="critical"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        class InstalledVestigraphEP:
            name = "vestigraph"
            value = "vestigraph.klink_extension:register"
            dist = types.SimpleNamespace(metadata={"Name": "vestigraph"})

            def load(self):
                return register

        def fake_entry_points(group=None):
            assert group == ext.ENTRY_POINT_GROUP
            return [InstalledVestigraphEP()]

        monkeypatch.setattr("importlib.metadata.entry_points", fake_entry_points)
        registry = ext.discover(force=True)
        assert "vestigraph.guide" in registry.tools
        bridge = KLinkMCPBridge(context_root=tmp_path / "context", registry_root=tmp_path / "registry")
        assert "extensions" in bridge.status()
        assert "vestigraph.refine" in {t["name"] for t in bridge.list_tools()["tools"]}
        discovered = json.loads(bridge.call_tool("klink.find_tools", {"domain": "vestigraph"})["content"][0]["text"])
        assert any(t["name"] == "vestigraph.submit" for t in discovered["tools"])
        for _ in range(2):
            result = json.loads(bridge.call_tool("vestigraph.refine", args)["content"][0]["text"])
            assert "skill" in result, result
        sid = result["skill"]["id"]
        for revision in (1, 2):
            result = call("submit", {"skill_id": sid, "expected_revision": revision, "body": "# Synthetic instructions"})
            assert result.get("revision") == revision + 1, result
        assert "synthetic-test-secret" not in json.dumps(result)
        assert not web.state.auth._sessions
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()


def test_skill_transport_rejects_remote_control_and_invalid_input(tmp_path, monkeypatch):
    control = tmp_path / "control.json"
    control.write_text(json.dumps({"port": 80, "host": "192.0.2.1", "secret": "test"}), encoding="utf-8")
    monkeypatch.setenv("VESTIGRAPH_CONTROL_FILE", str(control))
    failed = call("guide", {})
    assert failed["ok"] is False
    assert "vestigraph setup" not in failed["next_action"]
    assert "vestigraph serve" not in failed["next_action"]
    assert "KLINK_REGISTRY_ROOT" in failed["next_action"]
    assert call("submit", {"skill_id": "test", "expected_revision": True, "body": "text"})["ok"] is False


def test_installed_public_synthetic_agent_flow_check(tmp_path):
    import importlib.util
    script = Path(__file__).resolve().parents[1] / "tools" / "check_agent_flow.py"
    spec = importlib.util.spec_from_file_location("check_agent_flow", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = module.run(tmp_path / "installed-flow")
    assert result["ok"] is True
    assert result["submitted_revision"] == 2
    assert result["export_sha256"]
    assert result["next_actions"][-1] == "done"
