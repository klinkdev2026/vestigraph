"""The Rust scanner behind the real store: identical manifests, identical restored bytes,
same opaque fallback, cancel honoured, backend reported. Skipped without the installed native module."""
import copy
import os
from unittest.mock import patch

import pytest

from tests import gds_fixtures as fx
from vestigraph.store import Repository, RepositoryError, SaveCancelled
from vestigraph.storage import engine
from vestigraph.vesti_formats.vesti_format_gds import native as scan_backend

pytestmark = pytest.mark.skipif(not scan_backend.capability()["available"], reason="vestigraph_scan_core not installed")
TS_A, TS_B = fx.timestamps(second=1), fx.timestamps(second=2)


def _normalize(manifest):
    m = copy.deepcopy(manifest)
    m["storage"].pop("delta_encoder", None)
    return m


def _history(tmp_path, name, backend):
    repo = Repository.init(tmp_path / name)
    source = tmp_path / f"{name}.gds"
    records = []
    for data in (fx.sample_many_cells(400, stamp=TS_A), fx.sample_many_cells_edit(400, "modify", stamp=TS_B),
                 fx.sample_many_cells_edit(400, "insert", stamp=TS_B), fx.sample_big_cell(2 * 2 ** 20, stamp=TS_A),
                 fx.sample_big_cell_insert(2 * 2 ** 20, insert_after_shape=10000, stamp=TS_A)[1],
                 fx.sample_bad_order(), fx.sample_odd_timestamps()):
        source.write_bytes(data)
        records.append((repo.checkpoint(source, scan_backend=backend), data))
    return repo, records


def test_both_backends_produce_identical_manifests_and_bytes(tmp_path):
    py_repo, py = _history(tmp_path, "py", "python")
    rs_repo, rs = _history(tmp_path, "rs", "rust")
    for (a, data), (b, _) in zip(py, rs):
        if a["manifest"]["format_analysis"]["status"] == "complete":
            assert a["scan"]["backend"] == "python" and b["scan"]["backend"] == "rust"
        else:
            assert a["scan"]["backend"] == b["scan"]["backend"] == "none"      # opaque: no structured scan ran
        assert _normalize(a["manifest"]) == _normalize(b["manifest"])
        assert a["sha256"] == b["sha256"]
        assert rs_repo.export(b["id"], tmp_path / ("rs-%s.gds" % b["id"])).read_bytes() == data
        ca, cb = py_repo.get_changeset(a["id"]), rs_repo.get_changeset(b["id"])
        assert ca["summary"] == cb["summary"] and ca["coverage"]["status"] == cb["coverage"]["status"]
    assert rs[5][0]["manifest"]["format_analysis"]["status"] == "unavailable"       # same opaque fallback
    assert rs[-1][0]["scan"]["feed_calls"] >= 1 and rs[-1][0]["scan"]["bytes_copied_ffi"] == len(rs[-1][1])


def test_explicit_rust_when_missing_is_an_error_and_auto_falls_back(tmp_path):
    repo = Repository.init(tmp_path / "h")
    source = tmp_path / "a.gds"
    source.write_bytes(fx.sample_hierarchy(TS_A))
    with patch.object(scan_backend, "_import_rust", lambda: (None, "simulated missing")):
        with pytest.raises(RepositoryError, match="unavailable"):
            repo.checkpoint(source, scan_backend="rust")
        record = repo.checkpoint(source, scan_backend="auto")
    assert record["scan"]["backend"] == "python" and "falling back" in record["scan"]["reason"]


def test_env_selects_the_backend_and_stats_report_it(tmp_path, monkeypatch):
    repo = Repository.init(tmp_path / "h")
    source = tmp_path / "a.gds"
    source.write_bytes(fx.sample_hierarchy(TS_A))
    monkeypatch.setenv(engine.SCAN_BACKEND_ENV, "rust")
    assert repo.checkpoint(source)["scan"]["backend"] == "rust"
    assert repo.stats()["scan_backend"]["preference"] == "rust" and repo.stats()["scan_backend"]["native"]["available"]
    monkeypatch.setenv(engine.SCAN_BACKEND_ENV, "bogus")
    with pytest.raises(RepositoryError, match="python, rust or auto"):
        repo.checkpoint(source)


def test_cancel_through_the_rust_backend_publishes_nothing(tmp_path):
    import threading
    repo = Repository.init(tmp_path / "h")
    source = tmp_path / "a.gds"
    source.write_bytes(fx.sample_big_cell(3 * 2 ** 20, stamp=TS_A))
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(SaveCancelled):
        repo.prepare(source, cancel=cancel, scan_backend="rust")
    assert repo.history() == [] and not list(repo.tmp_dir.glob("vg2-*"))
    assert repo.checkpoint(source, scan_backend="rust")["scan"]["backend"] == "rust"
