"""Legacy GDS timestamp table interpretation."""
import bisect
from ...storage.errors import StorageError
from ..recipe import _iter_ref

def _iter_run_table(root, index):
    """Yield [hex, count] runs of a manifest, validated."""
    stamps = root.get("timestamps") or {}
    if not isinstance(stamps, dict):
        raise StorageError("Manifest timestamps must be an object")
    if "runs" in stamps:
        runs = stamps["runs"]
    elif "runs_ref" in stamps:
        runs = _iter_ref(stamps["runs_ref"], index)
    else:
        runs = []
    for run in runs:
        if (not isinstance(run, list) or len(run) != 2 or not isinstance(run[0], str)
                or len(run[0]) != 48 or not isinstance(run[1], int) or run[1] <= 0):
            raise StorageError("Manifest timestamp runs are malformed; restore metadata from backup.")
        yield run


def _iter_runs(root, index):
    """Yield one 24-byte value per stamp slot; enforce the declared count."""
    consumed = 0
    for value, count in _iter_run_table(root, index):
        raw = bytes.fromhex(value)
        for _ in range(count):
            consumed += 1
            yield raw
    if consumed != (root.get("timestamps") or {}).get("count", 0):
        raise StorageError("Manifest timestamp count disagrees with its runs; restore metadata from backup.")


class _RunLookup:
    """slot -> timestamp value over a bounded run table (cumulative starts + bisect)."""

    def __init__(self, runs):
        self.starts, self.values = [], []
        total = 0
        for value, count in runs:
            self.starts.append(total)
            self.values.append(value)
            total += count
        self.total = total

    def at(self, slot):
        if type(slot) is not int or not 0 <= slot < self.total:
            raise StorageError("Timestamp slot is outside the persisted runs table")
        i = bisect.bisect_right(self.starts, slot) - 1
        return self.values[i]


class VestiGdsTransform:
    """Restore the existing format-2 GDS normalization without a geometry parser."""
    def __init__(self, root, index):
        self.runs = _iter_runs(root, index)

    def begin(self, entry):
        if entry.get("kind") not in ("lib_head", "cell", "lib_tail"):
            raise StorageError("Invalid GDS recipe segment")
        self.stamp = entry.get("stamp")
        self.value = None
        if self.stamp is not None:
            if (type(self.stamp) is not int or self.stamp < 0
                    or self.stamp + 24 > entry["size"]):
                raise StorageError("Manifest timestamp offset is out of range; restore metadata from backup.")
            try:
                self.value = next(self.runs)
            except StopIteration:
                raise StorageError("Manifest has fewer timestamps than stamp slots; restore metadata from backup.")

    def apply(self, data, written):
        if (self.value is not None and self.stamp < written + len(data)
                and self.stamp + 24 > written):
            patched = bytearray(data)
            lo, hi = max(self.stamp, written), min(self.stamp + 24, written + len(data))
            patched[lo-written:hi-written] = self.value[lo-self.stamp:hi-self.stamp]
            return bytes(patched)
        return data

    def finish(self):
        try:
            next(self.runs)
        except StopIteration:
            return
        raise StorageError("Manifest has more timestamps than stamp slots; restore metadata from backup.")
