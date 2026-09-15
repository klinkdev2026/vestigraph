"""Reader side of ChangeSet v1 entries (docs/CHANGESET_V1.md §3, §5; SPEC_PERFORMANCE_READ_ACCESS §4).

Entries are stored at commit time as a paged sequence of metadata objects
(engine._change_root). Timestamp-only cells are stored as compact groups over
current-recipe ordinals; this module expands them to logical per-cell entries
without ever reading layout payload. Nothing here needs KLayout or the encoder.

Read discipline (P2): a kind filter is applied to STORED entries before any group
is expanded, so a query for a rare kind never touches the recipe; page directories
are located by item counts instead of being expanded up front; every read is
counted in `ReadCounters` so tests can assert what was NOT touched.
"""
from __future__ import annotations

import base64
import bisect
import hashlib
import json
from collections import OrderedDict

from .metadata import METADATA_OBJECT_LIMIT, MetadataError, _check_descriptor, canonical, parse, iter_sequence

ENTRIES_FORMAT = "vestigraph.changeset.entries"
EVIDENCE_FORMAT = "vestigraph.changeset.evidence"
JSONL_FORMAT = "vestigraph.changeset.jsonl"
DEFAULT_PAGE = 50
MAX_PAGE = 200
MAX_JSONL_LINE = 1024 * 1024
JSONL_LINE_HARD_CAP = 16 * 1024 * 1024


def display_name(b64: str) -> str:
    """Safe display text for a raw STRNAME payload: NUL padding stripped, non-ASCII replaced."""
    try:
        raw = base64.b64decode(b64)
    except (ValueError, TypeError):
        return "?"
    return raw.rstrip(b"\0").decode("ascii", "replace")


class CursorError(MetadataError):
    """The caller's cursor/limit is not valid for this change record and filter."""


class ReadCounters:
    """What a read actually touched; asserted by the algorithmic tests."""

    def __init__(self):
        self.stored_entries_examined = 0
        self.logical_entries_expanded = 0
        self.metadata_pages_read = 0
        self.recipe_pages_read = 0

    def as_dict(self, index=None):
        out = dict(self.__dict__)
        out["layout_payload_reads"] = getattr(index, "layout_reads", 0)
        out["metadata_objects_read"] = getattr(index, "metadata_reads", 0)
        return out


