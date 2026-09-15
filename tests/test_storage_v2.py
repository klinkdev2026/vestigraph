"""Storage format 2: strict scan, record-aligned CDC, packs, shared recipes, restore.

Synthetic GDS2 bytes only (tests/gds_fixtures.py); no EDA semantics are certified.
"""
import base64
import hashlib
import os
from pathlib import Path
import sqlite3
from unittest.mock import patch

import pytest

from tests import gds_fixtures as fx
from vestigraph.store import Repository, RepositoryError
from vestigraph.storage import metadata as md
from vestigraph.vesti_formats.vesti_format_gds import scan as gds_scan
from vestigraph.storage.packs import scan_pack, PackError


@pytest.fixture
def repo(tmp_path):
    return Repository.init(tmp_path / "history")


def save(repo, tmp_path, data, name="layout.gds", **kw):
    source = tmp_path / name
    source.write_bytes(data)
    return repo.checkpoint(source, **kw)


def restore(repo, record, tmp_path, suffix=""):
    out = repo.export(record["id"], tmp_path / f"restored-{record['id']}{suffix}.gds")
    return out.read_bytes()


def b64(name: str) -> str:
    return base64.b64encode(fx.string_payload(name)).decode("ascii")


TS_A, TS_B = fx.timestamps(second=1), fx.timestamps(second=2)


# ------------------------------------------------------------- exact bytes --
VALID_SAMPLES = {
    "hierarchy": lambda: fx.sample_hierarchy(TS_A),
    "many_cells": lambda: fx.sample_many_cells(300, stamp=TS_A),
    "big_cell": lambda: fx.sample_big_cell(3 * 2 ** 20, stamp=TS_A),
    "no_anchor": lambda: fx.sample_no_anchor_big_cell(2 * 2 ** 20),
    "fake_bgnstr": lambda: fx.sample_fake_bgnstr(TS_A),
    "odd_timestamps": fx.sample_odd_timestamps,
    "empty_library": lambda: fx.sample_empty_library(TS_A),
    "empty_cell": lambda: fx.sample_empty_cell(TS_A),
    "multi_top": lambda: fx.sample_multi_top(TS_A),
    "unknown_record": lambda: fx.sample_unknown_record(TS_A),
    "trailer": lambda: fx.sample_trailer(TS_A),
    "duplicate_names": lambda: fx.sample_duplicate_names(TS_A),
    "bgnstr_wrong_length": fx.sample_bgnstr_wrong_length,
    "utf8_name": fx.sample_utf8_name,
}


@pytest.mark.parametrize("name", sorted(VALID_SAMPLES))
def test_valid_gds_roundtrips_with_complete_index(repo, tmp_path, name):
    data = VALID_SAMPLES[name]()
    record = save(repo, tmp_path, data)
    manifest = record["manifest"]
    assert manifest["format"] == 2 and manifest["format_analysis"]["status"] == "complete"
    assert record["sha256"] == hashlib.sha256(data).hexdigest()
    assert restore(repo, record, tmp_path) == data
    assert manifest["counts"]["layout_chunk_refs"] >= 1


BROKEN_SAMPLES = {
    "truncated_mid_record": lambda: fx.sample_truncated(fx.sample_hierarchy(TS_A), 200),
    "truncated_no_endlib": lambda: fx.sample_truncated(fx.sample_hierarchy(TS_A), len(fx.sample_hierarchy(TS_A)) - 4),
    "odd_length": fx.sample_odd_length,
    "zero_length": fx.sample_zero_length,
    "bad_order": fx.sample_bad_order,
    "not_gds": lambda: os.urandom(3 * 2 ** 20 + 17),
    "oasis_like": lambda: b"%SEMI-OASIS\r\n" + os.urandom(5000),
    "empty": lambda: b"",
}


@pytest.mark.parametrize("name", sorted(BROKEN_SAMPLES))
def test_unscannable_input_falls_back_to_opaque_and_still_roundtrips(repo, tmp_path, name):
    data = BROKEN_SAMPLES[name]()
    record = save(repo, tmp_path, data)
    analysis = record["manifest"]["format_analysis"]
    assert analysis["status"] == "unavailable" and analysis["reason"]
    assert record["manifest"]["normalized_algorithm"] == "raw"
    assert record["manifest"]["top_cells"] is None
    assert restore(repo, record, tmp_path) == data
    if name == "not_gds":
        assert record["manifest"]["counts"]["layout_chunk_refs"] == 4
    if name == "oasis_like":
        assert analysis["kind"] == "oasis"


def test_scan_reports_the_offset_of_a_structure_error(tmp_path):
    with pytest.raises(gds_scan.GdsStructureError) as exc:
        with open(tmp_path / "x.gds", "wb") as f:
            f.write(fx.sample_bad_order())
        with open(tmp_path / "x.gds", "rb") as f:
            gds_scan.scan_gds(f, _NullSink())
    assert exc.value.offset > 0 and "STRNAME" in exc.value.reason


