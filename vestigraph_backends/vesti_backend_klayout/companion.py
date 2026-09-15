"""Register Vestigraph as a klink *companion* (``vestigraph companion register|unregister|status``).

Nothing of Vestigraph runs inside KLayout. klink's plugin reads one JSON
descriptor per companion from ``<klink registry root>/companions/`` and does
the two in-KLayout jobs itself: start the service when KLayout starts and put
a toolbar button that opens the panel. This module only writes that
descriptor (stdlib only, cross-platform).

klink's control-link protocol, which ``vestigraph serve`` implements:
``GET /healthz`` -> 200; ``--control-file`` writes ``<state>/control.json``
``{"port", "pid", "secret"}`` owner-only; ``POST /api/v1/auth/issue-link``
with ``X-Control-Secret`` answers ``{"data": {"link": ...}}``.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from vestigraph.service.paths import user_home

DEFAULT_PORT = int(os.environ.get("VESTIGRAPH_PORT", "8787"))
COMPANION_NAME = "vestigraph"
CONFIG_NAME = "plugin.json"
IDLE_EXIT_S = 60.0


# ------------------------------------------------------------- locations --
def state_dir() -> Path:
    return user_home() / "state"


def klink_registry_root() -> Path:
    """Where klink keeps its local registry (mirrors klink_server.session_registry.default_registry_root)."""
    configured = os.environ.get("KLINK_REGISTRY_ROOT")
    if configured:
        return Path(configured).expanduser()
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if base:
            return Path(base) / "klink" / "registry"
        return Path.cwd() / ".klink" / "registry"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "klink" / "registry"
    base = os.environ.get("XDG_STATE_HOME")
    if base:
        return Path(base) / "klink" / "registry"
    return Path.home() / ".local" / "state" / "klink" / "registry"


def descriptor_path(root: str | Path | None = None) -> Path:
    base = Path(root).expanduser() if root else klink_registry_root()
    return base / "companions" / f"{COMPANION_NAME}.json"


def config_path() -> Path:
    return user_home() / CONFIG_NAME


def load_config() -> dict:
    try:
        data = json.loads(config_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json_atomic(path: Path, payload) -> None:
    """Write via a temp file + os.replace: a crash mid-write must not leave a half JSON that
    KLink's descriptor loader (or our own config reader) then rejects on every start."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def save_config(config: dict) -> None:
    _write_json_atomic(config_path(), config)


# ---------------------------------------------------------------- python --
def child_env() -> dict:
    """A neutral environment for probing: the caller may itself be KLayout's embedded Python."""
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("PYTHON")}
    return env


def _no_window():
    if sys.platform == "win32":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {}


def python_has_vestigraph_argv(argv: list, timeout=10.0) -> bool:
    """Probe from a neutral working directory so a source checkout in cwd cannot fake an install."""
    try:
        result = subprocess.run([*argv, "-I", "-c", "import vestigraph, vestigraph_backends, fastapi, uvicorn"], capture_output=True,
                                timeout=timeout, cwd=tempfile.gettempdir(), env=child_env(), **_no_window())
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def python_has_vestigraph(python: str, timeout=10.0) -> bool:
    return python_has_vestigraph_argv(_argv(python), timeout=timeout)


def _argv(python: str) -> list:
    return python.split(" ") if python.endswith(" -3") else [python]


def find_python(config: dict | None = None, *, probe=python_has_vestigraph_argv) -> str | None:
    """A Python that can run the service: env -> this interpreter -> cached config -> launchers on PATH.

    A real python.exe is preferred over the ``py`` launcher: the launcher adds a console child."""
    config = config if config is not None else load_config()
    candidates = []
    if os.environ.get("VESTIGRAPH_PYTHON"):
        candidates.append(os.environ["VESTIGRAPH_PYTHON"])
    if sys.executable and "klayout" not in Path(sys.executable).name.lower():
        candidates.append(sys.executable)          # the interpreter running `register`: a real python.exe
    if config.get("python"):
        candidates.append(config["python"])
    for name in (("py", "-3"), ("python3",), ("python",)) if sys.platform == "win32" else (("python3",), ("python",)):
        exe = shutil.which(name[0])
        if exe:
            candidates.append(exe if len(name) == 1 else exe + " -3")
    seen = set()
    for candidate in candidates:
        if candidate in seen or "WindowsApps" in candidate:   # Store alias opens the Store or hangs
            continue
        seen.add(candidate)
        if probe(_argv(candidate)):
            if config.get("python") != candidate:
                try:
                    save_config({**config, "python": candidate})   # remember: next call skips probing
                except OSError:
                    pass
            return candidate
    return None


# ------------------------------------------------------------ descriptor --
def descriptor(python: str, *, port: int = DEFAULT_PORT, state: Path | None = None,
               idle_exit_s: float = IDLE_EXIT_S) -> dict:
    state = Path(state) if state else state_dir()
    home = user_home()
    return {
        "name": COMPANION_NAME,
        "label": "HIST",
        "title": "Vestigraph: open the layout history panel",
        "command": [*_argv(python), "-m", "vestigraph", "serve", "--state", str(state), "--port", "{port}",
                    "--control-file", "--exit-when-idle", str(idle_exit_s)],
        "port": int(port),
        "control_file": str(state / "control.json"),
        "health_path": "/healthz",
        "link_path": "/api/v1/auth/issue-link",
        "autostart": True,
        "cwd": str(home),
        "log_file": str(home / "logs" / "service.log"),
        "env": {"VESTIGRAPH_HOME": str(home)} if os.environ.get("VESTIGRAPH_HOME") else {},
    }


def _serve_command_index(command: list) -> int | None:
    if not isinstance(command, list) or not all(isinstance(item, str) for item in command):
        return None
    needle = ["-m", "vestigraph", "serve"]
    for index in range(1, len(command) - len(needle) + 1):
        if command[index:index + len(needle)] == needle:
            return index
    return None


