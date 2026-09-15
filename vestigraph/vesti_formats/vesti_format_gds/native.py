"""Backend selection + Rust adapter for GDS scanning (SPEC_PERFORMANCE_READ_ACCESS.md P1 3.1/3.2).

The pure-Python reference scanner (`vestigraph.vesti_formats.vesti_format_gds.scan.scan_gds`) stays the default
and the fallback; this module adds an explicit python/rust/auto backend choice plus a
capability probe, and implements the Rust session-API feed loop against
`vestigraph_scan_core.Scanner` (native crate under `native/scan_core/`, not a runtime
dependency of this package -- see `native/scan_core/README.md`). The engine's prepare path
uses this adapter when native scanning is explicitly selected (or selected through auto).

Backend selection:

    backend, module, reason = select_backend("auto")   # or "python" / "rust"

`"python"` always returns the `vestigraph.vesti_formats.vesti_format_gds.scan` module itself (no native
dependency). `"rust"` requires `vestigraph_scan_core` to import cleanly and raises a clear
`RuntimeError` naming the failure otherwise. `"auto"` prefers rust, and falls back to python
with `reason` explaining why whenever rust is not importable -- never the other way around
(silently downgrading `"rust"` to `"python"` on request would hide a real breakage).

`capability()` reports `{"available": bool, "backend_id": str | None, "version": str | None,
"reason": str}` without raising, for status/diagnostics endpoints.

`scan_with_rust(stream, sink, cancel=None, diagnostics=None, buffer=None)` drives the actual
scan: reads `buffer`-sized blocks from `stream` (default `gds_scan.BUFFER`, 4 MiB, same as the
Python reference; overridable for parity testing at other slice sizes -- see the function's own
docstring), feeds them to a `Scanner` constructed with the SAME `CDC_MIN` / `CDC_MAX` /
`CDC_MASK` / `REFS_MAX_DISTINCT` / `REFS_MAX_BYTES` / `TARGET_BATCH` constants `gds_scan.py`
uses (imported, not duplicated, so the two backends can never silently drift onto different
parameters), translates each returned event onto `sink`
exactly like `gds_scan.scan_gds` would (`sink.chunk` / `sink.references` / `sink.segment`), and
returns a `gds_scan.ScanResult`. A `vestigraph_scan_core.GdsStructureError` raised by the
native scanner is re-raised as `gds_scan.GdsStructureError(reason, offset)` so callers can
catch one exception type regardless of which backend ran (SPEC_PERFORMANCE_READ_ACCESS.md P1
3.3: "同类格式错误返回稳定错误类别与字节位置... 走同一 opaque 回退"). A
`vestigraph_scan_core.ScanCancelled` is left to propagate as-is -- callers that want it mapped
onto `engine.CancelledError` (the convention `engine._Progress.check()` uses) do that mapping
themselves; this module does not import from `engine.py`.

`cancel`, when given, follows the same duck-typed protocol as `engine._Progress`'s own `cancel`
parameter: any object with `.is_set()` (a `threading.Event` in practice). It is checked before
each new block and each bounded drain. The adapter forwards cancellation on those boundaries;
it does not poll the Python event while a native call is in flight. Direct native callers can
call Scanner.cancel() from another thread for between-record cancellation inside a feed.

`diagnostics`, when given a dict, is updated in place with FFI/batch bookkeeping
(`feed_calls`, `drain_calls`, `blocks_read`, `bytes_read`, `bytes_copied_ffi`,
`events_emitted`, `max_batch_events`) per SPEC_PERFORMANCE_READ_ACCESS.md P1 3.2's "批次数、
FFI 次数、复制字节作为诊断".
"""
from __future__ import annotations

from typing import Any

from . import scan as gds_scan

RUST_MODULE_NAME = "vestigraph_scan_core"


def _import_rust():
    """Return (module, reason). module is None (with reason explaining why) when the optional
    native backend is not installed or fails to import for any other reason."""
    try:
        import vestigraph_scan_core as module  # noqa: PLC0415 -- optional, imported lazily
    except ImportError as exc:
        return None, f"{RUST_MODULE_NAME} not importable: {exc}"
    except Exception as exc:  # pragma: no cover - a broken native build, not a missing one
        return None, f"{RUST_MODULE_NAME} import raised {type(exc).__name__}: {exc}"
    if (getattr(module, "BOUNDED_INPUT_API", None) != 1
            or not callable(getattr(getattr(module, "Scanner", None), "needs_drain", None))):
        return None, f"{RUST_MODULE_NAME} needs rebuilding: bounded-input API 1 is required"
    return module, "ok"


def select_backend(preference: str = "auto"):
    """Resolve a backend preference ("python" | "rust" | "auto") to
    (backend: str, module: object | None, reason: str).

    "python"  -> ("python", gds_scan, "explicit python backend selected"); always succeeds.
    "rust"    -> ("rust", module, reason) if vestigraph_scan_core imports; otherwise raises
                 RuntimeError naming the import failure -- an explicit request for a backend
                 that is not available is an error, not a silent fallback.
    "auto"    -> prefers rust when importable; otherwise falls back to python and `reason`
                 explains why (never raises).
    """
    if preference not in ("python", "rust", "auto"):
        raise ValueError(f"unknown scan backend preference {preference!r}; expected 'python', 'rust', or 'auto'")

    if preference == "python":
        return "python", gds_scan, "explicit python backend selected"

    module, reason = _import_rust()

    if preference == "rust":
        if module is None:
            raise RuntimeError(f"rust scan backend explicitly requested but unavailable: {reason}")
        return "rust", module, reason

    # auto
    if module is not None:
        return "rust", module, reason
    return "python", gds_scan, f"auto: falling back to python backend ({reason})"