class _NullSink:
    def chunk(self, digest, data, *context): pass
    def references(self, targets): pass
    def segment(self, entry, chunks, timestamp): pass


# ------------------------------------------------------------- normalization --
def test_timestamp_only_resave_is_recognised_and_costs_no_layout_bytes(repo, tmp_path):
    n = 200
    first = save(repo, tmp_path, fx.sample_many_cells(n, stamp=TS_A), title="a")
    second = save(repo, tmp_path, fx.sample_many_cells(n, stamp=TS_B), title="b")
    assert first["sha256"] != second["sha256"]
    assert first["manifest"]["normalized_sha256"] == second["manifest"]["normalized_sha256"]
    assert second["manifest"]["counts"]["new_layout_objects"] == 0        # spec 9.2: no new layout payload
    assert second["manifest"]["counts"]["new_metadata_objects"] <= 3       # change entries + evidence only
    assert second["manifest"]["counts"]["new_pack_bytes"] < 2048
    assert second["manifest"]["timestamps"] == {"count": n + 2, "runs": [[TS_B.hex(), n + 2]]}
    assert restore(repo, first, tmp_path) == fx.sample_many_cells(n, stamp=TS_A)
    assert restore(repo, second, tmp_path) == fx.sample_many_cells(n, stamp=TS_B)
    change = repo.get_changeset(second["id"])
    assert change["coverage"]["status"] == "complete"
    assert change["summary"]["cells_timestamp_only"] == n + 1
    assert change["summary"]["cells_changed"] == 0
    assert change["file"]["normalized_bytes_equal"] is True and change["file"]["raw_bytes_equal"] is False
    assert len(list(repo.packs_dir.glob("*.pack"))) == 2     # the second pack holds only the change record


def test_odd_timestamps_produce_several_runs(repo, tmp_path):
    data = fx.sample_odd_timestamps()
    record = save(repo, tmp_path, data)
    runs = record["manifest"]["timestamps"]["runs"]
    assert sum(r[1] for r in runs) == record["manifest"]["timestamps"]["count"] == 4
    assert len(runs) == 4
    assert restore(repo, record, tmp_path) == data


def test_fake_bgnstr_inside_geometry_is_not_a_cell_and_not_normalized(repo, tmp_path):
    data = fx.sample_fake_bgnstr(TS_A)
    record = save(repo, tmp_path, data)
    assert record["manifest"]["segments"] == 3          # lib_head, one real cell, lib_tail
    assert record["manifest"]["top_cells"] == [b64("FAKEHOST")]
    at = data.index(fx._FAKE_BGNSTR_PATTERN)
    altered = bytearray(data)
    altered[at + 10] ^= 0x5A                             # inside the fake 24-byte "timestamp"
    other = save(repo, tmp_path, bytes(altered))
    assert other["manifest"]["normalized_sha256"] != record["manifest"]["normalized_sha256"]
    assert restore(repo, other, tmp_path) == bytes(altered)


def test_bgnstr_with_non_standard_length_has_no_timestamp_slot(repo, tmp_path):
    data = fx.sample_bgnstr_wrong_length()
    record = save(repo, tmp_path, data)
    assert record["manifest"]["timestamps"]["count"] == 1   # only BGNLIB
    assert restore(repo, record, tmp_path) == data


def test_names_keep_raw_bytes_and_top_cells_are_derived(repo, tmp_path):
    record = save(repo, tmp_path, fx.sample_multi_top(TS_A))
    tops = [base64.b64decode(t) for t in record["manifest"]["top_cells"]]
    assert sorted(t.rstrip(b"\0") for t in tops) == [b"TOPA", b"TOPB"]
    dup = save(repo, tmp_path, fx.sample_duplicate_names(TS_A))
    assert dup["manifest"]["format_analysis"]["duplicate_names"] is True
    assert dup["manifest"]["top_cells"] is None
    assert repo.get_changeset(dup["id"])["coverage"]["status"] == "partial"
    utf = save(repo, tmp_path, fx.sample_utf8_name())
    names = [base64.b64decode(t) for t in utf["manifest"]["top_cells"]]
    assert any(b > 0x7F for name in names for b in name)


# ------------------------------------------------------------- cell isolation --
def test_editing_one_of_many_cells_costs_one_cell_plus_a_page(repo, tmp_path):
    n = 2000
    base = save(repo, tmp_path, fx.sample_many_cells(n, stamp=TS_A))
    edited_bytes = fx.sample_many_cells_edit(n, "modify", stamp=TS_B)
    edited = save(repo, tmp_path, edited_bytes)
    counts = edited["manifest"]["counts"]
    assert counts["unique_layout_objects"] == base["manifest"]["counts"]["unique_layout_objects"]
    assert counts["new_layout_objects"] == 1                 # the edited cell only
    assert counts["new_metadata_objects"] <= 4               # recipe page(s), change entries, evidence
    assert counts["new_pack_bytes"] < 32 * 1024      # measured ~21 KiB: the cell + one ~300-entry page + index
    assert restore(repo, edited, tmp_path) == edited_bytes
    summary = repo.get_changeset(edited["id"])["summary"]
    assert summary["cells_changed"] == 1 and summary["cells_timestamp_only"] == n
    assert summary["cells_added"] == summary["cells_removed"] == summary["cells_reordered"] == 0


