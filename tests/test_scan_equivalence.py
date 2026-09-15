"""Python vs Rust scanner equivalence on the fixture matrix and random slicing (spec P1 3.3/3.4).
Skipped entirely when the installed native module is not importable."""
import random

import pytest

from tests import gds_fixtures as fx
from tests.scan_transcript import python_transcript, rust_transcript, first_difference
from vestigraph.vesti_formats.vesti_format_gds import native as scan_backend

pytestmark = pytest.mark.skipif(not scan_backend.capability()["available"], reason="vestigraph_scan_core not installed")

TS = fx.timestamps(second=1)
_wide_targets = ["T%05d" % i for i in range(1100)]
SAMPLES = {
    'long_sname_refs_trim_empty': lambda: fx.library(
        [fx.cell('TOP', fx.sref(b'T' * 25000), stamp=TS)], stamp=TS),
    "hierarchy": lambda: fx.sample_hierarchy(TS),
    "many_cells": lambda: fx.sample_many_cells(300, stamp=TS),
    "big_cell": lambda: fx.sample_big_cell(2 * 2 ** 20, stamp=TS),
    "no_anchor": lambda: fx.sample_no_anchor_big_cell(2 * 2 ** 20),
    "fake_bgnstr": lambda: fx.sample_fake_bgnstr(TS),
    "odd_timestamps": fx.sample_odd_timestamps,
    "empty_library": lambda: fx.sample_empty_library(TS),
    "empty_cell": lambda: fx.sample_empty_cell(TS),
    "multi_top": lambda: fx.sample_multi_top(TS),
    "unknown_record": lambda: fx.sample_unknown_record(TS),
    "trailer": lambda: fx.sample_trailer(TS),
    "duplicate_names": lambda: fx.sample_duplicate_names(TS),
    "bgnstr_wrong_length": fx.sample_bgnstr_wrong_length,
    "utf8_name": fx.sample_utf8_name,
    "wide_refs_1100": lambda: fx.library(
        [fx.cell(t, fx.boundary(1, 0, [(0, 0), (1, 0), (1, 1), (0, 1)]), stamp=TS) for t in _wide_targets]
        + [fx.cell("TOP", b"".join(fx.sref(t) for t in _wide_targets), stamp=TS)], stamp=TS),
    "bad_order": fx.sample_bad_order,
    "truncated": lambda: fx.sample_truncated(fx.sample_hierarchy(TS), 200),
    "truncated_no_endlib": lambda: fx.sample_truncated(fx.sample_hierarchy(TS), len(fx.sample_hierarchy(TS)) - 4),
    "odd_length": fx.sample_odd_length,
    "zero_length": fx.sample_zero_length,
    "sname_outside": lambda: (lambda d: d[:d.index(b"\x00\x1c\x05\x02")] + fx.sref("UNIT") + d[d.index(b"\x00\x1c\x05\x02"):])(fx.sample_hierarchy(TS)),
}



def _flatten_refs(events):
    out = []
    for event in events:
        if event[0] == "refs":
            out.extend(event[1])
    return tuple(out)


def _without_refs(events):
    return tuple(event for event in events if event[0] != "refs")

@pytest.mark.parametrize("name", sorted(SAMPLES))
@pytest.mark.parametrize("buffer", [4 * 2 ** 20, 65537, 4096, 3])
def test_rust_matches_python_transcript(name, buffer):
    data = SAMPLES[name]()
    py = python_transcript(data, 4 * 2 ** 20)
    diff = first_difference(py, rust_transcript(data, buffer))
    assert diff is None, diff



def test_long_sname_refs_split_into_identical_batches_on_both_scanners():
    """Spec §3.3 asks for identical transcripts, batch boundaries included. The Python
    reference now splits reference batches by count AND payload bytes exactly like scan_core,
    so this fixture (129 x 64 KiB SNAME targets, well over one 8 MiB batch) is compared
    event for event -- not "same multiset in some batching", which let the two diverge."""
    module = scan_backend.select_backend("rust")[1]
    payload = b"R" * 65_530
    data = fx.library([fx.cell("TOP", b"".join(fx.sref(payload) for _ in range(129)), stamp=TS)], stamp=TS)

    py = python_transcript(data, 4 * 2 ** 20)
    for buffer in (4 * 2 ** 20, 65537, 3):
        rust = rust_transcript(data, buffer)
        diff = first_difference(py, rust)
        assert diff is None, (buffer, diff)

    ref_payloads = [sum(len(t) for t in event[1]) for event in py["events"] if event[0] == "refs"]
    assert len(ref_payloads) > 1
    assert all(size <= module.MAX_BATCH_PAYLOAD_BYTES for size in ref_payloads)
    assert _flatten_refs(py["events"]) == tuple([payload] * 129)
def test_random_slicing_and_random_bytes_agree():
    rng = random.Random(1234)
    base = fx.sample_many_cells(80, stamp=TS)
    for trial in range(30):
        data = bytearray(base)
        for _ in range(rng.randrange(0, 4)):                       # random corruption, sometimes none
            data[rng.randrange(len(data))] = rng.randrange(256)
        data = bytes(data)
        py = python_transcript(data, 4 * 2 ** 20)
        buffer = rng.choice([1, 2, 7, 64, 1000, 8191])
        diff = first_difference(py, rust_transcript(data, buffer))
        assert diff is None, (trial, buffer, diff)


def test_rust_cancel_is_honoured_mid_scan():
    import threading
    from vestigraph.vesti_formats.vesti_format_gds import scan as gds_scan
    module = scan_backend.select_backend("rust")[1]
    data = fx.sample_big_cell(4 * 2 ** 20, stamp=TS)
    cancel = threading.Event()
    cancel.set()

    class Sink:
        def chunk(self, *a): pass
        def references(self, *a): pass
        def segment(self, *a): pass
    import io
    with pytest.raises(module.ScanCancelled):
        scan_backend.scan_with_rust(io.BytesIO(data), Sink(), cancel=cancel)


def test_backend_diagnostics_are_reported():
    import io
    data = fx.sample_many_cells(200, stamp=TS)

    class Sink:
        def chunk(self, *a): pass
        def references(self, *a): pass
        def segment(self, *a): pass
    diag = {}
    scan_backend.scan_with_rust(io.BytesIO(data), Sink(), diagnostics=diag, buffer=65536)
    assert diag["feed_calls"] >= 2 and diag["bytes_copied_ffi"] == len(data) and diag["events_emitted"] > 200


def test_reference_byte_budget_resets_between_cells():
    payload = b"R" * 65530
    data = fx.library([fx.cell("A", fx.sref(payload)*100, stamp=TS),
                       fx.cell("B", fx.sref(payload)*40, stamp=TS)], stamp=TS)
    reference = python_transcript(data, 4*2**20)
    for buffer in (65537, 4096):
        assert first_difference(reference, rust_transcript(data, buffer)) is None
    assert [len(e[1]) for e in reference["events"] if e[0] == "refs"] == [100, 40]
