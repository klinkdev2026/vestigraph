"""Build stored version-change evidence from current/previous work indices.

Does not interpret geometry, publish checkpoints or depend on engine.py.
"""
import base64
import bisect
import json
import uuid
from array import array

from ...storage import changes as _changes, policy
from ...storage.errors import StorageError, CancelledError, CORRUPT_DATA_ERRORS
from ...storage.metadata import SequenceWriter, canonical
from ..recipe import _b64, _iter_recipe, _entry_chunks
from .recipe import _iter_run_table, _RunLookup

def _parent_manifest(parent):
    pm = parent["manifest"]
    if not isinstance(pm, dict):
        raise StorageError("Previous manifest must be an object")
    if not isinstance(pm.get("format_analysis") or {}, dict):
        raise StorageError("Previous format analysis must be an object")
    return pm


def _load_previous(index, parent, work):
    """Previous version's cells (for the change record) and chunk byte ranges (delta bases)
    into the work database, streamed page by page. False when there is nothing usable."""
    if parent is None:
        return False
    try:
        pm = _parent_manifest(parent)
        if pm.get("format") != 2:
            return False
        analysis = pm.get("format_analysis") or {}
        if (analysis.get("status") != "complete" or analysis.get("kind") != "gds2"
                or pm.get("profile") != "gds-record-cdc-v1"
                or pm.get("normalized_algorithm") != "gds-timestamp-zero-v1"):
            return False
        slot = 0
        rows, chunk_rows = [], []
        for ordinal, entry in enumerate(_iter_recipe(pm, index)):
            kind = entry.get("kind")
            this_slot = None
            if "stamp" in entry:
                this_slot, slot = slot, slot + 1
            if kind == "lib_head":
                work.execute("INSERT INTO prev_cells VALUES(?,?,?,?,?,?,?)",
                             (ordinal, b"\x00lib_head", entry["hash"], this_slot, entry.get("units"), 0, None))
                continue
            if kind != "cell":
                continue
            refs = entry.get("refs")
            name = base64.b64decode(entry["name"])
            rows.append((ordinal, name, entry["hash"], this_slot,
                         json.dumps(refs, sort_keys=True, separators=(",", ":")) if refs else None,
                         int(bool(entry.get("refs_truncated"))), entry.get("ref_records")))
            if index.codecs.delta_selection.base_eligible(entry.get("size", 0)):
                start = 0
                for h in _entry_chunks(entry, index):
                    row = index.row(h)
                    size = row["raw_size"] if row is not None else None
                    if size is None:
                        break
                    chunk_rows.append((name, start, size, h))
                    start += size
            if len(rows) >= 1000:
                work.executemany("INSERT INTO prev_cells VALUES(?,?,?,?,?,?,?)", rows)
                rows = []
            if len(chunk_rows) >= 1000:
                work.executemany("INSERT INTO prev_chunks VALUES(?,?,?,?)", chunk_rows)
                chunk_rows = []
        if rows:
            work.executemany("INSERT INTO prev_cells VALUES(?,?,?,?,?,?,?)", rows)
        if chunk_rows:
            work.executemany("INSERT INTO prev_chunks VALUES(?,?,?,?)", chunk_rows)
        return True
    except CancelledError:
        raise
    except CORRUPT_DATA_ERRORS as exc:
        index.previous_corruption = str(exc)[:500]
        work.execute("DELETE FROM prev_cells")
        work.execute("DELETE FROM prev_chunks")
        return False


from ..changes import _evidence_root, _new_change_root


def _timestamp_lookups(pm, index, sink, warnings):
    # timestamp lookups over bounded run tables
    prev_runs = []
    ts_available = not sink.runs_spilled
    if ts_available:
        for run in _iter_run_table(pm, index):
            prev_runs.append(run)
            if len(prev_runs) > policy.RUNS_MEMORY_LIMIT:
                ts_available = False
                break
    prev_lookup = _RunLookup(prev_runs) if ts_available else None
    cur_lookup = _RunLookup(sink.runs) if ts_available else None
    if not ts_available:
        warnings.append("too many timestamp runs; per-cell timestamp comparison skipped")

    return ts_available, prev_lookup, cur_lookup


def _reference_changes(key, p_refs, c_refs, entries, basis):
    ref_added = ref_removed = 0
    old_refs = json.loads(p_refs) if p_refs else {}
    new_refs = json.loads(c_refs) if c_refs else {}
    for target in sorted(set(old_refs) | set(new_refs)):
        before_n, after_n = old_refs.get(target, 0), new_refs.get(target, 0)
        delta = after_n - before_n
        if delta > 0:
            ref_added += delta
        elif delta < 0:
            ref_removed -= delta
        if delta:
            entries.add({"kind": "reference.changed", "cell_key": key, "name": _changes.display_name(key),
                         "target": target, "target_name": _changes.display_name(target),
                         "before_count": before_n, "after_count": after_n, "comparison_basis": basis})
    return ref_added, ref_removed


