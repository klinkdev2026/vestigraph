"""Synthetic bytes only: these tests do not certify EDA format semantics."""
import hashlib
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest

from vestigraph.store import CHUNK_SIZE, Repository, RepositoryError


@pytest.fixture
def repo(tmp_path):
    # These are white-box tests of the format-1 (loose 1 MiB chunk) path, which
    # must keep working unchanged for existing histories. Format 2 has its own
    # suite in test_storage_v2.py.
    return Repository.init(tmp_path / "memory", storage_format=1)


def test_two_versions_deduplicate_and_restore_exactly(repo, tmp_path):
    source = tmp_path / "编号 test.gds"
    a, b = b"a" * CHUNK_SIZE, b"b" * CHUNK_SIZE
    source.write_bytes(a + b)
    one = repo.checkpoint(source, title="before")
    source.write_bytes(a + b"c" * CHUNK_SIZE)
    two = repo.checkpoint(source, title="after", source="automation")
    assert two["parent_id"] == one["id"]
    assert repo.stats()["objects"] == 3
    assert repo.stats()["logical_bytes"] == 4 * CHUNK_SIZE
    assert repo.export(one["id"], tmp_path / "旧版.gds").read_bytes() == a + b
    assert repo.export(two["id"], tmp_path / "新版.gds").read_bytes() == source.read_bytes()
    assert repo.history()[0]["id"] == two["id"]


def test_unchanged_manual_milestones_reuse_chunks(repo, tmp_path):
    source = tmp_path / "layout.oas"
    source.write_bytes(os.urandom(4096))
    first, second = repo.checkpoint(source), repo.checkpoint(source)
    assert first["id"] != second["id"]
    assert first["sha256"] == second["sha256"]
    assert repo.stats()["objects"] == 1
    assert Repository.init(repo.root).stats()["checkpoints"] == 2


def test_empty_file(repo, tmp_path):
    source = tmp_path / "empty"
    source.touch()
    record = repo.checkpoint(source)
    assert record["manifest"]["chunks"] == []
    assert repo.export(record["id"], tmp_path / "empty-copy").read_bytes() == b""


def test_handle_and_path_ctime_can_differ(repo, tmp_path):
    source = tmp_path / "layout"
    source.write_bytes(b"test")
    real_fstat = os.fstat

    def handle_stat(fd):
        st = real_fstat(fd)
        return SimpleNamespace(st_dev=st.st_dev, st_ino=st.st_ino,
                               st_size=st.st_size, st_mtime_ns=st.st_mtime_ns,
                               st_ctime_ns=st.st_ctime_ns + 1000)

    with patch("vestigraph.store.os.fstat", side_effect=handle_stat):
        assert repo.checkpoint(source)["size"] == 4


def test_existing_output_is_never_overwritten(repo, tmp_path):
    source = tmp_path / "layout"
    source.write_bytes(b"test")
    record = repo.checkpoint(source)
    with pytest.raises(RepositoryError, match="already exists"):
        repo.export(record["id"], source)
    assert source.read_bytes() == b"test"


@pytest.mark.parametrize("damage", ["corrupt", "missing", "trailing"])
def test_damaged_chunks_do_not_publish_export(repo, tmp_path, damage):
    source = tmp_path / "layout"
    source.write_bytes(b"test")
    record = repo.checkpoint(source)
    digest = record["manifest"]["chunks"][0]["hash"]
    chunk = repo.root / "objects" / digest[:2] / digest
    if damage == "corrupt":
        chunk.write_bytes(b"broken")
    elif damage == "trailing":
        chunk.write_bytes(chunk.read_bytes() + b"junk")
    else:
        chunk.unlink()
    target = tmp_path / "export"
    with pytest.raises(RepositoryError, match="missing or corrupt"):
        repo.export(record["id"], target)
    assert not target.exists()
    assert not list(tmp_path.glob(".vestigraph-export-*"))


def test_corrupt_existing_object_not_reused(repo, tmp_path):
    source = tmp_path / "layout"
    source.write_bytes(b"test")
    record = repo.checkpoint(source)
    digest = record["sha256"]
    (repo.root / "objects" / digest[:2] / digest).write_bytes(b"damaged")
    with pytest.raises(RepositoryError):
        repo.checkpoint(source)
    assert len(repo.history()) == 1