def _path_problem(value, field: str, *, required: bool = True) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value:
        return f"{field} must be a non-empty path"
    if not Path(value).expanduser().is_absolute():
        return f"{field} must be an absolute path"
    return None


def _optional_str_problem(spec: dict, field: str) -> str | None:
    if field in spec and (not isinstance(spec[field], str) or not spec[field]):
        return f"{field} must be a non-empty string"
    return None


def _validate_existing_descriptor(spec: dict) -> str | None:
    if spec.get("name") != COMPANION_NAME:
        return f"descriptor name must be {COMPANION_NAME!r}"
    if _serve_command_index(spec.get("command")) is None:
        return "descriptor command is not a supported 'python -m vestigraph serve' command"
    port = spec.get("port")
    if type(port) is not int or not 1 <= port <= 65535:
        return "port must be an integer between 1 and 65535"
    problem = _path_problem(spec.get("control_file"), "control_file")
    if problem:
        return problem
    problem = _path_problem(spec.get("cwd"), "cwd")
    if problem:
        return problem
    problem = _path_problem(spec.get("log_file"), "log_file", required=False)
    if problem:
        return problem
    for field in ("label", "title"):
        problem = _optional_str_problem(spec, field)
        if problem:
            return problem
    for field in ("health_path", "link_path"):
        problem = _optional_str_problem(spec, field)
        if problem:
            return problem
        if field in spec and not spec[field].startswith("/"):
            return f"{field} must start with '/'"
    if "autostart" in spec and type(spec["autostart"]) is not bool:
        return "autostart must be a boolean"
    env = spec.get("env", {})
    if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
        return "env must be an object of string keys and values"
    return None


def _auto_descriptor(python: str, existing: dict | None = None) -> tuple[dict | None, str | None]:
    if existing is None:
        return descriptor(python), None
    problem = _validate_existing_descriptor(existing)
    if problem:
        return None, problem
    command = existing["command"]
    index = _serve_command_index(command)
    spec = dict(existing)
    spec["command"] = [*_argv(python), *command[index:]]
    spec["name"] = COMPANION_NAME
    return spec, None


def _read_descriptor(path: Path) -> tuple[dict | None, str | None]:
    if not path.exists():
        return None, None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"descriptor unreadable: {exc}"
    if not isinstance(data, dict):
        return None, "descriptor is not a JSON object"
    return data, None


def ensure_auto_registered(*, root: str | Path | None = None, python: str | None = None) -> dict:
    """Best-effort MCP-discovery registration for the current interpreter.

    This never installs or overwrites the KLayout KLink plugin, never starts the
    service, never opens a browser and never raises to KLink discovery. Existing
    descriptors are only updated when they are valid Vestigraph serve descriptors;
    their service flags and user choices are preserved while the interpreter
    prefix is refreshed to the current MCP Python. Invalid descriptors are
    reported but not overwritten.
    """
    path = descriptor_path(root)
    try:
        existing, problem = _read_descriptor(path)
        if problem:
            return {"registered": False, "descriptor": str(path), "changed": False, "problem": problem}
        spec, problem = _auto_descriptor(python or sys.executable, existing)
        if problem:
            return {"registered": False, "descriptor": str(path), "changed": False, "problem": problem}
        if existing == spec:
            return {"registered": True, "descriptor": str(path), "changed": False,
                    "python": spec["command"][0], "port": spec["port"]}
        Path(spec["cwd"]).mkdir(parents=True, exist_ok=True)
        _write_json_atomic(path, spec)
        return {"registered": True, "descriptor": str(path), "changed": True,
                "python": spec["command"][0], "port": spec["port"]}
    except Exception as exc:
        return {"registered": False, "descriptor": str(path), "changed": False,
                "problem": f"auto registration failed: {type(exc).__name__}: {exc}"}

def register(*, python: str | None = None, port: int = DEFAULT_PORT, root: str | Path | None = None,
             state: Path | None = None) -> dict:
    """Write the descriptor klink reads at KLayout startup. Idempotent; overwrites a previous one."""
    python = python or find_python()
    if not python:
        raise RuntimeError("No Python with vestigraph[web,preview] installed was found; install it or pass --python.")
    path = descriptor_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    spec = descriptor(python, port=port, state=state)
    # Explicit --python bypasses find_python/save_config; cwd must still exist.
    # Do this before publishing a descriptor which KLink could immediately launch.
    Path(spec["cwd"]).mkdir(parents=True, exist_ok=True)
    _write_json_atomic(path, spec)
    return {"registered": True, "descriptor": str(path), "python": python, "port": int(port),
            "next_action": "Restart KLayout (with the klink plugin): the HIST toolbar button opens the panel."}


def unregister(root: str | Path | None = None) -> dict:
    path = descriptor_path(root)
    existed = path.exists()
    if existed:
        path.unlink()
    return {"registered": False, "descriptor": str(path), "removed": existed}


def status(root: str | Path | None = None) -> dict:
    path = descriptor_path(root)
    report = {"registered": path.exists(), "descriptor": str(path), "klink_registry_root": str(path.parent.parent)}
    if path.exists():
        try:
            spec = json.loads(path.read_text(encoding="utf-8"))
            report["python"] = spec["command"][0]
            report["port"] = spec["port"]
            command = spec["command"]
            argv = command[:2] if len(command) > 1 and command[1] == "-3" else command[:1]
            report["python_ok"] = python_has_vestigraph_argv(argv)
        except (OSError, ValueError, KeyError, IndexError) as exc:
            report["problem"] = f"descriptor unreadable: {exc}"
    else:
        report["next_action"] = "python -m vestigraph companion register"
    return report