class _ReorderTracker:
    """Minimal reorder set via LIS; one compact predecessor entry per common cell."""

    def __init__(self):
        self.tails = []
        self.tail_idx = array("q")
        self.prev_link = array("q")
        self.new_ords = array("q")

    def observe(self, new_ord):
        j = len(self.new_ords)
        self.new_ords.append(new_ord)
        i = bisect.bisect_left(self.tails, new_ord)
        self.prev_link.append(self.tail_idx[i - 1] if i > 0 else -1)
        if i == len(self.tails):
            self.tails.append(new_ord)
            self.tail_idx.append(j)
        else:
            self.tails[i] = new_ord
            self.tail_idx[i] = j

    def emit(self, work, entries, basis):
        common = len(self.new_ords)
        tails, tail_idx = self.tails, self.tail_idx
        prev_link, new_ords = self.prev_link, self.new_ords
        # cells outside the LIS: walk back from the top of the last pile
        in_lis = set()
        k = tail_idx[-1] if len(tails) else -1
        while k >= 0:
            in_lis.add(k)
            k = prev_link[k]
        reordered = common - len(tails)
        if reordered:
            for j in range(common):
                if j in in_lis:
                    continue
                row = work.execute("SELECT c.name, p.ordinal FROM cells c JOIN prev_cells p ON p.name = c.name "
                                   "WHERE c.ordinal=?", (new_ords[j],)).fetchone()
                key = _b64(row[0])
                entries.add({"kind": "cell.reordered", "cell_key": key, "name": _changes.display_name(key),
                             "ordinal_before": row[1], "ordinal_after": new_ords[j], "comparison_basis": basis,
                             "explanation": "minimal (longest-increasing-subsequence) reorder set, not an observed move"})
        return reordered


