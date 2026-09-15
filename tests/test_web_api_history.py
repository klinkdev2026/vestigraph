"""Behavior tests; shared fixtures live in fixtures_web_api_history."""
from tests.fixtures_web_api_history import *

def test_status_projects_documents(web):
    session, app, project, document, repo = web
    status = data(session.get("/api/v1/status"))
    assert status["capabilities"]["remote_access"] is False and status["projects"][0]["id"] == project["id"]
    assert status["capabilities"]["adaptive_compression"] is True
    assert status["capabilities"]["gpu_compression"] is False
    assert status["compression"]["model"] == "compression-cost-v2"
    assert status["compression"]["scope"] == "service_process"
    assert status["compression"]["gpu"]["eligible"] is False
    assert status["projects"][0]["capture_state"] in ("waiting_session", "blocked")
    projects = data(session.get("/api/v1/projects"))["items"]
    assert projects[0]["policy"]["enabled"] is True and "workspace" not in projects[0]
    documents = data(session.get(f"/api/v1/projects/{project['id']}/documents"))["items"]
    assert documents[0]["id"] == document["id"] and documents[0]["read_only"] is True
    assert documents[0]["capture_state"] == "read_only" and documents[0]["checkpoint_count"] == 3
    assert documents[0]["last_checkpoint_id"] == repo.history(1)[0]["id"]
    assert "store_path" not in documents[0]
    project_status = data(session.get(f"/api/v1/projects/{project['id']}/status"))
    assert project_status["policy_version"] == 1 and project_status["state"] in ("waiting_session", "blocked")


def test_policy_put_with_version_and_no_paths(web, tmp_path):
    session, app, project, document, repo = web
    url = f"/api/v1/projects/{project['id']}/policy"
    updated = data(session.put(url, json={"enabled": False, "expected_policy_version": 1}))
    assert updated["policy"]["enabled"] is False and updated["policy_version"] == 2
    assert updated["capture_state"] == "disabled"
    conflict = session.put(url, json={"enabled": True, "expected_policy_version": 1})
    assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "POLICY_VERSION_CONFLICT"
    assert session.put(url, json={"session_selector": {"mode": "pinned", "session_id": str(tmp_path / "evil")}}).status_code == 400
    assert session.put(url, json={"workspace": str(tmp_path / "x")}).status_code == 400


def test_lists_paginate_with_scope_bound_cursors(web):
    session, app, project, document, repo = web
    base = f"/api/v1/documents/{document['id']}"
    page = data(session.get(f"{base}/checkpoints?limit=2"))
    assert len(page["items"]) == 2 and page["next_cursor"] and page["window_id"]
    latest = repo.history(1)[0]
    assert page["items"][0]["title"] == latest["title"] and page["items"][0]["document_filename"] == document["name"]
    assert page["items"][0]["format"] == "GDS2" and "manifest" not in page["items"][0]
    rest = data(session.get(f"{base}/checkpoints?limit=2&cursor={page['next_cursor']}"))
    oldest = repo.history(3)[-1]
    assert [i["title"] for i in rest["items"]] == [oldest["title"]] and rest["next_cursor"] is None
    # The cursor belongs to (document, checkpoints); anything else is rejected.
    wrong_kind = session.get(f"{base}/events?cursor={page['next_cursor']}")
    assert wrong_kind.status_code == 400 and wrong_kind.json()["error"]["code"] == "CURSOR_SCOPE_MISMATCH"
    other = app.catalog.add_history(project["id"], synthetic_repo(app.state.root.parent / "o", document_name="o.gds").root)
    wrong_doc = session.get(f"/api/v1/documents/{other['id']}/checkpoints?cursor={page['next_cursor']}")
    assert wrong_doc.status_code == 400 and wrong_doc.json()["error"]["code"] == "CURSOR_SCOPE_MISMATCH"
    assert session.get(f"{base}/checkpoints?cursor=%%%").status_code == 400
    assert session.get(f"{base}/checkpoints?limit=201").status_code == 400
    segments = data(session.get(f"{base}/segments"))["items"]
    assert segments[0]["checkpoint_count"] == 1 and segments[0]["event_count"] == 2 and segments[0]["status"] == "closed"
    filtered = data(session.get(f"{base}/checkpoints?segment_id={segments[0]['id']}"))["items"]
    assert len(filtered) == 1 and filtered[0]["segment_id"] == segments[0]["id"]
    events = data(session.get(f"{base}/events?limit=3"))
    assert len(events["items"]) == 3 and events["items"][0]["truncated"] is False
    assert "payload" not in events["items"][0] and events["items"][0]["summary"]["cell"] == "TOP"
    full = data(session.get(f"{base}/events/{events['items'][0]['seq']}"))
    assert full["payload"] == {"count": 1, "cell": "TOP"}
    detail = data(session.get(f"{base}/checkpoints/{page['items'][0]['id']}"))
    assert detail["chunk_count"] == 1 and "manifest" not in detail and detail["metadata"]["format"] == "GDS2"


