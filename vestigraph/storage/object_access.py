"""Verified object reads and bounded delta-chain traversal (no engine dependency)."""
import hashlib
import zlib
from collections import OrderedDict

from . import policy
from .errors import StorageError, StorageIOError, CancelledError, CORRUPT_DATA_ERRORS
from .packs import (MAX_DELTA_DEPTH, LAYOUT_OBJECT_LIMIT,
                    METADATA_OBJECT_LIMIT, PackError, PackReader)

def _validate_chain_row(row, limit, registry):
    """Shared index contract for full roots and delta links, before payload IO."""
    codec, depth, size = row["codec"], row["depth"], row["raw_size"]
    base_size, base_hash = row["base_raw_size"], row["base_hash"]
    if (type(codec) is not int
            or type(depth) is not int or type(size) is not int
            or not 0 <= size <= LAYOUT_OBJECT_LIMIT
            or type(base_size) is not int or not 0 <= base_size <= LAYOUT_OBJECT_LIMIT):
        raise PackError("invalid object codec, depth or size")
    if registry.is_full(codec):
        if depth != 0 or base_hash is not None or base_size != 0:
            raise PackError("full object carries delta fields")
    elif not 1 <= depth <= min(limit, MAX_DELTA_DEPTH) or not base_hash:
        raise PackError("delta chain deeper than allowed or missing its base")


def _delta_base(index, row, limit):
    """One checked link, used by both base selection and recursive restoration."""
    _validate_chain_row(row, limit, index.codecs.registry)
    if index.codecs.registry.is_full(row["codec"]):
        raise PackError("expected a delta object")
    base = index.row(row["base_hash"])
    if base is None:
        raise PackError("delta base %s is missing" % row["base_hash"])
    _validate_chain_row(base, row["depth"] - 1, index.codecs.registry)
    if (base["hash"] != row["base_hash"] or base["depth"] >= row["depth"]
            or base["raw_size"] != row["base_raw_size"]):
        raise PackError("delta base depth/size disagree with the object")
    return base


def _chain_root(index, row, limit=MAX_DELTA_DEPTH):
    """Verified depth-0 index root, or None for corrupt metadata; never loops."""
    try:
        seen = {row["hash"]}
        _validate_chain_row(row, limit, index.codecs.registry)
        while not index.codecs.registry.is_full(row["codec"]):
            row = _delta_base(index, row, limit)
            if row["hash"] in seen:
                return None
            seen.add(row["hash"])
        return row
    except CancelledError:
        raise
    except CORRUPT_DATA_ERRORS:
        return None


class ObjectIndex:
    """Verified object access over the objects table, packs and v1 loose files."""

    def __init__(self, repo):
        self.repo = repo
        self.codecs = repo.services.codecs
        self.reader = PackReader(lambda pack_id: repo.pack_path(pack_id), registry=self.codecs.registry)
        self._has_loose = any(repo.objects_dir.glob("*/*")) if repo.objects_dir.is_dir() else False
        self._db = repo._open_connection()      # one read connection for the whole job
        self._cache = OrderedDict()             # decoded objects (delta bases are re-used a lot)
        self._cache_bytes = 0
        self.delta_decodes = 0
        self.metadata_reads = 0                 # objects fetched with the metadata limit (pages, roots)
        self.layout_reads = 0                   # objects fetched with the layout limit (chunks, bases)

    def row(self, digest):
        return self._db.execute("SELECT * FROM objects WHERE hash=?", (digest,)).fetchone()

    def exists_many(self, digests):
        """Subset of `digests` already committed (objects table or loose)."""
        found = set()
        digests = list(digests)
        db = self._db
        for i in range(0, len(digests), 500):
            batch = digests[i:i + 500]
            marks = ",".join("?" * len(batch))
            for row in db.execute(f"SELECT hash FROM objects WHERE hash IN ({marks})", batch):
                found.add(row[0])
        if self._has_loose:
            for digest in digests:
                if digest in found:
                    continue
                path = self.repo.loose_path(digest)
                if path.exists():
                    try:
                        self._read_loose(digest, path, LAYOUT_OBJECT_LIMIT)   # a damaged v1 chunk is never reused
                    except StorageError:
                        continue
                    found.add(digest)
        return found

    def _cached(self, digest, data):
        if len(data) <= policy.DECODE_CACHE_BYTES:
            self._cache[digest] = data
            self._cache_bytes += len(data)
            while self._cache_bytes > policy.DECODE_CACHE_BYTES:
                _, old = self._cache.popitem(last=False)
                self._cache_bytes -= len(old)
        return data

    def get(self, digest, limit, _depth_budget=MAX_DELTA_DEPTH):
        cached = self._cache.get(digest)
        if cached is not None:
            if len(cached) > limit:
                raise StorageError(f"Object {digest} is larger than allowed here.")
            self._cache.move_to_end(digest)
            return cached
        row = self.row(digest)
        if row is not None:
            if limit <= METADATA_OBJECT_LIMIT:
                self.metadata_reads += 1
            else:
                self.layout_reads += 1
            try:
                _validate_chain_row(row, MAX_DELTA_DEPTH, self.codecs.registry)
                if self.codecs.registry.is_full(row["codec"]):
                    return self._cached(digest, self.reader.read(row, limit))
                return self._cached(digest, self._get_delta(row, limit, _depth_budget))
            except OSError as exc:
                if not isinstance(exc, FileNotFoundError):
                    raise StorageIOError("History object is temporarily inaccessible; retry when the file is available.") from exc
                raise StorageError(f"Object {digest} is missing; restore it from backup before exporting.") from exc
            except PackError as exc:
                raise StorageError(f"Object {digest} is missing or corrupt; restore it from backup before exporting.") from exc
        if self._has_loose:
            path = self.repo.loose_path(digest)
            if path.exists():
                return self._read_loose(digest, path, limit)
        raise StorageError(f"Object {digest} is missing; restore it from backup before exporting.")

    def _get_delta(self, row, limit, depth_budget):
        """Delta chain: depth strictly decreases (repair may shorten a chain), every level verified."""
        _delta_base(self, row, depth_budget)
        patch_bytes = self.reader.read_stored(row, limit)
        base = self.get(row["base_hash"], LAYOUT_OBJECT_LIMIT, row["depth"] - 1)
        data = self.codecs.registry.decode(row["codec"], patch_bytes, row["raw_size"], limit, base=base)
        self.delta_decodes += 1
        if hashlib.sha256(data).hexdigest() != row["hash"]:
            raise PackError("delta-decoded object hash mismatch")
        return data

    @staticmethod
    def _read_loose(digest, path, limit):
        try:
            encoded = path.read_bytes()
            decoder = zlib.decompressobj()
            data = decoder.decompress(encoded, limit + 1)
            if len(data) > limit or not decoder.eof or decoder.unused_data or hashlib.sha256(data).hexdigest() != digest:
                raise ValueError("loose object integrity mismatch")
            return data
        except OSError as exc:
            if not isinstance(exc, FileNotFoundError):
                raise StorageIOError("History object is temporarily inaccessible; retry when the file is available.") from exc
            raise StorageError(f"Object {digest} is missing; restore it from backup before exporting.") from exc
        except (ValueError, zlib.error) as exc:
            raise StorageError(f"Object {digest} is missing or corrupt; restore it from backup before exporting.") from exc

    def close(self):
        self.reader.close()
        self._db.close()