def test_events_segments_and_idempotent_close(repo):
    segment = repo.begin_segment("numbering", source="mixed")
    one = repo.append_event("shapes_changed", {}, segment_id=segment["id"])
    two = repo.append_event("shapes_changed", {"caused_by": [{"method": "shape.create"}]},
                            source="automation", segment_id=segment["id"])
    assert two["seq"] > one["seq"]
    assert repo.events(segment["id"])[0]["source"] == "automation"
    closed = repo.close_segment(segment["id"])
    assert repo.close_segment(segment["id"]) == closed
    with pytest.raises(RepositoryError):
        repo.close_segment(segment["id"], "failed")
    with pytest.raises(RepositoryError):
        repo.append_event("late", {}, segment_id=segment["id"])
    assert len(repo.events()) == 2


def test_open_segments_survive_restart_without_forged_success(repo):
    segment = repo.begin_segment("unfinished")
    reopened = Repository(repo.root)
    assert reopened.segments()[0]["status"] == "open"
    assert reopened.close_segment(segment["id"], "interrupted")["status"] == "interrupted"


def test_source_change_during_read_rejected(repo, tmp_path):
    source = tmp_path / "layout"
    source.write_bytes(b"a" * CHUNK_SIZE)
    original = repo._put_chunk

    def concurrent_change(data):
        result = original(data)
        source.write_bytes(b"changed")
        return result

    with patch.object(repo, "_put_chunk", side_effect=concurrent_change):
        with pytest.raises(RepositoryError, match="Source changed"):
            repo.checkpoint(source)
    assert not repo.history()


def test_failed_metadata_commit_publishes_no_checkpoint(repo, tmp_path):
    source = tmp_path / "layout"
    source.write_bytes(b"payload")
    with sqlite3.connect(repo.database) as db:
        db.execute("CREATE TRIGGER fail_insert BEFORE INSERT ON checkpoints BEGIN SELECT RAISE(ABORT,'test failure'); END")
    with pytest.raises(sqlite3.DatabaseError):
        repo.checkpoint(source)
    assert not repo.history()
    # Orphan objects are intentionally retained, not falsely advertised as versions.
    assert repo.stats()["objects"] == 1


def test_concurrent_writers_keep_one_parent_chain(repo, tmp_path):
    source = tmp_path / "layout"
    source.write_bytes(os.urandom(10000))
    with ThreadPoolExecutor(max_workers=4) as pool:
        records = list(pool.map(lambda n: Repository(repo.root).checkpoint(source, title=str(n)), range(8)))
    history = repo.history()
    assert len(history) == len(records) == 8
    assert repo.stats()["objects"] == 1
    for newer, older in zip(history, history[1:]):
        assert newer["parent_id"] == older["id"]
    assert history[-1]["parent_id"] is None


def test_unsupported_format_and_bad_inputs_rejected(repo, tmp_path):
    with pytest.raises(RepositoryError):
        repo.append_event("test", {"x": float("nan")})
    with pytest.raises(RepositoryError):
        repo.append_event("test", {"x": "x" * (256 * 1024)})
    with pytest.raises(RepositoryError):
        repo.begin_segment("test", source="ai_proven")
    with pytest.raises(RepositoryError):
        repo.history(-1)
    with pytest.raises(RepositoryError):
        Repository(tmp_path / "not-a-repo")
    with sqlite3.connect(repo.database) as db:
        db.execute("UPDATE config SET value='999' WHERE key='format_version'")
    with pytest.raises(RepositoryError, match="Unsupported"):
        Repository.init(repo.root)


def test_manifest_path_traversal_rejected(repo, tmp_path):
    source = tmp_path / "layout"
    source.write_bytes(b"payload")
    record = repo.checkpoint(source)
    original = repo.get_checkpoint

    def tampered(checkpoint_id):
        result = original(checkpoint_id)
        result["manifest"]["chunks"][0]["hash"] = "../../outside"
        return result

    with patch.object(repo, "get_checkpoint", side_effect=tampered):
        with pytest.raises(RepositoryError, match="Invalid chunk hash"):
            repo.export(record["id"], tmp_path / "output")
    assert not (tmp_path / "output").exists()
