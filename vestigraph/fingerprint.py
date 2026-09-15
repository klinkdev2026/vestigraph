"""Legacy fingerprint API, dispatched through the file-format registry."""
from pathlib import Path
from .vesti_formats.registry import FORMATS

def content_fingerprint(path, *, formats=None):
    formats = formats if formats is not None else FORMATS.copy(frozen=True)
    path = Path(path)
    with path.open("rb") as stream:
        handler = formats.storage_for_content(stream.read(formats.probe_size))
    return handler.fingerprint(path)