def test_insert_delete_rename_are_reported_and_cheap(repo, tmp_path):
    n = 1000
    save(repo, tmp_path, fx.sample_many_cells(n, stamp=TS_A))
    inserted = save(repo, tmp_path, fx.sample_many_cells_edit(n, "insert", stamp=TS_A))
    s = repo.get_changeset(inserted["id"])["summary"]
    assert s["cells_added"] == 1 and s["cells_removed"] == 0 and s["cells_reordered"] == 0
    assert s["cells_changed"] == 1 and s["reference_records_added"] == 1   # TOP gained an SREF
    assert inserted["manifest"]["counts"]["new_layout_objects"] <= 2
    deleted = save(repo, tmp_path, fx.sample_many_cells_edit(n, "delete", stamp=TS_A))
    s = repo.get_changeset(deleted["id"])["summary"]
    assert s["cells_removed"] == 2 and s["cells_added"] == 0     # NEW and the middle cell
    assert s["reference_records_removed"] == 2
    save(repo, tmp_path, fx.sample_many_cells(n, stamp=TS_A))            # back to the base as parent
    renamed_bytes = fx.sample_many_cells_edit(n, "rename", stamp=TS_A)
    renamed = save(repo, tmp_path, renamed_bytes)
    s = repo.get_changeset(renamed["id"])["summary"]
    assert s["cells_added"] == 1 and s["cells_removed"] == 1 and s["cells_changed"] == 1
    assert s["reference_records_added"] == 1 and s["reference_records_removed"] == 1
    assert restore(repo, renamed, tmp_path) == renamed_bytes
    assert restore(repo, inserted, tmp_path) == fx.sample_many_cells_edit(n, "insert", stamp=TS_A)


def test_reordered_cells_use_the_minimal_explanation(repo, tmp_path):
    a, b, c, d = (fx.cell(n, fx.boundary(1, 0, [(0, 0), (10, 0), (10, 10), (0, 10)]), stamp=TS_A)
                  for n in ("A", "B", "C", "D"))
    save(repo, tmp_path, fx.library([a, b, c, d], stamp=TS_A))
    moved = save(repo, tmp_path, fx.library([b, c, d, a], stamp=TS_A))
    assert repo.get_changeset(moved["id"])["summary"]["cells_reordered"] == 1


def test_reference_retarget_is_a_structural_change_even_if_geometry_matches(repo, tmp_path):
    box = fx.boundary(1, 0, [(0, 0), (10, 0), (10, 10), (0, 10)])
    a, b = fx.cell("A", box, stamp=TS_A), fx.cell("B", box, stamp=TS_A)
    top1 = fx.cell("TOP", fx.sref("A") + fx.sref("A") + fx.sref("B"), stamp=TS_A)
    top2 = fx.cell("TOP", fx.sref("B") + fx.sref("B") + fx.sref("B"), stamp=TS_A)
    save(repo, tmp_path, fx.library([a, b, top1], stamp=TS_A))
    swapped = save(repo, tmp_path, fx.library([a, b, top2], stamp=TS_A))
    s = repo.get_changeset(swapped["id"])["summary"]
    assert s["cells_changed"] == 1 and s["reference_records_added"] == 2 and s["reference_records_removed"] == 2


def test_units_change_alters_library_context(repo, tmp_path):
    first, second = fx.sample_units_changed((0.001, 1e-9), (0.0005, 5e-10), stamp=TS_A)
    save(repo, tmp_path, first)
    changed = save(repo, tmp_path, second)
    s = repo.get_changeset(changed["id"])["summary"]
    assert s["library_context_changed"] is True and s["cells_changed"] == 0


# ---------------------------------------------------------------- CDC in a cell --
def test_insert_in_a_big_cell_resyncs_after_the_edit(repo, tmp_path):
    base = fx.sample_big_cell(4 * 2 ** 20, stamp=TS_A)
    first = save(repo, tmp_path, base)
    assert first["manifest"]["counts"]["layout_chunk_refs"] >= 6
    before, edited_bytes = fx.sample_big_cell_insert(4 * 2 ** 20, insert_after_shape=20000, stamp=TS_A)
    assert before == base
    edited = save(repo, tmp_path, edited_bytes)
    counts = edited["manifest"]["counts"]
    assert counts["layout_chunk_refs"] == first["manifest"]["counts"]["layout_chunk_refs"]
    # the chunk containing the insertion (+ at most one resync chunk); metadata counted separately
    assert 1 <= counts["new_layout_objects"] <= 2
    assert counts["new_pack_bytes"] < 2 * gds_scan.CDC_MAX
    assert restore(repo, edited, tmp_path) == edited_bytes


