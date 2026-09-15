"""Common ordered-object restore, independent of file format and normalization."""
import hashlib
from .recipe import _iter_recipe, _entry_chunks
from ..storage.errors import StorageError
from ..storage.packs import LAYOUT_OBJECT_LIMIT

def vesti_restore_parts(root, index, transform):
    segments = 0
    for entry in _iter_recipe(root, index):
        segments += 1
        size, digest = entry.get("size"), entry.get("hash")
        if type(size) is not int or size < 0 or not isinstance(digest, str):
            raise StorageError("Manifest segment is malformed")
        transform.begin(entry)
        hashed = hashlib.sha256()
        written = 0
        for key in _entry_chunks(entry, index):
            data = index.get(key, LAYOUT_OBJECT_LIMIT)
            hashed.update(data)
            if written + len(data) > size:
                raise StorageError("Segment longer than declared; restore metadata from backup.")
            yield transform.apply(data, written)
            written += len(data)
        if written != size or hashed.hexdigest() != digest:
            raise StorageError("Segment integrity mismatch; restore complete metadata and objects from backup.")
    if segments != root.get("segments"):
        raise StorageError("Manifest segment count mismatch; restore metadata from backup.")
    transform.finish()

class VestiRawTransform:
    def begin(self, entry):
        if entry.get("kind") != "opaque" or "stamp" in entry:
            raise StorageError("Invalid opaque recipe")
    def apply(self, data, offset):
        return data
    def finish(self): pass
