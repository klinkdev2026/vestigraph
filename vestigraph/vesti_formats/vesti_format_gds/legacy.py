"""Strict normalization of old format-1 GDS snapshots; used only across migration."""
from .scan import scan_gds, NORMALIZED_ALGORITHM


class _Sink:
    def chunk(self, *args): pass
    def references(self, *args): pass
    def segment(self, *args): pass


class _Chunks:
    def __init__(self, repo, manifest):
        self.repo, self.chunks = repo, iter(manifest["chunks"])
        self.pending = b""

    def read(self, size):
        pieces, total = [], 0
        while total < size:
            if not self.pending:
                item = next(self.chunks, None)
                if item is None:
                    break
                self.pending = self.repo._read_chunk(item["hash"], item["size"])
            take = min(size-total, len(self.pending))
            pieces.append(self.pending[:take])
            self.pending = self.pending[take:]
            total += take
        return b"".join(pieces)


def normalized_legacy(repo, parent):
    manifest = parent["manifest"]
    if manifest.get("format") != 1:
        return None
    key = (parent["id"], parent["sha256"])
    if getattr(repo, "_legacy_comparison_cache", (None,))[0] == key:
        return repo._legacy_comparison_cache[1]
    result = scan_gds(_Chunks(repo, manifest), _Sink())
    if result.raw_sha256 != parent["sha256"] or manifest["sha256"] != parent["sha256"]:
        raise ValueError("Legacy snapshot checksum mismatch.")
    value = (NORMALIZED_ALGORITHM, result.normalized_sha256)
    repo._legacy_comparison_cache = (key, value)
    return value