class SequenceReader:
    """Random access into a SequenceRef by item index with on-demand directory pages.

    Only the root descriptor list (inline) or the root index object is read at construction;
    for a two-level index each inner index is read when an item in its range is first needed.
    Data pages are read when needed. Both directory and data caches have at most
    cache_pages entries; traversing the entire sequence cannot accumulate all directories."""

    def __init__(self, ref, source, cache_pages=4, counters=None, kind="metadata"):
        self.source = source
        self.counters = counters
        self.kind = kind
        if not isinstance(cache_pages, int) or isinstance(cache_pages, bool) or cache_pages < 0:
            raise ValueError("cache_pages must be a non-negative integer")
        if not isinstance(ref, dict) or not isinstance(ref.get("count"), int) or ref["count"] < 0:
            raise MetadataError("bad SequenceRef")
        self.count = ref["count"]
        self._inline = None            # flat page descriptors (inline or one-level index)
        self._outer = None             # two-level: outer descriptors [hash, size, item_count_sum]
        self._outer_starts = None
        self._inner = OrderedDict()    # bounded directory LRU: outer -> (starts, descriptors)
        if "pages" in ref:
            self._inline = _validated_pages(ref["pages"])
        else:
            levels = ref.get("levels")
            if not isinstance(ref.get("index"), str) or levels not in (1, 2):
                raise MetadataError("bad SequenceRef index")
            index = self._read_object(ref["index"])
            if not isinstance(index, dict):
                raise MetadataError("bad index object")
            if levels == 1:
                self._inline = _validated_pages(index.get("pages"))
            else:
                outer = index.get("indexes")
                if not isinstance(outer, list):
                    raise MetadataError("bad two-level index")
                for d in outer:
                    _check_descriptor(d)
                if sum(d[2] for d in outer) != self.count:
                    raise MetadataError("sequence index counts disagree with the declared count")
                self._outer = outer
                self._outer_starts = _starts(outer)
        if self._inline is not None:
            self._starts = _starts(self._inline)
            if sum(d[2] for d in self._inline) != self.count:
                raise MetadataError("sequence page counts disagree with the declared count")
        self._pages = {}
        self._order = []
        self._cache_pages = cache_pages

    def _read_object(self, digest, expected_size=None):
        payload = self.source.get(digest, METADATA_OBJECT_LIMIT)
        if self.counters is not None:
            self.counters.metadata_pages_read += 1
        if expected_size is not None and len(payload) != expected_size:
            raise MetadataError("index size mismatch")
        return parse(payload)

    def _descriptor_for(self, i):
        """(page key, descriptor, first item index of that page) for item i, reading only what is needed."""
        if self._inline is not None:
            p = bisect.bisect_right(self._starts, i) - 1
            return ("p", p), self._inline[p], self._starts[p]
        o = bisect.bisect_right(self._outer_starts, i) - 1
        inner = self._inner.get(o)
        if inner is None:
            obj = self._read_object(self._outer[o][0], self._outer[o][1])
            pages = _validated_pages(obj.get("pages") if isinstance(obj, dict) else None)
            if sum(d[2] for d in pages) != self._outer[o][2]:
                raise MetadataError("inner index count mismatch")
            inner = (_starts(pages), pages)
            self._inner[o] = inner
            while len(self._inner) > self._cache_pages:
                self._inner.popitem(last=False)
        else:
            self._inner.move_to_end(o)
        starts, pages = inner
        local = i - self._outer_starts[o]
        p = bisect.bisect_right(starts, local) - 1
        return ("i", o, p), pages[p], self._outer_starts[o] + starts[p]

    def _page(self, key, d):
        page = self._pages.get(key)
        if page is not None:
            return page
        payload = self.source.get(d[0], METADATA_OBJECT_LIMIT)
        if self.counters is not None:
            if self.kind == "recipe":
                self.counters.recipe_pages_read += 1
            else:
                self.counters.metadata_pages_read += 1
        if len(payload) != d[1]:
            raise MetadataError("page size mismatch")
        page = parse(payload)
        if not isinstance(page, list) or len(page) != d[2]:
            raise MetadataError("page item count mismatch")
        self._pages[key] = page
        self._order.append(key)
        while len(self._order) > self._cache_pages:
            self._pages.pop(self._order.pop(0), None)
        return page

    def get(self, i):
        if not isinstance(i, int) or not 0 <= i < self.count:
            raise MetadataError("sequence index out of range")
        key, d, start = self._descriptor_for(i)
        return self._page(key, d)[i - start]

    def iter_from(self, i):
        while i < self.count:
            key, d, start = self._descriptor_for(i)
            page = self._page(key, d)
            for j in range(i - start, len(page)):
                yield start + j, page[j]
            i = start + len(page)


def _validated_pages(pages):
    if not isinstance(pages, list):
        raise MetadataError("bad page list")
    for d in pages:
        _check_descriptor(d)
    return pages


def _starts(descriptors):
    starts, total = [], 0
    for d in descriptors:
        starts.append(total)
        total += d[2]
    return starts


def load_entries_root(index, root, counters=None):
    """The entries object referenced by a change root, or None when not recorded."""
    digest = root.get("entries_root")
    if not digest:
        return None
    obj = parse(index.get(digest, METADATA_OBJECT_LIMIT))
    if counters is not None:
        counters.metadata_pages_read += 1
    if not isinstance(obj, dict) or obj.get("format") != ENTRIES_FORMAT or obj.get("version") != 1:
        raise MetadataError("entries object has an unknown format")
    return obj


def _group_range(entry):
    lo, hi = entry.get("ordinal_from"), entry.get("ordinal_to")
    if not (isinstance(lo, int) and isinstance(hi, int) and 0 <= lo <= hi):
        raise MetadataError("bad group range")
    return lo, hi


