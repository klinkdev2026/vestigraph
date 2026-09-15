"""A write configuration retains all registered historical decoders."""
from dataclasses import dataclass, field
from .registry import VestiCodecRegistry
from .selection import VestiFullSelection, VestiSmallestFull, VestiDeltaSelection, VestiDeltaSavings
from .contract import VestiCodecError


@dataclass(frozen=True)
class VestiCodecServices:
    registry: VestiCodecRegistry
    full_selection: VestiFullSelection = field(default_factory=VestiSmallestFull)
    delta_codec_id: str | None = "bsdiff40-restricted-v1"
    delta_selection: VestiDeltaSelection = field(default_factory=VestiDeltaSavings)

    def __post_init__(self):
        if (not isinstance(self.registry, VestiCodecRegistry)
                or not isinstance(self.full_selection, VestiFullSelection)
                or not isinstance(self.delta_selection, VestiDeltaSelection)):
            raise ValueError("Invalid codec service composition")
        self.full_selection.validate(self.registry)
        if self.delta_codec_id is not None and self.delta_codec.descriptor.kind != "delta":
            raise ValueError("Selected delta encoder is a full codec")

    @property
    def delta_codec(self):
        return self.registry.get(self.delta_codec_id) if self.delta_codec_id is not None else None

    def encode_full(self, payload, levels=(1, 9)):
        codec, stored = self.full_selection.encode(payload, self.registry, levels)
        if (not isinstance(stored, bytes) or len(stored) > len(payload)
                or not self.registry.is_full(codec)):
            raise VestiCodecError("Full encoder violated the bounded-output contract")
        # All provider outputs are verified before publication, including custom full codecs.
        if self.registry.decode(codec, stored, len(payload), len(payload)) != payload:
            raise VestiCodecError("full codec self-check failed; nothing was published")
        return codec, stored
