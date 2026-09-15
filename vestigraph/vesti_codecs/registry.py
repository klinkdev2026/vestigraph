"""Immutable per-composition codec registry. No implicit plugin loading or fallback."""
from types import MappingProxyType
from .contract import VestiCodec, VestiCodecError, VESTI_CODEC_API

_RESERVED = {0: ("raw-v1", "full"), 1: ("zlib-v1", "full"),
             2: ("bsdiff40-restricted-v1", "delta")}


class VestiCodecRegistry:
    def __init__(self, codecs):
        by_wire, by_id = {}, {}
        for codec in codecs:
            if not isinstance(codec, VestiCodec):
                raise ValueError("Codec must implement VestiCodec")
            d = codec.descriptor
            if (type(d.wire_id) is not int or not 0 <= d.wire_id <= 255
                    or type(d.api_version) is not int or d.api_version != VESTI_CODEC_API
                    or d.kind not in ("full", "delta")
                    or not isinstance(d.codec_id, str) or not d.codec_id
                    or d.wire_id in by_wire or d.codec_id in by_id
                    or (d.wire_id in _RESERVED and _RESERVED[d.wire_id] != (d.codec_id, d.kind))):
                raise ValueError("Invalid, duplicate or reserved codec descriptor")
            by_wire[d.wire_id], by_id[d.codec_id] = codec, codec
        self._by_wire = MappingProxyType(by_wire)
        self._by_id = MappingProxyType(by_id)

    def wire(self, wire_id):
        if type(wire_id) is not int or wire_id not in self._by_wire:
            raise VestiCodecError("unknown codec %r; install its matching decoder" % (wire_id,))
        return self._by_wire[wire_id]

    def get(self, codec_id):
        try:
            return self._by_id[codec_id]
        except KeyError as exc:
            raise VestiCodecError("Missing codec provider: %s" % codec_id) from exc

    def is_full(self, wire_id):
        return self.wire(wire_id).descriptor.kind == "full"

    def decode(self, wire_id, stored, raw_size, limit, *, base=None, native=None):
        if type(raw_size) is not int or type(limit) is not int or not 0 <= raw_size <= limit:
            raise VestiCodecError("object larger than the caller's limit")
        codec = self.wire(wire_id)
        if (codec.descriptor.kind == "delta") != (base is not None):
            raise VestiCodecError("codec base requirement mismatch")
        data = codec.decode(stored, raw_size, limit, base=base, native=native)
        if not isinstance(data, bytes) or len(data) != raw_size:
            raise VestiCodecError("decoded object length mismatch")
        return data

    def describe(self):
        return [{"codec_id": c.descriptor.codec_id, "wire_id": n,
                 "kind": c.descriptor.kind, "api_version": c.descriptor.api_version,
                 "encode_available": c.encoder_available(), "decode_available": True}
                for n, c in sorted(self._by_wire.items())]
