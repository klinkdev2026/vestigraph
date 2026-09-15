"""Backend selection + capability reporting for the Rust scan backend.

vestigraph_scan_core is a base install dependency. These tests still force it absent to cover
the safe auto fallback and explicit-rust error path, and include smoke tests that run only when
the installed module is importable in this interpreter.
"""
from __future__ import annotations

import sys

import pytest

from vestigraph.vesti_formats.vesti_format_gds import scan as gds_scan
from vestigraph.vesti_formats.vesti_format_gds import native as scan_backend
from vestigraph.storage import engine

RUST_MODULE_NAME = scan_backend.RUST_MODULE_NAME


def _force_rust_absent(monkeypatch):
    """Make `import vestigraph_scan_core` raise ImportError regardless of whether it is
    actually installed on sys.path, by placing the documented `None` sentinel in sys.modules
    (see https://docs.python.org/3/reference/import.html#the-module-cache)."""
    monkeypatch.setitem(sys.modules, RUST_MODULE_NAME, None)



def test_storage_default_preference_is_auto(monkeypatch):
    monkeypatch.delenv(engine.SCAN_BACKEND_ENV, raising=False)
    assert engine.scan_backend_preference() == "auto"


# --------------------------------------------------------------- select_backend --
def test_select_backend_python_always_available():
    backend, module, reason = scan_backend.select_backend("python")
    assert backend == "python"
    assert module is gds_scan
    assert "explicit python backend selected" in reason


def test_select_backend_rejects_unknown_preference():
    with pytest.raises(ValueError):
        scan_backend.select_backend("fast")


def test_select_backend_auto_falls_back_to_python_when_rust_absent(monkeypatch):
    _force_rust_absent(monkeypatch)
    backend, module, reason = scan_backend.select_backend("auto")
    assert backend == "python"
    assert module is gds_scan
    assert "falling back to python" in reason
    assert RUST_MODULE_NAME in reason  # names what's missing, not just "unavailable"


def test_select_backend_explicit_rust_raises_when_absent(monkeypatch):
    _force_rust_absent(monkeypatch)
    with pytest.raises(RuntimeError, match=RUST_MODULE_NAME):
        scan_backend.select_backend("rust")


# ------------------------------------------------------------------ capability --
def test_capability_reports_unavailable_when_rust_absent(monkeypatch):
    _force_rust_absent(monkeypatch)
    cap = scan_backend.capability()
    assert cap == {
        "available": False,
        "backend_id": None,
        "version": None,
        "reason": cap["reason"],  # exact wording asserted below
    }
    assert RUST_MODULE_NAME in cap["reason"]


def test_capability_never_raises_when_rust_absent(monkeypatch):
    _force_rust_absent(monkeypatch)
    # capability() must be a safe status probe, not something a caller has to wrap in try/except.
    scan_backend.capability()


# ------------------------------------------------------------- scan_with_rust --
def test_scan_with_rust_raises_clear_error_when_rust_absent(monkeypatch):
    _force_rust_absent(monkeypatch)

    class _Sink:
        def chunk(self, *a, **k):
            raise AssertionError("sink must not be touched when the backend is unavailable")

        def references(self, *a, **k):
            raise AssertionError("sink must not be touched when the backend is unavailable")

        def segment(self, *a, **k):
            raise AssertionError("sink must not be touched when the backend is unavailable")

    import io

    with pytest.raises(RuntimeError, match=RUST_MODULE_NAME):
        scan_backend.scan_with_rust(io.BytesIO(b"anything"), _Sink())


# --------------------------------------------------------- import smoke (real) --
HAVE_RUST = scan_backend.capability()["available"]
needs_rust = pytest.mark.skipif(not HAVE_RUST, reason="vestigraph_scan_core not installed")