def test_ten_thousand_and_one_events_paginate_without_full_load(tmp_path):
    init_state(tmp_path / "state")
    application = open_application(tmp_path / "state")
    try:
        auth = AuthManager()
        session = WebSession(create_app(application, port=8787, auth=auth), auth)
        session.login()
        workspace, history = project_dirs(tmp_path)
        project = application.catalog.add_project("p", workspace, history, allow_inside_git=True)
        from vestigraph.store import Repository
        repo = Repository.init(tmp_path / "big")
        segment = repo.begin_segment("big")["id"]
        bulk_events(repo, segment, 10001)
        document = application.catalog.add_history(project["id"], repo.root)
        before = tree_hashes(repo.root)
        seen, cursor, pages = [], None, 0
        while True:
            url = f"/api/v1/documents/{document['id']}/events?limit=200" + (f"&cursor={cursor}" if cursor else "")
            page = data(session.get(url))
            pages += 1
            seen.extend(i["seq"] for i in page["items"])
            if pages == 2:
                repo.append_event("late", {}, segment_id=segment)      # must not shift the window
            cursor = page["next_cursor"]
            if cursor is None:
                break
        assert len(seen) == 10001 and len(set(seen)) == 10001 and pages == 51
        assert tree_hashes(repo.root) != before          # the deliberate late append is the only write
        after_write = tree_hashes(repo.root)
        segments = data(session.get(f"/api/v1/documents/{document['id']}/segments"))["items"]
        assert segments[0]["event_count"] == 10002
        fresh = data(session.get(f"/api/v1/documents/{document['id']}/events?limit=1"))["items"]
        assert fresh[0]["kind"] == "late"
        assert tree_hashes(repo.root) == after_write     # reading through the service changed nothing
    finally:
        application.stop()


def test_annotations_roundtrip(web):
    session, app, project, document, repo = web
    base = f"/api/v1/documents/{document['id']}"
    checkpoint = repo.history(1)[0]["id"]
    created = data(session.post(f"{base}/annotations",
                                json={"target_type": "checkpoint", "target_id": checkpoint, "text": "第一次调整 <b>x</b>"}))
    assert created["text"] == "第一次调整 <b>x</b>"
    listed = data(session.get(f"{base}/annotations?target_type=checkpoint&target_id={checkpoint}"))["items"]
    assert [a["id"] for a in listed] == [created["id"]]
    assert session.post(f"{base}/annotations", json={"target_type": "segment", "target_id": "nope", "text": "x"}).status_code == 404
    assert repo.history(1)[0]["metadata"] == {"capture": "klink", "format": "GDS2", "document": {"filename": str(repo.root.parent / "workspace" / document["name"])}}


def test_download_job_and_asset_headers(web):
    session, app, project, document, repo = web
    base = f"/api/v1/documents/{document['id']}"
    checkpoint = repo.history(1)[0]
    key = "22222222-2222-2222-2222-222222222222"
    accepted = session.post(f"{base}/checkpoints/{checkpoint['id']}/downloads", request_id=key)
    assert accepted.status_code == 202
    job_id = accepted.json()["data"]["job_id"]
    assert session.post(f"{base}/checkpoints/{checkpoint['id']}/downloads", request_id=key).json()["data"]["job_id"] == job_id
    other = repo.history(2)[1]["id"]
    conflict = session.post(f"{base}/checkpoints/{other}/downloads", request_id=key)
    assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert app.runner.wait_idle(30)
    job = data(session.get(f"/api/v1/jobs/{job_id}"))
    assert job["status"] == "succeeded" and job["result"]["filename"] == document["name"]
    asset = session.get(f"/api/v1/jobs/{job_id}/asset")
    assert asset.status_code == 200
    assert asset.content == repo.export(checkpoint["id"], app.state.root.parent / "ref.gds").read_bytes()
    from urllib.parse import quote
    disposition = asset.headers["content-disposition"]
    assert f"filename*=UTF-8''{quote(document['name'])}" in disposition and "\n" not in disposition
    assert asset.headers["cache-control"] == "no-store"


