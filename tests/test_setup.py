"""Installation commands must fail before publishing a broken companion registration."""
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest
from vestigraph_backends.vesti_backend_klayout import installation
from vestigraph.cli import main
from vestigraph_backends.vesti_backend_klayout import companion


def test_setup_rejects_missing_dependencies_before_mutation(monkeypatch):
    monkeypatch.setattr(installation, "_required_for_setup", lambda: [{"package": "klayout-klink", "ok": False}])
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("Must not install plugin"))
    monkeypatch.setattr(companion, "register", lambda **k: pytest.fail("Must not register"))
    with pytest.raises(RuntimeError, match="klayout-klink"):
        installation.setup()


def test_setup_plugin_failure_does_not_register(monkeypatch):
    monkeypatch.setattr(installation, "_required_for_setup", lambda: [{"package": "klayout-klink", "ok": True}])
    monkeypatch.setattr(companion, "python_has_vestigraph", lambda *a: True)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1, stderr="existing foreign plugin", stdout=""))
    monkeypatch.setattr(companion, "register", lambda **k: pytest.fail("Must not register after failure"))
    with pytest.raises(RuntimeError, match="existing foreign plugin"):
        installation.setup()


def test_setup_success_points_to_integration_doctor(monkeypatch):
    monkeypatch.setattr(installation, "_required_for_setup", lambda: [{"package": "klayout-klink", "ok": True}])
    monkeypatch.setattr(companion, "python_has_vestigraph", lambda *a: True)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stderr="", stdout=""))
    monkeypatch.setattr(companion, "register", lambda **k: {"registered": True})

    result = installation.setup()

    assert result["ok"] is True
    assert "Restart KLayout" in result["next_action"]
    assert "HIST" in result["next_action"]
    assert "doctor --integration" in result["next_action"]


@pytest.mark.parametrize(
    ("version", "ok"),
    [("0.5.9", False), ("0.6.0", True), ("0.6.1", True), ("0.7.0", False)],
)
def test_klink_version_gate_accepts_only_0_6_series(monkeypatch, version, ok):
    monkeypatch.setattr(installation.importlib, "import_module", lambda module: object())
    monkeypatch.setattr(installation.metadata, "version", lambda distribution: version)

    result = installation._check("klayout-klink", "klink")

    assert result["ok"] is ok


@pytest.mark.parametrize(
    ("version", "ok"),
    [("0.1.9", False), ("0.2.0", True), ("0.2.5", True), ("0.3.0", False)],
)
def test_scan_core_version_gate_accepts_only_0_2_series(monkeypatch, version, ok):
    monkeypatch.setattr(installation.importlib, "import_module", lambda module: object())
    monkeypatch.setattr(installation.metadata, "version", lambda distribution: version)

    result = installation._check("vestigraph-scan-core", "vestigraph_scan_core")

    assert result["ok"] is ok


def test_doctor_default_requires_klink_but_does_not_inspect_registration(monkeypatch):
    monkeypatch.setattr(installation, "dependencies", lambda: [
        {"package": "fastapi", "ok": True},
        {"package": "uvicorn", "ok": True},
        {"package": "klayout", "ok": True},
        {"package": "vestigraph-scan-core", "ok": True},
        {"package": "klayout-klink", "ok": True},
    ])
    monkeypatch.setattr(companion, "status", lambda *a, **k: pytest.fail("default doctor must not inspect registration"))

    result = installation.doctor()

    assert result["ok"] is True
    assert result["scope"] == "package_installation"
    assert [check["package"] for check in result["dependencies"]] == ["fastapi", "uvicorn", "klayout", "vestigraph-scan-core", "klayout-klink"]
    assert "klink_optional" not in result
    assert "MCP server" in result["next_action"]
    assert "HIST" in result["next_action"]
    assert "vestigraph setup" not in result["next_action"]
    assert "vestigraph serve" not in result["next_action"]


def test_doctor_default_fails_when_required_klink_is_missing(monkeypatch):
    monkeypatch.setattr(installation, "dependencies", lambda: [
        {"package": "fastapi", "ok": True},
        {"package": "uvicorn", "ok": True},
        {"package": "klayout", "ok": True},
        {"package": "vestigraph-scan-core", "ok": True},
        {"package": "klayout-klink", "ok": False, "problem": "blocked in test"},
    ])
    monkeypatch.setattr(companion, "status", lambda *a, **k: pytest.fail("default doctor must not inspect registration"))

    result = installation.doctor()

    assert result["ok"] is False
    assert result["scope"] == "package_installation"
    assert "vestigraph setup" not in result["next_action"]
    assert "vestigraph serve" not in result["next_action"]


@pytest.mark.parametrize("port", [0, -1, 65536, True])
def test_setup_rejects_invalid_port(port):
    with pytest.raises(ValueError):
        installation.setup(port=port)


def test_doctor_failure_has_nonzero_exit(monkeypatch, capsys):
    monkeypatch.setattr(installation, "doctor", lambda: {"ok": False, "scope": "installation_only"})
    assert main(["doctor"]) == 1
    assert '"ok": false' in capsys.readouterr().out


