"""Bound actual native work/retained input, not merely the returned Python list."""
import hashlib
import io
import sys
import threading
from types import SimpleNamespace

import pytest

from tests import gds_fixtures as fx
from tests.scan_transcript import first_difference, python_transcript, rust_transcript, TranscriptSink
from vestigraph.vesti_formats.vesti_format_gds import scan as gds_scan
from vestigraph.vesti_formats.vesti_format_gds import native as scan_backend

needs_rust = pytest.mark.skipif(not scan_backend.capability()["available"], reason="bounded Rust backend not built")


def scanner():
    import vestigraph_scan_core as rust
    return rust.Scanner(gds_scan.CDC_MIN, gds_scan.CDC_MAX, gds_scan.CDC_MASK,
                        gds_scan.REFS_MAX_DISTINCT, gds_scan.REFS_MAX_BYTES, gds_scan.TARGET_BATCH)


@pytest.fixture(scope="module")
def dense():
    return fx.library([fx.cell("C%d" % i, b"") for i in range(10000)])


def test_old_extension_requires_rebuild_not_silent_unbounded_use(monkeypatch):
    monkeypatch.setitem(sys.modules, scan_backend.RUST_MODULE_NAME, SimpleNamespace())
    assert not scan_backend.capability()["available"]
    assert "rebuilding" in scan_backend.capability()["reason"]
    assert scan_backend.select_backend("auto")[0] == "python"
    with pytest.raises(RuntimeError, match="bounded-input API"):
        scan_backend.select_backend("rust")


@needs_rust
def test_native_pauses_parsing_and_retains_only_one_input(dense):
    import vestigraph_scan_core as rust
    s = scanner()
    events = s.feed(dense)
    assert len(events) <= rust.MAX_BATCH_EVENTS
    assert s.stats()[0] < 30000  # old backend framed every record before returning
    assert s.stats()[1] <= rust.MAX_BATCH_EVENTS
    assert 0 < s.buffered_input_bytes() < len(dense)
    assert s.needs_drain()
    cells = sum(e["t"] == "segment" and e["entry"]["kind"] == "cell" for e in events)
    previous = s.buffered_input_bytes()
    calls = 0
    while s.needs_drain():
        events = s.feed(b"")
        calls += 1
        assert len(events) <= rust.MAX_BATCH_EVENTS
        assert s.stats()[1] <= rust.MAX_BATCH_EVENTS
        assert s.buffered_input_bytes() <= previous
        previous = s.buffered_input_bytes()
        cells += sum(e["t"] == "segment" and e["entry"]["kind"] == "cell" for e in events)
    result = s.finish()
    assert calls > 1 and cells == 10000
    assert result["cells"] == 10000
    assert result["size"] == len(dense)
    assert result["raw_sha256"] == hashlib.sha256(dense).hexdigest()
    assert s.buffered_input_bytes() == 0


@needs_rust
def test_rejected_feed_or_finish_does_not_lose_pending_input(dense):
    s = scanner()
    s.feed(dense)
    with pytest.raises(RuntimeError, match="drain pending"):
        s.feed(b"must not be hashed")
    with pytest.raises(RuntimeError, match="drain pending"):
        s.finish()
    while s.needs_drain():
        s.feed(b"")
    result = s.finish()
    assert result["raw_sha256"] == hashlib.sha256(dense).hexdigest()
    with pytest.raises(RuntimeError, match="finish"):
        s.feed(b"")


@needs_rust
@pytest.mark.parametrize("buffer", [4 * 1024 * 1024, 65537, 21001])
def test_dense_parity_across_output_pauses_and_partial_records(dense, buffer):
    actual = rust_transcript(dense, buffer)
    assert actual["diagnostics"]["drain_calls"] > 0
    assert first_difference(python_transcript(dense), actual) is None


@needs_rust
@pytest.mark.parametrize("suffix", [b"\x00\x03\x07\x00", b"\x00", b""])
def test_error_offsets_after_multiple_output_pauses(dense, suffix):
    data = dense[:-4] + suffix  # replace ENDLIB with malformed/truncated/end-of-file
    expected, actual = python_transcript(data), rust_transcript(data)
    assert expected["error"] is not None
    assert actual["diagnostics"]["drain_calls"] > 1
    assert first_difference(expected, actual) is None


@needs_rust
def test_native_rejects_oversized_input_before_consuming_it(dense):
    import vestigraph_scan_core as rust
    s = scanner()
    with pytest.raises(ValueError, match="MAX_INPUT_BYTES"):
        s.feed(bytes(rust.MAX_INPUT_BYTES + 1))
    assert s.stats() == (0, 0, 0) and not s.needs_drain()
    s.feed(fx.library([]))
    assert s.finish()["cells"] == 0


@needs_rust
def test_adapter_caps_reads_and_preserves_trailer(monkeypatch):
    import vestigraph_scan_core as rust
    # Small chunks force output pauses even while scanning opaque trailer bytes.
    monkeypatch.setattr(gds_scan, "CDC_MIN", 4096)
    monkeypatch.setattr(gds_scan, "CDC_MAX", 4096)
    data = fx.library([]) + b"trailer!" * (rust.MAX_INPUT_BYTES // 4)

    class BoundedRead(io.BytesIO):
        def read(self, size=-1):
            assert 0 < size <= rust.MAX_INPUT_BYTES
            return super().read(size)

    sink = TranscriptSink()
    diag = {}
    result = scan_backend.scan_with_rust(BoundedRead(data), sink, buffer=len(data) * 2, diagnostics=diag)
    assert diag["drain_calls"] > 0
    assert result.raw_sha256 == hashlib.sha256(data).hexdigest()
    assert sink.events == python_transcript(data)["events"]


@needs_rust
def test_adapter_checks_cancellation_between_drains(dense):
    import vestigraph_scan_core as rust
    cancel = threading.Event()

    class CancellingSink(TranscriptSink):
        def segment(self, *args):
            super().segment(*args)
            cancel.set()

    diag = {}
    with pytest.raises(rust.ScanCancelled):
        scan_backend.scan_with_rust(io.BytesIO(dense), CancellingSink(), cancel=cancel, diagnostics=diag)
    assert diag["feed_calls"] == 1
    assert diag["events_emitted"] <= rust.MAX_BATCH_EVENTS


@needs_rust
def test_oversized_refs_are_split_without_losing_targets_or_later_events():
    # Byte-capped Refs can be split sooner than the Python reference's count-only
    # batching. All ordered targets and non-Refs events must remain identical.
    ref = fx.record(fx.SNAME, fx.DT_ASCII, b"R" * 32000)
    data = fx.library([fx.cell("TOP", ref * 300)])
    py, native = python_transcript(data), rust_transcript(data)
    assert py["error"] is native["error"] is None
    assert py["result"] == native["result"]
    assert [e for e in py["events"] if e[0] != "refs"] == [e for e in native["events"] if e[0] != "refs"]
    assert [t for e in py["events"] if e[0] == "refs" for t in e[1]] == [
        t for e in native["events"] if e[0] == "refs" for t in e[1]]
    assert all(sum(map(len, e[1])) <= 8 * 1024 * 1024 for e in native["events"] if e[0] == "refs")


@needs_rust
@pytest.mark.parametrize("buffer", [0, -1, True, 1.5])
def test_invalid_adapter_buffer_rejected(buffer):
    with pytest.raises(ValueError, match="positive integer"):
        scan_backend.scan_with_rust(io.BytesIO(b""), TranscriptSink(), buffer=buffer)