def _change_root(index, parent, prepared, sink, analyze, prev_loaded=False, evidence=None):
    root = _new_change_root(parent, prepared, sink, evidence)
    if parent is None:
        return root
    pm = _parent_manifest(parent)
    root["file"]["raw_bytes_equal"] = parent["sha256"] == prepared.raw_sha256
    if pm.get("format") != 2:
        root["coverage"]["status"] = "unavailable"
        root["coverage"]["reason"] = "previous version has no format-2 index"
        return root
    root["file"]["before_normalized_sha256"] = pm.get("normalized_sha256")
    if pm.get("normalized_algorithm") == prepared.manifest["normalized_algorithm"]:
        root["file"]["normalized_bytes_equal"] = pm.get("normalized_sha256") == prepared.normalized_sha256
    cur_ok = prepared.format_analysis["status"] == "complete"
    prev_ok = ((pm.get("format_analysis") or {}).get("status") == "complete"
               and (pm.get("format_analysis") or {}).get("kind") == "gds2"
               and pm.get("profile") == prepared.manifest.get("profile")
               and pm.get("normalized_algorithm") == prepared.manifest["normalized_algorithm"])
    if not (cur_ok and prev_ok):
        root["coverage"]["status"] = "unavailable"
        root["coverage"]["reason"] = "one side has no cell index"
        return root
    if not analyze:
        root["coverage"]["status"] = "unavailable"
        root["coverage"]["reason"] = "analysis disabled for this commit"
        return root
    if prepared.format_analysis.get("duplicate_names") or pm["format_analysis"].get("duplicate_names"):
        root["coverage"]["status"] = "partial"
        root["coverage"]["warnings"] = ["duplicate cell names"]
        return root
    warnings = []
    work = sink.work
    if not prev_loaded:
        raise StorageError("previous version index not loaded")
    head_row = work.execute("SELECT hash, refs FROM prev_cells WHERE name=?", (b"\x00lib_head",)).fetchone()
    lib_head_prev = head_row[0] if head_row else None
    units_prev = head_row[1] if head_row else None
    work.execute("DELETE FROM prev_cells WHERE name=?", (b"\x00lib_head",))
    ts_available, prev_lookup, cur_lookup = _timestamp_lookups(pm, index, sink, warnings)

    added = work.execute("SELECT count(*), coalesce(sum(ref_records), 0), coalesce(sum(ref_records IS NULL), 0) "
                         "FROM cells WHERE name NOT IN (SELECT name FROM prev_cells)").fetchone()
    removed = work.execute("SELECT count(*), coalesce(sum(ref_records), 0), coalesce(sum(ref_records IS NULL), 0) "
                           "FROM prev_cells WHERE name NOT IN (SELECT name FROM cells)").fetchone()
    changed = ts_only = 0
    ref_added, ref_removed = added[1], removed[1]
    refs_known = not (added[2] or removed[2])
    entries = _EntryWriter(sink)
    basis = root["coverage"]["basis"]
    units_changed = (units_prev != sink.lib_head_units) if (units_prev and sink.lib_head_units) else None
    if lib_head_prev != sink.lib_head_hash:
        entries.add({"kind": "file.context_changed", "scope": "lib_head", "comparison_basis": basis,
                     "before": {"hash": lib_head_prev}, "after": {"hash": sink.lib_head_hash},
                     "units_changed": units_changed,
                     "units_before": units_prev, "units_after": sink.lib_head_units})
    reorder = _ReorderTracker()
    query = ("SELECT c.ordinal, p.ordinal, p.hash, c.hash, p.slot, c.slot, p.refs, c.refs, p.truncated, c.truncated, "
             "p.ref_records, c.ref_records, c.name FROM prev_cells p JOIN cells c ON c.name = p.name ORDER BY p.ordinal")
    group = None                                 # open timestamp-only group [from, to] over current ordinals
    for (new_ord, old_ord, p_hash, c_hash, p_slot, c_slot, p_refs, c_refs, p_trunc, c_trunc, p_cnt, c_cnt, name) in work.execute(query):
        reorder.observe(new_ord)
        key = _b64(name)
        if p_hash != c_hash:
            changed += 1
            entries.add({"kind": "cell.changed", "cell_key": key, "name": _changes.display_name(key),
                         "comparison_basis": basis, "ordinal_before": old_ord, "ordinal_after": new_ord,
                         "before": {"hash": p_hash}, "after": {"hash": c_hash}})
        elif (ts_available and p_slot is not None and c_slot is not None
              and prev_lookup.at(p_slot) != cur_lookup.at(c_slot)):
            ts_only += 1
            if group is not None and group[1] + 1 == new_ord:
                group[1] = new_ord
            else:
                if group is not None:
                    entries.add_group(group, basis)
                group = [new_ord, new_ord]
        if p_trunc or c_trunc or p_cnt is None or c_cnt is None:
            refs_known = False
            continue
        if p_refs == c_refs:
            continue
        plus, minus = _reference_changes(key, p_refs, c_refs, entries, basis)
        ref_added += plus
        ref_removed += minus
    if group is not None:
        entries.add_group(group, basis)
    reordered = reorder.emit(work, entries, basis)
    for (name, digest, ordinal, cnt) in work.execute(
            "SELECT name, hash, ordinal, ref_records FROM cells WHERE name NOT IN (SELECT name FROM prev_cells) ORDER BY ordinal"):
        key = _b64(name)
        entries.add({"kind": "cell.added", "cell_key": key, "name": _changes.display_name(key),
                     "hash": digest, "ordinal": ordinal, "ref_records": cnt, "comparison_basis": basis})
    for (name, digest, ordinal, cnt) in work.execute(
            "SELECT name, hash, ordinal, ref_records FROM prev_cells WHERE name NOT IN (SELECT name FROM cells) ORDER BY ordinal"):
        key = _b64(name)
        entries.add({"kind": "cell.removed", "cell_key": key, "name": _changes.display_name(key),
                     "hash": digest, "ordinal": ordinal, "ref_records": cnt, "comparison_basis": basis})
    if not refs_known:
        warnings.append("reference summaries truncated or unknown; reference counts not compared")
    for warning in warnings:
        entries.add({"kind": "coverage.warning", "message": warning})
    root["summary"].update({
        "cells_added": added[0], "cells_removed": removed[0], "cells_changed": changed,
        "cells_timestamp_only": ts_only if ts_available else None,
        "cells_reordered": reordered,
        "reference_records_added": ref_added if refs_known else None,
        "reference_records_removed": ref_removed if refs_known else None,
        "library_context_changed": lib_head_prev != sink.lib_head_hash,
        "units_changed": units_changed,
    })
    root["entries_root"] = entries.finish()
    root["coverage"]["status"] = "partial" if warnings else "complete"
    if warnings:
        root["coverage"]["warnings"] = warnings
    return root


class _EntryWriter:
    """Entries go into pages like any sequence; seq is the stored index."""

    def __init__(self, sink):
        self.sink = sink
        self.writer = SequenceWriter(sink)
        self.kinds = {}
        self.stored = 0
        self.logical = 0

    def add(self, entry, logical=1):
        entry = dict(entry)
        entry["seq"] = self.stored
        self.writer.add(entry)
        self.kinds[entry["kind"]] = self.kinds.get(entry["kind"], 0) + logical
        self.stored += 1
        self.logical += logical

    def add_group(self, group, basis):
        lo, hi = group
        self.add({"kind": "cell.timestamp_only", "group": True, "ordinal_from": lo, "ordinal_to": hi,
                  "count": hi - lo + 1, "comparison_basis": basis}, logical=hi - lo + 1)

    def finish(self):
        ref = self.writer.finish()
        obj = {"format": _changes.ENTRIES_FORMAT, "version": 1, "entries": ref,
               "count": self.stored, "logical_count": self.logical, "kinds": self.kinds}
        return self.sink.put(canonical(obj))
