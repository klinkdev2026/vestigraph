"""KLink MCP entry point: register local descriptor plus agent tools."""
from .agent_contract import specifications

_COMPANION_STATUS = {"registered": False, "changed": False, "problem": "not attempted"}


def companion_status() -> dict:
    return dict(_COMPANION_STATUS)


def _companion_usage(status: dict) -> str:
    if status.get("registered"):
        return ("Vestigraph also registered its KLink companion descriptor for this Python. "
                "Restart/open KLayout with the KLink plugin and click HIST; ensure KLINK_REGISTRY_ROOT "
                "matches if you use a custom registry root.")
    problem = status.get("problem") or "unknown problem"
    return ("Vestigraph MCP tools are available, but automatic HIST companion registration did not complete: "
            f"{problem}. Fix the registry path or descriptor permissions, restart this MCP server, then read klink.status.")


def _ensure_companion_descriptor() -> dict:
    try:
        from vestigraph_backends.vesti_backend_klayout.companion import ensure_auto_registered
        return ensure_auto_registered()
    except Exception as exc:
        return {"registered": False, "changed": False,
                "problem": f"auto registration failed: {type(exc).__name__}: {exc}"}


def register(hook):
    global _COMPANION_STATUS
    try:
        _COMPANION_STATUS = _ensure_companion_descriptor()
    except Exception as exc:
        _COMPANION_STATUS = {"registered": False, "changed": False,
                             "problem": f"auto registration failed: {type(exc).__name__}: {exc}"}
    hook.add_domain("vestigraph", title="Vestigraph local history and skills",
        summary="Discover local history, refine selected evidence into a draft, validate structure and export locally.",
        usage='Start with vestigraph.guide {}. Follow next_action. For an existing request use vestigraph.skill; for a new request use history then refine. Submit saves a draft and checks structure. Export only on user request. Never upload, execute or install private skills; never infer GUI action order from saved endpoints. VESTIGRAPH_EXPERIMENTAL_SKILLS=1 enables refinement in the local service. '
              + _companion_usage(_COMPANION_STATUS))
    for spec in specifications():
        name = spec["name"].split(".", 1)[1]
        def handler(ctx, arguments, operation=name):
            from .agent_client import call
            root = getattr(getattr(ctx, "_sessions", None), "root", None)
            if root is not None:
                try:
                    from vestigraph_backends.vesti_backend_klayout.companion import ensure_auto_registered
                    ensure_auto_registered(root=root)
                except Exception:
                    pass
            return call(operation, arguments, root)
        hook.add_tool(spec["name"], handler, description=spec["description"],
                      input_schema=spec["inputSchema"], domain="vestigraph")
