"""Single-file format contracts, independent of an editor or optional parser.

Recognizing an extension never asserts that a native project is self-contained.
Opaque storage is lossless byte storage, not semantic understanding.
"""
from dataclasses import dataclass
from collections.abc import Callable
from functools import wraps
from threading import RLock


def _synchronized(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return call


@dataclass(frozen=True)
class FormatSpec:
    format_id: str
    extensions: tuple[str, ...]
    artifact_role: str = "exchange_snapshot"
    structured_changes: bool = False
    native_project_complete: bool = False


class FormatRegistry:
    def __init__(self):
        self._lock = RLock()
        self._frozen = False
        self._formats = {}
        self._aliases = {}
        self._readers = {}
        self._storage = {}
        self._signatures = {}
        self._decoders = {}

    @_synchronized
    def copy(self, *, frozen=False):
        registry = FormatRegistry()
        from types import MappingProxyType
        for name in ("_formats", "_aliases", "_readers", "_storage", "_signatures", "_decoders"):
            values = dict(getattr(self, name))
            setattr(registry, name, MappingProxyType(values) if frozen else values)
        registry._frozen = frozen
        return registry

    def _check_mutable(self):
        if self._frozen:
            raise ValueError("Repository format services are frozen; compose a new repository instance")

    @_synchronized
    def register(self, spec, *, aliases=()):
        self._check_mutable()
        key = spec.format_id.upper()
        if (key in self._aliases or not spec.extensions or any(
                not ext.startswith(".") or not ext[1:].isalnum() for ext in spec.extensions)
                or spec.artifact_role not in ("exchange_snapshot", "file_snapshot")
                or spec.native_project_complete):
            raise ValueError("Invalid or duplicate single-file format.")
        names = (key, *(alias.upper() for alias in aliases))
        if len(set(names)) != len(names) or any(name in self._aliases for name in names):
            raise ValueError("Duplicate format alias.")
        self._formats[key] = spec
        self._aliases.update(dict.fromkeys(names, key))

    @_synchronized
    def get(self, name):
        key = self._aliases.get(str(name).upper())
        if key is None:
            raise ValueError("Unsupported file format.")
        return self._formats[key]

    @_synchronized
    def register_storage(self, format_id, factory, *, signatures, recipe_keys):
        """Explicit providers, selected by bytes for writes and persisted keys for reads.
        Factories are lazy and connection-free. Unknown restore keys never fall back.
        """
        self._check_mutable()
        key = self.get(format_id).format_id
        signatures, recipe_keys = tuple(signatures), tuple(recipe_keys)
        from .contract import VESTI_LEGACY_PROFILE
        if (not signatures or not recipe_keys
                or any(not isinstance(sig, bytes) or not sig or len(sig) > 64 for sig in signatures)
                or any(not isinstance(pair, tuple) or len(pair) != 2
                       or any(not isinstance(value, str) or not value for value in pair)
                       for pair in recipe_keys)):
            raise ValueError("Invalid storage provider signatures or recipe keys")
        if (key in self._storage or not callable(factory)
                or len(set(signatures)) != len(signatures)
                or any(sig in self._signatures for sig in signatures)
                or len(set(recipe_keys)) != len(recipe_keys)
                or any(pair in self._decoders or pair == (VESTI_LEGACY_PROFILE, "raw")
                       for pair in recipe_keys)):
            raise ValueError("Duplicate or reserved storage provider")
        self._storage[key] = factory
        self._signatures.update((sig, key) for sig in signatures)
        self._decoders.update((pair, key) for pair in recipe_keys)

    def _create_storage(self, key):
        from .contract import VestiFormatHandler, VESTI_FORMAT_API
        handler = self._storage[key]()
        if (not isinstance(handler, VestiFormatHandler) or handler.format_id != key
                or type(handler.api_version) is not int or handler.api_version != VESTI_FORMAT_API):
            raise ValueError("Incompatible Vestigraph format provider")
        return handler

    @property
    @_synchronized
    def probe_size(self):
        """Bounded content sniffing; supports full signatures longer than GDS's four bytes."""
        return max((len(sig) for sig in self._signatures), default=4)

    @_synchronized
    def storage_for_content(self, head):
        for signature in sorted(self._signatures, key=len, reverse=True):
            if head.startswith(signature):
                return self._create_storage(self._signatures[signature])
        return self.opaque_for_content(head)

    @_synchronized
    def opaque_for_content(self, head, reason=None):
        from .opaque import VestiOpaqueHandler
        kind = "unknown"
        for signature, key in self._signatures.items():
            if head.startswith(signature):
                kind = key.lower()
                break
        if head.startswith(b"%SEM"):
            kind = "oasis"
        return VestiOpaqueHandler(kind, reason if reason is not None else (
            "not a GDS2 stream" if head else "empty file"))

    @_synchronized
    def storage_for_manifest(self, root):
        from .contract import VESTI_LEGACY_PROFILE
        from ..storage.errors import StorageError
        if not isinstance(root, dict) or root.get("format") != 2:
            raise StorageError("Unsupported manifest; use the matching Vestigraph version.")
        pair = (root.get("profile"), root.get("normalized_algorithm"))
        if any(not isinstance(value, str) for value in pair):
            raise StorageError("Invalid persisted format profile")
        if pair == (VESTI_LEGACY_PROFILE, "raw"):
            from .opaque import VestiOpaqueHandler
            return VestiOpaqueHandler()
        key = self._decoders.get(pair)
        if key is None:
            raise StorageError("Unsupported persisted format profile; install the matching format reader.")
        return self._create_storage(key)

    @_synchronized
    def register_reader(self, format_id: str, capability: str, factory: Callable):
        """Explicit optional semantic reader; never import a parser just to store bytes."""
        self._check_mutable()
        spec = self.get(format_id)
        if capability not in ("changes", "preview", "compare", "thumbnail") or not callable(factory):
            raise ValueError("Invalid format capability.")
        key = (spec.format_id, capability)
        if key in self._readers:
            raise ValueError("Reader already registered.")
        self._readers[key] = factory

    @_synchronized
    def reader_formats(self, capability):
        return tuple(sorted(fmt for fmt, cap in self._readers if cap == capability))

    @_synchronized
    def has_reader(self, format_id, capability):
        try:
            key = self.get(format_id).format_id
        except ValueError:
            return False
        return (key, capability) in self._readers

    @_synchronized
    def reader(self, format_id, capability):
        if not self.has_reader(format_id, capability):
            return None
        factory = self._readers.get((self.get(format_id).format_id, capability))
        return factory() if factory else None

    @_synchronized
    def describe(self):
        return [{"format_id": spec.format_id, "extensions": list(spec.extensions),
                 "artifact_role": spec.artifact_role, "byte_restore": True,
                 "native_project_complete": False,
                 "structured_changes": spec.structured_changes,
                 "readers": [cap for (fmt, cap) in self._readers if fmt == spec.format_id]}
                for spec in self._formats.values()]


from .defaults import vesti_default_formats

# Default convenience registry for legacy application composition. Repository takes a frozen copy.
FORMATS = vesti_default_formats()


def filename_suffix(format_id, stored_name, *, formats=None):
    """Canonical registered suffix, with the historical unknown-file fallback."""
    formats = formats if formats is not None else FORMATS.copy(frozen=True)
    from pathlib import Path
    try:
        return formats.get(format_id).extensions[0]
    except ValueError:
        own = Path(stored_name or "").suffix.lower()
        return own if any(own in spec["extensions"] for spec in formats.describe()) else ".gds"


def select_export_format(capabilities, preferred=None, *, formats=None):
    formats = formats if formats is not None else FORMATS.copy(frozen=True)
    choices = []
    for value in capabilities.export_formats:
        try:
            choices.append(formats.get(value))
        except ValueError:
            continue
    if preferred:
        try:
            desired = formats.get(preferred)
            if desired in choices:
                return desired
        except ValueError:
            pass
    if not choices:
        raise ValueError("No registered export format supported by this backend.")
    return choices[0]
