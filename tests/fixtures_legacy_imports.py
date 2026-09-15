"""Legacy insertion: immutable bytes/parents, preserved head, restart-safe item publication."""


import hashlib


import pytest


from tests import gds_fixtures as fx


from tests.service_helpers import init_state, open_application, project_dirs, WebSession


from vestigraph.store import Repository, RepositoryError


from vestigraph.web.app import create_app


from vestigraph.web.auth import AuthManager


def source(root, name, second):
    path = root / name
    path.write_bytes(fx.sample_hierarchy(fx.timestamps(second=second)))
    return path


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def put(repo, path, anchor, batch="batch", item="item"):
    return repo.import_checkpoint(path, before_id=anchor, batch_id=batch, item_id=item,
                                  expected_sha256=digest(path), title=path.stem)


@pytest.fixture
def history(tmp_path):
    repo = Repository.init(tmp_path / "history")
    final_path = source(tmp_path, "final.gds", 10)
    final = repo.checkpoint(final_path)
    return repo, final, final_path


@pytest.fixture
def service(tmp_path, history):
    repo, final, _ = history
    init_state(tmp_path / "state")
    app = open_application(tmp_path / "state")
    ws, hr = project_dirs(tmp_path)
    project = app.catalog.add_project("import test", ws, hr, allow_inside_git=True)
    doc = app.catalog.add_history(project["id"], repo.root, read_only=False)
    auth = AuthManager()
    session = WebSession(create_app(app, port=8787, auth=auth), auth)
    session.login()
    yield app, session, doc, repo, final
    app.stop()


def prepare(app, doc, paths):
    job = app.legacy_imports.prepare(doc["id"], [str(p) for p in paths])
    assert app.runner.wait_idle(15)
    result = app.catalog.get_job(job["id"])
    assert result["status"] == "succeeded", result
    return app.legacy_imports.get(doc["id"], result["result"]["plan_id"])


def wait_import(app, job):
    assert app.runner.wait_idle(15)
    result = app.catalog.get_job(job["id"])
    assert result["status"] == "succeeded", result
    return result["result"]


__all__ = [name for name in globals() if not name.startswith("__")]
