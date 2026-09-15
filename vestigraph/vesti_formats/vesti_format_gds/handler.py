"""GDS format provider: one owner for scan, index, changes and reversible timestamps."""
import base64
from ..contract import VestiFormatHandler
from ..restore import vesti_restore_parts
from ...storage.errors import StorageError, CancelledError
from . import scan as scan
from . import native as native
from . import changes as changes
from .sink import ScanSink
from .recipe import VestiGdsTransform
from .fingerprint import content_fingerprint

VESTI_GDS_WORK_SCHEMA = """
CREATE TABLE prev_chunks (name BLOB NOT NULL, start INTEGER NOT NULL, size INTEGER NOT NULL, hash TEXT NOT NULL);
CREATE INDEX prev_chunks_name ON prev_chunks(name, start);
CREATE TABLE cells (ordinal INTEGER PRIMARY KEY, name BLOB NOT NULL, hash TEXT NOT NULL, slot INTEGER,
                    refs TEXT, truncated INTEGER NOT NULL, ref_records INTEGER);
CREATE INDEX cells_name ON cells(name);
CREATE TABLE refs (target BLOB PRIMARY KEY);
CREATE TABLE runs (idx INTEGER PRIMARY KEY, value TEXT NOT NULL, count INTEGER NOT NULL);
CREATE TABLE prev_cells (ordinal INTEGER PRIMARY KEY, name BLOB NOT NULL, hash TEXT NOT NULL, slot INTEGER,
                         refs TEXT, truncated INTEGER NOT NULL, ref_records INTEGER);
CREATE INDEX prev_cells_name ON prev_cells(name);
"""

class VestiGdsHandler(VestiFormatHandler):
    format_id = "GDS2"
    sink_type = ScanSink

    def normalized_legacy(self, repo, parent):
        from .legacy import normalized_legacy
        return normalized_legacy(repo, parent)

    def setup_work(self, work):
        work.executescript(VESTI_GDS_WORK_SCHEMA)

    def create_sink(self, prepared, index, work, monitor):
        return self.sink_type(prepared, index, work, monitor)

    def load_previous(self, index, parent, work):
        return changes._load_previous(index, parent, work)

    def scan(self, stream, sink, monitor, preference):
        try:
            backend, module, reason = native.select_backend(preference)
        except RuntimeError as exc:
            raise StorageError(str(exc)) from exc
        diag = {"backend": backend, "reason": reason}
        if backend == "python":
            return scan.scan_gds(stream, sink), diag
        try:
            result = native.scan_with_rust(stream, sink, cancel=monitor.cancel, diagnostics=diag)
        except module.ScanCancelled as exc:
            raise CancelledError("Save cancelled by the user; nothing was published.") from exc
        return result, diag

    def finish_snapshot(self, sink, result):
        recipe = sink.recipe.finish()
        timestamps = sink.finish_timestamps()
        duplicate = sink.duplicate_names()
        tops = sink.finish_top_cells(not duplicate)
        if result.stamps != sink.runs_count:
            raise StorageError("internal: timestamp slot count mismatch")
        return {"profile": scan.PROFILE, "normalized_sha256": result.normalized_sha256,
                "normalized_algorithm": scan.NORMALIZED_ALGORITHM,
                "format_analysis": {"status": "complete", "kind": "gds2", "duplicate_names": duplicate},
                "recipe": recipe, "timestamps": timestamps, "top_cells": tops}

    def build_changes(self, index, parent, prepared, sink, analyze, prev_loaded, evidence):
        return changes._change_root(index, parent, prepared, sink, analyze, prev_loaded, evidence)

    def restore_parts(self, root, index):
        return vesti_restore_parts(root, index, VestiGdsTransform(root, index))

    def snapshot_top_cells(self, manifest):
        tops = manifest.get("top_cells")
        return None if tops is None else ([base64.b64decode(t) for t in tops] if isinstance(tops, list) else "paged")

    def fingerprint(self, path):
        return content_fingerprint(path)

    def noop_probe(self, repo, parent, size):
        from .noop import NoopProbe
        return NoopProbe(repo, parent, size)
