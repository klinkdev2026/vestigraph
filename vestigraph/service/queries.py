"""History queries over one document's v1 store, with scoped cursors.

Read paths open the repository read-only unless the service already holds a
writable handle for it (live documents being recorded). Nothing here loads
whole histories into memory; every list is a bounded keyset page.
"""
from __future__ import annotations

from pathlib import Path

from ..store import Repository, RepositoryError, RepositoryDataError
from . import cursors
from .errors import ServiceError, not_found


def open_store(document: dict, writable_handles: dict | None = None, *, services=None) -> Repository:
    handle = (writable_handles or {}).get(document["id"])
    if handle is not None:
        return handle
    try:
        return Repository.open_readonly(document["store_path"], services=services)
    except RepositoryError as exc:
        raise ServiceError("HISTORY_UNREADABLE", f"Cannot open the history: {exc}", status=503,
                           next_action="Check that the history folder still exists and is readable.") from exc


def _page(document, kind, fetch, cursor, limit, extra_scope=None):
    scope = {"document_id": document["id"], "kind": kind}
    if extra_scope:
        scope.update(extra_scope)
    limit = cursors.limit_of(limit)
    try:
        state = cursors.decode(cursor, scope)
    except cursors.ServiceCursorMismatch:
        raise cursors.scope_error() from None
    try:
        result = fetch(limit, state.get("before"), state.get("upper"))
    except RepositoryError as exc:
        raise ServiceError("HISTORY_UNREADABLE", f"Cannot read the history: {exc}", status=503,
                           next_action="Check the history folder.") from exc
    return scope, result


def checkpoint_summary(item: dict, *, formats=None) -> dict:
    metadata = item.get("metadata") or {}
    document = metadata.get("document") if isinstance(metadata.get("document"), dict) else {}
    legacy = metadata.get("legacy_import") if isinstance(metadata.get("legacy_import"), dict) else {}
    return {
        "id": item["id"], "title": item["title"], "source": item["source"],
        "created_at": item["created_at"], "size": item["size"], "sha256": item["sha256"],
        "segment_id": item.get("segment_id"), "parent_id": item.get("parent_id"),
        "filename": item["filename"],
        "document_filename": Path(document["filename"]).name if isinstance(document.get("filename"), str) and document["filename"] else None,
        "format": metadata.get("format") or _format_from_name(item["filename"], formats=formats),
        "coverage": metadata.get("coverage"),
        "operation": metadata.get("operation"),
        "modified_at": metadata.get("modified_at"),
        "modified_at_basis": metadata.get("modified_at_basis"),
        "restore_of": metadata.get("restore_of"),
        "capture": metadata.get("capture"),
        "ordinal": item.get("ordinal"),
        **({"history_parent_id": item["history_parent_id"]} if "history_parent_id" in item else {}),
        "imported": metadata.get("origin") == "legacy_file_import",
        "historical_at": legacy.get("historical_at"),
    }


def _format_from_name(name: str, *, formats=None):
    suffix = Path(name).suffix.lower()
    from ..vesti_formats.registry import FORMATS
    formats = formats if formats is not None else FORMATS.copy(frozen=True)
    return next((spec["format_id"] for spec in formats.describe() if suffix in spec["extensions"]), None)


def event_summary(item: dict) -> dict:
    payload = item.get("payload") or {}
    truncated = isinstance(payload, dict) and payload.get("vestigraph_truncated") is True
    return {
        "seq": item["seq"], "kind": item["kind"], "source": item["source"],
        "created_at": item["created_at"], "segment_id": item.get("segment_id"),
        "truncated": truncated, "summary": _summarize(payload),
    }


def _summarize(payload) -> dict:
    if not isinstance(payload, dict):
        return {}
    out = {}
    for key in ("reason", "status", "cell", "layer", "count", "coverage", "warning"):
        if key in payload and isinstance(payload[key], (str, int, float, bool)):
            out[key] = payload[key] if not isinstance(payload[key], str) else payload[key][:200]
    causes = payload.get("caused_by")
    if isinstance(causes, list):
        out["caused_by"] = [c.get("method") for c in causes[:5] if isinstance(c, dict) and isinstance(c.get("method"), str)]
    return out


def list_checkpoints(store: Repository, document, cursor=None, limit=None, segment_id=None):
    revision = store.history_revision()
    extra = {"segment_id": segment_id} if segment_id else {}
    if revision:
        extra["history_revision"] = revision
    scope, result = _page(document, "checkpoints",
                          lambda l, b, u: store.page_checkpoints(l, b, u, segment_id=segment_id, expected_revision=revision),
                          cursor, limit, extra or None)
    page = cursors.page(scope, result, (checkpoint_summary(i, formats=store.services.formats) for i in result["items"]))
    page["history_revision"] = revision
    return page


def list_segments(store: Repository, document, cursor=None, limit=None):
    scope, result = _page(document, "segments", store.page_segments, cursor, limit)
    items = []
    for item in result["items"]:
        counts = store.segment_counts(item["id"])
        items.append({**{k: item[k] for k in ("id", "title", "source", "status", "started_at", "ended_at")},
                      "checkpoint_count": counts["checkpoints"], "event_count": counts["events"],
                      "capture": (item.get("metadata") or {}).get("capture"),
                      "ordinal": item.get("ordinal")})
    return cursors.page(scope, result, items)


def list_events(store: Repository, document, cursor=None, limit=None, segment_id=None):
    scope, result = _page(document, "events",
                          lambda l, b, u: store.page_events(l, b, u, segment_id=segment_id),
                          cursor, limit, {"segment_id": segment_id} if segment_id else None)
    return cursors.page(scope, result, (event_summary(i) for i in result["items"]))


def get_checkpoint(store: Repository, checkpoint_id, *, with_manifest=False):
    try:
        item = store.get_checkpoint(checkpoint_id)
    except RepositoryDataError as exc:
        raise ServiceError("HISTORY_UNREADABLE", "Saved version metadata is corrupt.", status=503,
                           next_action="Preserve the history and run an integrity check.") from exc
    except RepositoryError:
        raise not_found("Saved version") from None
    out = checkpoint_summary(item, formats=store.services.formats)
    out["metadata"] = item.get("metadata") or {}
    manifest = item.get("manifest") or {}
    try:
        if manifest.get("format") == 2:
            out["chunk_count"] = (manifest.get("counts") or {}).get("layout_chunk_refs", 0)
            if type(out["chunk_count"]) is not int or out["chunk_count"] < 0:
                raise ValueError("Invalid chunk count")
        elif manifest.get("format") == 1 and isinstance(manifest.get("chunks"), list):
            out["chunk_count"] = len(manifest["chunks"])
        else:
            raise ValueError("Invalid manifest")
    except (ValueError, TypeError, AttributeError) as exc:
        raise ServiceError("HISTORY_UNREADABLE", "Saved version manifest is corrupt.", status=503,
                           next_action="Preserve the history and run an integrity check.") from exc
    if with_manifest:
        out["manifest"] = item.get("manifest")
    return out


def get_event(store: Repository, seq):
    try:
        item = store.get_event(seq)
    except RepositoryError:
        raise not_found("Recorded event") from None
    out = event_summary(item)
    out["payload"] = item.get("payload")
    return out


def counts(store: Repository) -> dict:
    try:
        return store.counts()
    except RepositoryError as exc:
        raise ServiceError("HISTORY_UNREADABLE", f"Cannot read the history: {exc}", status=503,
                           next_action="Check the history folder.") from exc
