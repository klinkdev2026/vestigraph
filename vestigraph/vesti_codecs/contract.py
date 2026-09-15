"""Versioned byte-codec contract. Implementations must bound decoding before allocation."""
from abc import ABC, abstractmethod
from dataclasses import dataclass

VESTI_CODEC_API = 1


class VestiCodecError(ValueError):
    """Unsupported or invalid persisted encoding."""


class VestiPatchRejected(VestiCodecError):
    """Encoder output is outside the supported patch subset; full storage is allowed."""


@dataclass(frozen=True)
class VestiCodecDescriptor:
    codec_id: str
    wire_id: int
    kind: str
    api_version: int = VESTI_CODEC_API


class VestiCodec(ABC):
    descriptor: VestiCodecDescriptor

    def encoder_available(self):
        return True

    def encoder_version(self):
        return self.descriptor.codec_id if self.encoder_available() else None

    @abstractmethod
    def encode(self, payload, *, base=None, level=None):
        """Return bytes. Instances must support concurrent full-encoding calls."""

    @abstractmethod
    def decode(self, stored, raw_size, limit, *, base=None, native=None):
        """Return exactly raw_size bytes, or raise; never allocate unbounded output.
        native=None lets the provider choose; False requests the reference path;
        True requests native execution. Unsupported hints must not weaken validation.
        """

    def verify_preference(self, preference=None):
        if preference not in (None, "stdlib"):
            raise VestiCodecError("This codec only supports stdlib verification")
        return "stdlib"
