"""Read-only recipe and reversible timestamp interpretation shared by readers."""
import base64

from ..storage import policy
from ..storage.errors import StorageError
from ..storage.metadata import iter_sequence

def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")

def _iter_ref(ref, index):
    return iter_sequence(ref, index)


def _iter_recipe(root, index):
    for entry in iter_sequence(root["recipe"], index):
        if not isinstance(entry, dict):
            raise StorageError("Recipe entry must be an object")
        yield entry


def _entry_chunks(entry, index):
    if "chunks_ref" in entry:
        return _iter_ref(entry["chunks_ref"], index)
    if "chunks" in entry:
        chunks = entry["chunks"]
        if not isinstance(chunks, list) or not 2 <= len(chunks) <= policy.INLINE_CHUNKS:
            raise StorageError("Manifest chunk list is malformed; restore metadata from backup.")
        return iter(chunks)
    if entry.get("size", 0) == 0:
        return iter(())
    return iter((entry["hash"],))