def test_unimplemented_capabilities_are_explicit(web):
    session, app, project, document, repo = web
    base = f"/api/v1/documents/{document['id']}"
    checkpoint = repo.history(1)[0]["id"]
    for method, url, body in (
        ("post", f"{base}/checkpoints/{checkpoint}/open-in-klayout", {"session_id": "klayout-8765"}),
        ("post", f"/api/v1/projects/{project['id']}/pause", {"reason": "x"}),       # autocapture off in tests
        ("post", f"/api/v1/projects/{project['id']}/resume", None),
    ):
        response = session.post(url, json=body)
        assert response.status_code == 503, url
        error = response.json()["error"]
        assert error["code"] == "CAPABILITY_UNAVAILABLE" and error["message_key"] and error["next_action"]
    # A named save needs a live document; an imported read-only history is refused, not faked.
    refused = session.post(f"{base}/milestones", json={"title": "里程碑"})
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "DOCUMENT_READ_ONLY"
    sessions = session.get("/api/v1/sessions")
    assert sessions.status_code in (200, 503)
    if sessions.status_code == 200:
        for item in sessions.json()["data"]["items"]:
            assert item["loopback"] is True


def test_annotation_with_request_id_is_stored_once(web):
    """Review round 4: a double click sends the same X-Request-ID twice; one note must result."""
    session, app, project, document, repo = web
    checkpoint = repo.history(limit=1)[0]["id"]
    base = f"/api/v1/documents/{document['id']}"
    body = {"target_type": "checkpoint", "target_id": checkpoint, "text": "once"}
    key = "11111111-2222-4333-8444-555555555555"
    first = data(session.post(f"{base}/annotations", json=body, request_id=key))
    second = data(session.post(f"{base}/annotations", json=body, request_id=key))
    assert first["id"] == second["id"]
    items = data(session.get(f"{base}/annotations?target_type=checkpoint&target_id={checkpoint}"))["items"]
    assert [a["text"] for a in items] == ["once"]
    third = data(session.post(f"{base}/annotations", json=body))          # no key: a new note
    assert third["id"] != first["id"]


def test_checkpoint_changes_unavailable_and_scope(web):
    session, app, project, document, repo = web
    base = f"/api/v1/documents/{document['id']}"
    checkpoints = repo.history(3)  # newest first
    oldest, newest = checkpoints[-1], checkpoints[0]

    # first saved version: a change root exists but there is nothing to compare against yet.
    baseline = data(session.get(f"{base}/checkpoints/{oldest['id']}/changes"))
    assert baseline["status"] == "baseline" and baseline["items"] == [] and baseline["next_cursor"] is None

    # synthetic_repo's payload is not real GDS, so later versions can't be cell-indexed:
    # "unavailable" is a normal 200 result, not an error.
    unavailable = data(session.get(f"{base}/checkpoints/{newest['id']}/changes"))
    assert unavailable["status"] == "unavailable" and unavailable["items"] == []

    # a checkpoint id from a DIFFERENT document is not found under this document's scope.
    other = app.catalog.add_history(project["id"], synthetic_repo(app.state.root.parent / "o2", document_name="o2.gds").root)
    cross = session.get(f"/api/v1/documents/{other['id']}/checkpoints/{oldest['id']}/changes")
    assert cross.status_code == 404 and cross.json()["error"]["code"] == "NOT_FOUND"

    # an unknown checkpoint id under the right document is also not found.
    missing = session.get(f"{base}/checkpoints/{'0' * 32}/changes")
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "NOT_FOUND"


def test_checkpoint_changes_paging_and_limit_clamp(web, tmp_path):
    session, app, project, document, repo = web
    n = 50
    gds_repo = Repository.init(tmp_path / "gds-history")
    source = tmp_path / "layout.gds"
    source.write_bytes(fx.sample_many_cells(n, stamp=fx.timestamps(second=1)))
    first = gds_repo.checkpoint(source, title="v0")
    source.write_bytes(fx.sample_many_cells_edit(n, "modify", stamp=fx.timestamps(second=2)))
    second = gds_repo.checkpoint(source, title="v1")
    gds_document = app.catalog.add_history(project["id"], gds_repo.root)
    base = f"/api/v1/documents/{gds_document['id']}/checkpoints/{second['id']}/changes"

    normal = data(session.get(f"{base}?limit=5"))
    assert normal["status"] == "complete"
    assert 0 < len(normal["items"]) <= 5
    assert normal["entry_count"] == n + 1
    assert normal["next_cursor"]

    # limit is clamped to the store's max (200), not rejected.
    clamped = data(session.get(f"{base}?limit=100000"))
    assert clamped["status"] == "complete"
    assert len(clamped["items"]) <= 200
    assert len(clamped["items"]) == n + 1  # every entry fits in one page once clamped to 200

    seen = list(normal["items"])
    cursor = normal["next_cursor"]
    while cursor:
        page = data(session.get(f"{base}?limit=5&cursor={cursor}"))
        seen.extend(page["items"])
        cursor = page["next_cursor"]
    assert len(seen) == n + 1
    assert any(e["kind"] == "cell.changed" for e in seen)

    only = data(session.get(f"{base}?kind=cell.changed&limit=200"))
    assert [e["kind"] for e in only["items"]] == ["cell.changed"]

    # an invalid/foreign cursor is a caller mistake -> 400, not a 500 or a 503.
    bad = session.get(f"{base}?cursor=someone-else:0:0")
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "BAD_REQUEST"


