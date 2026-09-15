"""Bounded, privacy-safe diagnostics for optional capture images."""
from collections import OrderedDict
import hashlib
import json
import logging
import threading
import time

LOGGER = logging.getLogger("vestigraph.capture.thumbnail")
REASONS = frozenset(("identity_changed", "timeout", "invalid_image", "storage_failed",
                     "unexpected", "unsupported", "cancelled"))
STATES = frozenset(("failed", "unavailable", "cancelled"))


def safe_id(value):
    # Never log paths, names, addresses, provider messages or user-controlled IDs.
    return hashlib.sha256(str(value).encode("utf-8", "replace")).hexdigest()[:16]


class ThumbnailDiagnostics:
    def __init__(self, *, clock=time.monotonic, logger=LOGGER, interval=60, capacity=512, idle=600):
        if interval <= 0 or capacity < 1 or idle < interval:
            raise ValueError("Invalid diagnostic bounds.")
        self.clock, self.logger = clock, logger
        self.interval, self.capacity, self.idle = interval, capacity, idle
        self.entries = OrderedDict()
        self.lock = threading.Lock()

    def report(self, *, backend, session, document, capture, reason, duration_ms=0, exception=None):
        """reason=None is recovery. Logger failures must never change save outcome."""
        if reason is not None and reason not in REASONS:
            reason = "unexpected"
        scope = tuple(safe_id(v) for v in (backend, session, document))
        fields = dict(backend_id=scope[0], session_id=scope[1], document_id=scope[2],
                      capture_id=safe_id(capture), reason_code=reason,
                      duration_ms=round(max(0, duration_ms), 3),
                      exception_type=type(exception).__name__[:80] if exception else None,
                      retryable=reason not in ("unsupported", "cancelled", None))
        messages = []
        with self.lock:
            now = self.clock()
            for key in list(self.entries):
                if now - self.entries[key]["seen"] >= self.idle:
                    del self.entries[key]
            if reason is None:
                for key in list(self.entries):
                    if key[:3] == scope:
                        entry = self.entries.pop(key)
                        messages.append((logging.INFO, dict(fields, reason_code="recovered",
                                         previous_reason=key[3], suppressed_count=entry["suppressed"])))
            else:
                key = (*scope, reason)
                entry = self.entries.get(key)
                if entry is None or now - entry["emitted"] >= self.interval:
                    messages.append((logging.INFO if reason in ("unsupported", "cancelled") else logging.WARNING,
                                     dict(fields, suppressed_count=entry["suppressed"] if entry else 0)))
                    self.entries[key] = dict(emitted=now, seen=now, suppressed=0)
                else:
                    entry["seen"] = now
                    entry["suppressed"] += 1
                self.entries.move_to_end(key)
                while len(self.entries) > self.capacity:
                    self.entries.popitem(last=False)
        for level, payload in messages:
            try:
                self.logger.log(level, "capture_thumbnail " + json.dumps(payload, sort_keys=True),
                                extra={"thumbnail": payload})
            except Exception:
                pass


DIAGNOSTICS = ThumbnailDiagnostics()
