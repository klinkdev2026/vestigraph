"""Synthetic GDS2 byte builder for storage tests: stdlib-only (no bench/
import, no klayout import), deterministic, no randomness without an
explicit ``seed``.

Every builder in this module writes structurally correct big-endian GDS2
records (2-byte length INCLUDING the 4-byte header, 1-byte record type,
1-byte datatype) using only ``struct``. It reimplements the small ideas
already proven in ``bench/storage/gen_gds.py`` (the excess-64 GDS "real8"
encoder, and hash-derived-not-random box geometry so unrelated shapes stay
byte-identical across edits) but does not import that module -- tests must
not depend on bench/.

GDS2 encoding details looked up while writing this (not otherwise obvious
from the record-type table alone):

* STRANS is a 16-bit BITARRAY. Bit 0 (the MSB, mask 0x8000) is the
  "reflect about the X axis" flag, applied BEFORE rotation/magnification/
  translation. Bits 13/14 (ABSA/ABSM, masks 0x0004/0x0002) mark absolute
  (vs. relative) angle/magnification for arrays; unused here, left 0.
* AREF's XY record carries exactly 3 points, in this order: the reference
  (origin) corner, a point displaced from the origin by
  ``col_step * n_columns`` (the "far end of the column direction"), and a
  point displaced by ``row_step * n_rows`` ("far end of the row
  direction") -- NOT the opposite corner of the bounding box.
* A record's declared length is 4 (header) + payload, must stay even, and
  must not exceed 65534 (so the payload itself is even, or gets a single
  NUL pad byte for ASCII strings).
* BOUNDARY's XY is conventionally a *closed* ring (first point repeated as
  the last); PATH's XY is left open.
"""
from __future__ import annotations

import struct
import zlib

# --------------------------------------------------------------------- record types --
HEADER, BGNLIB, LIBNAME, UNITS, ENDLIB = 0x00, 0x01, 0x02, 0x03, 0x04
BGNSTR, STRNAME, ENDSTR = 0x05, 0x06, 0x07
BOUNDARY, PATH, SREF, AREF, TEXT = 0x08, 0x09, 0x0A, 0x0B, 0x0C
LAYER, DATATYPE, WIDTH = 0x0D, 0x0E, 0x0F
XY, ENDEL, SNAME, COLROW = 0x10, 0x11, 0x12, 0x13
TEXTTYPE = 0x16
PRESENTATION = 0x17
STRING = 0x19
STRANS, MAG, ANGLE = 0x1A, 0x1B, 0x1C
PROPATTR, PROPVALUE = 0x2B, 0x2C

DT_NONE, DT_BITARRAY, DT_I16, DT_I32, DT_R4, DT_R8, DT_ASCII = (
    0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06,
)

MAX_RECORD_LEN = 65534

STRANS_REFLECT_X = 0x8000

TS_ZERO = bytes(24)


# ------------------------------------------------------------------------- GDS real8 --
def gds_real8(value: float) -> bytes:
    """Encode a float as an 8-byte GDS "real": excess-64, base-16,
    sign + 7-bit exponent + 56-bit mantissa. NOT IEEE-754."""
    if value == 0.0:
        return b"\x00" * 8
    sign = 0x80 if value < 0 else 0x00
    value = abs(value)
    exponent = 0
    while value >= 1.0:
        value /= 16.0
        exponent += 1
    while value < 1.0 / 16.0:
        value *= 16.0
        exponent -= 1
    mantissa = round(value * (1 << 56))
    if mantissa >= (1 << 56):
        mantissa >>= 4
        exponent += 1
    exponent += 64
    if not (0 <= exponent <= 127):
        raise ValueError(f"GDS real8 exponent out of range for value {value!r}")
    return bytes([sign | exponent]) + mantissa.to_bytes(7, "big")