def iter_logical_entries(index, root, recipe_root, start=(0, 0), kind=None, counters=None, entries_obj=None):
    """Yield ((stored_index, offset), logical_entry) from a cursor position.

    Groups (`group: true`) expand to one logical entry per current-recipe ordinal in
    [ordinal_from, ordinal_to]; `offset` is the position inside the current group.
    With `kind`, stored entries of other kinds are skipped BEFORE expansion: a group of
    100,000 timestamp-only cells costs one stored-entry look and zero recipe reads."""
    counters = counters if counters is not None else ReadCounters()
    if entries_obj is None:
        entries_obj = load_entries_root(index, root, counters)
    if entries_obj is None:
        return
    reader = SequenceReader(entries_obj["entries"], index, counters=counters)
    recipe = None
    stored, offset = start
    for i, entry in reader.iter_from(stored):
        counters.stored_entries_examined += 1
        if not isinstance(entry, dict):
            raise MetadataError("entry is not an object")
        if kind is not None and entry.get("kind") != kind:
            offset = 0
            continue
        if entry.get("group"):
            lo, hi = _group_range(entry)
            skip = offset if i == stored else 0
            if not 0 <= skip <= hi - lo:
                raise CursorError("cursor offset is outside its group")
            if recipe is None:
                recipe = SequenceReader(recipe_root["recipe"], index, counters=counters, kind="recipe")
            for ordinal in range(lo + skip, hi + 1):
                cell = recipe.get(ordinal)
                counters.logical_entries_expanded += 1
                yield (i, ordinal - lo), {
                    "seq": i, "seq_sub": ordinal - lo, "kind": entry["kind"],
                    "cell_key": cell.get("name"), "name": display_name(cell.get("name", "")),
                    "ordinal": ordinal, "comparison_basis": entry.get("comparison_basis"),
                }
        else:
            counters.logical_entries_expanded += 1
            yield (i, 0), entry
        offset = 0


def encode_cursor(change_id, kind, position):
    """Opaque cursor bound to the change record AND the kind filter (CHANGESET_V1 §5)."""
    return "%s:%s:%d:%d" % (change_id, kind or "-", position[0], position[1])


def decode_cursor(cursor, change_id, kind):
    try:
        cid, bound_kind, a, b = cursor.split(":")
        stored, offset = int(a), int(b)
    except (ValueError, AttributeError):
        raise CursorError("cursor is malformed") from None
    if cid != change_id:
        raise CursorError("cursor does not belong to this change record")
    if bound_kind != (kind or "-"):
        raise CursorError("cursor was issued for a different kind filter")
    if stored < 0 or offset < 0:
        raise CursorError("cursor position is negative")
    return stored, offset


def page(index, root, recipe_root, *, limit=DEFAULT_PAGE, cursor=None, kind=None):
    """One page of logical entries; returns {items, next_cursor, diagnostics}.
    `limit` is clamped to [1, MAX_PAGE]; a cursor past the end is the end."""
    try:
        limit = max(1, min(int(limit), MAX_PAGE))
    except (TypeError, ValueError):
        raise CursorError("limit must be an integer") from None
    counters = ReadCounters()
    start = decode_cursor(cursor, root["id"], kind) if cursor else (0, 0)
    entries_obj = load_entries_root(index, root, counters)
    empty = {"items": [], "next_cursor": None}
    if entries_obj is None:
        return dict(empty, diagnostics=counters.as_dict(index))
    if kind and not entries_obj.get("kinds", {}).get(kind):
        return dict(empty, diagnostics=counters.as_dict(index))     # no such entries: no page walk at all
    if start[0] >= entries_obj.get("count", 0):
        return dict(empty, diagnostics=counters.as_dict(index))
    items, next_cursor = [], None
    for position, entry in iter_logical_entries(index, root, recipe_root, start, kind=kind,
                                                counters=counters, entries_obj=entries_obj):
        if len(items) >= limit:
            next_cursor = encode_cursor(root["id"], kind, position)
            break
        items.append(entry)
    return {"items": items, "next_cursor": next_cursor, "diagnostics": counters.as_dict(index)}


def portable_header(root, entries_obj, evidence):
    header = {"type": "header", "format": JSONL_FORMAT, "version": 1}
    header.update({k: root.get(k) for k in ("id", "document_id", "from_checkpoint_id", "to_checkpoint_id",
                                              "algorithm", "coverage", "file", "summary")})
    header["entry_count_stored"] = entries_obj.get("count") if entries_obj else 0
    header["kinds"] = entries_obj.get("kinds") if entries_obj else {}
    header["evidence"] = evidence
    header["evidence_resolvability"] = "portable: names, hashes, counts and references only; original files are not included"
    header["footer_hash"] = "sha256 over every preceding line, each normalized to a single LF terminator"
    return header


