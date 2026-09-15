"""Strict streaming GDS2 record scan with lossless timestamp normalization and
record-aligned content-defined chunking (FORMAT §4/§5, profile gds-record-cdc-v1).

One pass over the file, 4 MiB slices, no whole-file or whole-cell buffering.
The scanner never parses geometry; it only frames records, tracks the
library/cell structure, zeroes the 24-byte timestamps of real BGNLIB/BGNSTR
records, and hands normalized chunks + per-segment entries to a sink:

    sink.chunk(digest_hex, data[, name, offset])  # normalized chunk, in order; cell chunks carry
                                                  # the raw STRNAME and their offset in the segment
    sink.references(targets)                     # every SNAME target seen, in batches (may repeat)
    sink.segment(entry, chunk_hashes, timestamp)  # after the segment's last chunk

Cell entries carry the raw STRNAME bytes (`name`), the bounded SNAME multiset
(`refs`, at most REFS_MAX_DISTINCT keys, `refs_truncated` when more targets were
seen) and the exact number of SREF/AREF records (`ref_records`). Top cells and
duplicate names are the sink's business: the scanner keeps no per-file tables.

Structure violations raise GdsStructureError; the caller falls back to the
opaque path. Nothing is written by this module.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import zlib

PROFILE = "gds-record-cdc-v1"
NORMALIZED_ALGORITHM = "gds-timestamp-zero-v1"
CDC_MIN = 64 * 1024
CDC_MAX = 1024 * 1024
CDC_MASK = 0x0FFF
CDC_MIN_RECORD = 12
BUFFER = 4 * 1024 * 1024
MAX_RECORD = 65534

GDS_MAGIC = b"\x00\x06\x00\x02"
ZERO24 = bytes(24)

HEADER, BGNLIB, LIBNAME, UNITS, ENDLIB, BGNSTR, STRNAME, ENDSTR = 0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07
SNAME = 0x12
STRUCTURAL = frozenset((HEADER, BGNLIB, UNITS, ENDLIB, BGNSTR, STRNAME, ENDSTR, SNAME))

REFS_MAX_DISTINCT = 1024
REFS_MAX_BYTES = 32 * 1024
TARGET_BATCH = 1024
TARGET_BATCH_BYTES = 8 * 1024 * 1024   # == scan_core MAX_BATCH_PAYLOAD_BYTES: both scanners split identically


from ..contract import VestiFormatError, VestiScanResult

class GdsStructureError(VestiFormatError):
    pass

ScanResult = VestiScanResult


def is_gds(head: bytes) -> bool:
    return head[:4] == GDS_MAGIC


def scan_gds(stream, sink) -> ScanResult:
    """Scan an open binary stream positioned at 0. See module docstring."""
    raw = hashlib.sha256()
    normalized = hashlib.sha256()
    result = ScanResult()
    crc32 = zlib.crc32
    sha256 = hashlib.sha256

    # --- chunk / segment accumulation state ---
    parts = []            # pieces (memoryview/bytes) of the open chunk
    seg_len = 0           # bytes since last cut within the segment
    seg_hash = None       # sha256 of the segment's normalized bytes
    seg_size = 0
    seg_chunks = []
    seg_kind = None
    seg_abs_start = 0     # absolute file offset of the segment start
    seg_name = None
    seg_stamp = None      # offset of the 24-byte timestamp in the segment
    seg_timestamp = None  # original 24 bytes
    seg_refs = None       # Counter of SNAME payloads (bounded to REFS_MAX_DISTINCT keys)
    seg_refs_truncated = False
    seg_ref_count = 0     # every SREF/AREF record, including those beyond the cap
    seg_units = None      # raw UNITS payload of the library header
    seg_targets = []      # SNAME targets not yet handed to the sink (bounded batch)
    seg_targets_bytes = 0 # payload bytes in seg_targets (the Rust scanner splits on this too)


    def emit_chunk():
        nonlocal parts, seg_len
        if not parts:
            return
        data = parts[0] if len(parts) == 1 else b"".join(parts)
        data = bytes(data)
        parts = []
        seg_len = 0
        if not data:
            return
        digest = sha256(data).hexdigest()
        seg_hash.update(data)
        normalized.update(data)
        seg_chunks.append(digest)
        result.chunk_refs += 1
        if seg_kind == "cell" and seg_name is not None:
            sink.chunk(digest, data, seg_name, seg_size - len(data))
        else:
            sink.chunk(digest, data)

    def begin_segment(kind, abs_start):
        nonlocal seg_units
        seg_units = None
        nonlocal seg_hash, seg_size, seg_chunks, seg_kind, seg_abs_start, seg_name, seg_stamp
        nonlocal seg_timestamp, seg_refs, seg_refs_truncated, seg_len, parts, seg_ref_count
        seg_hash, seg_size, seg_chunks, seg_kind = sha256(), 0, [], kind
        seg_abs_start, seg_name, seg_stamp, seg_timestamp = abs_start, None, None, None
        seg_refs, seg_refs_truncated, seg_len, parts, seg_ref_count = Counter(), False, 0, [], 0

    def end_segment():
        nonlocal seg_targets, seg_targets_bytes
        emit_chunk()
        if seg_targets:
            sink.references(seg_targets)
            seg_targets = []
            seg_targets_bytes = 0
        entry = {"kind": seg_kind, "size": seg_size, "hash": seg_hash.hexdigest()}
        if seg_stamp is not None:
            entry["stamp"] = seg_stamp
            result.stamps += 1
        if seg_kind == "lib_head" and seg_units is not None:
            entry["units"] = seg_units.hex()
        if seg_kind == "cell":
            entry["name"] = seg_name
            entry["ref_records"] = seg_ref_count
            refs = seg_refs
            truncated = seg_refs_truncated
            if refs:
                approx = sum(len(k) * 4 // 3 + 12 for k in refs)
                if approx > REFS_MAX_BYTES:
                    kept, total = {}, 0
                    for key, count in refs.items():
                        item = len(key) * 4 // 3 + 12
                        if total + item > REFS_MAX_BYTES:
                            truncated = True
                            break
                        kept[key] = count
                        total += item
                    refs = kept
                entry["refs"] = {k: v for k, v in refs.items()}
                if truncated:
                    entry["refs_truncated"] = True
        result.segments += 1
        sink.segment(entry, seg_chunks, seg_timestamp)

    # --- parse state ---
    state = "start"       # start -> head -> (cell | between)* -> tail(trailer)
    expect_strname = False
    units_seen = False
    tail = b""
    abs_base = 0          # absolute offset of data[0]
    piece_start = 0
    trailer = False
    ended = False

    begin_segment("lib_head", 0)
    while True:
        chunk_in = stream.read(BUFFER)
        if not chunk_in:
            break
        raw.update(chunk_in)
        result.size += len(chunk_in)
        if tail:
            data = tail + chunk_in
            abs_base -= len(tail)
            tail = b""
        else:
            data = chunk_in
        mv = memoryview(data)
        n = len(data)
        pos = 0
        piece_start = 0
        if trailer:
            pos = 0
        else:
            while pos + 4 <= n:
                length = (data[pos] << 8) | data[pos + 1]
                if length < 4 or length & 1:
                    raise GdsStructureError("bad record length %d" % length, abs_base + pos)
                end = pos + length
                if end > n:
                    break
                rtype = data[pos + 2]
                if rtype in STRUCTURAL:
                    abs_pos = abs_base + pos
                    if rtype == BGNSTR:
                        if state == "start" or state == "cell":
                            raise GdsStructureError("BGNSTR in an illegal position", abs_pos)
                        if not units_seen:
                            raise GdsStructureError("BGNSTR before UNITS", abs_pos)
                        if state == "head":
                            if pos > piece_start:
                                parts.append(mv[piece_start:pos])
                                seg_size += pos - piece_start
                            end_segment()
                        begin_segment("cell", abs_pos)
                        result.cells += 1
                        piece_start = pos
                        if length == 28 and data[pos + 3] == 2:      # real BGNSTR: 12 x int16
                            parts.append(mv[pos:pos + 4])
                            parts.append(ZERO24)
                            seg_timestamp = bytes(mv[pos + 4:end])
                            seg_stamp = 4
                            seg_size += 28
                            seg_len += 28
                            piece_start = end
                        else:
                            seg_len += length
                        state = "cell"
                        expect_strname = True
                        pos = end
                        continue
                    if rtype == STRNAME:
                        if not expect_strname:
                            raise GdsStructureError("STRNAME not directly after BGNSTR", abs_pos)
                        expect_strname = False
                        seg_name = bytes(mv[pos + 4:end])
                    elif rtype == ENDSTR:
                        if state != "cell" or expect_strname:
                            raise GdsStructureError("ENDSTR outside a cell", abs_pos)
                        parts.append(mv[piece_start:end])
                        seg_size += end - piece_start
                        seg_len += length
                        end_segment()
                        begin_segment("lib_tail", abs_base + end)
                        piece_start = end
                        state = "between"
                        pos = end
                        continue
                    elif rtype == SNAME:
                        if state != "cell":
                            raise GdsStructureError("SNAME outside a cell", abs_pos)
                        if True:
                            target = bytes(mv[pos + 4:end])
                            if target in seg_refs or len(seg_refs) < REFS_MAX_DISTINCT:
                                seg_refs[target] += 1
                            else:
                                seg_refs_truncated = True     # bounded: never accumulate beyond the cap
                            seg_ref_count += 1
                            # Batch boundaries are part of the transcript both scanners must agree on
                            # (spec §3.3): split by count AND by payload bytes exactly like scan_core.
                            if seg_targets and (len(seg_targets) >= TARGET_BATCH
                                                or seg_targets_bytes + len(target) > TARGET_BATCH_BYTES):
                                sink.references(seg_targets)
                                seg_targets = []
                                seg_targets_bytes = 0
                            seg_targets_bytes += len(target)
                            seg_targets.append(target)        # the full multiset still reaches the sink
                            if len(seg_targets) >= TARGET_BATCH or seg_targets_bytes >= TARGET_BATCH_BYTES:
                                sink.references(seg_targets)
                                seg_targets = []
                                seg_targets_bytes = 0
                    elif rtype == HEADER:
                        if state != "start":
                            raise GdsStructureError("HEADER not first", abs_pos)
                        state = "head"
                    elif rtype == BGNLIB:
                        if state != "head" or abs_pos != 6 or seg_stamp is not None:
                            raise GdsStructureError("BGNLIB not directly after HEADER", abs_pos)
                        if length == 28 and data[pos + 3] == 2:      # real BGNLIB: 12 x int16
                            parts.append(mv[piece_start:pos + 4])
                            parts.append(ZERO24)
                            seg_size += pos + 4 - piece_start + 24
                            seg_len += length
                            seg_timestamp = bytes(mv[pos + 4:end])
                            seg_stamp = abs_pos - seg_abs_start + 4
                            piece_start = end
                            pos = end
                            continue
                    elif rtype == UNITS:
                        if state != "head":
                            raise GdsStructureError("UNITS outside the library header", abs_pos)
                        units_seen = True
                        seg_units = bytes(mv[pos + 4:end])
                    elif rtype == ENDLIB:
                        if state == "cell" or state == "start":
                            raise GdsStructureError("ENDLIB inside a cell or before HEADER", abs_pos)
                        if state == "head":
                            if pos > piece_start:
                                parts.append(mv[piece_start:pos])
                                seg_size += pos - piece_start
                            end_segment()
                            begin_segment("lib_tail", abs_pos)
                            piece_start = pos
                        parts.append(mv[piece_start:end])
                        seg_size += end - piece_start
                        seg_len += length
                        piece_start = end
                        pos = end
                        trailer = True
                        ended = True
                        break
                else:
                    if state == "start":
                        raise GdsStructureError("first record is not HEADER", abs_base + pos)
                    if state == "between":
                        raise GdsStructureError("record between ENDSTR and BGNSTR", abs_base + pos)
                    if expect_strname:
                        raise GdsStructureError("STRNAME not directly after BGNSTR", abs_base + pos)
                # plain record: chunk accounting
                seg_len += length
                if seg_len >= CDC_MIN and ((length >= CDC_MIN_RECORD and crc32(mv[pos:end]) & CDC_MASK == 0)
                                           or seg_len >= CDC_MAX):
                    parts.append(mv[piece_start:end])
                    seg_size += end - piece_start
                    piece_start = end
                    emit_chunk()
                pos = end
        if trailer:
            # everything from pos on is trailer bytes of lib_tail; fixed 1 MiB cuts
            while pos < n:
                if seg_len >= CDC_MAX:          # never let `take` go to zero or negative
                    emit_chunk()
                take = min(n - pos, CDC_MAX - seg_len)
                assert take > 0
                parts.append(bytes(mv[pos:pos + take]))
                seg_size += take
                seg_len += take
                pos += take
                if seg_len >= CDC_MAX:
                    emit_chunk()
            piece_start = n
            tail = b""
        else:
            if pos > piece_start:
                parts.append(bytes(mv[piece_start:pos]))
                seg_size += pos - piece_start
            # carry the partial record; pieces already copied out of this slice
            tail = data[pos:] if pos < n else b""
            parts = [bytes(p) if isinstance(p, memoryview) else p for p in parts]
        abs_base += n

    if not ended:
        if result.size == 0:
            raise GdsStructureError("empty file", 0)
        raise GdsStructureError("truncated record or missing ENDLIB", abs_base - len(tail))
    end_segment()

    result.raw_sha256 = raw.hexdigest()
    result.normalized_sha256 = normalized.hexdigest()
    return result
