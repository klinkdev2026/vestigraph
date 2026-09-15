"""Fault recovery must preserve every previously committed version."""
import shutil
import sqlite3
from pathlib import Path
import pytest
from tests import gds_fixtures as fx
from vestigraph.store import Repository, RepositoryError
from vestigraph.storage.packs import PackReader, PackError


def variant(edits):
    shapes = []
    for i in range(650):
        geom = fx.modified_geom(1, 0, i, salt=i) if i in edits else fx.base_geom(1, 0, i)
        x0, y0, x1, y1, layer, dtype = geom
        shapes.append(fx.boundary(layer, dtype, fx._box_points(x0, y0, x1, y1)))
    return fx.library([fx.cell("BIG", b"".join(shapes), stamp=fx.TS_ZERO)], stamp=fx.TS_ZERO)


@pytest.mark.parametrize("transient", [True, False])
def test_repair_or_transient_failure_preserves_descendant_exports(tmp_path, monkeypatch, transient):
    pytest.importorskip("bsdiff4")
    repo = Repository.init(tmp_path / "history")
    source = tmp_path / "layout.gds"
    data = [variant(set()), variant({10}), variant({10, 500})]
    records = []
    for payload in data:
        source.write_bytes(payload)
        records.append(repo.checkpoint(source))
    with repo._connect() as db:
        assert db.execute("SELECT max(depth) FROM objects").fetchone()[0] >= 2
    original = PackReader._handle
    blocked = {r["id"] for r in records[1:]}
    def fail(reader, pack_id):
        if pack_id in blocked:
            raise PermissionError("sharing violation") if transient else PackError("damaged stored payload")
        return original(reader, pack_id)
    source.write_bytes(data[1])
    with monkeypatch.context() as m:
        m.setattr(PackReader, "_handle", fail)
        if transient:
            with pytest.raises(RepositoryError):
                repo.checkpoint(source)
        else:
            repo.checkpoint(source)
    for n, record in enumerate(records):
        assert repo.export(record["id"], tmp_path / f"restored-{n}.gds").read_bytes() == data[n]
    assert repo._open_prepare is None


def test_old_database_backup_quarantines_newer_packs_without_losing_bytes(tmp_path):
    repo = Repository.init(tmp_path / "history")
    source = tmp_path / "layout.gds"
    source.write_bytes(fx.sample_hierarchy())
    repo.checkpoint(source)
    backup = tmp_path / "backup.sqlite3"
    with repo._connect() as db, sqlite3.connect(backup) as target:
        db.backup(target)
    for count in (50, 60):
        source.write_bytes(fx.sample_many_cells(count))
        repo.checkpoint(source)
    before = {p.name: p.read_bytes() for p in repo.packs_dir.glob("*.pack")}
    with sqlite3.connect(backup) as db, sqlite3.connect(repo.database) as target:
        db.backup(target)
    repo.acquire_writer("recovery test")
    repo.release_writer()
    retained = {p.name: p.read_bytes() for p in repo.packs_dir.glob("*.pack")}
    retained.update({p.name: p.read_bytes() for p in (repo.root / "quarantine/packs").glob("*.pack")})
    assert retained == before
    assert len(list((repo.root / "quarantine/packs").glob("*.pack"))) == 2


def test_deleted_source_at_commit_releases_prepare_and_writer(tmp_path):
    repo = Repository.init(tmp_path / "history")
    source = tmp_path / "source.bin"
    source.write_bytes(b"payload")
    prepared = repo.prepare(source)
    source.unlink()
    with pytest.raises(RepositoryError, match="unavailable"):
        repo.commit(prepared)
    assert prepared.state == "discarded" and repo._open_prepare is None
    source.write_bytes(b"new payload")
    assert Repository(repo.root).checkpoint(source)["sha256"]


def test_index_rebuild_into_new_history_preserves_original_and_restores(tmp_path):
    from vestigraph.storage.recovery import inspect_history, rebuild_index
    repo = Repository.init(tmp_path / "original")
    source = tmp_path / "source.bin"
    source.write_bytes(b"restore me")
    record = repo.checkpoint(source)
    with repo._connect() as db:
        db.execute("DELETE FROM objects")
    before = repo.database.read_bytes()
    assert not inspect_history(repo)["ok"]
    report = rebuild_index(repo, tmp_path / "recovered")
    assert report["ok"] and report["history_metadata_complete"]
    assert repo.database.read_bytes() == before
    assert Repository.open_readonly(tmp_path / "recovered").export(record["id"], tmp_path / "out.bin").read_bytes() == b"restore me"
    assert not inspect_history(repo)["ok"]


def test_export_without_hardlinks_is_atomic_and_refuses_replacement(tmp_path, monkeypatch):
    import errno
    import vestigraph.storage.publish as publish
    repo = Repository.init(tmp_path / "history")
    source = tmp_path / "source.bin"
    source.write_bytes(b"portable")
    record = repo.checkpoint(source)
    def no_links(*args, **kwargs):
        raise OSError(errno.EOPNOTSUPP, "no hard links")
    monkeypatch.setattr(publish.os, "link", no_links)
    assert repo.export(record["id"], tmp_path / "out.bin").read_bytes() == b"portable"
    with pytest.raises(RepositoryError, match="already exists"):
        repo.export(record["id"], tmp_path / "out.bin")
    temporary = tmp_path / "temporary"
    temporary.write_bytes(b"other")
    with pytest.raises(FileExistsError):
        publish.publish_new(temporary, tmp_path / "out.bin")
    assert (tmp_path / "out.bin").read_bytes() == b"portable"
