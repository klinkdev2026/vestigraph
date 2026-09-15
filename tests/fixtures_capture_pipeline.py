"""New durable path: real SQLite/packs/GDS bytes; no live editor is contacted."""


import hashlib


from pathlib import Path


import threading


import pytest


from tests import gds_fixtures as fx


from tests.fixtures_capture_safety import Endpoint


from vestigraph_backends.vesti_backend_klayout.capture import KLayoutObserver as _Observer


from vestigraph.capture_pipeline import CapturePipeline


from vestigraph.capture_spool import Spool, SpoolFull, SpoolError, pending_stats


from vestigraph.store import Repository, RepositoryError, SaveCancelled


OPTIONS = dict(max_items=3, max_bytes=100000, max_file_bytes=20000, min_free_bytes=0)


def gds(name="TOP", label="one", second=1):
    stamp = fx.timestamps(second=second)
    return fx.library([fx.cell(name, fx.text(1, 0, label), stamp)], stamp=stamp)


@pytest.fixture
def repo(tmp_path):
    result = Repository.init(tmp_path / "history")
    result.acquire_writer("capture-tests")
    yield result
    result.release_writer()


def enqueue(spool, data, *, title="auto", segment=None, source="system"):
    with spool.repo._connect() as db:
        evidence = spool.repo._evidence(db, segment)
    item = spool.reserve({"segment_id": segment, "title": title, "source": source,
                          "evidence": evidence, "checkpoint_metadata": {
                              "document": {"filename": "customer.gds"},
                              "binding": {"identity": {"filename": "customer.gds"}}}})
    Path(item["path"]).write_bytes(data)
    return spool.accept(item["id"], export_ms=7.5)


__all__ = [name for name in globals() if not name.startswith("__")]
