"""Immutable pack files (VGPK0002) and bounded object decoding.

One pack per commit. A record is a 112-byte header followed by the stored
payload; every field is verified on read and nothing is decoded beyond the
caller's size limit. Codec services and registries must be supplied explicitly;
this layer never selects a default algorithm set. See docs/STORAGE_V2_FORMAT.md §2.
"""
from __future__ import annotations

from collections import OrderedDict
import hashlib
import os
import struct
from ..vesti_codecs.contract import VestiCodecError as PackError

MAGIC = b"VGPK0002"
HEADER = struct.Struct("<32sBBHII32sI32s")
HEADER_SIZE = HEADER.size
assert HEADER_SIZE == 112

MAX_DELTA_DEPTH = 16

LAYOUT_OBJECT_LIMIT = 1024 * 1024 + 64 * 1024
METADATA_OBJECT_LIMIT = 256 * 1024
ZERO32 = bytes(32)


def sha256_hex(data) -> str:
    return hashlib.sha256(data).hexdigest()


def encode_full(payload: bytes, levels=(1, 9), *, codecs):
    return codecs.encode_full(payload, levels)


def decode_full(codec: int, stored: bytes, raw_size: int, limit: int, *, registry) -> bytes:
    return registry.decode(codec, stored, raw_size, limit)


class PackWriter:
    """Append-only writer for one not-yet-published pack file."""

    def __init__(self, path):
        self.path = path
        self._stream = open(path, "wb")
        self._stream.write(MAGIC)
        self.size = len(MAGIC)
        self.rows = []          # (hash, offset, raw_size, stored_size, codec, depth, base_hash, base_raw_size, stored_sha256)
        self.count = 0

    def put_record(self, digest_hex: str, codec: int, stored: bytes, raw_size: int,
                   depth: int = 0, base_hash: str | None = None, base_raw_size: int = 0):
        stored_hash = hashlib.sha256(stored).digest()
        base = bytes.fromhex(base_hash) if base_hash else ZERO32
        header = HEADER.pack(bytes.fromhex(digest_hex), codec, depth, 0, raw_size, len(stored),
                             base, base_raw_size, stored_hash)
        offset = self.size
        self._stream.write(header)
        self._stream.write(stored)
        self.size += HEADER_SIZE + len(stored)
        self.count += 1
        self.rows.append((digest_hex, offset, raw_size, len(stored), codec, depth, base_hash,
                          base_raw_size, stored_hash.hex()))
        return offset

    def finish(self):
        """flush + fsync; the file is complete but not yet published."""
        self._stream.flush()
        os.fsync(self._stream.fileno())
        self._stream.close()
        self._stream = None

    def abort(self):
        if self._stream is not None:
            self._stream.close()
            self._stream = None


