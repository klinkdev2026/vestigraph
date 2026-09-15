"""Replaceable encoding selection; codecs do not own policy or storage IO."""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from .contract import VestiCodecError


class VestiFullSelection(ABC):
    def validate(self, registry):
        """Validate encoder dependencies at service composition time."""

    @abstractmethod
    def encode(self, payload, registry, levels):
        """Return (wire_id, bytes); retained output must never exceed input size."""


@dataclass(frozen=True)
class VestiSmallestFull(VestiFullSelection):
    """Historical policy: raw, then zlib levels; each step must save at least 32 B."""
    codec_ids: tuple = ("zlib-v1",)
    min_saving: int = 32
    fallback_codec_id: str = "raw-v1"

    def validate(self, registry):
        if type(self.min_saving) is not int or self.min_saving < 0:
            raise VestiCodecError("min_saving must be a nonnegative integer")
        for codec_id in (self.fallback_codec_id, *self.codec_ids):
            codec = registry.get(codec_id)
            if codec.descriptor.kind != "full" or not codec.encoder_available():
                raise VestiCodecError("Full selection requires available full encoders")

    def encode(self, payload, registry, levels):
        raw = registry.get(self.fallback_codec_id)
        best_codec, best = raw.descriptor.wire_id, raw.encode(payload)
        for codec_id in self.codec_ids:
            codec = registry.get(codec_id)
            if codec.descriptor.kind != "full":
                raise VestiCodecError("Full selection received a delta codec")
            for level in levels:
                packed = codec.encode(payload, level=level)
                if not isinstance(packed, bytes):
                    raise VestiCodecError("Encoder must return bytes")
                if len(packed) + self.min_saving <= len(best):
                    best_codec, best = codec.descriptor.wire_id, packed
        return best_codec, best


class VestiDeltaSelection(ABC):
    def base_eligible(self, size):
        """Whether a format should collect prior byte regions of this size."""
        return True

    @abstractmethod
    def eligible(self, payload_size, full_size):
        """Whether to evaluate format-supplied bases."""

    @abstractmethod
    def accepts(self, patch_record_size, full_record_size):
        """Whether the best verified candidate saves enough bytes."""


@dataclass(frozen=True)
class VestiDeltaSavings(VestiDeltaSelection):
    min_chunk: int = 16 * 1024
    min_full: int = 1024
    min_saving: int = 128

    def base_eligible(self, size):
        return size >= self.min_chunk

    def eligible(self, payload_size, full_size):
        return payload_size >= self.min_chunk and full_size > self.min_full

    def accepts(self, patch_record_size, full_record_size):
        return patch_record_size + self.min_saving <= full_record_size