def test_no_anchor_cell_still_roundtrips_and_reports_forced_cuts(repo, tmp_path):
    data = fx.sample_no_anchor_big_cell(3 * 2 ** 20)
    record = save(repo, tmp_path, data)
    assert record["manifest"]["counts"]["layout_chunk_refs"] >= 5    # forced 1 MiB cuts
    assert restore(repo, record, tmp_path) == data


def test_cdc_boundaries_do_not_depend_on_the_read_buffer(tmp_path):
    data = fx.sample_big_cell(3 * 2 ** 20, stamp=TS_A)
    (tmp_path / "x.gds").write_bytes(data)

    def collect(buffer):
        chunks = []

        class Sink:
            def chunk(self, digest, payload, *context): chunks.append(digest)
            def references(self, targets): pass
            def segment(self, entry, hashes, ts): pass
        with patch.object(gds_scan, "BUFFER", buffer), open(tmp_path / "x.gds", "rb") as f:
            gds_scan.scan_gds(f, Sink())
        return chunks
    assert collect(4 * 1024 * 1024) == collect(4096) == collect(1000)


# --------------------------------------------------------------- integrity --
def _corrupt_first_pack(repo):
    pack = next(repo.packs_dir.glob("*.pack"))
    data = bytearray(pack.read_bytes())
    data[200] ^= 0xFF
    pack.write_bytes(bytes(data))


def test_corrupt_pack_refuses_export_and_publishes_nothing(repo, tmp_path):
    record = save(repo, tmp_path, fx.sample_hierarchy(TS_A))
    _corrupt_first_pack(repo)
    with pytest.raises(RepositoryError, match="corrupt"):
        repo.export(record["id"], tmp_path / "out.gds")
    assert not (tmp_path / "out.gds").exists()
    assert not list(tmp_path.glob(".vestigraph-export-*"))


def test_tampered_manifest_is_refused(repo, tmp_path):
    record = save(repo, tmp_path, fx.sample_many_cells(50, stamp=TS_A))
    with sqlite3.connect(repo.database) as db:
        manifest = record["manifest"]
        manifest["timestamps"]["runs"][0][1] -= 1
        import json
        db.execute("UPDATE checkpoints SET manifest=? WHERE id=?", (json.dumps(manifest), record["id"]))
    with pytest.raises(RepositoryError):
        repo.export(record["id"], tmp_path / "out.gds")


def test_pack_index_can_be_rebuilt_from_the_pack_file(repo, tmp_path):
    save(repo, tmp_path, fx.sample_many_cells(100, stamp=TS_A))
    pack = next(repo.packs_dir.glob("*.pack"))
    rows = list(scan_pack(pack, registry=repo.services.codecs.registry))
    with sqlite3.connect(repo.database) as db:
        indexed = db.execute("SELECT hash, offset, raw_size, stored_size, codec FROM objects ORDER BY offset").fetchall()
    assert [(r[0], r[1], r[2], r[3], r[4]) for r in rows] == [tuple(i) for i in indexed]
    data = bytearray(pack.read_bytes())
    data[-1] ^= 1
    pack.write_bytes(bytes(data))
    with pytest.raises(PackError):
        list(scan_pack(pack, registry=repo.services.codecs.registry))


def test_source_change_between_prepare_and_commit_is_rejected(repo, tmp_path):
    source = tmp_path / "layout.gds"
    source.write_bytes(fx.sample_hierarchy(TS_A))
    prepared = repo.prepare(source)
    source.write_bytes(fx.sample_hierarchy(TS_B))
    with pytest.raises(RepositoryError, match="changed"):
        repo.commit(prepared, title="x")
    assert repo.history() == [] and not list(repo.tmp_dir.glob("vg2-*"))
    assert repo.checkpoint(source)["sha256"] == hashlib.sha256(fx.sample_hierarchy(TS_B)).hexdigest()


def test_prepare_context_discards_and_releases_the_lease(repo, tmp_path):
    source = tmp_path / "layout.gds"
    source.write_bytes(fx.sample_hierarchy(TS_A))
    with repo.prepare(source) as prepared:
        assert prepared.normalized_sha256 and prepared.top_cells == [fx.string_payload("TOP")]
        assert list(repo.tmp_dir.glob("vg2-*.pack"))
    assert not list(repo.tmp_dir.glob("vg2-*.pack")) and prepared.state == "discarded"
    with pytest.raises(RepositoryError, match="discarded"):
        repo.commit(prepared)
    assert repo.checkpoint(source)["manifest"]["counts"]["new_objects"] > 0   # lease was released


