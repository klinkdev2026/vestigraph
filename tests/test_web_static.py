"""Static delivery of the panel: files exist in the package, correct MIME types, CSP, no inline script."""
import re
from pathlib import Path

import pytest

from tests.service_helpers import WebSession, init_state, open_application
from vestigraph.web.app import STATIC_DIR, create_app
from vestigraph.web.auth import AuthManager

REQUIRED = ["index.html", "boot.js", "app.js", "api.js", "i18n.js", "styles.css",
            "locales/zh-CN.json", "locales/en.json"]


@pytest.fixture
def web(tmp_path):
    init_state(tmp_path / "state")
    application = open_application(tmp_path / "state")
    auth = AuthManager()
    session = WebSession(create_app(application, port=8787, auth=auth), auth)
    yield session
    application.stop()


def test_package_ships_every_static_file():
    missing = [name for name in REQUIRED if not (STATIC_DIR / name).is_file()]
    assert missing == []
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    # No inline scripts: the CSP forbids them and the bootstrap token handling lives in boot.js.
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html)
    assert 'src="/static/boot.js"' in html and 'src="/static/app.js"' in html
    referenced = set(re.findall(r'/static/([\w./-]+)', html))
    for name in referenced:
        assert (STATIC_DIR / name).is_file(), name


def test_static_routes_serve_types_and_headers(web):
    index = web.get("/")
    assert index.status_code == 200 and index.headers["content-type"].startswith("text/html")
    csp = index.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp and "unsafe-inline" not in csp
    assert index.headers["cache-control"] == "no-store"
    for name, prefix in (("app.js", "text/javascript"), ("styles.css", "text/css"), ("locales/en.json", "application/json")):
        response = web.get(f"/static/{name}")
        assert response.status_code == 200, name
        assert response.headers["content-type"].startswith(prefix), (name, response.headers["content-type"])
        assert response.headers["x-content-type-options"] == "nosniff"
    assert web.get("/static/nope.js").status_code == 404
    assert web.get("/static/../app.py").status_code in (404, 400)
    # Anonymous static delivery never leaks service data.
    assert "index.sqlite3" not in web.get("/static/app.js").text
