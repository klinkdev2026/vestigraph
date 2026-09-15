"""Format-selected no-op optimization; document identity is independent of format."""

def same_document(parent, metadata):
    previous = parent.get("metadata") or {}
    current = metadata.get("checkpoint_metadata") or {}
    if previous.get("format", "GDS2") != current.get("format", "GDS2"):
        return False
    left, right = previous.get("document"), current.get("document")
    if not isinstance(left, dict) or not isinstance(right, dict):
        return left == right
    # Saved-file identity survives editor reconnects. Runtime DocumentRefs still
    # fence every export; they must not create new history for an unchanged file.
    if left.get("filename") and right.get("filename"):
        from pathlib import Path
        return Path(left["filename"]).resolve() == Path(right["filename"]).resolve()
    return left == right



class NoopProbe:
    def __init__(self, repo, parent, size):
        root = parent.get("manifest") or {}
        if root.get("format") != 2:
            from .vesti_formats.contract import VestiNoopUnavailable
            self._probe = VestiNoopUnavailable()
        else:
            self._probe = repo.services.formats.storage_for_manifest(root).noop_probe(repo, parent, size)
    def update(self, data):
        return self._probe.update(data)
    def matches(self, size):
        return self._probe.matches(size)
    def close(self):
        return self._probe.close()