def write_jsonl(stream, index, root, recipe_root, evidence=None):
    """Portable JSONL: header, flat entries, footer with the hash of all preceding lines
    (each line normalized to one LF terminator). Returns the entry count. `stream` binary."""
    digest = hashlib.sha256()
    entries_obj = load_entries_root(index, root)

    def emit(obj):
        line = json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8") + b"\n"
        digest.update(line)
        stream.write(line)

    emit(portable_header(root, entries_obj, evidence))
    count = 0
    for _, entry in iter_logical_entries(index, root, recipe_root):
        record = {"type": "entry"}
        record.update(entry)
        emit(record)
        count += 1
    emit({"type": "footer", "entry_count": count, "coverage": root.get("coverage"), "sha256": digest.hexdigest()})
    return count


def _reject_constant(name):
    raise ValueError("non-finite number %s is not allowed" % name)


def _parse_line(line: bytes):
    try:
        obj = json.loads(line.decode("utf-8"), parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError) as exc:
        raise MetadataError("JSONL line is not valid UTF-8 JSON: %s" % exc) from None
    if not isinstance(obj, dict):
        raise MetadataError("JSONL line is not a JSON object")
    return obj


def iter_jsonl(stream, *, max_line=MAX_JSONL_LINE):
    """Streaming stdlib reader: yields ("header", obj), then ("entry", obj) per entry, then
    ("footer", obj) once the footer hash and count have been verified.

    `max_line` is the maximum CONTENT length of one line, excluding its LF or CRLF (a positive
    integer, hard-capped at JSONL_LINE_HARD_CAP). Every underlying read asks for at most
    max_line + 3 bytes, so an over-long line is rejected before it is ever held in memory.
    Anything after the footer other than whitespace, a missing header/footer, an unknown line
    type, a non-object, NaN/Infinity or bad UTF-8 raises MetadataError.

    Entries yielded before the footer are PROVISIONAL: only reaching the footer proves the
    input; a consumer that stops early must not treat what it saw as verified."""
    if not isinstance(max_line, int) or isinstance(max_line, bool) or max_line <= 0:
        raise ValueError("max_line must be a positive integer")
    max_line = min(max_line, JSONL_LINE_HARD_CAP)
    digest = hashlib.sha256()
    header_seen = footer_seen = False
    count = 0
    while True:
        raw = stream.readline(max_line + 3)          # content + CRLF + 1 byte to detect overflow
        if not raw:
            break
        if not raw.endswith(b"\n"):
            if len(raw) > max_line + 2:
                raise MetadataError("JSONL line exceeds %d bytes" % max_line)
            # a last line without a newline is legal; readline capped it, so it is bounded
        elif len(raw) - (2 if raw.endswith(b"\r\n") else 1) > max_line:
            raise MetadataError("JSONL line exceeds %d bytes" % max_line)
        if footer_seen:
            if raw.strip():
                raise MetadataError("content after the footer")
            continue
        line = raw.rstrip(b"\r\n") + b"\n"
        obj = _parse_line(line)
        kind = obj.get("type")
        if kind == "header":
            if header_seen:
                raise MetadataError("duplicate header")
            if obj.get("format") != JSONL_FORMAT or obj.get("version") != 1:
                raise MetadataError("unknown JSONL format/version")
            header_seen = True
            digest.update(line)
            yield "header", obj
        elif kind == "entry":
            if not header_seen:
                raise MetadataError("entry before header")
            count += 1
            digest.update(line)
            yield "entry", obj
        elif kind == "footer":
            if not header_seen:
                raise MetadataError("footer before header")
            if obj.get("sha256") != digest.hexdigest() or obj.get("entry_count") != count:
                raise MetadataError("footer hash or count does not match the file")
            footer_seen = True
            yield "footer", obj
        else:
            raise MetadataError("unknown line type %r" % (kind,))
    if not header_seen or not footer_seen:
        raise MetadataError("file is missing its header or footer")


def read_jsonl(stream):
    """Convenience for small files: (header, list of entries, footer) via iter_jsonl.
    Use iter_jsonl for large exports; this materializes every entry."""
    header = footer = None
    entries = []
    for kind, obj in iter_jsonl(stream):
        if kind == "header":
            header = obj
        elif kind == "entry":
            entries.append(obj)
        else:
            footer = obj
    return header, entries, footer