def capability() -> dict:
    """Report native-backend availability without raising or scanning anything."""
    module, reason = _import_rust()
    if module is None:
        return {"available": False, "backend_id": None, "version": None, "reason": reason}
    backend_id = getattr(module, "BACKEND_ID", None)
    try:
        version = module.version() if hasattr(module, "version") else None
    except Exception as exc:  # pragma: no cover - version() is not expected to raise
        version = None
        reason = f"{reason}; version() raised {type(exc).__name__}: {exc}"
    return {"available": True, "backend_id": backend_id, "version": version, "reason": reason}


def _apply_events(sink, events: list, diagnostics: dict) -> None:
    diagnostics["events_emitted"] += len(events)
    if len(events) > diagnostics["max_batch_events"]:
        diagnostics["max_batch_events"] = len(events)
    for event in events:
        kind = event["t"]
        if kind == "chunk":
            sink.chunk(event["digest"], event["data"], event.get("name"), event.get("offset"))
        elif kind == "refs":
            sink.references(event["targets"])
        elif kind == "segment":
            sink.segment(event["entry"], event["chunks"], event.get("timestamp"))
        else:
            raise RuntimeError(f"unknown event kind {kind!r} from rust scan backend")


def _to_scan_result(fields: dict) -> gds_scan.ScanResult:
    result = gds_scan.ScanResult()
    for name in result.__slots__:
        if name not in fields:
            raise RuntimeError(
                f"rust scan backend finish() result is missing field {name!r}; "
                f"got keys {sorted(fields)!r}"
            )
        setattr(result, name, fields[name])
    return result


def scan_with_rust(
    stream, sink, cancel=None, diagnostics: dict | None = None, buffer: int | None = None
) -> gds_scan.ScanResult:
    """Scan `stream` with the native backend, replaying every event onto `sink` in order, and
    return a `gds_scan.ScanResult`. See the module docstring for the full contract.

    `buffer` overrides the input block size (default `gds_scan.BUFFER`, 4 MiB, the same size
    the Python reference reads). This exists so the same input can be fed through in different
    slice sizes for parity testing (SPEC_PERFORMANCE_READ_ACCESS.md P1 3.3: "同输入不同 read
    buffer 分割... 完全一致") -- the scanner keeps its own partial-record tail internally, so
    any positive block size must produce byte-identical events regardless of where reads land.

    Raises RuntimeError if vestigraph_scan_core is not importable, gds_scan.GdsStructureError
    on a structural violation (translated from the native module's own exception type), and
    lets vestigraph_scan_core.ScanCancelled propagate as-is on cancellation.
    """
    module, reason = _import_rust()
    if module is None:
        raise RuntimeError(f"rust scan backend unavailable: {reason}")

    read_size = buffer if buffer is not None else gds_scan.BUFFER
    if not isinstance(read_size, int) or isinstance(read_size, bool) or read_size <= 0:
        raise ValueError("buffer must be a positive integer")
    # Requests above the session limit are split into bounded reads; event semantics
    # are independent of read size, including timestamps spanning two input blocks.
    read_size = min(read_size, module.MAX_INPUT_BYTES)

    diag: dict[str, Any] = diagnostics if diagnostics is not None else {}
    diag.setdefault("feed_calls", 0)
    diag.setdefault("drain_calls", 0)
    diag.setdefault("blocks_read", 0)
    diag.setdefault("bytes_read", 0)
    diag.setdefault("bytes_copied_ffi", 0)
    diag.setdefault("events_emitted", 0)
    diag.setdefault("max_batch_events", 0)

    scanner = module.Scanner(
        gds_scan.CDC_MIN,
        gds_scan.CDC_MAX,
        gds_scan.CDC_MASK,
        gds_scan.REFS_MAX_DISTINCT,
        gds_scan.REFS_MAX_BYTES,
        gds_scan.TARGET_BATCH,
    )

    def check_cancel() -> None:
        if cancel is not None and cancel.is_set():
            scanner.cancel()
            scanner.feed(b"")  # native cancellation type, before accepting any input

    def feed_and_drain(data: bytes) -> None:
        check_cancel()
        events = scanner.feed(data)
        diag["feed_calls"] += 1
        diag["bytes_copied_ffi"] += len(data)
        _apply_events(sink, events, diag)
        # A short batch can still leave unread input (or an indivisible next event).
        # Query the actual state instead of inferring it from the returned batch length.
        while scanner.needs_drain():
            check_cancel()
            events = scanner.feed(b"")
            diag["drain_calls"] += 1
            _apply_events(sink, events, diag)

    try:
        while True:
            check_cancel()
            block = stream.read(read_size)
            diag["blocks_read"] += 1
            if not block:
                break
            diag["bytes_read"] += len(block)
            feed_and_drain(block)
        feed_and_drain(b"")  # final drain: nothing new, just flush whatever remains buffered
        check_cancel()
        fields = scanner.finish()
        # The closing segment's events come back with the result and must reach the sink
        # BEFORE the result is reported (the Python reference ends the segment, then returns).
        _apply_events(sink, list(fields.pop("events", [])), diag)
    except module.GdsStructureError as exc:
        # Events the scanner had queued before the failing record travel with the error
        # (scan_core >= 0.1.2): replay them so the sink sees the same prefix the Python
        # reference delivers, then translate. The engine discards the attempt either way; the
        # transcript parity tests compare that prefix.
        _apply_events(sink, list(getattr(exc, "pending_events", None) or []), diag)
        reason_arg, offset_arg = exc.args[0], exc.args[1]
        raise gds_scan.GdsStructureError(reason_arg, offset_arg) from exc

    # `diag` IS `diagnostics` (mutated in place) whenever a dict was passed in; nothing further
    # to copy back.
    return _to_scan_result(fields)
