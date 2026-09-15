"""History contract over HTTP: pagination, scope-bound cursors, annotations, downloads, unavailable."""


import pytest


from tests import gds_fixtures as fx


from tests.service_helpers import WebSession, bulk_events, init_state, open_application, project_dirs, synthetic_repo, tree_hashes


from vestigraph.store import Repository


from vestigraph.web.app import create_app


from vestigraph.web.auth import AuthManager


@pytest.fixture
def web(tmp_path):
    init_state(tmp_path / "state")
    application = open_application(tmp_path / "state")
    auth = AuthManager()
    session = WebSession(create_app(application, port=8787, auth=auth), auth)
    session.login()
    workspace, history = project_dirs(tmp_path)
    project = application.catalog.add_project("p", workspace, history, allow_inside_git=True)
    application.supervisor.on_project_added(project)
    repo = synthetic_repo(tmp_path / "legacy", checkpoints=3, events=2)
    document = application.catalog.add_history(project["id"], repo.root)
    yield session, application, project, document, repo
    application.stop()


def data(response):
    assert response.status_code in (200, 201, 202), response.text
    body = response.json()
    assert body["ok"] is True and body["request_id"]
    return body["data"]


__all__ = [name for name in globals() if not name.startswith("__")]
