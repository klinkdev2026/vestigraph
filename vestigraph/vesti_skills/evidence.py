"""A logical history window, not an inferred physical edit chain."""
import json
from .. import history_order
from ..service import queries
from ..service.catalog import fingerprint
from ..service.errors import bad_request, conflict
from ..store import RepositoryError

MAX_VERSIONS = 100
MAX_EVIDENCE_BYTES = 512 * 1024


class HistoryWindow:
    id = "history-window"
    api_version = 1
    label = "Saved version interval"

    def collect(self, app, document, selection):
        if not isinstance(selection, dict) or set(selection) != {"from_id", "to_id", "history_revision"}:
            raise bad_request("Select two versions and supply the history revision.")
        start, end = selection["from_id"], selection["to_id"]
        if not all(isinstance(x, str) and 1 <= len(x) <= 128 for x in (start, end)):
            raise bad_request("Invalid source version IDs.")
        if type(selection["history_revision"]) is not int:
            raise bad_request("History revision must be an integer.")
        _, repo = app._store(document["id"])
        with app._presentation_lock:
            with repo._connect() as db:
                db.execute("BEGIN")
                revision = history_order.revision(db)
                if revision != selection["history_revision"]:
                    raise conflict("SKILL_HISTORY_CHANGED", "History order changed; reselect the two versions.")
                ordered = history_order.enabled(db)
                source = "checkpoints c JOIN history_timeline h ON h.checkpoint_id=c.id" if ordered else "checkpoints c"
                position = "h.position" if ordered else "c.ordinal"
                ends = db.execute(f"SELECT c.id,{position} AS pos FROM {source} WHERE c.id IN (?,?) AND " +
                                  history_order.visible(db), (start, end)).fetchall()
                ranks = {r["id"]: r["pos"] for r in ends}
                if len(ranks) != 2 or ranks.get(start, 0) >= ranks.get(end, 0):
                    raise bad_request("Choose distinct versions, with the start older than the end.")
                rows = db.execute(f"SELECT c.* FROM {source} WHERE {position} BETWEEN ? AND ? AND " +
                                  history_order.visible(db) + f" ORDER BY {position} LIMIT ?",
                                  (ranks[start], ranks[end], MAX_VERSIONS + 1)).fetchall()
                if len(rows) > MAX_VERSIONS:
                    raise bad_request("Choose a shorter interval (at most 100 versions).")
                records = [repo._row(r) for r in rows]
                segments = sorted({r["segment_id"] for r in records if r["segment_id"]})
                events = []
                if segments:
                    marks = ",".join("?" for _ in segments)
                    events = [dict(r) for r in db.execute(
                        f"SELECT seq,created_at,kind,source,segment_id,payload FROM events WHERE segment_id IN ({marks}) ORDER BY seq LIMIT 101", segments)]
            artifacts, changes = [], []
            for i, record in enumerate(records):
                item = queries.checkpoint_summary(record, formats=app.services.formats)
                item["history_parent_id"] = records[i-1]["id"] if i else None
                item["history_parent_scope"] = "selected_window"
                item["import_note"] = (record["metadata"].get("legacy_import") or {}).get("note", "")
                artifacts.append(item)
                if not i:
                    continue
                previous = records[i-1]["id"]
                if record["parent_id"] != previous:
                    changes.append({"from_id": previous, "to_id": record["id"],
                                    "status": "unavailable", "reason": "physical_parent_differs_from_logical_predecessor",
                                    "recorded_parent_id": record["parent_id"]})
                    continue
                try:
                    page = repo.changes(record["id"], limit=20)
                    root = page.get("root") or {}
                    changes.append({"from_id": previous, "to_id": record["id"],
                                    "status": page["status"], "summary": root.get("summary"),
                                    "coverage": root.get("coverage"), "entries": page.get("items", []),
                                    "next_cursor": page.get("next_cursor"),
                                    "read_more": {"checkpoint_id": record["id"], "kind": "repository_changes"}})
                except RepositoryError as exc:
                    changes.append({"from_id": previous, "to_id": record["id"],
                                    "status": "unavailable", "reason": str(exc)[:300]})
        ids = [r["id"] for r in records]
        targets = ids + segments
        with app.catalog._db() as db:
            marks = ",".join("?" for _ in targets)
            notes = [dict(r) for r in db.execute(
                f"SELECT id,target_type,target_id,text,created_at FROM annotations WHERE document_id=? AND target_id IN ({marks}) ORDER BY ordinal LIMIT 101",
                (document["id"], *targets))]
        parsed_events = []
        for event in events[:100]:
            event["payload"] = json.loads(event["payload"])
            parsed_events.append(event)
        result = {"schema": "vestigraph.skill_evidence", "version": 1, "source_provider": self.id,
                  "document_id": document["id"], "document_name": document["name"],
                  "selection": dict(selection), "artifacts": artifacts, "changes": changes,
                  "annotations": notes[:100], "events": parsed_events,
                  "coverage": {"versions": "complete_logical_window",
                               "changes": "recorded_diffs_only_where_parent_matches",
                               "events": "observed_segments_not_atomic_edit_trace",
                               "annotation_limit_reached": len(notes) > 100,
                               "event_limit_reached": len(events) > 100,
                               "layout_payloads": "not_included",
                               "design_intent": "only_user_explanation_and_annotations"}}
        if len(json.dumps(result, ensure_ascii=False).encode()) > MAX_EVIDENCE_BYTES:
            raise bad_request("Evidence exceeds 512 KiB; choose a shorter interval.")
        result["digest"] = fingerprint(result)
        return result
