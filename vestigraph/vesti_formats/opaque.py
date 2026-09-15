"""Lossless fallback for files with no installed structural provider."""
import hashlib
from pathlib import Path
from .contract import VestiFormatHandler, VestiScanResult, VESTI_LEGACY_PROFILE
from .restore import vesti_restore_parts, VestiRawTransform
from .changes import vesti_unavailable_changes
from ..storage import policy
from ..storage.metadata import SequenceWriter
from ..storage.object_writer import ObjectWriter
from ..storage.timing import _timed_sink

class VestiOpaqueSink(ObjectWriter):
    def __init__(self, prepared, index, work, monitor):
        super().__init__(prepared, index, work, monitor)
        self.recipe = SequenceWriter(self)
        self.layout_refs = 0

    @_timed_sink
    def chunk(self, digest, data):
        self.layout_refs += 1
        self.monitor.bytes += len(data)
        self.work.execute("INSERT OR IGNORE INTO seen(hash) VALUES(?)", (digest,))
        self._enqueue(digest, data)

    @_timed_sink
    def segment(self, entry, chunks):
        if 2 <= len(chunks) <= policy.INLINE_CHUNKS:
            entry["chunks"] = list(chunks)
        elif len(chunks) > policy.INLINE_CHUNKS:
            writer = SequenceWriter(self)
            for key in chunks:
                writer.add(key)
            entry["chunks_ref"] = writer.finish()
        self.recipe.add(entry)

class VestiOpaqueHandler(VestiFormatHandler):
    format_id = "OPAQUE"

    def __init__(self, kind="unknown", reason="not a GDS2 stream"):
        # Preserve historical metadata; this does not claim an OASIS parser exists.
        self.kind, self.reason = kind, reason

    def create_sink(self, prepared, index, work, monitor):
        return VestiOpaqueSink(prepared, index, work, monitor)

    def load_previous(self, index, parent, work):
        return False

    def scan(self, stream, sink, monitor, preference):
        whole = hashlib.sha256()
        result = VestiScanResult()
        chunks = []
        while True:
            data = stream.read(policy.OPAQUE_CHUNK)
            if not data:
                break
            whole.update(data)
            result.size += len(data)
            digest = hashlib.sha256(data).hexdigest()
            chunks.append(digest)
            result.chunk_refs += 1
            sink.chunk(digest, data)
        result.raw_sha256 = result.normalized_sha256 = whole.hexdigest()
        result.segments = 1
        sink.segment({"kind": "opaque", "size": result.size, "hash": result.raw_sha256}, chunks)
        return result, {"backend": "none"}

    def finish_snapshot(self, sink, result):
        return {"profile": VESTI_LEGACY_PROFILE, "normalized_sha256": result.raw_sha256,
                "normalized_algorithm": "raw",
                "format_analysis": {"status": "unavailable", "kind": self.kind, "reason": self.reason},
                "recipe": sink.recipe.finish(), "timestamps": {"count": 0, "runs": []},
                "top_cells": None}

    def build_changes(self, index, parent, prepared, sink, analyze, prev_loaded, evidence):
        return vesti_unavailable_changes(parent, prepared, sink, evidence)

    def restore_parts(self, root, index):
        timestamps = root.get("timestamps") or {}
        if timestamps != {"count": 0, "runs": []}:
            from ..storage.errors import StorageError
            raise StorageError("Invalid opaque timestamp metadata")
        return vesti_restore_parts(root, index, VestiRawTransform())

    def fingerprint(self, path):
        digest = hashlib.sha256()
        with Path(path).open("rb") as stream:
            for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
