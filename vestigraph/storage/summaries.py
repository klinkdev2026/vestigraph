"""Multi-version summary batch entry (docs/SPEC_PERFORMANCE_READ_ACCESS.md §4.4).

Read-only, DB-only: fetches up to 100 checkpoint rows and their changeset-root
rows in ONE read connection/snapshot, so an agent can decide which versions
deserve a further ``changes()`` read without expanding every version's full
entry list first. This module must NEVER instantiate ``ObjectIndex``, open a
pack, or read a recipe/entry object -- everything here comes straight out of
the ``checkpoints``/``changesets`` SQLite rows.
"""
from __future__ import annotations
import logging

import json

from ..store import CursorError
from .errors import StorageError

MAX_IDS = 100
MAX_RESPONSE_BYTES = 1024 * 1024


def validate_ids(checkpoint_ids):
    """Shape-check a requested checkpoint id list: 1-100 distinct, non-empty strings.

    Public so callers (e.g. the Application layer, before its own one-query scope
    check) can validate/normalize the same way `change_summaries` does, without
    duplicating the rules."""
    if not isinstance(checkpoint_ids, list) or isinstance(checkpoint_ids, (str, bytes)):
        raise CursorError("checkpoint_ids must be a list of checkpoint id strings.")
    if not checkpoint_ids:
        raise CursorError("checkpoint_ids must not be empty; pass at least one checkpoint id.")
    if len(checkpoint_ids) > MAX_IDS:
        raise CursorError(f"checkpoint_ids has {len(checkpoint_ids)} items; ask for at most {MAX_IDS} at a time.")
    seen = set()
    for checkpoint_id in checkpoint_ids:
        if not isinstance(checkpoint_id, str) or not checkpoint_id:
            raise CursorError("Every checkpoint id must be a non-empty string.")
        if checkpoint_id in seen:
            raise CursorError(f"Duplicate checkpoint id in request: {checkpoint_id}.")
        seen.add(checkpoint_id)
    return list(checkpoint_ids)


def _reject_constant(name):
    raise ValueError(f"non-finite number {name} is not allowed")


def _loads_object(payload, label):
    if payload is None or payload == "":
        raise StorageError(f"CHANGES_UNREADABLE: {label} is empty")
    try:
        obj = json.loads(payload, parse_constant=_reject_constant)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise StorageError(f"CHANGES_UNREADABLE: {label} is not valid JSON") from exc
    if not isinstance(obj, dict):
        raise StorageError(f"CHANGES_UNREADABLE: {label} is not a JSON object")
    return obj


def _require_object(value, label):
    if not isinstance(value, dict):
        raise StorageError(f"CHANGES_UNREADABLE: {label} is not a JSON object")
    return value


def _coverage_view(coverage):
    coverage = _require_object(coverage, "change root coverage")
    out = {"status": coverage.get("status"), "basis": coverage.get("basis"),
           "limitations": coverage.get("limitations")}
    if "warnings" in coverage:
        out["warnings"] = coverage["warnings"]
    if "reason" in coverage:
        out["reason"] = coverage["reason"]
    return out


class _QueryCounter:
    """Small per-call counter fed by sqlite3's trace callback (SPEC §4.4 instrumentation)."""

    def __init__(self):
        self.queries = 0

    def __call__(self, _sql):
        self.queries += 1


def _validate_root_links(row, root, coverage):
    checkpoint_id = row["id"]
    parent_id = row["parent_id"]
    if root.get("to_checkpoint_id") != checkpoint_id:
        raise StorageError(
            f"CHANGES_UNREADABLE: checkpoint {checkpoint_id} change root points to {root.get('to_checkpoint_id')}")
    if root.get("from_checkpoint_id") != parent_id:
        raise StorageError(
            f"CHANGES_UNREADABLE: checkpoint {checkpoint_id} change root parent does not match checkpoint parent")
    if coverage.get("status") == "baseline" and (parent_id is not None or root.get("from_checkpoint_id") is not None):
        raise StorageError(f"CHANGES_UNREADABLE: checkpoint {checkpoint_id} baseline change root has a parent")