def test_failure_after_pack_publish_leaves_no_visible_version_and_is_cleaned_up(repo, tmp_path):
    source = tmp_path / "layout.gds"
    source.write_bytes(fx.sample_hierarchy(TS_A))
    first = repo.checkpoint(source)
    source.write_bytes(fx.sample_multi_top(TS_A))
    prepared = repo.prepare(source)
    original = repo._open_connection

    def failing():
        raise sqlite3.OperationalError("disk I/O error")
    with patch.object(repo, "_open_connection", failing), pytest.raises(sqlite3.OperationalError):
        repo.commit(prepared)
    assert [h["id"] for h in repo.history()] == [first["id"]]
    orphans = list(repo.packs_dir.glob("*.pack"))
    assert len(orphans) == 2                         # the published-but-unreferenced pack is an orphan
    assert restore(repo, first, tmp_path) == fx.sample_hierarchy(TS_A)
    (repo.tmp_dir / "not-ours.gds").write_bytes(b"late RPC still writing")
    (repo.packs_dir / "readme.txt").write_bytes(b"foreign")
    repo.acquire_writer("test")          # the lease holder removes this format's orphans
    try:
        assert len(list(repo.packs_dir.glob("*.pack"))) == 1
        assert (repo.tmp_dir / "not-ours.gds").exists() and (repo.packs_dir / "readme.txt").exists()
    finally:
        repo.release_writer()
    again = repo.checkpoint(source)
    assert restore(repo, again, tmp_path) == fx.sample_multi_top(TS_A)


def test_export_never_overwrites_and_verifies_before_publishing(repo, tmp_path):
    record = save(repo, tmp_path, fx.sample_hierarchy(TS_A))
    target = tmp_path / "exists.gds"
    target.write_bytes(b"keep")
    with pytest.raises(RepositoryError):
        repo.export(record["id"], target)
    assert target.read_bytes() == b"keep"


# ------------------------------------------------------------ v1 coexistence --
def test_v1_history_upgrades_additively_and_old_handles_keep_writing_v1(tmp_path):
    root = tmp_path / "history"
    old = Repository.init(root, storage_format=1)
    source = tmp_path / "layout.gds"
    source.write_bytes(fx.sample_hierarchy(TS_A))
    first = old.checkpoint(source, title="v1")
    assert first["manifest"]["format"] == 1
    new = Repository(root)
    assert new.format == 1
    with pytest.raises(RepositoryError, match="format 1"):
        new.prepare(source)
    result = new.upgrade_storage()
    assert result["upgraded"] and Path(result["backup"]).exists()
    assert new.format == 2 and Repository(root).format == 2
    with sqlite3.connect(result["backup"]) as db:
        assert db.execute("SELECT value FROM config WHERE key='format_version'").fetchone()[0] == "1"
    source.write_bytes(fx.sample_hierarchy(TS_B))
    second = new.checkpoint(source, title="v2")
    assert second["manifest"]["format"] == 2 and second["parent_id"] == first["id"]
    # A stale format-1 handle still writes a valid v1 checkpoint; the new program reads it.
    source.write_bytes(fx.sample_multi_top(TS_A))
    third = old.checkpoint(source, title="stale writer")
    assert third["manifest"]["format"] == 1 and third["parent_id"] == second["id"]
    for record, data in ((first, fx.sample_hierarchy(TS_A)), (second, fx.sample_hierarchy(TS_B)),
                         (third, fx.sample_multi_top(TS_A))):
        assert restore(new, record, tmp_path) == data
    fourth = new.checkpoint(source, title="after stale")
    assert fourth["parent_id"] == third["id"]
    assert new.get_changeset(fourth["id"])["coverage"]["status"] == "unavailable"
    stats = new.stats()
    assert stats["loose_objects"] >= 2 and stats["pack_objects"] >= 1 and stats["format"] == 2
    assert Repository.open_readonly(root).format == 2


def test_readonly_open_never_upgrades(tmp_path):
    root = tmp_path / "history"
    Repository.init(root, storage_format=1)
    ro = Repository.open_readonly(root)
    with pytest.raises(RepositoryError):
        ro.upgrade_storage()
    assert Repository(root).format == 1


# --------------------------------------------------------------- SequenceRef --
def test_sequence_pages_are_content_defined_and_verified():
    sink = md.MemorySink()
    writer = md.SequenceWriter(sink)
    items = [{"hash": hashlib.sha256(str(i).encode()).hexdigest(), "i": i} for i in range(30000)]
    for item in items:
        writer.add(item)
    ref = writer.finish()
    assert "index" in ref and ref["levels"] == 1 and ref["count"] == 30000
    assert list(md.iter_sequence(ref, sink)) == items
    # inserting one item in the middle rewrites only the page containing it (plus the index)
    sink2 = md.MemorySink()
    writer2 = md.SequenceWriter(sink2)
    for item in items[:15000] + [{"hash": "ff" * 32, "i": -1}] + items[15000:]:
        writer2.add(item)
    ref2 = writer2.finish()
    pages1 = {p[0] for p in md.parse(sink.get(ref["index"], 2 ** 20))["pages"]}
    pages2 = {p[0] for p in md.parse(sink2.get(ref2["index"], 2 ** 20))["pages"]}
    assert len(pages2 - pages1) == 1
    bad = dict(ref, count=29999)
    with pytest.raises(md.MetadataError):
        list(md.iter_sequence(bad, sink))


