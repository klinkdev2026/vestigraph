"""Offline tests for ``vestigraph_backends.vesti_backend_klayout.companion`` and the
``vestigraph companion register|unregister|status`` CLI wiring.

Every test pins VESTIGRAPH_HOME to a tmp_path and passes an explicit
``root``/``--registry-root`` so nothing ever touches the real user's
AppData/Library/XDG locations or the real klink registry. Every probe is
faked (either via an explicit ``probe=`` callable or by monkeypatching
``companion.subprocess.run`` / ``companion.python_has_vestigraph_argv``) so
no subprocess is ever spawned.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from vestigraph.cli import main
from vestigraph_backends.vesti_backend_klayout import companion as companion


def output(capsys):
    return json.loads(capsys.readouterr().out)


def _forbid_subprocess(monkeypatch):
    def fake_run(*args, **kwargs):
        raise AssertionError("subprocess.run must not be called in offline tests")
    monkeypatch.setattr(companion.subprocess, "run", fake_run)


# ------------------------------------------------------------- user_home --
def test_user_home_env_override_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("VESTIGRAPH_HOME", str(tmp_path / "custom-home"))
    assert companion.user_home() == tmp_path / "custom-home"


def test_user_home_windows_branch_with_localappdata(tmp_path, monkeypatch):
    monkeypatch.delenv("VESTIGRAPH_HOME", raising=False)
    monkeypatch.setattr(companion.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    assert companion.user_home() == tmp_path / "local" / "Vestigraph"


def test_user_home_windows_branch_falls_back_to_appdata(tmp_path, monkeypatch):
    monkeypatch.delenv("VESTIGRAPH_HOME", raising=False)
    monkeypatch.setattr(companion.sys, "platform", "win32")
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    assert companion.user_home() == tmp_path / "roaming" / "Vestigraph"


def test_user_home_darwin_branch(tmp_path, monkeypatch):
    monkeypatch.delenv("VESTIGRAPH_HOME", raising=False)
    monkeypatch.setattr(companion.sys, "platform", "darwin")
    monkeypatch.setattr(companion.Path, "home", lambda: tmp_path / "mac-home")
    assert companion.user_home() == tmp_path / "mac-home" / "Library" / "Application Support" / "Vestigraph"


def test_user_home_linux_branch_with_xdg_data_home(tmp_path, monkeypatch):
    monkeypatch.delenv("VESTIGRAPH_HOME", raising=False)
    monkeypatch.setattr(companion.sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    assert companion.user_home() == tmp_path / "xdg-data" / "vestigraph"


def test_user_home_linux_branch_without_xdg_data_home(tmp_path, monkeypatch):
    monkeypatch.delenv("VESTIGRAPH_HOME", raising=False)
    monkeypatch.setattr(companion.sys, "platform", "linux")
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setattr(companion.Path, "home", lambda: tmp_path / "linux-home")
    assert companion.user_home() == tmp_path / "linux-home" / ".local" / "share" / "vestigraph"


# ------------------------------------------------------- klink_registry_root --
def test_klink_registry_root_env_override_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("KLINK_REGISTRY_ROOT", str(tmp_path / "reg-override"))
    assert companion.klink_registry_root() == tmp_path / "reg-override"


def test_klink_registry_root_windows_with_localappdata(tmp_path, monkeypatch):
    monkeypatch.delenv("KLINK_REGISTRY_ROOT", raising=False)
    monkeypatch.setattr(companion.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    assert companion.klink_registry_root() == tmp_path / "local" / "klink" / "registry"


def test_klink_registry_root_windows_without_localappdata_or_appdata_falls_back_to_cwd(tmp_path, monkeypatch):
    monkeypatch.delenv("KLINK_REGISTRY_ROOT", raising=False)
    monkeypatch.setattr(companion.sys, "platform", "win32")
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.chdir(tmp_path)
    assert companion.klink_registry_root() == tmp_path / ".klink" / "registry"


def test_klink_registry_root_darwin(tmp_path, monkeypatch):
    monkeypatch.delenv("KLINK_REGISTRY_ROOT", raising=False)
    monkeypatch.setattr(companion.sys, "platform", "darwin")
    monkeypatch.setattr(companion.Path, "home", lambda: tmp_path / "mac-home")
    assert companion.klink_registry_root() == tmp_path / "mac-home" / "Library" / "Application Support" / "klink" / "registry"


def test_klink_registry_root_linux_with_xdg_state_home(tmp_path, monkeypatch):
    monkeypatch.delenv("KLINK_REGISTRY_ROOT", raising=False)
    monkeypatch.setattr(companion.sys, "platform", "linux")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    assert companion.klink_registry_root() == tmp_path / "xdg-state" / "klink" / "registry"


def test_klink_registry_root_linux_without_xdg_state_home(tmp_path, monkeypatch):
    monkeypatch.delenv("KLINK_REGISTRY_ROOT", raising=False)
    monkeypatch.setattr(companion.sys, "platform", "linux")
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setattr(companion.Path, "home", lambda: tmp_path / "linux-home")
    assert companion.klink_registry_root() == tmp_path / "linux-home" / ".local" / "state" / "klink" / "registry"


# ------------------------------------------------------------- find_python --
def test_find_python_tries_documented_order_and_skips_windows_apps_alias(tmp_path, monkeypatch):
    monkeypatch.setenv("VESTIGRAPH_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(companion.sys, "platform", "win32")
    env_python = str(tmp_path / "env" / "python.exe")
    exe_python = str(tmp_path / "exe" / "python.exe")
    config_python = str(tmp_path / "config" / "python.exe")
    launcher_python = str(tmp_path / "Launcher" / "py.exe")
    path_python = str(tmp_path / "Path" / "python.exe")
    windows_apps_python = str(tmp_path / "Microsoft" / "WindowsApps" / "python3.exe")
    monkeypatch.setenv("VESTIGRAPH_PYTHON", env_python)
    monkeypatch.setattr(companion.sys, "executable", exe_python)

    which_map = {
        "py": launcher_python,
        "python3": windows_apps_python,  # Store alias, must be skipped
        "python": path_python,
    }
    monkeypatch.setattr(companion.shutil, "which", lambda name: which_map.get(name))

    calls = []

    def probe(argv):
        calls.append(argv)
        return False  # nothing succeeds -> we see the *entire* documented order

    result = companion.find_python({"python": config_python}, probe=probe)

    assert result is None
    assert calls == [
        [env_python],
        [exe_python],           # the interpreter running `register` beats the cached config
        [config_python],
        [launcher_python, "-3"],   # "py -3" candidate split into two argv entries
        [path_python],         # python3's WindowsApps candidate never probed
    ]
    assert not companion.config_path().exists()  # nothing succeeded -> nothing cached


def test_find_python_returns_none_when_nothing_succeeds(tmp_path, monkeypatch):
    monkeypatch.setenv("VESTIGRAPH_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("VESTIGRAPH_PYTHON", raising=False)
    monkeypatch.setattr(companion.sys, "platform", "linux")
    monkeypatch.setattr(companion.shutil, "which", lambda name: None)
    monkeypatch.setattr(companion.sys, "executable", str(tmp_path / "bin" / "python3"))

    result = companion.find_python({}, probe=lambda argv: False)
    assert result is None


def test_find_python_caches_first_success_and_prefers_it_next_call(tmp_path, monkeypatch):
    monkeypatch.setenv("VESTIGRAPH_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("VESTIGRAPH_PYTHON", raising=False)
    monkeypatch.setattr(companion.sys, "platform", "linux")
    monkeypatch.setattr(companion.shutil, "which", lambda name: None)
    python311 = str(tmp_path / "bin" / "python3.11")
    python399 = str(tmp_path / "bin" / "python3.99")
    monkeypatch.setattr(companion.sys, "executable", python311)

    result = companion.find_python(probe=lambda argv: argv == [python311])
    assert result == python311

    saved = json.loads(companion.config_path().read_text(encoding="utf-8"))
    assert saved == {"python": python311}

    # Next call: the running interpreter is tried before the cached config value; when it
    # fails the probe, the cached value is the next candidate.
    monkeypatch.setattr(companion.sys, "executable", python399)
    calls = []

    def probe2(argv):
        calls.append(argv)
        return argv == [python311]

    result2 = companion.find_python(probe=probe2)
    assert result2 == python311
    assert calls[:2] == [[python399], [python311]]


# ------------------------------------------------------------- descriptor --
def test_descriptor_shape_and_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("VESTIGRAPH_HOME", str(tmp_path / "home"))
    state = tmp_path / "custom-state"
    spec = companion.descriptor("fake-python", port=9999, state=state, idle_exit_s=42.0)

    assert spec["name"] == "vestigraph"
    assert spec["label"] == "HIST"
    assert spec["command"] == [
        "fake-python", "-m", "vestigraph", "serve", "--state", str(state),
        "--port", "{port}", "--control-file", "--exit-when-idle", "42.0",
    ]
    assert spec["control_file"] == str(state / "control.json")
    assert spec["port"] == 9999
    assert spec["health_path"] == "/healthz"
    assert spec["link_path"] == "/api/v1/auth/issue-link"
    assert spec["autostart"] is True
    assert spec["log_file"] == str(tmp_path / "home" / "logs" / "service.log")
    assert spec["env"] == {"VESTIGRAPH_HOME": str(tmp_path / "home")}


def test_descriptor_env_empty_when_vestigraph_home_not_set(tmp_path, monkeypatch):
    monkeypatch.delenv("VESTIGRAPH_HOME", raising=False)
    monkeypatch.setattr(companion, "user_home", lambda: tmp_path / "home")  # never touch a real platform path
    spec = companion.descriptor("fake-python")
    assert spec["env"] == {}


def test_descriptor_splits_launcher_dash3_candidate_into_command_head(tmp_path, monkeypatch):
    monkeypatch.setenv("VESTIGRAPH_HOME", str(tmp_path / "home"))
    launcher = str(tmp_path / "py.exe")
    spec = companion.descriptor(launcher + " -3", port=1234)
    assert spec["command"][:2] == [launcher, "-3"]
    assert spec["command"][2] == "-m"


# --------------------------------------------------------------- register --
def test_register_writes_descriptor_matching_descriptor_function(tmp_path, monkeypatch):
    monkeypatch.setenv("VESTIGRAPH_HOME", str(tmp_path / "home"))
    _forbid_subprocess(monkeypatch)
    root = tmp_path / "reg"

    result = companion.register(python="fake-python", port=8811, root=root)

    path = root / "companions" / "vestigraph.json"
    assert path.exists()
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved == companion.descriptor("fake-python", port=8811)
    assert result == {
        "registered": True, "descriptor": str(path), "python": "fake-python", "port": 8811,
        "next_action": "Restart KLayout (with the klink plugin): the HIST toolbar button opens the panel.",
    }


def test_register_is_idempotent_overwrite(tmp_path, monkeypatch):
    monkeypatch.setenv("VESTIGRAPH_HOME", str(tmp_path / "home"))
    _forbid_subprocess(monkeypatch)
    root = tmp_path / "reg"

    companion.register(python="fake-python", port=8811, root=root)
    companion.register(python="fake-python-2", port=9000, root=root)

    path = root / "companions" / "vestigraph.json"
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["port"] == 9000
    assert saved["command"][0] == "fake-python-2"


def test_register_raises_when_no_python_found_and_names_the_python_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("VESTIGRAPH_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("VESTIGRAPH_PYTHON", raising=False)
    monkeypatch.setattr(companion.shutil, "which", lambda name: None)

    def fake_run(*args, **kwargs):
        class Result:
            returncode = 1
        return Result()

    monkeypatch.setattr(companion.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError) as excinfo:
        companion.register(root=tmp_path / "reg")
    assert "--python" in str(excinfo.value)


def test_register_uses_explicit_python_without_probing(tmp_path, monkeypatch):
    monkeypatch.setenv("VESTIGRAPH_HOME", str(tmp_path / "home"))
    _forbid_subprocess(monkeypatch)  # would raise AssertionError if find_python()/probe were ever invoked
    result = companion.register(python="explicit-python", root=tmp_path / "reg")
    assert result["python"] == "explicit-python"


# ------------------------------------------------------ unregister/status --
def test_unregister_removed_true_then_false(tmp_path, monkeypatch):
    monkeypatch.setenv("VESTIGRAPH_HOME", str(tmp_path / "home"))
    _forbid_subprocess(monkeypatch)
    root = tmp_path / "reg"
    companion.register(python="fake-python", root=root)

    first = companion.unregister(root)
    assert first["removed"] is True
    assert first["registered"] is False

    second = companion.unregister(root)
    assert second["removed"] is False
    assert second["registered"] is False


def test_status_unregistered(tmp_path, monkeypatch):
    monkeypatch.setenv("VESTIGRAPH_HOME", str(tmp_path / "home"))
    root = tmp_path / "reg"
    result = companion.status(root)
    assert result["registered"] is False
    assert result["next_action"] == "python -m vestigraph companion register"


def test_status_registered_reports_python_ok_true_and_false(tmp_path, monkeypatch):
    monkeypatch.setenv("VESTIGRAPH_HOME", str(tmp_path / "home"))
    _forbid_subprocess(monkeypatch)
    root = tmp_path / "reg"
    companion.register(python="fake-python", port=8899, root=root)

    monkeypatch.setattr(companion, "python_has_vestigraph_argv", lambda argv, timeout=10.0: True)
    ok = companion.status(root)
    assert ok["registered"] is True
    assert ok["python"] == "fake-python"
    assert ok["port"] == 8899
    assert ok["python_ok"] is True

    monkeypatch.setattr(companion, "python_has_vestigraph_argv", lambda argv, timeout=10.0: False)
    bad = companion.status(root)
    assert bad["python_ok"] is False


def test_status_unreadable_descriptor_reports_problem(tmp_path, monkeypatch):
    monkeypatch.setenv("VESTIGRAPH_HOME", str(tmp_path / "home"))
    root = tmp_path / "reg"
    path = root / "companions" / "vestigraph.json"
    path.parent.mkdir(parents=True)
    path.write_text("not json", encoding="utf-8")

    result = companion.status(root)
    assert "problem" in result


# --------------------------------------------------------------------- CLI --
def test_cli_companion_register_status_unregister_roundtrip(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("VESTIGRAPH_HOME", str(tmp_path / "home"))
    _forbid_subprocess(monkeypatch)
    root = tmp_path / "reg"

    assert main(["companion", "register", "--registry-root", str(root),
                 "--python", "fake-python", "--port", "8790"]) == 0
    registered = output(capsys)
    assert registered["registered"] is True
    assert registered["python"] == "fake-python"
    assert registered["port"] == 8790
    descriptor_file = Path(registered["descriptor"])
    assert descriptor_file.exists()

    monkeypatch.setattr(companion, "python_has_vestigraph_argv", lambda argv, timeout=10.0: True)
    assert main(["companion", "status", "--registry-root", str(root)]) == 0
    status_result = output(capsys)
    assert status_result["registered"] is True
    assert status_result["python_ok"] is True

    assert main(["companion", "unregister", "--registry-root", str(root)]) == 0
    unregistered = output(capsys)
    assert unregistered["removed"] is True
    assert not descriptor_file.exists()


def test_cli_companion_register_without_python_found_fails_cleanly(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("VESTIGRAPH_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("VESTIGRAPH_PYTHON", raising=False)
    monkeypatch.setattr(companion.shutil, "which", lambda name: None)

    def fake_run(*args, **kwargs):
        class Result:
            returncode = 1
        return Result()

    monkeypatch.setattr(companion.subprocess, "run", fake_run)

    exit_code = main(["companion", "register", "--registry-root", str(tmp_path / "reg")])
    assert exit_code == 1
    captured = capsys.readouterr()
    assert not captured.out
    assert "--python" in captured.err


def test_cli_old_plugin_subcommand_no_longer_exists(tmp_path, monkeypatch):
    monkeypatch.setenv("VESTIGRAPH_HOME", str(tmp_path / "home"))
    with pytest.raises(SystemExit):
        main(["plugin", "register", "--registry-root", str(tmp_path / "reg")])


def test_python_probe_ignores_modules_planted_in_working_directory(tmp_path,monkeypatch):
    import sys
    planted=tmp_path/"PLANTED"
    (tmp_path/"vestigraph.py").write_text("from pathlib import Path; Path("+repr(str(planted))+").write_text('executed')",encoding="utf-8")
    monkeypatch.setattr(companion.tempfile,"gettempdir",lambda:str(tmp_path))
    companion.python_has_vestigraph_argv([sys.executable])
    assert not planted.exists()
