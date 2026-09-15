"""Identity codec; stable VGPK0002 wire ID 0."""
from .contract import VestiCodec, VestiCodecDescriptor, VestiCodecError


class VestiRawCodec(VestiCodec):
    descriptor = VestiCodecDescriptor("raw-v1", 0, "full")

    def encode(self, payload, *, base=None, level=None):
        return payload

    def decode(self, stored, raw_size, limit, *, base=None, native=False):
        if not 0 <= raw_size <= limit or len(stored) != raw_size:
            raise VestiCodecError("raw object length mismatch")
        return stored