def test_sequence_falls_back_to_two_index_levels():
    sink = md.MemorySink()
    with patch.object(md, "PAGE_MAX_BYTES", 2048):
        writer = md.SequenceWriter(sink)
        for i in range(4000):
            writer.add(hashlib.sha256(str(i).encode()).hexdigest())
        ref = writer.finish()
        assert ref["levels"] == 2
        assert list(md.iter_sequence(ref, sink)) == [hashlib.sha256(str(i).encode()).hexdigest() for i in range(4000)]


def test_long_chunk_lists_are_externalised(repo, tmp_path):
    with patch.object(gds_scan, "CDC_MAX", 64 * 1024), patch.object(gds_scan, "CDC_MIN", 16 * 1024):
        data = fx.sample_big_cell(6 * 2 ** 20, stamp=TS_A)
        record = save(repo, tmp_path, data)
    from vestigraph.storage import engine
    index = engine.ObjectIndex(repo)
    try:
        entries = list(engine._iter_recipe(record["manifest"], index))
    finally:
        index.close()
    cell = [e for e in entries if e["kind"] == "cell"][0]
    assert "chunks_ref" in cell and cell["chunks_ref"]["count"] > 64
    assert restore(repo, record, tmp_path) == data


def test_manifest_root_stays_small_for_many_cells(repo, tmp_path):
    import json
    record = save(repo, tmp_path, fx.sample_many_cells(5000, shapes=2, stamp=TS_A))
    assert len(json.dumps(record["manifest"])) < 4096
    assert record["manifest"]["recipe"]["count"] == 5003


# ------------------------------------------------ review round 1 (Codex) fixes --
def test_only_one_prepare_may_be_open_per_repository(repo, tmp_path):
    source = tmp_path / "layout.gds"
    source.write_bytes(fx.sample_hierarchy(TS_A))
    repo.acquire_writer("test")
    try:
        first = repo.prepare(source)
        with pytest.raises(RepositoryError, match="still open"):
            repo.prepare(source)
        first.discard()
        second = repo.prepare(source)              # allowed again once the first is finished
        record = repo.commit(second, title="ok")
        assert record["parent_id"] is None and repo.get_changeset(record["id"])["from_checkpoint_id"] is None
    finally:
        repo.release_writer()


def test_commit_refuses_when_the_head_moved_after_prepare(repo, tmp_path):
    source = tmp_path / "layout.gds"
    source.write_bytes(fx.sample_hierarchy(TS_A))
    first = repo.checkpoint(source, title="first")
    source.write_bytes(fx.sample_hierarchy(TS_B))
    prepared = repo.prepare(source)
    assert prepared.parent_id == first["id"]
    # A stale format-1 handle (or any other writer) appends a version in between.
    with sqlite3.connect(repo.database) as db:
        db.execute("INSERT INTO checkpoints(id,parent_id,segment_id,title,source,created_at,filename,size,sha256,manifest,metadata) "
                   "VALUES('deadbeef' || substr(?, 9), ?, NULL, 'stale', 'manual', 'now', 'x.gds', 0, ?, ?, '{}')",
                   (first["id"], first["id"], "0" * 64, '{"format":1,"chunk_size":1048576,"size":0,"sha256":"' + "0" * 64 + '","chunks":[]}'))
    with pytest.raises(RepositoryError, match="Another version was committed"):
        repo.commit(prepared, title="stale parent")
    assert prepared.state == "discarded"
    ids = [h["id"] for h in repo.history()]
    assert len(ids) == 2 and first["id"] in ids                       # no third version appeared
    assert not list(repo.tmp_dir.glob("vg2-*"))
    record = repo.checkpoint(source, title="retry")                    # parent is now the injected head
    assert record["parent_id"] == ids[0]
    assert repo.get_changeset(record["id"])["from_checkpoint_id"] == ids[0]


def _set_datatype(data: bytes, header: bytes, datatype: int) -> bytes:
    at = data.index(header)
    out = bytearray(data)
    out[at + 3] = datatype
    return bytes(out)


def test_timestamp_records_with_a_wrong_datatype_are_not_normalized(repo, tmp_path):
    base = fx.sample_hierarchy(TS_A)
    wrong_lib = _set_datatype(base, b"\x00\x1c\x01\x02", 0x03)         # BGNLIB with datatype int32
    wrong_str = _set_datatype(base, b"\x00\x1c\x05\x02", 0x03)         # first BGNSTR with datatype int32
    normal = save(repo, tmp_path, base)
    for data, header in ((wrong_lib, b"\x00\x1c\x01\x03"), (wrong_str, b"\x00\x1c\x05\x03")):
        record = save(repo, tmp_path, data)
        assert record["manifest"]["timestamps"]["count"] == normal["manifest"]["timestamps"]["count"] - 1
        assert restore(repo, record, tmp_path) == data
        altered = bytearray(data)
        altered[data.index(header) + 4 + 7] ^= 0x11                    # inside the un-normalized payload
        other = save(repo, tmp_path, bytes(altered))
        assert other["manifest"]["normalized_sha256"] != record["manifest"]["normalized_sha256"]
        assert restore(repo, other, tmp_path) == bytes(altered)


