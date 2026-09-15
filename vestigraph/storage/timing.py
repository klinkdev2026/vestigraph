"""Window-scoped coordinator timing, progress and cooperative cancellation."""
import logging
from functools import wraps
import time

from .errors import CancelledError

TIMING = True   # disable itemized clocks only; progress, adaptive cost and total wall clocks remain


def _item_clock():
    return time.perf_counter() if TIMING else 0.0


def _timed_sink(method):
    """Count the union of sink calls, not nested segment -> put -> flush twice."""
    @wraps(method)
    def timed(self, *args, **kwargs):
        if not TIMING:
            return method(self, *args, **kwargs)
        monitor = self.monitor
        outer = monitor.sink_depth == 0
        started = _item_clock() if outer else 0.0
        monitor.sink_depth += 1
        try:
            return method(self, *args, **kwargs)
        finally:
            monitor.sink_depth -= 1
            if outer:
                monitor.sink_wall_ms += (_item_clock() - started) * 1000
    return timed

# Exclusive timing categories (spec P0). Inclusive roll-ups are computed at the end and named *_inclusive.
TIMING_KEYS = ("source_read_ms", "scanner_compute_ms", "sink_other_ms", "dedupe_query_ms", "base_decode_ms",
               "full_compress_ms", "delta_encode_ms", "delta_verify_ms", "pack_write_ms", "changes_ms",
               "pack_fsync_ms", "load_previous_ms", "unattributed_ms")


class _Progress:
    """Cooperative cancel + rate-limited progress for one prepare (checked at chunk batches),
    plus the exclusive timing accumulators."""

    def __init__(self, progress=None, cancel=None, total=0):
        self.progress = progress
        self.cancel = cancel
        self.total = total
        self.phase = "scanning"
        self.bytes = 0
        self._last = 0.0
        self.timing = {k: 0.0 for k in TIMING_KEYS}
        self.sink_wall_ms = 0.0          # inclusive time inside sink calls (chunk/segment/references/put)
        self.scan_sink_wall_ms = 0.0     # retained scan-window sink snapshot
        self.scan_wall_ms = 0.0          # inclusive time of the scan phase (read + compute + sink)
        self.changes_wall_ms = 0.0
        self.sink_depth = 0
        self.scan_windows_closed = 0

    def window_start(self):
        return (_item_clock(), self.sink_wall_ms, self.timing["source_read_ms"])

    def window_close(self, kind, mark):
        elapsed = (_item_clock() - mark[0]) * 1000
        sink_delta = self.sink_wall_ms - mark[1]
        read_delta = self.timing["source_read_ms"] - mark[2]
        if kind == "scan":
            self.scan_windows_closed += 1
            self.scan_sink_wall_ms = self.sink_wall_ms
            self.scan_wall_ms += elapsed
            self.timing["scanner_compute_ms"] += elapsed - sink_delta - read_delta
        elif kind == "changes":
            self.changes_wall_ms += elapsed
            self.timing["changes_ms"] += elapsed - sink_delta - read_delta
        else:
            raise ValueError("unknown timing window")

    def check(self):
        if self.cancel is not None and self.cancel.is_set():
            raise CancelledError("Save cancelled by the user; nothing was published.")

    def report(self, force=False, **fields):
        self.check()
        if self.progress is None:
            return
        now = time.monotonic()
        if not force and now - self._last < 0.2:
            return
        self._last = now
        info = {"phase": self.phase, "bytes": self.bytes, "total": self.total,
                "fraction": (self.bytes / self.total) if self.total else None}
        info.update(fields)
        try:
            self.progress(info)
        except Exception:  # noqa: BLE001 - a listener must never break a save
            logging.getLogger(__name__).warning("Nonfatal callback or cleanup failure", exc_info=True)

class _TimedStream:
    """Times the source reads (one clock per 4 MiB read, never per record)."""

    def __init__(self, stream, monitor):
        self._stream, self._monitor = stream, monitor

    def read(self, n=-1):
        t0 = _item_clock()
        data = self._stream.read(n)
        self._monitor.timing["source_read_ms"] += (_item_clock() - t0) * 1000
        return data

    def seek(self, *args):
        return self._stream.seek(*args)

    def fileno(self):
        return self._stream.fileno()


def _finish_timing(monitor, total_ms):
    """Window-scoped attribution. Signed residuals expose accounting bugs."""
    t = dict(monitor.timing)
    # Also accept a diagnostic monitor populated with already-closed raw snapshots.
    # Production derives the category at window_close, never from all-time sink work.
    if TIMING and not monitor.scan_windows_closed:
        t["scanner_compute_ms"] = (monitor.scan_wall_ms - t["source_read_ms"]
                                   - monitor.scan_sink_wall_ms)
    sink_sub = sum(t[k] for k in ("dedupe_query_ms", "base_decode_ms", "full_compress_ms",
                                 "delta_encode_ms", "delta_verify_ms", "pack_write_ms"))
    t["sink_other_ms"] = monitor.sink_wall_ms - sink_sub if TIMING else 0.0
    exclusive = sum(t[k] for k in TIMING_KEYS if k != "unattributed_ms")
    t["unattributed_ms"] = total_ms - exclusive
    out = {k: round(v, 1) for k, v in t.items()}
    out["prepare_total_ms"] = round(total_ms, 1)
    out["scan_inclusive_ms"] = round(monitor.scan_wall_ms, 1)
    out["sink_inclusive_ms"] = round(monitor.sink_wall_ms, 1)
    out["changes_inclusive_ms"] = round(monitor.changes_wall_ms, 1)
    out["instrumented"] = TIMING
    out["accounting"] = {"version": "window-scoped-v2",
                         "negative_categories": {k: v for k, v in t.items() if v < -0.01},
                         "balanced": all(v >= -0.01 for v in t.values())}
    out["semantics"] = ("TIMING_KEYS are exclusive coordinator wall-time categories; their signed residual "
                        "sums to prepare_total_ms before rounding. *_inclusive_ms overlap and must not be added; "
                        "publish_ms/db_commit_ms are outside prepare. Parallel compression is coordinator wait, "
                        "not total worker CPU time. instrumented=false disables item clocks, not operational clocks.")
    return out
