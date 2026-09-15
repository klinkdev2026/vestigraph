"""Stable recording error codes, independent of an editor provider."""
class CaptureError(RuntimeError):
    """Capture failure with a stable ``code`` (state machines must not parse text)."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


NO_DOCUMENT = "NO_DOCUMENT"
DISCONNECTED = "DISCONNECTED"
UNSUPPORTED = "UNSUPPORTED"
CORRUPT_STORE = "CORRUPT_STORE"
EXPORT_FAILED = "EXPORT_FAILED"
DOCUMENT_CHANGED = "DOCUMENT_CHANGED"
OVERFLOW = "OVERFLOW"
NO_BASELINE = "NO_BASELINE"
INVALID_EVENT = "INVALID_EVENT"
DOCUMENT_AMBIGUOUS = "DOCUMENT_AMBIGUOUS"
