"""Behavior tests; shared fixtures live in fixtures_web_api_auth."""
from tests.fixtures_web_api_auth import *

def test_anonymous_gets_nothing_but_liveness(web):
    session, app, project, document = web
    assert session.get("/healthz").json() == {"ok": True}
    for path in ("/api/v1/status", "/api/v1/projects", f"/api/v1/documents/{document['id']}/checkpoints",
                 "/api/v1/sessions", "/api/v1/jobs/x"):
        response = session.get(path)
        assert response.status_code == 401
        body = response.json()
        assert body["ok"] is False and body["error"]["code"] == "UNAUTHENTICATED"
        assert project["name"] not in response.text and str(app.state.root) not in response.text
    assert session.get("/").status_code == 200


def test_host_header_and_dns_rebinding_rejected(web):
    session, *_ = web
    for host in ("evil.example:8787", "127.0.0.1:9999", "10.0.0.5:8787", "127.0.0.1.nip.io:8787"):
        response = session.get("/healthz", headers={"Host": host})
        assert response.status_code == 421, host
        assert response.json()["error"]["code"] == "HOST_NOT_ALLOWED"
    assert session.get("/healthz", headers={"Host": "localhost:8787"}).status_code == 200


def test_bootstrap_is_single_use_needs_origin_and_expires(tmp_path):
    init_state(tmp_path / "state")
    application = open_application(tmp_path / "state")
    try:
        clock = {"now": 1000.0}
        auth = AuthManager(clock=lambda: clock["now"])
        session = WebSession(create_app(application, port=8787, auth=auth), auth)
        token = auth.issue_bootstrap()
        headers = {"Authorization": f"Bearer {token}"}
        assert session.client.post("/api/v1/auth/bootstrap", headers=headers).status_code == 403
        assert session.client.post("/api/v1/auth/bootstrap",
                                   headers={**headers, "Origin": "http://evil.example"}).status_code == 403
        wrong = session.client.post("/api/v1/auth/bootstrap",
                                    headers={"Authorization": "Bearer nope", "Origin": session.origin})
        assert wrong.status_code == 401 and wrong.json()["error"]["code"] == "BOOTSTRAP_INVALID"
        ok = session.client.post("/api/v1/auth/bootstrap", headers={**headers, "Origin": session.origin})
        assert ok.status_code == 200
        cookie = ok.headers["set-cookie"]
        assert "HttpOnly" in cookie and "SameSite=strict" in cookie.lower().replace("samesite=strict", "SameSite=strict")
        assert token not in cookie
        again = session.client.post("/api/v1/auth/bootstrap", headers={**headers, "Origin": session.origin})
        assert again.status_code == 401
        expired = auth.issue_bootstrap()
        clock["now"] += 301
        late = session.client.post("/api/v1/auth/bootstrap",
                                   headers={"Authorization": f"Bearer {expired}", "Origin": session.origin})
        assert late.status_code == 401
        assert session.get("/api/v1/auth/session").status_code == 200
        clock["now"] += 13 * 3600
        assert session.get("/api/v1/auth/session").status_code == 401
    finally:
        application.stop()


def test_writes_require_same_origin_and_csrf(web):
    session, app, project, document = web
    session.login()
    body = {"target_type": "checkpoint", "target_id": "x", "text": "hi"}
    url = f"/api/v1/documents/{document['id']}/annotations"
    assert session.client.post(url, json=body).status_code == 403                      # no Origin, no CSRF
    assert session.client.post(url, json=body, headers={"Origin": session.origin}).status_code == 403
    assert session.client.post(url, json=body, headers={"Origin": "http://evil.example",
                                                        "X-CSRF-Token": session.csrf}).status_code == 403
    assert session.client.post(url, json=body, headers={"Origin": session.origin,
                                                        "X-CSRF-Token": "wrong"}).status_code == 403
    bad_key = session.post(url, json=body, request_id="not-a-uuid")
    assert bad_key.status_code == 400
    response = session.post(url, json=body)
    assert response.status_code == 404      # correct auth; the target does not exist in this document
    assert session.post(url, json={"target_type": "checkpoint", "target_id": "x", "text": "hi", "extra": 1}).status_code == 400
    assert session.client.post(url, content=b"x" * (64 * 1024 + 1),
                               headers=session.write_headers(**{"Content-Type": "application/json"})).status_code == 413
    logout = session.post("/api/v1/auth/logout")
    assert logout.status_code == 200 and logout.json()["data"]["recording_affected"] is False
    assert session.get("/api/v1/status").status_code == 401


