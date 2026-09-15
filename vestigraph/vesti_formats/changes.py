"""Common ChangeSet envelope; structural facts belong to format providers."""
import uuid
from ..storage import changes as _changes, policy
from ..storage.metadata import canonical
from ..storage.errors import StorageError

def _evidence_root(sink, evidence):
    if not evidence:
        return None
    obj = {"format": _changes.EVIDENCE_FORMAT, "version": 1}
    obj.update(evidence)
    obj["binding"] = "checkpoint and events are not an atomic revision; seq ranges are what was observed before this save"
    return sink.put(canonical(obj))


def _new_change_root(parent, prepared, sink, evidence):
    root = {
        "format": "vestigraph.changeset", "version": 1, "id": uuid.uuid4().hex,
        "document_id": None,
        "from_checkpoint_id": parent["id"] if parent else None,
        "to_checkpoint_id": prepared.checkpoint_id,
        "algorithm": policy.CHANGESET_ALGORITHM,
        "coverage": {"status": "baseline", "basis": "serialized_records_except_verified_timestamps",
                     "limitations": ["not_geometry_equivalence", "not_edit_intent"]},
        "file": {
            "before_raw_sha256": parent["sha256"] if parent else None,
            "after_raw_sha256": prepared.raw_sha256,
            "before_normalized_sha256": None, "after_normalized_sha256": prepared.normalized_sha256,
            "normalized_algorithm": prepared.manifest["normalized_algorithm"],
            "raw_bytes_equal": None, "normalized_bytes_equal": None,
            "geometry_equivalent": "not_evaluated"},
        "summary": {k: None for k in ("cells_added", "cells_removed", "cells_changed", "cells_timestamp_only",
                                      "cells_reordered", "reference_records_added", "reference_records_removed",
                                      "units_changed", "library_context_changed")},
        "entries_root": None, "evidence_root": _evidence_root(sink, evidence),
    }
    return root



def vesti_unavailable_changes(parent, prepared, sink, evidence):
    root = _new_change_root(parent, prepared, sink, evidence)
    if parent is None:
        return root
    pm = parent["manifest"]
    if not isinstance(pm, dict):
        raise StorageError("Previous manifest must be an object")
    root["file"]["raw_bytes_equal"] = parent["sha256"] == prepared.raw_sha256
    if pm.get("format") != 2:
        reason = "previous version has no format-2 index"
    else:
        root["file"]["before_normalized_sha256"] = pm.get("normalized_sha256")
        if pm.get("normalized_algorithm") == prepared.manifest["normalized_algorithm"]:
            root["file"]["normalized_bytes_equal"] = pm.get("normalized_sha256") == prepared.normalized_sha256
        reason = "one side has no cell index"
    root["coverage"].update(status="unavailable", reason=reason)
    return root