def test_change_summaries_route_orders_by_request_and_reports_availability(web, tmp_path):
    session, app, project, document, repo = web
    n = 20
    gds_repo = Repository.init(tmp_path / "gds-history-summaries")
    source = tmp_path / "layout.gds"
    source.write_bytes(fx.sample_many_cells(n, stamp=fx.timestamps(second=1)))
    first = gds_repo.checkpoint(source, title="v0")
    source.write_bytes(fx.sample_many_cells_edit(n, "modify", stamp=fx.timestamps(second=2)))
    second = gds_repo.checkpoint(source, title="v1")
    gds_document = app.catalog.add_history(project["id"], gds_repo.root)
    base = f"/api/v1/documents/{gds_document['id']}"

    # request order is [second, first]: the response must NOT silently sort by history order.
    body = {"checkpoint_ids": [second["id"], first["id"]]}
    result = data(session.post(f"{base}/changes/summaries", json=body))
    items = result["items"]
    assert [i["checkpoint_id"] for i in items] == [second["id"], first["id"]]
    assert items[0]["has_entries"] is True and items[0]["coverage"]["status"] == "complete"
    assert items[0]["change_id"] and items[0]["algorithm"]
    assert items[0]["input_ids"] == {"from": first["id"], "to": second["id"]}
    assert items[1]["has_entries"] is False and items[1]["coverage"]["status"] == "baseline"
    assert items[1]["parent_id"] is None
    assert result["diagnostics"]["object_reads"] == 0


def test_change_summaries_route_scope_and_duplicates(web):
    session, app, project, document, repo = web
    base = f"/api/v1/documents/{document['id']}"
    checkpoints = repo.history(3)  # newest first
    ids = [c["id"] for c in checkpoints]

    ok = data(session.post(f"{base}/changes/summaries", json={"checkpoint_ids": ids}))
    assert [i["checkpoint_id"] for i in ok["items"]] == ids
    # synthetic_repo payload is not real GDS: only the oldest (baseline) has entries possible,
    # later ones carry a root but stay "unavailable" -- still very different from "no root at all".
    assert ok["items"][-1]["coverage"]["status"] == "baseline"
    for later in ok["items"][:-1]:
        assert later["coverage"]["status"] == "unavailable"
        assert later["change_id"] is not None       # a root exists, unlike the "no change record" case

    dup = session.post(f"{base}/changes/summaries", json={"checkpoint_ids": [ids[0], ids[0]]})
    assert dup.status_code == 400 and dup.json()["error"]["code"] == "BAD_REQUEST"

    other = app.catalog.add_history(
        project["id"], synthetic_repo(app.state.root.parent / "o3", document_name="o3.gds").root)
    cross = session.post(f"/api/v1/documents/{other['id']}/changes/summaries", json={"checkpoint_ids": [ids[0]]})
    assert cross.status_code == 404 and cross.json()["error"]["code"] == "NOT_FOUND"

    missing = session.post(f"{base}/changes/summaries", json={"checkpoint_ids": ["0" * 32]})
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "NOT_FOUND"

    empty = session.post(f"{base}/changes/summaries", json={"checkpoint_ids": []})
    assert empty.status_code == 400

    too_many = session.post(f"{base}/changes/summaries", json={"checkpoint_ids": [ids[0]] * 101})
    assert too_many.status_code == 400


def test_change_summaries_route_100_ids_stays_under_the_size_guard(web, tmp_path):
    session, app, project, document, repo = web
    gds_repo = Repository.init(tmp_path / "gds-history-100")
    source = tmp_path / "layout.gds"
    ids = []
    for i in range(100):
        source.write_bytes(fx.sample_hierarchy(fx.timestamps(second=i)))
        ids.append(gds_repo.checkpoint(source, title=f"v{i}")["id"])
    gds_document = app.catalog.add_history(project["id"], gds_repo.root)
    base = f"/api/v1/documents/{gds_document['id']}"

    response = session.post(f"{base}/changes/summaries", json={"checkpoint_ids": ids})
    assert response.status_code == 200
    body = response.json()
    assert [i["checkpoint_id"] for i in body["data"]["items"]] == ids
    assert len(response.content) < 1024 * 1024


def test_cancel_save_route_needs_a_live_recording(web):
    session, app, project, document, repo = web
    response = session.post(f"/api/v1/documents/{document['id']}/save/cancel", json={})
    assert response.status_code == 409 and response.json()["error"]["code"] in ("NOT_RECORDING_DOCUMENT", "DOCUMENT_READ_ONLY")
    missing = session.post("/api/v1/documents/nope/save/cancel", json={})
    assert missing.status_code == 404