# --------------------------------------------------------------------- record helpers --
def record(rtype: int, dtype: int = DT_NONE, payload: bytes = b"") -> bytes:
    """One GDS2 record: 4-byte header (length, rtype, dtype) + payload."""
    length = 4 + len(payload)
    assert length % 2 == 0, f"odd GDS record length {length}"
    assert length <= MAX_RECORD_LEN, f"GDS record exceeds {MAX_RECORD_LEN} bytes"
    return struct.pack(">HBB", length, rtype, dtype) + payload


def string_payload(name) -> bytes:
    """NUL-pad ``name`` (str, encoded as UTF-8, or raw bytes) to even length."""
    raw = name.encode("utf-8") if isinstance(name, str) else bytes(name)
    if len(raw) % 2 != 0:
        raw += b"\x00"
    return raw


def _xy_closed(points) -> bytes:
    pts = list(points)
    if not pts:
        raise ValueError("boundary() requires at least one point")
    if pts[0] != pts[-1]:
        pts = pts + [pts[0]]
    flat = []
    for x, y in pts:
        flat.append(int(x))
        flat.append(int(y))
    return struct.pack(f">{len(flat)}i", *flat)


def _xy_open(points) -> bytes:
    flat = []
    for x, y in points:
        flat.append(int(x))
        flat.append(int(y))
    return struct.pack(f">{len(flat)}i", *flat)


# ------------------------------------------------------------------------- elements --
def boundary(layer: int, datatype: int, points, extra: bytes = b"") -> bytes:
    """BOUNDARY/LAYER/DATATYPE/XY(closed)/[extra]/ENDEL."""
    body = (
        record(LAYER, DT_I16, struct.pack(">h", layer))
        + record(DATATYPE, DT_I16, struct.pack(">h", datatype))
        + record(XY, DT_I32, _xy_closed(points))
        + extra
    )
    return record(BOUNDARY) + body + record(ENDEL)


def path(layer: int, datatype: int, points, width: int = 100, extra: bytes = b"") -> bytes:
    """PATH/LAYER/DATATYPE/WIDTH/XY(open)/[extra]/ENDEL."""
    body = (
        record(LAYER, DT_I16, struct.pack(">h", layer))
        + record(DATATYPE, DT_I16, struct.pack(">h", datatype))
        + record(WIDTH, DT_I32, struct.pack(">i", width))
        + record(XY, DT_I32, _xy_open(points))
        + extra
    )
    return record(PATH) + body + record(ENDEL)


def text(layer: int, texttype: int, string, position=(0, 0), extra: bytes = b"") -> bytes:
    """TEXT/LAYER/TEXTTYPE/PRESENTATION/STRANS/MAG/ANGLE/XY/STRING/[extra]/ENDEL."""
    body = (
        record(LAYER, DT_I16, struct.pack(">h", layer))
        + record(TEXTTYPE, DT_I16, struct.pack(">h", texttype))
        + record(PRESENTATION, DT_BITARRAY, struct.pack(">H", 0))
        + record(STRANS, DT_BITARRAY, struct.pack(">H", 0))
        + record(MAG, DT_R8, gds_real8(1.0))
        + record(ANGLE, DT_R8, gds_real8(0.0))
        + record(XY, DT_I32, struct.pack(">2i", int(position[0]), int(position[1])))
        + record(STRING, DT_ASCII, string_payload(string))
        + extra
    )
    return record(TEXT) + body + record(ENDEL)


def sref(name, position=(0, 0), angle=None, mag=None, reflect: bool = False,
         extra: bytes = b"") -> bytes:
    """SREF/SNAME/[STRANS/MAG/ANGLE]/XY/[extra]/ENDEL."""
    body = record(SNAME, DT_ASCII, string_payload(name))
    if angle is not None or mag is not None or reflect:
        strans_bits = STRANS_REFLECT_X if reflect else 0
        body += record(STRANS, DT_BITARRAY, struct.pack(">H", strans_bits))
        if mag is not None:
            body += record(MAG, DT_R8, gds_real8(mag))
        if angle is not None:
            body += record(ANGLE, DT_R8, gds_real8(angle))
    body += record(XY, DT_I32, struct.pack(">2i", int(position[0]), int(position[1])))
    body += extra
    return record(SREF) + body + record(ENDEL)