def _wide_top(extra_ref_to_last=0, stamp=TS_A):
    targets = ["T%05d" % i for i in range(1025)]
    refs = b"".join(fx.sref(t) for t in targets) + b"".join(fx.sref(targets[-1]) for _ in range(extra_ref_to_last))
    cells = [fx.cell(t, fx.boundary(1, 0, [(0, 0), (1, 0), (1, 1), (0, 1)]), stamp=stamp) for t in targets]
    return fx.library(cells + [fx.cell("TOP", refs, stamp=stamp)], stamp=stamp)


def test_truncated_reference_summary_makes_reference_counts_unknown(repo, tmp_path):
    first = save(repo, tmp_path, _wide_top())
    from vestigraph.storage import engine
    index = engine.ObjectIndex(repo)
    try:
        top = [e for e in engine._iter_recipe(first["manifest"], index) if e.get("name") == b64("TOP")][0]
    finally:
        index.close()
    assert top["refs_truncated"] is True and len(top["refs"]) == 1024 and top["ref_records"] == 1025
    second = save(repo, tmp_path, _wide_top(extra_ref_to_last=1))
    change = repo.get_changeset(second["id"])
    assert change["coverage"]["status"] == "partial"
    assert any("truncated" in w for w in change["coverage"]["warnings"])
    assert change["summary"]["reference_records_added"] is None
    assert change["summary"]["reference_records_removed"] is None
    assert change["summary"]["cells_changed"] == 1                     # TOP's bytes did change
    assert restore(repo, second, tmp_path) == _wide_top(extra_ref_to_last=1)


def test_upgrade_is_one_transaction(tmp_path):
    root = tmp_path / "history"
    Repository.init(root, storage_format=1)
    repo = Repository(root)

    def boom(db):
        raise sqlite3.OperationalError("simulated failure between DDL and the format flip")
    with patch.object(Repository, "_upgrade_hook", staticmethod(boom)), pytest.raises(sqlite3.OperationalError):
        repo.upgrade_storage()
    with sqlite3.connect(root / "index.sqlite3") as db:
        assert db.execute("SELECT value FROM config WHERE key='format_version'").fetchone()[0] == "1"
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert not tables & {"packs", "objects", "changesets"}
    assert Repository(root).format == 1
    assert repo.upgrade_storage()["upgraded"] and Repository(root).format == 2


# ------------------------------------------------ review round 2 fixes --
def test_large_trailer_is_chunked_correctly_across_small_buffers(repo, tmp_path):
    trailer = bytes(range(256)) * (10 * 1024)                          # 2.5 MiB after ENDLIB
    data = fx.library([fx.cell("A", fx.boundary(1, 0, [(0, 0), (1, 0), (1, 1), (0, 1)]), stamp=TS_A)],
                      stamp=TS_A, trailer=trailer)
    with patch.object(gds_scan, "BUFFER", 1000):
        record = save(repo, tmp_path, data)
    assert record["manifest"]["format_analysis"]["status"] == "complete"
    assert record["manifest"]["counts"]["layout_chunk_refs"] >= 5
    assert restore(repo, record, tmp_path) == data


