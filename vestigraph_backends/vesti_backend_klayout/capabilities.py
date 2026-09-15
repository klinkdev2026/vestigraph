"""Dependency detection for this provider; never opens an editor connection."""
import importlib.util


def has_module(name):
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError, AttributeError):
        return False


def legacy_capabilities():
    client = has_module("klink")
    registry = has_module("klink.mcp.session_registry") if client else False
    renderer = has_module("klayout")
    return {"autocapture": bool(client and registry), "autocapture_dependency": client,
            "session_registry": registry, "preview": renderer, "preview_dependency": renderer,
            "open_history": bool(client and registry)}
