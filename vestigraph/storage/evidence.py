"""Read-only evidence package builder for external agents.

The package is deliberately summary-level: it reads checkpoint/change-root rows
through ``summaries.change_summaries`` and never opens layout packs, entries, or
recipes. Deeper facts stay behind the existing ``changes()`` cursor API.
"""
from __future__ import annotations

import hashlib
import json

from ..store import CursorError
from .summaries import MAX_IDS, MAX_RESPONSE_BYTES, change_summaries, validate_ids

PACKAGE_FORMAT = "vestigraph.evidence_package"
PACKAGE_VERSION = 1
ALGORITHM_VERSION = "evidence-package-v1"
HTTP_WRAPPER_RESERVE_BYTES = 2 * 1024


def _canonical_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _http_json_bytes(value):
    return json.dumps(value, ensure_ascii=True, allow_nan=False).encode("utf-8")


def _digest(value):
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _checkpoint_ref(checkpoint_id):
    return {
        "checkpoint_id": checkpoint_id,
        "checkpoint_detail": {"kind": "repository_checkpoint", "checkpoint_id": checkpoint_id},
        "layout_export": {"kind": "repository_export", "checkpoint_id": checkpoint_id},
        "changes_page": {"kind": "repository_changes", "checkpoint_id": checkpoint_id},
        "changes_download": {"kind": "repository_changes_download", "checkpoint_id": checkpoint_id},
    }


def _node(item, index):
    coverage = item.get("coverage")
    change_id = item.get("change_id")
    references = [{"kind": "checkpoint", "checkpoint_id": item["checkpoint_id"]}]
    if change_id:
        references.append({"kind": "change_root", "checkpoint_id": item["checkpoint_id"], "change_id": change_id})
    return {
        "index": index,
        "checkpoint_id": item["checkpoint_id"],
        "parent_id": item["parent_id"],
        "created_at": item["created_at"],
        "title": item["title"],
        "source": item["source"],
        "size": item["size"],
        "raw_sha256": item["raw_sha256"],
        "normalized_sha256": item["normalized_sha256"],
        "normalized_algorithm": item["normalized_algorithm"],
        "change": {
            "change_id": change_id,
            "algorithm": item.get("algorithm"),
            "from_checkpoint_id": item.get("from_checkpoint_id"),
            "to_checkpoint_id": item["checkpoint_id"] if change_id else None,
            "summary": item.get("summary"),
            "coverage": coverage,
            "has_entries": item.get("has_entries", False),
            "has_evidence": item.get("has_evidence", False),
            "input_ids": item.get("input_ids"),
        },
        "references": references,
        "read_more": _checkpoint_ref(item["checkpoint_id"]),
    }


def _validate_parent_chain(items):
    for previous, current in zip(items, items[1:]):
        if current["parent_id"] != previous["checkpoint_id"]:
            raise CursorError(
                "checkpoint_ids must be a continuous parent-to-child chain in request order; "
                f"{current['checkpoint_id']} has parent {current['parent_id']}, expected {previous['checkpoint_id']}."
            )


def _set_body_size_fixed_point(package):
    diagnostics = package["diagnostics"]
    diagnostics["canonical_body_bytes"] = 0
    diagnostics["response_bytes"] = 0
    previous = None
    for _ in range(8):
        sizes = (len(_canonical_bytes(package)), len(_http_json_bytes(package)))
        if sizes == previous:
            break
        diagnostics["canonical_body_bytes"], diagnostics["response_bytes"] = sizes
        previous = sizes
    return len(_http_json_bytes(package))


def evidence_package(repo, checkpoint_ids):
    """Build a deterministic, bounded evidence package for a checkpoint chain.

    ``checkpoint_ids`` must be 1-100 distinct checkpoint ids ordered from parent
    to child. Non-contiguous chains are rejected instead of inferred.
    """
    ids = validate_ids(checkpoint_ids)
    summaries = change_summaries(repo, ids)
    items = summaries["items"]
    _validate_parent_chain(items)

    nodes = [_node(item, index) for index, item in enumerate(items)]
    boundary_parent = items[0]["parent_id"]
    unavailable = [item["checkpoint_id"] for item in items
                   if (item.get("coverage") or {}).get("status") == "unavailable"]
    entry_omissions = [item["checkpoint_id"] for item in items if item.get("has_entries")]
    event_omissions = [item["checkpoint_id"] for item in items]
    input_facts = {
        "format": PACKAGE_FORMAT,
        "version": PACKAGE_VERSION,
        "history_uid_namespace": "repository_local",
        "history_uid": None,
        "checkpoint_ids": ids,
        "boundary_parent": boundary_parent,
        "nodes": nodes,
    }
    package = {
        "format": PACKAGE_FORMAT,
        "version": PACKAGE_VERSION,
        "algorithm": ALGORITHM_VERSION,
        "history_uid_namespace": "repository_local",
        "history_uid": None,
        "history_uid_portability": "valid only for the caller-selected repository or HTTP document scope; not portable until history_uid exists",
        "window": {
            "mode": "continuous_parent_chain",
            "checkpoint_ids": ids,
            "count": len(ids),
            "max_count": MAX_IDS,
            "boundary_parent": boundary_parent,
        },
        "input_digest": "sha256:" + _digest(input_facts),
        "nodes": nodes,
        "boundary_parent": boundary_parent,
        "coverage": {
            "snapshot_restorable": {"status": "not_verified", "reason": "package does not restore or open layout payloads"},
            "changes_coverage": {
                "status": "summary_roots_only",
                "unavailable_checkpoint_ids": unavailable,
            },
            "event_detail_coverage": {"status": "omitted", "reason": "events are not read by evidence_package v1"},
        },
        "omissions": {
            "layout_payloads": "omitted",
            "entries": {"status": "omitted", "checkpoint_ids_with_entries": entry_omissions},
            "events": {"status": "omitted", "checkpoint_ids": event_omissions},
            "known_discarded_events": "not_read",
            "missing_or_unparseable_sources": unavailable,
            "budget_truncated": False,
            "next_cursor": None,
        },
        "references": [_checkpoint_ref(checkpoint_id) for checkpoint_id in ids],
        "diagnostics": {
            "queries": summaries["diagnostics"]["queries"],
            "object_reads": summaries["diagnostics"]["object_reads"],
            "layout_payload_reads": 0,
            "metadata_objects_read": 0,
        },
    }
    body_size = _set_body_size_fixed_point(package)
    max_body = MAX_RESPONSE_BYTES - HTTP_WRAPPER_RESERVE_BYTES
    if body_size > max_body:
        raise CursorError(
            f"Evidence package would be {body_size} bytes plus HTTP wrapper reserve, over the 1 MiB guard; ask for fewer checkpoint ids.")
    return package