def change_summaries(repo, checkpoint_ids):
    """One-snapshot batch of ChangeSet-root summaries for ``checkpoint_ids`` (request order).

    Returns ``{"items": [...], "diagnostics": {"queries": N, "object_reads": 0}}``.
    Every id must exist; an unknown id raises ``vestigraph.store.CursorError`` naming it
    (nothing partial is returned). A checkpoint with no changeset root (format-1 history,
    or a damaged/removed changesets row) is reported per-item as ``coverage.status ==
    "unavailable"``, not as a request error.
    """
    ids = validate_ids(checkpoint_ids)
    counter = _QueryCounter()
    db = repo._open_connection()
    try:
        db.set_trace_callback(counter)
        db.execute("BEGIN")
        placeholders = ",".join("?" for _ in ids)
        checkpoint_rows = {
            row["id"]: row
            for row in db.execute(
                "SELECT id, parent_id, created_at, title, source, size, sha256, manifest "
                f"FROM checkpoints WHERE id IN ({placeholders})", ids).fetchall()
        }
        missing = [cid for cid in ids if cid not in checkpoint_rows]
        if missing:
            raise CursorError(f"Unknown checkpoint id: {missing[0]}.")
        root_json_by_id = {}
        if repo.format == 2:
            root_json_by_id = {
                row["checkpoint_id"]: row["root"]
                for row in db.execute(
                    f"SELECT checkpoint_id, root FROM changesets WHERE checkpoint_id IN ({placeholders})", ids).fetchall()
            }
        db.execute("COMMIT")
    except Exception:
        try:
            db.execute("ROLLBACK")
        except Exception:
            logging.getLogger(__name__).warning("Nonfatal callback or cleanup failure", exc_info=True)
        raise
    finally:
        db.set_trace_callback(None)
        db.close()

    items = []
    for checkpoint_id in ids:
        row = checkpoint_rows[checkpoint_id]
        manifest = _loads_object(row["manifest"], f"checkpoint {checkpoint_id} manifest")
        item = {
            "checkpoint_id": row["id"],
            "parent_id": row["parent_id"],
            "created_at": row["created_at"],
            "title": row["title"],
            "source": row["source"],
            "size": row["size"],
            "raw_sha256": row["sha256"],
            "normalized_sha256": manifest.get("normalized_sha256"),
            "normalized_algorithm": manifest.get("normalized_algorithm"),
        }
        root_json = root_json_by_id.get(checkpoint_id)
        if root_json is None:
            item.update({
                "change_id": None,
                "algorithm": None,
                "from_checkpoint_id": None,
                "summary": None,
                "coverage": {"status": "unavailable", "reason": "no change record for this version"},
                "has_entries": False,
                "has_evidence": False,
                "input_ids": None,
            })
            items.append(item)
            continue
        root = _loads_object(root_json, f"checkpoint {checkpoint_id} change root")
        coverage = _require_object(root.get("coverage"), f"checkpoint {checkpoint_id} change root coverage")
        summary = _require_object(root.get("summary"), f"checkpoint {checkpoint_id} change root summary")
        _validate_root_links(row, root, coverage)
        item.update({
            "change_id": root.get("id"),
            "algorithm": root.get("algorithm"),
            "from_checkpoint_id": root.get("from_checkpoint_id"),
            "summary": summary,
            "coverage": _coverage_view(coverage),
            "has_entries": bool(root.get("entries_root")),
            "has_evidence": bool(root.get("evidence_root")),
            "input_ids": {"from": root.get("from_checkpoint_id"), "to": root.get("to_checkpoint_id")},
        })
        items.append(item)

    result = {"items": items, "diagnostics": {"queries": counter.queries, "object_reads": 0}}
    encoded_size = len(json.dumps(result).encode("utf-8"))
    if encoded_size > MAX_RESPONSE_BYTES:
        raise CursorError(
            f"Summary response would be {encoded_size} bytes, over the 1 MiB guard; ask for fewer checkpoint ids.")
    return result