@needs_rust
def test_rust_module_import_smoke():
    import vestigraph_scan_core as rust
    from tests import gds_fixtures as fx

    assert isinstance(rust.version(), str) and rust.version()
    assert rust.BACKEND_ID.startswith("scan_core/")
    assert issubclass(rust.GdsStructureError, Exception)
    assert issubclass(rust.ScanCancelled, Exception)
    scanner = rust.Scanner(gds_scan.CDC_MIN, gds_scan.CDC_MAX, gds_scan.CDC_MASK,
                           gds_scan.REFS_MAX_DISTINCT, gds_scan.REFS_MAX_BYTES, gds_scan.TARGET_BATCH)
    events = scanner.feed(fx.sample_hierarchy())
    assert isinstance(events, list) and all(e["t"] in ("chunk", "refs", "segment") for e in events)
    result = scanner.finish()
    assert result["cells"] == 3 and result["segments"] == 5 and len(result["events"]) >= 1
    # arbitrary bytes are a strict-framing error, same category/offset as the Python reference
    bad = rust.Scanner(gds_scan.CDC_MIN, gds_scan.CDC_MAX, gds_scan.CDC_MASK,
                       gds_scan.REFS_MAX_DISTINCT, gds_scan.REFS_MAX_BYTES, gds_scan.TARGET_BATCH)
    junk = b"whatever bytes, this is not a real GDS stream"
    with pytest.raises(rust.GdsStructureError) as exc:
        bad.feed(junk)
        bad.finish()
    from tests.scan_transcript import python_transcript
    assert tuple(exc.value.args[:2]) == python_transcript(junk)["error"]      # same category and offset as Python


@needs_rust
def test_rust_scanner_cancel_before_feed_raises_scan_cancelled():
    import vestigraph_scan_core as rust

    scanner = rust.Scanner(gds_scan.CDC_MIN, gds_scan.CDC_MAX, gds_scan.CDC_MASK,
                           gds_scan.REFS_MAX_DISTINCT, gds_scan.REFS_MAX_BYTES, gds_scan.TARGET_BATCH)
    scanner.cancel()
    with pytest.raises(rust.ScanCancelled):
        scanner.feed(b"data fed after cancel")


@needs_rust
def test_scan_with_rust_translates_events_and_result_shape():
    import io
    from tests import gds_fixtures as fx

    class _RecordingSink:
        def __init__(self):
            self.chunks, self.refs, self.segments = [], [], []
        def chunk(self, digest, data, name=None, offset=None): self.chunks.append((digest, data, name, offset))
        def references(self, targets): self.refs.append(list(targets))
        def segment(self, entry, chunks, timestamp): self.segments.append((entry, chunks, timestamp))

    sink = _RecordingSink()
    diagnostics: dict = {}
    result = scan_backend.scan_with_rust(io.BytesIO(fx.sample_hierarchy()), sink, diagnostics=diagnostics)
    assert isinstance(result, gds_scan.ScanResult) and result.cells == 3 and result.segments == 5
    assert len(sink.segments) == 5 and len(sink.chunks) == 5 and diagnostics["feed_calls"] >= 1
    from tests.scan_transcript import python_transcript
    with pytest.raises(gds_scan.GdsStructureError) as exc:
        scan_backend.scan_with_rust(io.BytesIO(b"x" * 1000), _RecordingSink())
    assert (exc.value.reason, exc.value.offset) == python_transcript(b"x" * 1000)["error"]


@needs_rust
def test_scan_with_rust_honours_buffer_override():
    """`buffer=` controls the stream.read() block size (default gds_scan.BUFFER, 4 MiB) --
    the knob tests/scan_transcript.py's parity harness uses for different slice sizes."""
    import io
    from tests import gds_fixtures as fx

    class _Sink:
        def chunk(self, *a, **k): pass
        def references(self, *a, **k): pass
        def segment(self, *a, **k): pass

    data = fx.sample_many_cells(50)
    small_diag: dict = {}
    scan_backend.scan_with_rust(io.BytesIO(data), _Sink(), diagnostics=small_diag, buffer=16)
    default_diag: dict = {}
    scan_backend.scan_with_rust(io.BytesIO(data), _Sink(), diagnostics=default_diag)
    assert small_diag["blocks_read"] == len(data) // 16 + (1 if len(data) % 16 else 0) + 1
    assert default_diag["blocks_read"] == 2 and small_diag["bytes_read"] == default_diag["bytes_read"] == len(data)
