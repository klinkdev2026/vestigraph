"""Installation commands must fail before publishing a broken companion registration."""
import subprocess
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


def test_doctor_default_checks_standalone_and_reports_klink_optional(monkeypatch):
    monkeypatch.setattr(installation, "dependencies", lambda: [
        {"package": "fastapi", "ok": True},
        {"package": "uvicorn", "ok": True},
        {"package": "klayout", "ok": True},
    ])
    monkeypatch.setattr(installation, "integration_dependencies", lambda: [
        {"package": "klayout-klink", "ok": False, "problem": "blocked in test"},
    ])
    monkeypatch.setattr(companion, "status", lambda *a, **k: pytest.fail("standalone doctor must not inspect registration"))

    result = installation.doctor()

    assert result["ok"] is True
    assert result["scope"] == "standalone_installation"
    assert [check["package"] for check in result["dependencies"]] == ["fastapi", "uvicorn", "klayout"]
    assert result["klink_optional"]["package"] == "klayout-klink"
    assert result["klink_optional"]["ok"] is False


@pytest.mark.parametrize("port", [0, -1, 65536, True])
def test_setup_rejects_invalid_port(port):
    with pytest.raises(ValueError):
        installation.setup(port=port)


def test_doctor_failure_has_nonzero_exit(monkeypatch, capsys):
    monkeypatch.setattr(installation, "doctor", lambda: {"ok": False, "scope": "installation_only"})
    assert main(["doctor"]) == 1
    assert '"ok": false' in capsys.readouterr().out
