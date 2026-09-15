"""Conservative no-op probe using a previously verified format-2 recipe.

No GDS byte search, geometry parsing, layout payload reads or growing offset list.
A match certifies equality outside the PARENT'S verified timestamp slots. Those
fixed-size payloads cannot change record boundaries; all other bytes, including
their record headers, participate in SHA-256. A miss is only a cache miss.
"""
from __future__ import annotations

import hashlib

from ...storage.object_access import ObjectIndex
from ..recipe import _iter_recipe
from . import scan as gds_scan


class NoopProbe:
    def __init__(self, repo, parent, size):
        self.parent_id = parent["id"]
        self.expected = None
        self.normalized = hashlib.sha256()
        self.position = 0
        self.index = None
        self.spans = iter(())
        self.next_span = None
        root = parent.get("manifest") or {}
        if (parent["size"] != size or root.get("format") != 2
                or root.get("normalized_algorithm") != gds_scan.NORMALIZED_ALGORITHM):
            return
        self.expected = root.get("normalized_sha256")
        self.index = ObjectIndex(repo)
        self.spans = self._spans(root)
        try:
            self.next_span = next(self.spans, None)
        except BaseException:
            self.close()
            raise

    def _spans(self, root):
        offset, count, stamps = 0, 0, 0
        for entry in _iter_recipe(root, self.index):
            kind, size = entry.get("kind"), entry.get("size")
            if type(size) is not int or size < 0 or kind not in ("lib_head", "cell", "lib_tail"):
                raise ValueError("No-op template is not a verified GDS recipe")
            stamp = entry.get("stamp")
            if stamp is not None:
                expected = 10 if kind == "lib_head" else 4 if kind == "cell" else None
                if type(stamp) is not int or stamp != expected or stamp + 24 > size:
                    raise ValueError("Invalid verified timestamp position")
                stamps += 1
                yield offset + stamp, offset + stamp + 24
            offset += size
            count += 1
        if (offset != root["size"] or count != root["segments"]
                or stamps != root.get("timestamps", {}).get("count", 0)):
            raise ValueError("No-op template totals disagree")

    def update(self, data):
        if self.expected is None:
            return
        view = memoryview(data)
        start, end = self.position, self.position + len(view)
        pos = start
        while self.next_span and self.next_span[0] < end:
            lo, hi = self.next_span
            if pos < lo:
                self.normalized.update(view[pos-start:lo-start])
                pos = lo
            stop = min(hi, end)
            self.normalized.update(bytes(stop-pos))
            pos = stop
            if hi <= end:
                self.next_span = next(self.spans, None)
            else:
                break
        self.normalized.update(view[pos-start:])
        self.position = end

    def matches(self, size):
        return (self.expected is not None and self.position == size and self.next_span is None
                and self.normalized.hexdigest() == self.expected)

    def close(self):
        if self.index is not None:
            self.index.close()
            self.index = None
