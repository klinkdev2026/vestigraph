"""Existing repository use cases exposed through the same SDK/CLI/MCP descriptors."""
from .capabilities import VestiArgument, VestiCapability, VestiCapabilityRegistry


def repository_capabilities(repo):
    registry = VestiCapabilityRegistry()
    registry.register(VestiCapability(
        "vesti_capabilities_search", "Find available engineering capabilities.",
        "Returns short summaries only. Narrow by keyword, then request a capability description.",
        (VestiArgument("query", "string", default="", max_length=256),
         VestiArgument("limit", "integer", default=20, minimum=1, maximum=50)),
        ("discovery",), read_only=True), registry.search)
    registry.register(VestiCapability(
        "vesti_capabilities_describe", "Read a capability's arguments and usage.",
        "Detailed schemas are disclosed on demand. Descriptions do not grant permission.",
        (VestiArgument("name", "string", required=True, max_length=128),),
        ("discovery",), read_only=True), registry.describe)
    registry.register(VestiCapability(
        "vesti_runtime_inspect", "Inspect this repository's registered formats and codecs.",
        "Reports the repository service snapshot. Registered readers may still need optional SDKs; "
        "their registration does not certify runtime availability.",
        tags=("runtime", "formats", "codecs"), read_only=True), lambda: {
            "storage_format": repo.format, "formats": repo.services.formats.describe(),
            "codecs": repo.services.codecs.registry.describe(),
            "write_delta_codec": repo.services.codecs.delta_codec_id})
    registry.register(VestiCapability(
        "vesti_history_list", "List recent engineering checkpoints.",
        "Returns bounded checkpoint summaries, newest first. Detailed manifests and raw evidence "
        "are intentionally not embedded in this discovery result.",
        (VestiArgument("limit", "integer", default=20, minimum=1, maximum=100),),
        ("history", "versions"), read_only=True), lambda limit=20: {"items": [
            {key: row.get(key) for key in ("id", "parent_id", "title", "created_at", "filename", "size", "sha256")}
            for row in repo.history(limit=limit)]})
    registry.register(VestiCapability(
        "vesti_changes_read", "Read a page of recorded engineering changes.",
        "Use next_cursor to request later pages. Coverage may be unavailable for opaque files; "
        "recorded changes do not establish causation or signoff.",
        (VestiArgument("checkpoint_id", "string", required=True, max_length=128),
         VestiArgument("limit", "integer", default=20, minimum=1, maximum=100),
         VestiArgument("cursor", "string"),
         VestiArgument("kind", "string", max_length=128)),
        ("history", "changes", "evidence"), read_only=True),
        lambda checkpoint_id, limit=20, cursor=None, kind=None:
            repo.changes(checkpoint_id, limit=limit, cursor=cursor, kind=kind))
    return registry.freeze()
