"""Shared boundary for decoding persisted history data.

Use CORRUPT_DATA_ERRORS only around reads/interpretation of old persisted data,
never around encoders, arbitrary application code, or an entire save. Cancellation
must be re-raised first. RuntimeError/AssertionError/MemoryError are not fallbacks.
"""
import sqlite3


class StorageError(RuntimeError):
    """Expected storage failure with an actionable message."""


class StorageIOError(StorageError):
    """Transient access failure; never treat this as evidence for object repair."""


class IntegrityError(StorageError):
    """An integrity assertion failed; do not publish an unverified version."""


class CancelledError(StorageError):
    """The caller cancelled; no version is published."""


class DeltaBaseUnavailable(StorageError):
    """Old optional compression input is unreadable; retry with verified full objects."""


# PackError aliases VestiCodecError, which derives from ValueError, as do
# MetadataError, JSONDecodeError and UnicodeError.
# LookupError covers corrupt indices and missing keys; TypeError covers bad JSON shapes.
CORRUPT_DATA_ERRORS = (StorageError, ValueError, LookupError, TypeError, sqlite3.DatabaseError)