def _as_step_vector(step, axis: str):
    if isinstance(step, (tuple, list)):
        return int(step[0]), int(step[1])
    return (int(step), 0) if axis == "col" else (0, int(step))


def aref(name, origin, cols: int, rows: int, col_step, row_step,
         extra: bytes = b"") -> bytes:
    """AREF/SNAME/COLROW/XY(3 points)/[extra]/ENDEL.

    ``col_step``/``row_step`` are either (dx, dy) vectors or a bare number
    (a convenience meaning "step along x" for columns / "step along y" for
    rows). The XY record's 3 points are: the origin, the origin displaced
    by ``col_step * cols``, and the origin displaced by ``row_step * rows``
    -- per the GDS2 spec, NOT the opposite corner of the array's bbox.
    """
    ox, oy = int(origin[0]), int(origin[1])
    cx, cy = _as_step_vector(col_step, "col")
    rx, ry = _as_step_vector(row_step, "row")
    p0 = (ox, oy)
    p1 = (ox + cx * cols, oy + cy * cols)
    p2 = (ox + rx * rows, oy + ry * rows)
    body = (
        record(SNAME, DT_ASCII, string_payload(name))
        + record(COLROW, DT_I16, struct.pack(">2h", cols, rows))
        + record(XY, DT_I32, _xy_open([p0, p1, p2]))
        + extra
    )
    return record(AREF) + body + record(ENDEL)


def property_(attr: int, value) -> bytes:
    """PROPATTR + PROPVALUE, meant to be passed as an element's ``extra=``
    so it lands right before that element's ENDEL."""
    return (
        record(PROPATTR, DT_I16, struct.pack(">h", attr))
        + record(PROPVALUE, DT_ASCII, string_payload(value))
    )


# --------------------------------------------------------------------------- cell/library --
def timestamps(year=2026, month=9, day=8, hour=0, minute=0, second=0) -> bytes:
    """24-byte BGNLIB/BGNSTR payload: 12 big-endian int16 (mod-time then
    access-time, both set to the same 6 fields)."""
    six = (year, month, day, hour, minute, second)
    return struct.pack(">12h", *(six + six))


def cell(name, body: bytes, stamp: bytes = TS_ZERO) -> bytes:
    """BGNSTR(stamp)/STRNAME/body/ENDSTR."""
    return (
        record(BGNSTR, DT_I16, stamp)
        + record(STRNAME, DT_ASCII, string_payload(name))
        + body
        + record(ENDSTR)
    )


def library(cells, stamp: bytes = TS_ZERO, libname="LIB", units=(0.001, 1e-9),
            trailer: bytes = b"") -> bytes:
    """HEADER(600)/BGNLIB(stamp)/LIBNAME/UNITS/cells/ENDLIB + trailer."""
    out = record(HEADER, DT_I16, struct.pack(">h", 600))
    out += record(BGNLIB, DT_I16, stamp)
    out += record(LIBNAME, DT_ASCII, string_payload(libname))
    out += record(UNITS, DT_R8, gds_real8(units[0]) + gds_real8(units[1]))
    for c in cells:
        out += c
    out += record(ENDLIB)
    out += trailer
    return out


# --------------------------------------------------------------- deterministic geometry --
def _mix32(*ints: int) -> int:
    v = 0x9E3779B9
    for x in ints:
        v = ((v ^ (x & 0xFFFFFFFF)) * 0x85EBCA6B) & 0xFFFFFFFF
        v ^= v >> 13
    return v


def base_geom(seed: int, cell_i: int, shape_i: int):
    """Deterministic box (x0, y0, x1, y1, layer, datatype): a pure function
    of (seed, cell_i, shape_i), same idea as gen_gds.py's base_geom
    (reimplemented locally, not imported)."""
    h = _mix32(seed, cell_i, shape_i)
    x0 = (h % 900_000) + 1_000
    y0 = ((h >> 8) % 900_000) + 1_000
    w = 200 + (h % 300)
    ht = 200 + ((h >> 16) % 300)
    layer = 1 + (cell_i % 8)
    dtype = shape_i % 4
    return x0, y0, x0 + w, y0 + ht, layer, dtype


