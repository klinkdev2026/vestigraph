"""Backend-neutral transcript of a scan: the oracle for Python-vs-Rust equivalence (spec P1 §3.3).

A transcript is a list of plain tuples/dicts built ONLY from what the sink protocol delivers
(chunk digest/length/data hash/name/offset, reference batches, segment entries with chunk lists
and timestamps) plus the ScanResult fields. Two backends are equivalent when their transcripts
are equal for the same input, regardless of how the input was sliced.
"""
from __future__ import annotations

import hashlib
import io

from vestigraph.vesti_formats.vesti_format_gds import scan as gds_scan


class TranscriptSink:
    def __init__(self):
        self.events = []
        self.bytes_copied = 0

    def chunk(self, digest, data, name=None, offset=None):
        self.bytes_copied += len(data)
        self.events.append(("chunk", digest, len(data), hashlib.sha256(data).hexdigest(),
                            bytes(name) if name is not None else None, offset))

    def references(self, targets):
        self.events.append(("refs", tuple(bytes(t) for t in targets)))

    def segment(self, entry, chunks, timestamp):
        e = dict(entry)
        if isinstance(e.get("name"), (bytes, bytearray)):
            e["name"] = bytes(e["name"])
        if "refs" in e:
            e["refs"] = tuple((bytes(k), v) for k, v in e["refs"].items())
        self.events.append(("segment", tuple(sorted(e.items(), key=lambda kv: kv[0])),
                            tuple(chunks), bytes(timestamp) if timestamp is not None else None))


def result_fields(result):
    return {k: getattr(result, k) for k in ("raw_sha256", "normalized_sha256", "size", "segments", "cells",
                                             "stamps", "chunk_refs")}


def python_transcript(data: bytes, buffer: int = gds_scan.BUFFER):
    """Transcript of the Python reference scanner reading `data` in `buffer`-sized slices."""
    from unittest.mock import patch
    sink = TranscriptSink()
    with patch.object(gds_scan, "BUFFER", buffer):
        try:
            result = gds_scan.scan_gds(io.BytesIO(data), sink)
        except gds_scan.GdsStructureError as exc:
            return {"error": (exc.reason, exc.offset), "events": sink.events}
    return {"error": None, "events": sink.events, "result": result_fields(result)}


def rust_transcript(data: bytes, buffer: int = 4 * 1024 * 1024):
    """Transcript of the Rust backend through the same adapter the engine would use."""
    from vestigraph.vesti_formats.vesti_format_gds import native as scan_backend
    backend, module, reason = scan_backend.select_backend("rust")
    if module is None:
        raise RuntimeError("rust backend unavailable: %s" % reason)
    sink = TranscriptSink()
    diagnostics = {}
    try:
        result = scan_backend.scan_with_rust(io.BytesIO(data), sink, diagnostics=diagnostics, buffer=buffer)
    except gds_scan.GdsStructureError as exc:
        return {"error": (exc.reason, exc.offset), "events": sink.events, "diagnostics": diagnostics}
    return {"error": None, "events": sink.events, "result": result_fields(result), "diagnostics": diagnostics}


def first_difference(a, b):
    """Human-readable location of the first mismatch between two transcripts (None if equal)."""
    if a.get("error") != b.get("error"):
        return "error: %r vs %r" % (a.get("error"), b.get("error"))
    ea, eb = a["events"], b["events"]
    if a.get("error") is not None:
        # Both backends failed with the same reason at the same byte. The engine discards the
        # attempt, but the events delivered BEFORE the error must still agree: a scanner that
        # diverged earlier and only happened to fail at the same byte is not equivalent.
        for i, (x, y) in enumerate(zip(ea, eb)):
            if x != y:
                return "event %d before the error: %r / %r" % (i, x[:4], y[:4])
        if len(ea) != len(eb):
            return "event count before the error %d vs %d" % (len(ea), len(eb))
        return None
    for i, (x, y) in enumerate(zip(ea, eb)):
        if x != y:
            return "event %d: %r\n   vs %r" % (i, x[:4], y[:4])
    if len(ea) != len(eb):
        return "event count %d vs %d" % (len(ea), len(eb))
    if a.get("result") != b.get("result"):
        return "result: %r vs %r" % (a.get("result"), b.get("result"))
    return None
