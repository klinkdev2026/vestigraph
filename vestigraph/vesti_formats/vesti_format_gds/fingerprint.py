"""Content fingerprint of a saved layout file: GDS2 with its timestamps masked, anything else verbatim.

The GDS2 stream is walked RECORD BY RECORD (4-byte header: length, type, datatype). Only the
24-byte timestamp payload of a real BGNLIB / BGNSTR record is left out of the hash. Earlier
versions located BGNSTR by a raw byte search, which also matched the same four bytes inside
geometry payloads and masked real coordinates (a fake ``BGNSTR`` inside an XY record made an
edit there invisible). Framing cannot be fooled that way: a record's payload is never
interpreted, only skipped by its declared length.

Reading is streamed in bounded chunks; the file is never memory-mapped (a writer truncating a
mapped file is a process-level fault on Windows and a SIGBUS on POSIX, not a Python error).
A malformed stream (zero/short length, truncated record) is hashed verbatim from that point on
-- fingerprinting must never raise on a file that KLayout may still be writing.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

_HEADER_RECORD = b"\x00\x06\x00\x02"      # HEADER, INT2, one version word: first record of any GDS2 stream
_BGNLIB = 0x01
_BGNSTR = 0x05
_TIMESTAMP_BYTES = 24                     # modification + access time, 12 x INT2 each
_STAMPED_LENGTH = 4 + _TIMESTAMP_BYTES    # BGNLIB / BGNSTR records carry exactly the two timestamps
_CHUNK = 4 * 1024 * 1024


def _stamped_record(view, pos) -> bool:
    """True when the record header at ``pos`` is a BGNLIB/BGNSTR of the standard length."""
    length = (view[pos] << 8) | view[pos + 1]
    return length == _STAMPED_LENGTH and view[pos + 2] in (_BGNLIB, _BGNSTR) and view[pos + 3] == 2


def content_fingerprint(path: str | Path) -> str:
    """sha256 hex of the file's content with GDS2 timestamps masked out."""
    path = Path(path)
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        buf = bytearray(stream.read(4))
        if bytes(buf) != _HEADER_RECORD:
            digest.update(buf)
            for chunk in iter(lambda: stream.read(_CHUNK), b""):
                digest.update(chunk)
            return digest.hexdigest()
        pos = 0            # next record boundary inside buf
        hashed = 0         # bytes of buf already fed to the digest
        eof = False
        while True:
            if len(buf) - pos < 4:
                if eof:
                    break
                more = stream.read(_CHUNK)
                if not more:
                    eof = True
                buf += more
                continue
            length = (buf[pos] << 8) | buf[pos + 1]
            if length < 4:
                # Zero-length padding or a corrupt header: no framing beyond this point.
                break
            end = pos + length
            if end > len(buf):
                if eof:
                    break                              # truncated final record: hashed verbatim below
                more = stream.read(_CHUNK)
                if not more:
                    eof = True
                buf += more
                continue
            if _stamped_record(buf, pos):
                digest.update(buf[hashed:pos + 4])     # the header stays in the hash
                hashed = end                           # the 24 timestamp bytes do not
            pos = end
            if pos >= _CHUNK:
                # At a record boundary everything before ``pos`` is settled: hash the
                # pending span and drop it, so memory stays bounded by the chunk size.
                digest.update(buf[hashed:pos])
                del buf[:pos]
                pos = 0
                hashed = 0
        # Remainder (well-formed tail, malformed tail, or padding) verbatim.
        digest.update(buf[hashed:])
        for chunk in iter(lambda: stream.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()
