"""Canonical JSON metadata objects and the SequenceRef pager (FORMAT §3).

`ObjectSink.put(payload) -> hash` stores a metadata object (deduplicated);
`ObjectSource.get(hash, limit) -> bytes` reads one back, verified.
"""
from __future__ import annotations

import hashlib
import json

from .packs import METADATA_OBJECT_LIMIT, PackError

PAGE_MIN_ITEMS = 32
PAGE_MAX_ITEMS = 1024
PAGE_MAX_BYTES = METADATA_OBJECT_LIMIT
INLINE_DESCRIPTORS_BYTES = 4 * 1024


class MetadataError(ValueError):
    """Metadata failed a structural check; the version must not be used."""


def canonical(value) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise MetadataError("metadata is not finite JSON") from exc


def parse(data: bytes):
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise MetadataError("metadata object is not valid JSON") from exc


def _is_hash(value) -> bool:
    return (isinstance(value, str) and len(value) == 64
            and all(c in "0123456789abcdef" for c in value))


def is_anchor(item, encoded: bytes) -> bool:
    if isinstance(item, dict) and _is_hash(item.get("hash")):
        return item["hash"].startswith("00")
    if _is_hash(item):
        return item.startswith("00")
    return hashlib.sha256(encoded).digest()[0] == 0


class SequenceWriter:
    """Streams items into content-addressed pages; `finish()` returns the SequenceRef."""

    def __init__(self, sink):
        self._sink = sink
        self._items = []           # encoded items of the open page
        self._bytes = 2            # "[" + "]"
        self._descriptors = []     # [hash, payload_size, item_count]
        self.count = 0

    def add(self, item):
        encoded = canonical(item)
        if len(encoded) + 2 > PAGE_MAX_BYTES:
            raise MetadataError("a single sequence item exceeds the page limit")
        extra = len(encoded) + (1 if self._items else 0)
        if self._items and self._bytes + extra > PAGE_MAX_BYTES:
            self._flush()
            extra = len(encoded)
        self._items.append(encoded)
        self._bytes += extra
        self.count += 1
        n = len(self._items)
        if n >= PAGE_MAX_ITEMS or (n >= PAGE_MIN_ITEMS and is_anchor(item, encoded)):
            self._flush()

    def _flush(self):
        if not self._items:
            return
        payload = b"[" + b",".join(self._items) + b"]"
        digest = self._sink.put(payload)
        self._descriptors.append([digest, len(payload), len(self._items)])
        self._items, self._bytes = [], 2

    def finish(self):
        self._flush()
        descriptors = self._descriptors
        if len(canonical(descriptors)) <= INLINE_DESCRIPTORS_BYTES:
            return {"count": self.count, "pages": descriptors}
        index_payload = canonical({"pages": descriptors})
        if len(index_payload) <= PAGE_MAX_BYTES:
            return {"count": self.count, "index": self._sink.put(index_payload), "levels": 1}
        # Two levels: split the descriptor list into index objects that each fit.
        groups, group, size = [], [], 0
        for d in descriptors:
            item = len(canonical(d)) + 1
            if group and size + item > PAGE_MAX_BYTES - 64:
                groups.append(group)
                group, size = [], 0
            group.append(d)
            size += item
        if group:
            groups.append(group)
        outer = []
        for group in groups:
            payload = canonical({"pages": group})
            outer.append([self._sink.put(payload), len(payload), sum(g[2] for g in group)])
        outer_payload = canonical({"indexes": outer})
        if len(outer_payload) > PAGE_MAX_BYTES:
            raise MetadataError("sequence too large for two index levels")
        return {"count": self.count, "index": self._sink.put(outer_payload), "levels": 2}


def _check_descriptor(d):
    if (not isinstance(d, list) or len(d) != 3 or not _is_hash(d[0])
            or not isinstance(d[1], int) or not isinstance(d[2], int)
            or d[1] < 0 or d[1] > PAGE_MAX_BYTES or d[2] < 0):
        raise MetadataError("bad page descriptor")


def iter_sequence(ref, source):
    """Yield items of a SequenceRef in order, verifying every page."""
    if not isinstance(ref, dict) or not isinstance(ref.get("count"), int) or ref["count"] < 0:
        raise MetadataError("bad SequenceRef")
    if "pages" in ref:
        descriptors = ref["pages"]
    else:
        levels = ref.get("levels")
        if not _is_hash(ref.get("index")) or levels not in (1, 2):
            raise MetadataError("bad SequenceRef index")
        index = parse(source.get(ref["index"], METADATA_OBJECT_LIMIT))
        if levels == 1:
            descriptors = index.get("pages") if isinstance(index, dict) else None
        else:
            outer = index.get("indexes") if isinstance(index, dict) else None
            if not isinstance(outer, list):
                raise MetadataError("bad two-level index")
            descriptors = []
            for d in outer:
                _check_descriptor(d)
                inner = parse(source.get(d[0], METADATA_OBJECT_LIMIT))
                pages = inner.get("pages") if isinstance(inner, dict) else None
                if not isinstance(pages, list) or sum(p[2] for p in pages if isinstance(p, list) and len(p) == 3) != d[2]:
                    raise MetadataError("inner index count mismatch")
                descriptors.extend(pages)
    if not isinstance(descriptors, list):
        raise MetadataError("bad page list")
    seen = 0
    for d in descriptors:
        _check_descriptor(d)
        payload = source.get(d[0], METADATA_OBJECT_LIMIT)
        if len(payload) != d[1]:
            raise MetadataError("page size mismatch")
        items = parse(payload)
        if not isinstance(items, list) or len(items) != d[2]:
            raise MetadataError("page item count mismatch")
        seen += len(items)
        if seen > ref["count"]:
            raise MetadataError("sequence longer than declared")
        yield from items
    if seen != ref["count"]:
        raise MetadataError("sequence shorter than declared")


class MemorySink:
    """Test helper: in-memory object store implementing put/get."""

    def __init__(self):
        self.objects = {}

    def put(self, payload: bytes) -> str:
        digest = hashlib.sha256(payload).hexdigest()
        self.objects.setdefault(digest, bytes(payload))
        return digest

    def get(self, digest: str, limit: int) -> bytes:
        data = self.objects.get(digest)
        if data is None:
            raise PackError("missing object " + digest)
        if len(data) > limit:
            raise PackError("object larger than limit")
        return data