def test_auto_register_current_interpreter_writes_descriptor_only(tmp_path, monkeypatch):
    monkeypatch.setenv("VESTIGRAPH_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("KLINK_REGISTRY_ROOT", str(tmp_path / "registry"))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("auto registration must not install plugins or probe with subprocess"))

    result = companion.ensure_auto_registered()

    assert result["registered"] is True and result["changed"] is True
    descriptor = Path(result["descriptor"])
    spec = json.loads(descriptor.read_text(encoding="utf-8"))
    assert descriptor == tmp_path / "registry" / "companions" / "vestigraph.json"
    assert spec["command"][0] == sys.executable
    assert spec["command"][1:4] == ["-m", "vestigraph", "serve"]
    assert spec["port"] == 8787
    assert spec["control_file"] == str(tmp_path / "home" / "state" / "control.json")
    assert spec["env"] == {"VESTIGRAPH_HOME": str(tmp_path / "home")}


def test_auto_register_preserves_custom_descriptor_and_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("VESTIGRAPH_HOME", str(tmp_path / "home"))
    root = tmp_path / "registry"
    path = root / "companions" / "vestigraph.json"
    path.parent.mkdir(parents=True)
    custom_state = tmp_path / "custom-state"
    existing = {
        "name": "vestigraph",
        "label": "VG",
        "title": "Custom title",
        "command": ["old-python", "-m", "vestigraph", "serve", "--state", str(custom_state), "--port", "{port}", "--control-file", "--exit-when-idle", "12.5", "--no-zero-config"],
        "port": 9123,
        "control_file": str(tmp_path / "custom-control" / "control.json"),
        "health_path": "/ready",
        "link_path": "/link",
        "autostart": False,
        "cwd": str(tmp_path / "custom-cwd"),
        "log_file": str(tmp_path / "custom-log" / "service.log"),
        "env": {"CUSTOM": "1"},
    }
    path.write_text(json.dumps(existing), encoding="utf-8")

    result = companion.ensure_auto_registered(root=root)

    assert result["registered"] is True and result["changed"] is True
    spec = json.loads(path.read_text(encoding="utf-8"))
    for key in ("label", "title", "port", "control_file", "health_path", "link_path", "autostart", "cwd", "log_file", "env"):
        assert spec[key] == existing[key]
    assert spec["command"] == [sys.executable, "-m", "vestigraph", "serve", *existing["command"][4:]]

    def forbidden_write(*args, **kwargs):
        raise AssertionError("same descriptor must not be rewritten")
    monkeypatch.setattr(companion, "_write_json_atomic", forbidden_write)
    second = companion.ensure_auto_registered(root=root)
    assert second["registered"] is True and second["changed"] is False


def test_auto_register_invalid_descriptor_is_discoverable_but_not_overwritten(tmp_path):
    root = tmp_path / "registry"
    path = root / "companions" / "vestigraph.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not-json", encoding="utf-8")

    result = companion.ensure_auto_registered(root=root)

    assert result["registered"] is False
    assert result["changed"] is False
    assert "descriptor unreadable" in result["problem"]
    assert path.read_text(encoding="utf-8") == "{not-json"


def test_auto_register_unknown_existing_command_is_discoverable_but_not_overwritten(tmp_path):
    root = tmp_path / "registry"
    path = root / "companions" / "vestigraph.json"
    path.parent.mkdir(parents=True)
    existing = {
        "name": "vestigraph",
        "command": ["old-python", "-m", "other", "serve"],
        "port": 8787,
        "control_file": str(tmp_path / "state" / "control.json"),
        "cwd": str(tmp_path / "home"),
        "log_file": str(tmp_path / "home" / "logs" / "service.log"),
        "env": {},
    }
    path.write_text(json.dumps(existing), encoding="utf-8")

    result = companion.ensure_auto_registered(root=root)

    assert result["registered"] is False
    assert result["changed"] is False
    assert "not a supported" in result["problem"]
    assert json.loads(path.read_text(encoding="utf-8")) == existing


def test_klink_handler_uses_context_registry_root(monkeypatch, tmp_path):
    from vestigraph import agent_client, klink_extension
    from vestigraph_backends.vesti_backend_klayout import companion as companion_module

    class Hook:
        def __init__(self):
            self.domains = {}
            self.tools = {}
        def add_domain(self, token, **meta):
            self.domains[token] = meta
        def add_tool(self, name, handler, **meta):
            self.tools[name] = {"handler": handler, **meta}

    ensured = []
    calls = []
    monkeypatch.setattr(klink_extension, "_ensure_companion_descriptor", lambda: {"registered": True, "changed": False})
    monkeypatch.setattr(companion_module, "ensure_auto_registered", lambda **kwargs: ensured.append(kwargs) or {"registered": True})
    monkeypatch.setattr(agent_client, "call", lambda name, arguments, registry_root=None: calls.append((name, arguments, registry_root)) or {"ok": True})
    hook = Hook()
    registry_root = tmp_path / "registry"
    ctx = SimpleNamespace(_sessions=SimpleNamespace(root=registry_root))

    klink_extension.register(hook)
    result = hook.tools["vestigraph.guide"]["handler"](ctx, {"project_id": "p1"})

    assert result == {"ok": True}
    assert ensured == [{"root": registry_root}]
    assert calls == [("guide", {"project_id": "p1"}, registry_root)]


def test_klink_entrypoint_keeps_tools_when_auto_registration_fails(monkeypatch, capsys):
    from vestigraph import klink_extension

    class Hook:
        def __init__(self):
            self.domains = {}
            self.tools = {}
        def add_domain(self, token, **meta):
            self.domains[token] = meta
        def add_tool(self, name, handler, **meta):
            self.tools[name] = {"handler": handler, **meta}

    def broken():
        raise OSError("registry blocked")

    monkeypatch.setattr(klink_extension, "_ensure_companion_descriptor", broken)
    hook = Hook()

    klink_extension.register(hook)

    assert "vestigraph" in hook.domains
    assert "registry blocked" in hook.domains["vestigraph"]["usage"]
    assert "vestigraph.guide" in hook.tools
    assert capsys.readouterr().out == ""
