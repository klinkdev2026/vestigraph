"""User installation checks. No editor connection or service startup as a side effect."""
from __future__ import annotations

import importlib
from importlib import metadata
from pathlib import Path
import re
import subprocess
import sys


BASE_REQUIRED = {"fastapi": "fastapi", "uvicorn": "uvicorn", "klayout": "klayout.db"}
INTEGRATION_REQUIRED = {"klayout-klink": "klink"}
KLINK_SPEC = "klayout-klink>=0.6.0,<0.7"
KLINK_MIN = (0, 6, 0)
KLINK_MAX = (0, 7, 0)


def _check(distribution: str, module: str) -> dict:
    try:
        importlib.import_module(module)
        version = metadata.version(distribution)
        ok = True
        if distribution == "klayout-klink":
            match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", version)
            parsed = tuple(map(int, match.groups())) if match else None
            ok = bool(parsed and KLINK_MIN <= parsed < KLINK_MAX)
        return {"package": distribution, "version": version, "ok": ok}
    except (ImportError, metadata.PackageNotFoundError) as exc:
        return {"package": distribution, "ok": False, "problem": str(exc)}


def _checks(required: dict[str, str]) -> list[dict]:
    checks = []
    for distribution, module in required.items():
        checks.append(_check(distribution, module))
    return checks


def dependencies():
    """Packages required for standalone web/history usage."""
    return _checks(BASE_REQUIRED)


def integration_dependencies():
    """Optional KLayout integration packages."""
    return _checks(INTEGRATION_REQUIRED)


def _required_for_setup() -> list[dict]:
    return dependencies() + integration_dependencies()


def doctor(*, integration=False):
    base_checks = dependencies()
    optional_klink = integration_dependencies()[0]
    if not integration:
        ok = all(check["ok"] for check in base_checks)
        return {"ok": ok, "scope": "standalone_installation", "dependencies": base_checks,
                "klink_optional": optional_klink,
                "next_action": "Run python -m vestigraph serve --open-browser. For KLayout integration, install vestigraph[klink] and run python -m vestigraph setup."}
    from vestigraph_backends.vesti_backend_klayout import companion
    checks = base_checks + [optional_klink]
    registration = companion.status()
    plugin = {"installed": False, "companion_support": False}
    if all(check["ok"] for check in checks):
        from klink.cli import _default_salt_dir, _grain_version
        target = _default_salt_dir() / "klink_plugin"
        plugin = {"path": str(target), "installed": target.is_dir(), "version": _grain_version(target),
                  "companion_support": (target / "python/klink_server/companions.py").is_file()}
        plugin["matches_package"] = plugin["version"] == metadata.version("klayout-klink")
    ok = (all(check["ok"] for check in checks) and plugin.get("matches_package", False)
          and plugin["companion_support"] and registration.get("python_ok", False))
    return {"ok": bool(ok), "scope": "installation_only", "python": sys.executable,
            "dependencies": checks, "plugin": plugin, "companion": registration,
            "next_action": "Open KLayout, open a saved layout, and click HIST. Recording status is shown in the panel."
            if ok else f"Install vestigraph[klink] ({KLINK_SPEC}) into this Python, then run python -m vestigraph setup."}


def setup(*, port=8787):
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("The history web port must be between 1 and 65535.")
    checks = _required_for_setup()
    if not all(check["ok"] for check in checks):
        failed = ", ".join(check["package"] for check in checks if not check["ok"])
        raise RuntimeError("Missing/incompatible packages: " + failed + f". Install vestigraph[klink] ({KLINK_SPEC}) into this Python.")
    from vestigraph_backends.vesti_backend_klayout import companion
    # Neutral cwd + isolated imports prove this interpreter can launch the installed service.
    if not companion.python_has_vestigraph(sys.executable):
        raise RuntimeError("Vestigraph is not installed in this Python. Install the package before running setup.")
    kwargs = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if sys.platform == "win32" else {}
    result = subprocess.run([sys.executable, "-I", "-m", "klink.cli", "plugin", "install"],
                            capture_output=True, text=True, encoding="utf-8", errors="replace",
                            timeout=60, env={**companion.child_env(), "PYTHONIOENCODING": "utf-8"}, **kwargs)
    if result.returncode:
        raise RuntimeError("KLink plugin installation failed; companion was not registered. " + (result.stderr or result.stdout).strip())
    registration = companion.register(python=sys.executable, port=port)
    return {"ok": True, "plugin_installed": True, "companion": registration,
            "next_action": "Restart KLayout, open a saved GDS/OASIS layout, then click HIST. Run python -m vestigraph doctor --integration to check installation."}
