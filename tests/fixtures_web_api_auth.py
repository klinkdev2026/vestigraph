"""Security boundary tests: Host, Origin, bootstrap single-use, cookie session, CSRF, limits."""


import pytest


from tests.service_helpers import WebSession, init_state, open_application, project_dirs, synthetic_repo


from vestigraph.web.app import create_app


from vestigraph.web.auth import AuthManager, cookie_name


COOKIE_NAME = cookie_name(8787)


@pytest.fixture
def web(tmp_path):
    init_state(tmp_path / "state")
    application = open_application(tmp_path / "state")
    auth = AuthManager()
    session = WebSession(create_app(application, port=8787, auth=auth), auth)
    workspace, history = project_dirs(tmp_path)
    project = application.catalog.add_project("secret-project-name", workspace, history, allow_inside_git=True)
    application.supervisor.on_project_added(project)
    document = application.catalog.add_history(project["id"], synthetic_repo(tmp_path / "legacy").root)
    yield session, application, project, document
    application.stop()


__all__ = [name for name in globals() if not name.startswith("__")]
