"""Translate scanner callbacks into recipe pages, cell indices and timestamp runs."""
import json

from ...storage import policy
from ...storage.errors import StorageError
from ...storage.metadata import SequenceWriter, canonical
from ...storage.object_writer import ObjectWriter
from ..recipe import _b64
from ...storage.timing import _timed_sink

class ScanSink(ObjectWriter):
    """An object sink with scanner-specific recipe/index state."""

    def __init__(self, prepared, index, work, monitor=None):
        super().__init__(prepared, index, work, monitor)
        self.recipe = SequenceWriter(self)
        self.runs = []                   # [hex, count] while <= policy.RUNS_MEMORY_LIMIT
        self.runs_spilled = 0            # runs already moved to the work db
        self.runs_count = 0
        self.layout_refs = 0
        self.slot = 0
        self.ordinal = 0
        self.lib_head_hash = None
        self.lib_head_units = None

    @_timed_sink
    def references(self, targets):
        """All SNAME targets of the scan (not only the bounded per-cell summary): a cell that is
        referenced only beyond a cell's refs cap must still not count as a top cell."""
        self.work.executemany("INSERT OR IGNORE INTO refs(target) VALUES(?)", ((t,) for t in targets))

    @_timed_sink
    def chunk(self, digest, data, name=None, offset=None):
        self.layout_refs += 1
        self.monitor.bytes += len(data)
        self.work.execute("INSERT OR IGNORE INTO seen(hash) VALUES(?)", (digest,))
        context = (name, offset) if (name is not None and offset is not None) else None
        self._enqueue(digest, data, context=context)

    def _matching_base(self, context, payload_size):
        name, offset = context
        end = offset + payload_size
        return self.work.execute(
            "SELECT hash, start, size FROM prev_chunks WHERE name=? AND start < ? AND start + size > ? "
            "ORDER BY min(start + size, ?) - max(start, ?) DESC, start ASC, hash ASC LIMIT 1",
            (name, end, offset, end, offset)).fetchone()

    # ---- scan sink ----
    @_timed_sink
    def segment(self, entry, chunk_hashes, timestamp):
        self._segment(entry, chunk_hashes, timestamp)

    def _segment(self, entry, chunk_hashes, timestamp):
        kind = entry["kind"]
        raw_name = refs_json = None
        truncated = False
        ref_records = None
        if kind == "cell":
            raw_name = entry["name"]
            entry["name"] = _b64(raw_name)
            refs = entry.pop("refs", None)
            ref_records = entry.get("ref_records", 0)
            truncated = bool(entry.get("refs_truncated"))
            if refs:
                entry["refs"] = {_b64(k): v for k, v in refs.items()}
                refs_json = json.dumps(entry["refs"], sort_keys=True, separators=(",", ":"))
        elif kind == "lib_head":
            self.lib_head_hash = entry["hash"]
            self.lib_head_units = entry.get("units")
        if len(chunk_hashes) == 1:
            if chunk_hashes[0] != entry["hash"]:
                raise StorageError("internal: single-chunk segment hash mismatch")
        elif 2 <= len(chunk_hashes) <= policy.INLINE_CHUNKS:
            entry["chunks"] = list(chunk_hashes)
        elif len(chunk_hashes) > policy.INLINE_CHUNKS:
            writer = SequenceWriter(self)
            for h in chunk_hashes:
                writer.add(h)
            entry["chunks_ref"] = writer.finish()
        slot = None
        if timestamp is not None:
            self._add_run(timestamp.hex())
            slot = self.slot
            self.slot += 1
        if kind == "cell":
            self.work.execute("INSERT INTO cells VALUES(?,?,?,?,?,?,?)",
                              (self.ordinal, raw_name, entry["hash"], slot, refs_json, int(truncated), ref_records))
        self.ordinal += 1
        self.recipe.add(entry)

    def _add_run(self, value):
        self.runs_count += 1
        if self.runs and self.runs[-1][0] == value:
            self.runs[-1][1] += 1
            return
        self.runs.append([value, 1])
        if len(self.runs) > policy.RUNS_MEMORY_LIMIT:
            self._spill_runs()

    def _spill_runs(self):
        body, self.runs = self.runs[:-1], self.runs[-1:]
        self.work.executemany("INSERT INTO runs VALUES(?,?,?)",
                              [(self.runs_spilled + i, r[0], r[1]) for i, r in enumerate(body)])
        self.runs_spilled += len(body)

    def iter_runs(self):
        for _, value, count in self.work.execute("SELECT idx, value, count FROM runs ORDER BY idx"):
            yield [value, count]
        for run in self.runs:
            yield list(run)

    def finish_timestamps(self):
        if not self.runs_spilled and len(canonical(self.runs)) <= policy.INLINE_RUNS_BYTES:
            return {"count": self.runs_count, "runs": [list(r) for r in self.runs]}
        writer = SequenceWriter(self)
        for run in self.iter_runs():
            writer.add(run)
        return {"count": self.runs_count, "runs_ref": writer.finish()}

    def duplicate_names(self) -> bool:
        return self.work.execute("SELECT 1 FROM cells GROUP BY name HAVING count(*) > 1 LIMIT 1").fetchone() is not None

    def iter_top_cells(self):
        """Names never referenced by any cell, in file order (streamed from the work db)."""
        query = "SELECT name FROM cells WHERE name NOT IN (SELECT target FROM refs) ORDER BY ordinal"
        for (name,) in self.work.execute(query):
            yield name

    def finish_top_cells(self, available):
        if not available:
            return None
        head = []
        for name in self.iter_top_cells():
            head.append(_b64(name))
            if len(head) > 256:
                break
        if len(head) <= 256 and len(canonical(head)) <= policy.INLINE_TOP_BYTES:
            return head
        writer = SequenceWriter(self)
        for name in self.iter_top_cells():
            writer.add(_b64(name))
        return {"ref": writer.finish()}
