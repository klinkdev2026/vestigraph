"""Repository-scoped dependencies, fixed for the repository lifetime."""
from dataclasses import dataclass, field
from ..vesti_formats.registry import FormatRegistry, FORMATS
from ..vesti_codecs.defaults import vesti_default_codecs
from ..vesti_codecs.services import VestiCodecServices


@dataclass(frozen=True)
class VestiRepositoryServices:
    formats: FormatRegistry = field(default_factory=lambda: FORMATS.copy())
    codecs: VestiCodecServices = field(default_factory=vesti_default_codecs)

    def __post_init__(self):
        if not isinstance(self.formats, FormatRegistry) or not isinstance(self.codecs, VestiCodecServices):
            raise ValueError("Invalid repository service composition")
        object.__setattr__(self, "formats", self.formats.copy(frozen=True))
