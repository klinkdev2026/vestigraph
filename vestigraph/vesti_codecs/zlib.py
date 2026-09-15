"""Bounded zlib codec; stable VGPK0002 wire ID 1."""
import zlib
from .contract import VestiCodec, VestiCodecDescriptor, VestiCodecError


class VestiZlibCodec(VestiCodec):
    descriptor = VestiCodecDescriptor("zlib-v1", 1, "full")

    def encode(self, payload, *, base=None, level=None):
        return zlib.compress(payload, 9 if level is None else level)

    def decode(self, stored, raw_size, limit, *, base=None, native=False):
        if not 0 <= raw_size <= limit:
            raise VestiCodecError("object larger than the caller's limit")
        decoder = zlib.decompressobj()
        try:
            data = decoder.decompress(stored, raw_size + 1)
        except zlib.error as exc:
            raise VestiCodecError("zlib stream is corrupt") from exc
        if len(data) != raw_size or not decoder.eof or decoder.unused_data:
            raise VestiCodecError("zlib object length mismatch or trailing data")
        return data
