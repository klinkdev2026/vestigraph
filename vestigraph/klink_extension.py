"""Optional KLink MCP entry point. Registration is metadata-only and reads no history."""
from .agent_contract import specifications


def register(hook):
    hook.add_domain("vestigraph", title="Vestigraph local history and skills",
        summary="Discover local history, refine selected evidence into a draft, validate structure and export locally.",
        usage='Start with vestigraph.guide {}. Follow next_action. For an existing request use vestigraph.skill; for a new request use history then refine. Submit saves a draft and checks structure. Export only on user request. Never upload, execute or install private skills; never infer GUI action order from saved endpoints. VESTIGRAPH_EXPERIMENTAL_SKILLS=1 enables refinement in the local service.')
    for spec in specifications():
        name = spec["name"].split(".", 1)[1]
        def handler(ctx, arguments, operation=name):
            from .agent_client import call
            return call(operation, arguments, getattr(getattr(ctx, "_sessions", None), "root", None))
        hook.add_tool(spec["name"], handler, description=spec["description"],
                      input_schema=spec["inputSchema"], domain="vestigraph")