def test_damaged_previous_version_does_not_block_a_new_one(repo, tmp_path):
    first = save(repo, tmp_path, fx.sample_many_cells(300, stamp=TS_A))
    page = first["manifest"]["recipe"]["pages"][0][0]                   # corrupt a recipe page, not a layout chunk
    with sqlite3.connect(repo.database) as db:
        row = db.execute("SELECT pack, offset, stored_size FROM objects WHERE hash=?", (page,)).fetchone()
    pack = repo.pack_path(row[0])
    data = bytearray(pack.read_bytes())
    data[row[1] + 112 + row[2] // 2] ^= 0xFF
    pack.write_bytes(bytes(data))
    with pytest.raises(RepositoryError):
        repo.export(first["id"], tmp_path / "old.gds")
    second = save(repo, tmp_path, fx.sample_many_cells(300, stamp=TS_B))
    change = repo.get_changeset(second["id"])
    assert change["coverage"]["status"] == "unavailable" and "unreadable" in change["coverage"]["reason"]
    assert change["from_checkpoint_id"] == first["id"]
    assert restore(repo, second, tmp_path) == fx.sample_many_cells(300, stamp=TS_B)


def test_damaged_v1_loose_object_is_not_reused_by_format_2(tmp_path):
    root = tmp_path / "history"
    old = Repository.init(root, storage_format=1)
    source = tmp_path / "blob.bin"
    source.write_bytes(os.urandom(2 * 2 ** 20 + 5))
    first = old.checkpoint(source)
    digest = first["manifest"]["chunks"][0]["hash"]
    old.loose_path(digest).write_bytes(b"damaged")
    new = Repository(root)
    new.upgrade_storage()
    second = new.checkpoint(source)                                     # same bytes, opaque path
    assert second["manifest"]["counts"]["new_objects"] >= 1             # the damaged chunk was re-stored
    assert restore(new, second, tmp_path) == source.read_bytes()
    with pytest.raises(RepositoryError):
        new.export(first["id"], tmp_path / "old.bin")


def test_cli_style_checkpoint_cleans_orphans_without_a_long_lease(repo, tmp_path):
    stale_pack = repo.tmp_dir / ("vg2-%s.pack" % ("0" * 32))
    stale_pack.write_bytes(b"stale")
    stale_work = repo.tmp_dir / ("vg2-%s.work.sqlite3" % ("0" * 32))
    stale_work.write_bytes(b"stale")
    orphan = repo.packs_dir / ("%s.pack" % ("1" * 32))
    orphan.write_bytes(b"orphan")
    (repo.tmp_dir / "capture-late.gds").write_bytes(b"late RPC")
    save(repo, tmp_path, fx.sample_hierarchy(TS_A))                     # transient lease only
    assert not stale_pack.exists() and not stale_work.exists() and not orphan.exists()
    assert (repo.tmp_dir / "capture-late.gds").exists()


def test_units_changed_is_reported(repo, tmp_path):
    first, second = fx.sample_units_changed((0.001, 1e-9), (0.0005, 5e-10), stamp=TS_A)
    save(repo, tmp_path, first)
    same = save(repo, tmp_path, first[:-4] + first[-4:])                # identical bytes, manual resave
    assert repo.get_changeset(same["id"])["summary"]["units_changed"] is False
    changed = save(repo, tmp_path, second)
    assert repo.get_changeset(changed["id"])["summary"]["units_changed"] is True


def test_capture_baseline_is_per_document_and_per_algorithm(repo, tmp_path):
    from vestigraph.capture_runtime import _latest_content_fingerprint, FINGERPRINT_V1
    source = tmp_path / "a.gds"
    source.write_bytes(fx.sample_hierarchy(TS_A))
    record = repo.checkpoint(source, metadata={"document": {"filename": "a.gds"}, "content_sha256": "x" * 64})
    assert _latest_content_fingerprint(repo, {"filename": "a.gds"}) == (
        "gds-timestamp-zero-v1", record["manifest"]["normalized_sha256"])
    assert _latest_content_fingerprint(repo, {"filename": "copy_of_a.gds"}) is None   # Save As gets a baseline
    assert _latest_content_fingerprint(repo, None)[0] != FINGERPRINT_V1              # never the v1 algorithm
    v1 = Repository.init(tmp_path / "old", storage_format=1)
    old = v1.checkpoint(source, metadata={"document": {"filename": "a.gds"}, "content_sha256": "y" * 64})
    assert _latest_content_fingerprint(v1, {"filename": "a.gds"}) == (FINGERPRINT_V1, "y" * 64)


def test_cells_referenced_only_beyond_the_refs_cap_are_not_top_cells(repo, tmp_path):
    targets = ["T%05d" % i for i in range(1100)]
    cells = [fx.cell(t, fx.boundary(1, 0, [(0, 0), (1, 0), (1, 1), (0, 1)]), stamp=TS_A) for t in targets]
    top = fx.cell("TOP", b"".join(fx.sref(t) for t in targets), stamp=TS_A)
    record = save(repo, tmp_path, fx.library(cells + [top], stamp=TS_A))
    assert record["manifest"]["top_cells"] == [b64("TOP")]


def test_scan_pack_refuses_oversized_records(repo, tmp_path):
    save(repo, tmp_path, fx.sample_hierarchy(TS_A))
    pack = next(repo.packs_dir.glob("*.pack"))
    data = bytearray(pack.read_bytes())
    import struct
    struct.pack_into("<I", data, 8 + 40, 0x7FFFFFFF)          # first record's stored_size
    pack.write_bytes(bytes(data))
    with pytest.raises(PackError, match="larger"):
        list(scan_pack(pack, registry=repo.services.codecs.registry))


def test_sname_outside_a_cell_is_a_structure_error(repo, tmp_path):
    data = fx.sample_hierarchy(TS_A)
    at = data.index(b"\x00\x1c\x05\x02")                            # first BGNSTR
    broken = data[:at] + fx.sref("UNIT") + data[at:]
    record = save(repo, tmp_path, broken)
    assert record["manifest"]["format_analysis"]["status"] == "unavailable"
    assert "SNAME" in record["manifest"]["format_analysis"]["reason"]
    assert restore(repo, record, tmp_path) == broken
