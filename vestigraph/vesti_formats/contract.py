"""Versioned file-format boundary. Providers never publish history or own editor sessions."""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from collections.abc import Iterator

VESTI_FORMAT_API = 1
# Persisted format-2 compatibility identifier, including historical opaque snapshots.
VESTI_LEGACY_PROFILE = "gds-record-cdc-v1"

class VestiFormatError(ValueError):
    """Only a structural input failure permits an opaque retry."""
    def __init__(self, reason, offset):
        super().__init__("%s at byte %d" % (reason, offset))
        self.reason, self.offset = reason, offset

@dataclass(slots=True)
class VestiScanResult:
    raw_sha256: str = ""
    normalized_sha256: str = ""
    size: int = 0
    segments: int = 0
    cells: int = 0
    stamps: int = 0
    chunk_refs: int = 0

class VestiNoopUnavailable:
    """A missing optimization must never certify equality."""
    def update(self, data): pass
    def matches(self, size): return False
    def close(self): pass

class VestiFormatHandler(ABC):
    api_version = VESTI_FORMAT_API
    format_id: str

    def setup_work(self, work): pass

    @abstractmethod
    def create_sink(self, prepared, index, work, monitor): ...

    @abstractmethod
    def load_previous(self, index, parent, work) -> bool: ...

    @abstractmethod
    def scan(self, stream, sink, monitor, preference) -> tuple[VestiScanResult, dict]: ...

    @abstractmethod
    def finish_snapshot(self, sink, result) -> dict:
        """Persisted fields: profile, recipe, normalization, format_analysis and optional indexes."""

    @abstractmethod
    def build_changes(self, index, parent, prepared, sink, analyze, prev_loaded, evidence): ...

    @abstractmethod
    def restore_parts(self, root, index) -> Iterator[bytes]:
        """Bounded, validated original-byte parts; core checks final size/hash independently."""

    @abstractmethod
    def fingerprint(self, path) -> str: ...

    def snapshot_top_cells(self, manifest):
        return None

    def noop_probe(self, repo, parent, size):
        return VestiNoopUnavailable()

def vesti_validate_fields(fields):
    """Format metadata cannot replace transaction/version/raw-integrity fields."""
    from ..storage.errors import StorageError
    required = {"profile", "normalized_sha256", "normalized_algorithm", "format_analysis", "recipe"}
    reserved = {"format", "storage_policy", "size", "sha256", "segments", "counts", "storage"}
    if (not isinstance(fields, dict) or not required <= fields.keys() or reserved & fields.keys()
            or not isinstance(fields["profile"], str)
            or not isinstance(fields["normalized_algorithm"], str)
            or not isinstance(fields["normalized_sha256"], str)
            or len(fields["normalized_sha256"]) != 64
            or not isinstance(fields["format_analysis"], dict)):
        raise StorageError("Invalid file-format provider metadata")
    return fields