def modified_geom(seed: int, cell_i: int, shape_i: int, salt: int = 1):
    x0, y0, x1, y1, layer, dtype = base_geom(seed, cell_i, shape_i)
    delta = (salt * 997 + 1) % 500_000
    return x0 + delta, y0 + delta, x1 + delta, y1 + delta, layer, dtype


def _box_points(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def _many_cell_name(i: int) -> str:
    return f"C{i:06d}"


def _many_box_geom(seed, cell_i, shape_i):
    x0, y0, x1, y1, layer, dtype = base_geom(seed, cell_i, shape_i)
    return layer, dtype, _box_points(x0, y0, x1, y1)


def _many_box_geom_modified(seed, cell_i, shape_i):
    x0, y0, x1, y1, layer, dtype = modified_geom(seed, cell_i, shape_i)
    return layer, dtype, _box_points(x0, y0, x1, y1)


# --------------------------------------------------------------------------- samples --
def sample_hierarchy(stamp: bytes = TS_ZERO) -> bytes:
    """Three-level hierarchy: UNIT (two boxes + a path + a TEXT carrying a
    PROPATTR/PROPVALUE property) -> ARRAY (one AREF of UNIT, 4x3) -> TOP
    (two SREFs of UNIT -- one plain, one rotated 90 degrees AND X-reflected
    -- plus one SREF of ARRAY). Exercises SREF/AREF instance counting,
    STRANS rotation+reflection, and an element property. TOP is the only
    top cell: UNIT and ARRAY are both referenced."""
    unit_body = b"".join([
        boundary(1, 0, _box_points(0, 0, 100, 100)),
        boundary(1, 0, _box_points(200, 0, 300, 100)),
        path(2, 0, [(0, 0), (0, 500), (500, 500)], width=50),
        text(3, 0, "UNIT", position=(0, -50), extra=property_(1, "unit-label")),
    ])
    unit = cell("UNIT", unit_body, stamp=stamp)

    array_body = aref("UNIT", origin=(0, 0), cols=4, rows=3, col_step=400, row_step=400)
    array = cell("ARRAY", array_body, stamp=stamp)

    top_body = b"".join([
        sref("UNIT", position=(0, 0)),
        sref("UNIT", position=(2000, 0), angle=90.0, reflect=True),
        sref("ARRAY", position=(5000, 0)),
    ])
    top = cell("TOP", top_body, stamp=stamp)

    return library([unit, array, top], stamp=stamp)


def sample_many_cells(n: int, shapes: int = 8, seed: int = 1, stamp: bytes = TS_ZERO) -> bytes:
    """n small cells C000000.. each with ``shapes`` boxes (coordinates a
    pure function of (seed, cell index, shape index)), plus a TOP that
    SREFs every cell in order."""
    cells = []
    top_refs = []
    for i in range(n):
        name = _many_cell_name(i)
        body = b"".join(boundary(*_many_box_geom(seed, i, s)) for s in range(shapes))
        cells.append(cell(name, body, stamp=stamp))
        top_refs.append(sref(name, position=(i * 10, 0)))
    cells.append(cell("TOP", b"".join(top_refs), stamp=stamp))
    return library(cells, stamp=stamp)


_MANY_CELLS_EDITS = ("modify", "insert", "delete", "rename")
_RENAME_SUFFIX = "X" * 14  # grows the STRNAME/SNAME record payload by exactly 14 bytes


def sample_many_cells_edit(n: int, edit: str, shapes: int = 8, seed: int = 1,
                            stamp: bytes = TS_ZERO) -> bytes:
    """A variant of sample_many_cells(n, shapes, seed, stamp) that changes
    exactly one thing at the midpoint cell (index n // 2):

    * "modify": that cell's box coordinates change (modified_geom instead
      of base_geom), everything else stays byte-identical.
    * "insert": a new cell "NEW" is spliced in right after it, with a
      matching SREF appended to TOP right after that cell's SREF.
    * "delete": that cell and its SREF in TOP are both removed.
    * "rename": that cell's name grows by exactly 14 bytes (a 14-char
      ASCII suffix) everywhere it appears -- its own STRNAME and its SNAME
      reference inside TOP.
    """
    if edit not in _MANY_CELLS_EDITS:
        raise ValueError(f"edit {edit!r} must be one of {_MANY_CELLS_EDITS}")
    mid = n // 2

    bodies = []
    for i in range(n):
        if edit == "modify" and i == mid:
            boxes = (_many_box_geom_modified(seed, i, s) for s in range(shapes))
        else:
            boxes = (_many_box_geom(seed, i, s) for s in range(shapes))
        bodies.append(b"".join(boundary(*b) for b in boxes))

    cells_out = []
    top_refs = []
    for i in range(n):
        if edit == "delete" and i == mid:
            continue
        name = _many_cell_name(i)
        if edit == "rename" and i == mid:
            name = name + _RENAME_SUFFIX
        cells_out.append(cell(name, bodies[i], stamp=stamp))
        top_refs.append(sref(name, position=(i * 10, 0)))
        if edit == "insert" and i == mid:
            new_body = b"".join(
                boundary(*_many_box_geom(seed, n + 1, s)) for s in range(shapes)
            )
            cells_out.append(cell("NEW", new_body, stamp=stamp))
            top_refs.append(sref("NEW", position=(mid * 10 + 5, 0)))

    cells_out.append(cell("TOP", b"".join(top_refs), stamp=stamp))
    return library(cells_out, stamp=stamp)


def _big_cell_shapes(target_bytes: int, seed: int):
    shapes = []
    total = 0
    idx = 0
    while total < target_bytes:
        x0, y0, x1, y1, layer, dtype = base_geom(seed, 0, idx)
        rec = boundary(layer, dtype, _box_points(x0, y0, x1, y1))
        shapes.append(rec)
        total += len(rec)
        idx += 1
    return shapes


def sample_big_cell(target_bytes: int, seed: int = 1, stamp: bytes = TS_ZERO) -> bytes:
    """One flat cell ("BIG") of hash-derived boxes, sized so the returned
    library is >= target_bytes."""
    shapes = _big_cell_shapes(target_bytes, seed)
    return library([cell("BIG", b"".join(shapes), stamp=stamp)], stamp=stamp)


def sample_big_cell_insert(target_bytes: int, insert_after_shape: int, count: int = 64,
                            seed: int = 1, stamp: bytes = TS_ZERO):
    """The same flat "BIG" cell as sample_big_cell(target_bytes, seed,
    stamp), plus a variant with ``count`` extra boxes spliced in right
    after shape index ``insert_after_shape`` (drawn from a disjoint
    cell_i=1 geometry namespace, so they can't collide with the base
    shapes' coordinates). All records before the insertion point are
    byte-identical between the two outputs; all records after it are
    byte-identical but shifted by the inserted bytes. Returns
    (before_bytes, after_bytes)."""
    shapes = _big_cell_shapes(target_bytes, seed)
    before = library([cell("BIG", b"".join(shapes), stamp=stamp)], stamp=stamp)

    extra = []
    for k in range(count):
        x0, y0, x1, y1, layer, dtype = base_geom(seed, 1, k)
        extra.append(boundary(layer, dtype, _box_points(x0, y0, x1, y1)))

    spliced = shapes[:insert_after_shape] + extra + shapes[insert_after_shape:]
    after = library([cell("BIG", b"".join(spliced), stamp=stamp)], stamp=stamp)
    return before, after


def has_anchor(record_bytes: bytes, mask: int = 0x0FFF) -> bool:
    """True if this record would be picked as a record-aligned CDC cut
    point under ``mask`` (see bench/storage/walk_gds.py's "cdc" crc mode):
    ``zlib.crc32(record_bytes) & mask == 0``."""
    return (zlib.crc32(record_bytes) & mask) == 0


def _avoid_anchor(build, mask: int, max_tries: int = 10_000):
    """Call build(attempt) for attempt = 0, 1, 2, ... until it returns
    (record_bytes, extra) with has_anchor(record_bytes, mask) False; return
    that (record_bytes, extra) pair."""
    for attempt in range(max_tries):
        rec, extra = build(attempt)
        if not has_anchor(rec, mask):
            return rec, extra
    raise RuntimeError("could not avoid the CDC anchor mask within max_tries")


def sample_no_anchor_big_cell(target_bytes: int, seed: int = 1, mask: int = 0x0FFF) -> bytes:
    """Same shape as sample_big_cell, except every record of length >= 12
    (BGNLIB, BGNSTR, UNITS, and each box's XY record -- the only records in
    a flat single-cell library that reach that length; LAYER/DATATYPE/
    BOUNDARY/ENDEL/HEADER/ENDLIB/ENDSTR/short-name STRNAME/LIBNAME are all
    < 12 bytes and skipped by construction) is chosen so
    has_anchor(record, mask) is False. A record-aligned CDC chunker using
    this mask would never find a single cut point anywhere in this file.
    """

    def build_bgnlib(attempt):
        vals = list(struct.unpack(">12h", TS_ZERO))
        if attempt:
            vals[5] = attempt % 60
            vals[11] = vals[5]
        stamp = struct.pack(">12h", *vals)
        return record(BGNLIB, DT_I16, stamp), stamp

    _, lib_stamp = _avoid_anchor(build_bgnlib, mask)

    def build_bgnstr(attempt):
        vals = list(struct.unpack(">12h", TS_ZERO))
        if attempt:
            vals[4] = attempt % 60
            vals[10] = vals[4]
        stamp = struct.pack(">12h", *vals)
        return record(BGNSTR, DT_I16, stamp), stamp

    _, cell_stamp = _avoid_anchor(build_bgnstr, mask)

    def build_units(attempt):
        u0 = 0.001 * (1.0 + attempt * 1e-9)
        u1 = 1e-9
        payload = gds_real8(u0) + gds_real8(u1)
        return record(UNITS, DT_R8, payload), (u0, u1)

    _, units_val = _avoid_anchor(build_units, mask)

    prefix = (
        record(HEADER, DT_I16, struct.pack(">h", 600))
        + record(BGNLIB, DT_I16, lib_stamp)
        + record(LIBNAME, DT_ASCII, string_payload("LIB"))
        + record(UNITS, DT_R8, gds_real8(units_val[0]) + gds_real8(units_val[1]))
    )
    cell_head = record(BGNSTR, DT_I16, cell_stamp) + record(STRNAME, DT_ASCII, string_payload("BIG"))
    suffix = record(ENDSTR) + record(ENDLIB)
    overhead = len(prefix) + len(cell_head) + len(suffix)

    body_parts = []
    total = overhead
    idx = 0
    while total < target_bytes:
        def build_box(attempt, idx=idx):
            x0, y0, x1, y1, layer, dtype = base_geom(seed, 0, idx * 100_000 + attempt)
            xy = _xy_closed(_box_points(x0, y0, x1, y1))
            return record(XY, DT_I32, xy), (xy, layer, dtype)

        _, (xy_payload, layer, dtype) = _avoid_anchor(build_box, mask)
        box_rec = (
            record(BOUNDARY)
            + record(LAYER, DT_I16, struct.pack(">h", layer))
            + record(DATATYPE, DT_I16, struct.pack(">h", dtype))
            + record(XY, DT_I32, xy_payload)
            + record(ENDEL)
        )
        body_parts.append(box_rec)
        total += len(box_rec)
        idx += 1

    return prefix + cell_head + b"".join(body_parts) + suffix


_FAKE_BGNSTR_PATTERN = (
    struct.pack(">HBB", 28, BGNSTR, DT_I16) + b"\xAA" * 24
    + struct.pack(">HBB", 10, STRNAME, DT_ASCII) + b"FAKE\x00\x00"
)


def sample_fake_bgnstr(stamp: bytes = TS_ZERO) -> bytes:
    """A valid single-cell library whose one BOUNDARY's XY payload embeds
    the exact byte pattern of a fake BGNSTR(length=28)+STRNAME(length=10,
    "FAKE") record pair, padded to stay point-aligned (multiple of 8
    bytes). A byte-search-based cell scanner (as opposed to a real 4-byte-
    header record walker) would misdetect this as a second cell named
    "FAKE" that doesn't actually exist."""
    pad = (8 - (len(_FAKE_BGNSTR_PATTERN) % 8)) % 8
    xy_payload = struct.pack(">2i", 0, 0) + _FAKE_BGNSTR_PATTERN + b"\x00" * pad
    assert len(xy_payload) % 8 == 0
    body = (
        record(BOUNDARY)
        + record(LAYER, DT_I16, struct.pack(">h", 1))
        + record(DATATYPE, DT_I16, struct.pack(">h", 0))
        + record(XY, DT_I32, xy_payload)
        + record(ENDEL)
    )
    return library([cell("FAKEHOST", body, stamp=stamp)], stamp=stamp)


def sample_odd_timestamps() -> bytes:
    """BGNLIB and each of 3 cells carry different, non-zero, non-date-like
    24-byte timestamp payloads (not built through timestamps()) --
    exercises that timestamp handling treats the 24 bytes as opaque and
    never assumes calendar-shaped values."""
    lib_stamp = b"\xff" * 24
    cell_stamps = [
        bytes(range(24)),
        bytes((i * 3) % 256 for i in range(24)),
        bytes((255 - i) for i in range(24)),
    ]
    cells = [
        cell(f"C{i}", boundary(1 + i, 0, _box_points(0, 0, 10, 10)), stamp=s)
        for i, s in enumerate(cell_stamps)
    ]
    return library(cells, stamp=lib_stamp)


def sample_empty_library(stamp: bytes = TS_ZERO) -> bytes:
    """A library with zero cells."""
    return library([], stamp=stamp)


def sample_empty_cell(stamp: bytes = TS_ZERO) -> bytes:
    """A library with one cell that has no shapes or instances."""
    return library([cell("EMPTY", b"", stamp=stamp)], stamp=stamp)


def sample_multi_top(stamp: bytes = TS_ZERO) -> bytes:
    """Two cells, TOPA and TOPB, neither referenced by the other -- two
    unreferenced top cells."""
    a = cell("TOPA", boundary(1, 0, _box_points(0, 0, 10, 10)), stamp=stamp)
    b = cell("TOPB", boundary(2, 0, _box_points(0, 0, 20, 20)), stamp=stamp)
    return library([a, b], stamp=stamp)


_UNKNOWN_RTYPE, _UNKNOWN_DTYPE = 0x3F, 0x02


def sample_unknown_record(stamp: bytes = TS_ZERO) -> bytes:
    """A structurally valid library carrying an unknown record type
    (0x3F, datatype 0x02, one int16 payload) both right after UNITS
    (library level) and inside a cell body -- readers/walkers must skip
    unrecognized record types rather than fail."""
    unknown = record(_UNKNOWN_RTYPE, _UNKNOWN_DTYPE, struct.pack(">h", 42))
    header = record(HEADER, DT_I16, struct.pack(">h", 600))
    bgnlib = record(BGNLIB, DT_I16, stamp)
    libname = record(LIBNAME, DT_ASCII, string_payload("LIB"))
    units = record(UNITS, DT_R8, gds_real8(0.001) + gds_real8(1e-9))
    body = unknown + boundary(1, 0, _box_points(0, 0, 10, 10))
    c = cell("U", body, stamp=stamp)
    return header + bgnlib + libname + units + unknown + c + record(ENDLIB)


def sample_trailer(stamp: bytes = TS_ZERO) -> bytes:
    """A library with 6 bytes of NUL padding after ENDLIB (some GDS
    writers pad the file to a fixed block size)."""
    body = boundary(1, 0, _box_points(0, 0, 10, 10))
    return library([cell("U", body, stamp=stamp)], stamp=stamp, trailer=b"\x00" * 6)


def sample_duplicate_names(stamp: bytes = TS_ZERO) -> bytes:
    """Two cells both named DUP (with different geometry so they're
    distinguishable if a reader keeps both)."""
    a = cell("DUP", boundary(1, 0, _box_points(0, 0, 10, 10)), stamp=stamp)
    b = cell("DUP", boundary(2, 0, _box_points(0, 0, 20, 20)), stamp=stamp)
    return library([a, b], stamp=stamp)


def sample_truncated(base_bytes: bytes, keep: int) -> bytes:
    """``base_bytes`` cut to its first ``keep`` bytes -- a stream that ends
    mid-record or mid-file."""
    return base_bytes[:keep]


def sample_odd_length() -> bytes:
    """A valid HEADER/BGNLIB/LIBNAME prefix followed by a record whose
    header declares an odd total length (7) -- exercises the bad_length
    framing check."""
    prefix = (
        record(HEADER, DT_I16, struct.pack(">h", 600))
        + record(BGNLIB, DT_I16, TS_ZERO)
        + record(LIBNAME, DT_ASCII, string_payload("LIB"))
    )
    bad = struct.pack(">HBB", 7, UNITS, DT_R8) + b"\x00" * 3
    return prefix + bad


def sample_zero_length() -> bytes:
    """A valid HEADER/BGNLIB prefix followed by a record whose header
    declares length 0 (below the minimum 4-byte header size)."""
    prefix = record(HEADER, DT_I16, struct.pack(">h", 600)) + record(BGNLIB, DT_I16, TS_ZERO)
    bad = struct.pack(">HBB", 0, LIBNAME, DT_ASCII)
    return prefix + bad


def sample_bad_order() -> bytes:
    """BGNSTR immediately followed by a BOUNDARY element, skipping the
    required STRNAME record."""
    body = (
        record(BGNSTR, DT_I16, TS_ZERO)
        + boundary(1, 0, _box_points(0, 0, 10, 10))
        + record(ENDSTR)
    )
    return library([body])


def sample_bgnstr_wrong_length() -> bytes:
    """A BGNSTR record whose declared length is 20 (16-byte payload, 8
    int16 fields) instead of the correct 28 (24-byte payload, 12 int16
    fields). This is valid record FRAMING (even length, within the size
    cap) but wrong content for a timestamp record -- a timestamp decoder
    must not blindly unpack 12 int16 fields from a payload that is only
    16 bytes long."""
    prefix = (
        record(HEADER, DT_I16, struct.pack(">h", 600))
        + record(BGNLIB, DT_I16, TS_ZERO)
        + record(LIBNAME, DT_ASCII, string_payload("LIB"))
        + record(UNITS, DT_R8, gds_real8(0.001) + gds_real8(1e-9))
    )
    bad_bgnstr = struct.pack(">HBB", 20, BGNSTR, DT_I16) + struct.pack(">8h", *([0] * 8))
    tail = (
        record(STRNAME, DT_ASCII, string_payload("BAD"))
        + record(ENDSTR)
        + record(ENDLIB)
    )
    return prefix + bad_bgnstr + tail


def sample_utf8_name() -> bytes:
    """A cell whose STRNAME payload contains raw bytes > 0x7F ("中CELL" as
    UTF-8) -- strict GDS2 specifies ASCII names only, but real files carry
    whatever bytes a writer put there; those raw bytes must survive."""
    raw_name = "中CELL".encode("utf-8")
    body = boundary(1, 0, _box_points(0, 0, 10, 10))
    return library([cell(raw_name, body)])


def sample_units_changed(base_units, new_units, stamp: bytes = TS_ZERO):
    """Two otherwise-identical single-cell libraries differing only in the
    UNITS record. Returns (base_bytes, new_bytes)."""
    body = boundary(1, 0, _box_points(0, 0, 10, 10))
    c = cell("U", body, stamp=stamp)
    base = library([c], stamp=stamp, units=base_units)
    new = library([c], stamp=stamp, units=new_units)
    return base, new