def test_unknown_ids_and_forged_ownership_are_404(web):
    session, app, project, document = web
    session.login()
    assert session.get("/api/v1/projects/nope/documents").status_code == 404
    assert session.get("/api/v1/documents/nope/checkpoints").status_code == 404
    assert session.get("/api/v1/jobs/nope").status_code == 404
    other = synthetic_repo(app.state.root.parent / "other repo", document_name="o.gds")
    foreign_checkpoint = other.history(1)[0]["id"]
    response = session.get(f"/api/v1/documents/{document['id']}/checkpoints/{foreign_checkpoint}")
    assert response.status_code == 404 and response.json()["error"]["code"] == "NOT_FOUND"
    response = session.post(f"/api/v1/documents/{document['id']}/checkpoints/{foreign_checkpoint}/downloads")
    assert response.status_code == 404
    response = session.get(f"/api/v1/documents/{document['id']}/events/999999")
    assert response.status_code == 404
    assert "X-Request-ID" in response.headers and response.json()["request_id"] == response.headers["X-Request-ID"]


def test_no_docs_page_no_cors(web):
    session, *_ = web
    session.login()
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert session.get(path).status_code == 404
    preflight = session.client.options("/api/v1/status", headers={"Origin": "http://evil.example",
                                                                  "Access-Control-Request-Method": "GET"})
    assert "access-control-allow-origin" not in {k.lower() for k in preflight.headers}
    assert session.get("/static/../pyproject.toml").status_code in (404, 400)
    assert session.get("/api/v1/status").headers["Cache-Control"] == "no-store"


def test_write_from_localhost_spelling_is_allowed_but_foreign_origin_still_403(web):
    """A page opened as http://localhost:<port> (instead of 127.0.0.1) must still be
    able to write: app.state.origins/allowed_hosts include BOTH spellings of loopback.
    A write from a genuinely foreign origin remains 403 regardless of a valid Host."""
    session, app, project, document = web
    session.login()
    url = f"/api/v1/documents/{document['id']}/annotations"
    body = {"target_type": "checkpoint", "target_id": "x", "text": "hi"}

    response = session.client.post(url, json=body, headers={
        "Host": "localhost:8787", "Origin": "http://localhost:8787", "X-CSRF-Token": session.csrf})
    # Correct auth/origin/csrf: reaches the handler (404, the annotation target does not
    # exist) -- specifically NOT 403/421, which would mean the localhost spelling was rejected.
    assert response.status_code == 404, response.text

    foreign = session.client.post(url, json=body, headers={
        "Host": "127.0.0.1:8787", "Origin": "http://evil.test", "X-CSRF-Token": session.csrf})
    assert foreign.status_code == 403
    assert foreign.json()["error"]["code"] == "FORBIDDEN"


def test_create_app_ipv6_host_produces_bracketed_origin(tmp_path):
    init_state(tmp_path / "state")
    application = open_application(tmp_path / "state")
    try:
        ipv6_app = create_app(application, host="::1", port=9999)
        assert ipv6_app.state.host == "::1"
        assert ipv6_app.state.origin == "http://[::1]:9999"
        assert ipv6_app.state.origins == {"http://[::1]:9999", "http://localhost:9999"}
    finally:
        application.stop()


def test_write_with_chunked_transfer_encoding_is_411(web):
    session, app, project, document = web
    session.login()
    url = f"/api/v1/documents/{document['id']}/annotations"
    response = session.client.post(url, content=b"{}", headers={
        "Origin": session.origin, "X-CSRF-Token": session.csrf, "Content-Type": "application/json",
        "Transfer-Encoding": "chunked"})
    assert response.status_code == 411
    assert response.json()["error"]["code"] == "LENGTH_REQUIRED"


def test_non_ascii_credentials_are_denied_without_exceptions():
    auth=AuthManager();token=auth.issue_bootstrap();session=auth.exchange(token)
    assert auth.exchange("bad"+chr(0x4e2d)) is None
    assert auth.session("bad"+chr(0x4e2d)) is None
    assert not auth.check_csrf(session,"bad"+chr(0x4e2d))


def test_sqlite_corruption_is_a_history_error(web,monkeypatch):
    import sqlite3
    session,app,project,document=web;session.login()
    def corrupt(*a,**kw):raise sqlite3.DatabaseError("private sqlite path")
    monkeypatch.setattr(app,"checkpoints",corrupt)
    response=session.get(f"/api/v1/documents/{document['id']}/checkpoints")
    assert response.status_code==503
    assert response.json()["error"]["code"]=="HISTORY_UNREADABLE"
    assert "private sqlite path" not in response.text


@pytest.mark.parametrize("manifest", ["[]", "null", "{", '{"format":2,"counts":[1]}'])
def test_corrupt_manifest_is_not_reported_as_500_or_missing(web,manifest):
    import sqlite3
    from vestigraph.store import Repository
    session,app,project,document=web;session.login()
    repo=Repository.open_readonly(document["store_path"])
    cid=repo.history()[0]["id"]
    with sqlite3.connect(repo.database) as db:
        db.execute("UPDATE checkpoints SET manifest=? WHERE id=?",(manifest,cid))
    response=session.get(f"/api/v1/documents/{document['id']}/checkpoints/{cid}")
    assert response.status_code==503,response.text
    assert response.json()["error"]["code"]=="HISTORY_UNREADABLE"