class PackReader:
    """Reads verified objects from packs, keeping at most `max_open` handles."""

    def __init__(self, pack_path_for, max_open=16, *, registry):
        self.registry = registry
        self._path_for = pack_path_for
        self._max_open = max_open
        self._handles = OrderedDict()
        self.opens = 0
        self.reads = 0

    def _handle(self, pack_id):
        handle = self._handles.get(pack_id)
        if handle is not None:
            self._handles.move_to_end(pack_id)
            return handle
        if len(self._handles) >= self._max_open:
            _, old = self._handles.popitem(last=False)
            old.close()
        handle = open(self._path_for(pack_id), "rb")
        self._handles[pack_id] = handle
        self.opens += 1
        return handle

    def read_stored(self, row, limit: int) -> bytes:
        """Verified stored payload of `row` (objects-table row); the header must agree with the
        index field by field. Decoding (full or delta) is the caller's job."""
        digest = row["hash"]
        codec = row["codec"]
        full = self.registry.is_full(codec)
        if full and (row["depth"] != 0 or row["base_hash"] or row["base_raw_size"] != 0):
            raise PackError("full-encoded object %s carries delta fields" % digest)
        if not full and (not 1 <= row["depth"] <= MAX_DELTA_DEPTH or not row["base_hash"]):
            raise PackError("delta-encoded object %s has an invalid depth or base" % digest)
        handle = self._handle(row["pack"])
        handle.seek(row["offset"])
        header = handle.read(HEADER_SIZE)
        if len(header) != HEADER_SIZE:
            raise PackError("pack record header is truncated")
        (obj_hash, codec_h, depth, reserved, raw_size, stored_size,
         base_hash, base_raw_size, stored_hash) = HEADER.unpack(header)
        expected_base = bytes.fromhex(row["base_hash"]) if row["base_hash"] else ZERO32
        if (obj_hash.hex() != digest or codec_h != codec or depth != row["depth"] or reserved != 0
                or raw_size != row["raw_size"] or stored_size != row["stored_size"]
                or base_hash != expected_base or base_raw_size != row["base_raw_size"]
                or stored_hash.hex() != row["stored_sha256"]):
            raise PackError("pack record header disagrees with the object index")
        if raw_size > limit or stored_size > limit + 4096:
            raise PackError("object larger than the caller's limit")
        stored = handle.read(stored_size)
        self.reads += 1
        if len(stored) != stored_size or hashlib.sha256(stored).digest() != stored_hash:
            raise PackError("stored payload is corrupt")
        return stored

    def read(self, row, limit: int) -> bytes:
        """Full-encoded object of `row`, decoded and hash-verified (delta rows are refused here)."""
        if not self.registry.is_full(row["codec"]):
            raise PackError("object %s is delta-encoded; use ObjectIndex.get" % row["hash"])
        stored = self.read_stored(row, limit)
        data = decode_full(row["codec"], stored, row["raw_size"], limit, registry=self.registry)
        if hashlib.sha256(data).hexdigest() != row["hash"]:
            raise PackError("decoded object hash mismatch")
        return data

    def close(self):
        for handle in self._handles.values():
            handle.close()
        self._handles.clear()


def scan_pack(path, *, registry):
    """Yield (hash, offset, raw_size, stored_size, codec, depth, base_hash, base_raw_size, stored_sha256)
    for every well-formed record; used to rebuild the object index. Stops at the first bad record."""
    with open(path, "rb") as stream:
        if stream.read(len(MAGIC)) != MAGIC:
            raise PackError("not a VGPK0002 pack")
        offset = len(MAGIC)
        while True:
            header = stream.read(HEADER_SIZE)
            if not header:
                return
            if len(header) != HEADER_SIZE:
                raise PackError("truncated record header at %d" % offset)
            (obj_hash, codec, depth, reserved, raw_size, stored_size,
             base_hash, base_raw_size, stored_hash) = HEADER.unpack(header)
            if reserved != 0:
                raise PackError("unknown codec or reserved bits at %d" % offset)
            if registry.is_full(codec) and (depth != 0 or base_hash != ZERO32 or base_raw_size != 0):
                raise PackError("full record carries delta fields at %d" % offset)
            if not registry.is_full(codec) and (not 1 <= depth <= MAX_DELTA_DEPTH or base_hash == ZERO32):
                raise PackError("delta record has an invalid depth or base at %d" % offset)
            if stored_size > LAYOUT_OBJECT_LIMIT + 4096 or raw_size > LAYOUT_OBJECT_LIMIT:
                raise PackError("record larger than any object may be at %d" % offset)
            stored = stream.read(stored_size)
            if len(stored) != stored_size or hashlib.sha256(stored).digest() != stored_hash:
                raise PackError("truncated or corrupt record at %d" % offset)
            yield (obj_hash.hex(), offset, raw_size, stored_size, codec, depth,
                   None if base_hash == ZERO32 else base_hash.hex(), base_raw_size, stored_hash.hex())
            offset += HEADER_SIZE + stored_size
