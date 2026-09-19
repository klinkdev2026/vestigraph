"""Service-level M4 tests: preview job/cache, open-in-klayout navigation
hold/release, unknown-on-timeout, and sweep_assets.

No browser; drives vestigraph.service.application.Application directly, with
tests/autocapture_fakes.py fakes standing in for the KLayout RPC endpoint --
same pattern as tests/test_autocapture_service.py and scratch/m4_smoke.py
(whose Endpoint subclass and write_layout this file reuses/adapts).
"""
from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

import pytest

pytest.importorskip("klayout.db")
import klayout.db as db

from tests.autocapture_fakes import FakeEndpoint, FakeRegistry, make_client_factory, wait_for
from tests.service_helpers import init_state, open_application, project_dirs
from vestigraph.preview.budgets import Budgets
from vestigraph.service.application import Application
from vestigraph.service.errors import ServiceError
from vestigraph.service.agent import invoke as agent_invoke
from vestigraph.store import Repository

FAST = {"document_poll_s": 0.05, "session_poll_s": 0.05}


def write_layout(path, n):
    """dbu 0.001, layer 101/0: empty -> +Box -> +Path -> +Polygon (scratch/m4_smoke.py)."""
    ly = db.Layout()
    ly.dbu = 0.001
    top = ly.create_cell("TOP")
    l1 = ly.layer(101, 0)
    if n >= 1:
        top.shapes(l1).insert(db.Box(0, 0, 1000, 500))
    if n >= 2:
        top.shapes(l1).insert(db.Path([db.Point(0, 0), db.Point(1000, 500)], 100))
    if n >= 3:
        top.shapes(l1).insert(db.Polygon([db.Point(0, 0), db.Point(500, 0), db.Point(500, 500)]))
    ly.write(str(path))


class Endpoint(FakeEndpoint):
    """scratch/m4_smoke.py's Endpoint: layout.save_file exports the CURRENT
    synthetic state (self.state); layout.show_file records what was opened,
    or times out / fails when asked to."""

    def __init__(self, layout_path, *a, **k):
        super().__init__(*a, **k)
        self.layout_path = layout_path
        self.opened = []
        self.hang_open = False
        self.fail_open = False
        self.state = 0

    def call(self, method, params=None, timeout=None):
        if method == "layout.save_file":
            write_layout(self.layout_path, self.state)
            shutil.copy(self.layout_path, params["path"])
            self.saves += 1
            return {}
        if method == "layout.show_file":
            if self.hang_open:
                raise TimeoutError("timed out")
            if self.fail_open:
                return {"success": False}
            self.opened.append(params)
            self.filename = params["path"]
            self.no_document = False
            return {"loaded": params["path"], "type": "open"}
        return super().call(method, params, timeout)


def _wait_status(app, project_id, predicate, timeout=10.0):
    holder = {}

    def check():
        holder["last"] = status = app.supervisor.project_status(project_id)
        return status if predicate(status) else None

    return wait_for(check, timeout=timeout, status_fn=lambda: holder.get("last"))


def _build_recording(tmp_path, name="p"):
    """Real Coordinator + fake KLink endpoint, recording a workspace layout."""
    workspace, history = project_dirs(tmp_path, name)
    layout_path = workspace / "chip.gds"
    write_layout(layout_path, 0)
    registry = FakeRegistry()
    endpoint = Endpoint(layout_path, "127.0.0.1", 8765)
    endpoint.filename = str(layout_path)
    registry.add(f"klayout-{name}", port=8765)
    state_root = tmp_path / "state"
    init_state(state_root)
    app = Application.open(state_root, registry=registry, client_factory=make_client_factory({8765: endpoint}),
                           autocapture=True, coordinator_options=FAST).start()
    project = app.catalog.add_project(name, workspace, history, allow_inside_git=True)
    app.catalog.set_policy(project["id"], {"intervals": {"idle_seconds": 1, "min_interval": 1, "max_interval": 2}})
    app.supervisor.on_project_added(app.catalog.get_project(project["id"]))
    status = _wait_status(app, project["id"], lambda s: s["state"] == "recording")
    document = app.catalog.get_document(status["document_id"])
    return app, project, document, endpoint, registry


def _emit_checkpoints(document, endpoint, count=1):
    """Emit shapes_changed for states 1..count; return checkpoints oldest-first."""
    for n in range(1, count + 1):
        endpoint.state = n
        endpoint.emit("shapes_changed", {"n": n})
        wait_for(lambda: Repository.open_readonly(document["store_path"]).counts()["checkpoints"] >= n + 1,
                 timeout=15)
    return list(reversed(Repository.open_readonly(document["store_path"]).history()))


def _document_with_checkpoints(tmp_path, name="prev", states=(3,), fmt="GDS2"):
    """A live document with N real GDS checkpoints, no coordinator involved."""
    workspace, history = project_dirs(tmp_path, name)
    state_root = tmp_path / "state"
    init_state(state_root)
    app = open_application(state_root)
    project = app.catalog.add_project(name, workspace, history, allow_inside_git=True)
    layout_path = workspace / "chip.gds"
    document = app.catalog.add_live_document(project["id"], "chip.gds", {"kind": "saved", "path": str(layout_path)})
    repo = Repository(document["store_path"])
    checkpoints = []
    for i, n in enumerate(states):
        write_layout(layout_path, n)
        metadata = {"format": fmt, "document": {"filename": str(layout_path)}}
        checkpoints.append(repo.checkpoint(layout_path, title=f"v{i}", metadata=metadata))
    return app, project, document, checkpoints


# =========================================================================== #
#                                   preview                                   #
# =========================================================================== #
def test_preview_job_succeeds_asset_matches_state_and_caches_by_options(tmp_path):
    app, project, document, cps = _document_with_checkpoints(tmp_path, "prev1", states=(3,))
    try:
        job = app.request_preview(document["id"], cps[0]["id"], {})
        assert app.runner.wait_idle(30)
        done = app.job(job["id"])
        assert done["status"] == "succeeded"

        path, filename, content_type = app.job_asset(job["id"])
        assert filename == "preview.json"
        assert content_type == "application/json"
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        preview = payload["preview"]
        assert sorted(i["kind"] for i in preview["items"]) == ["box", "path", "polygon"]
        assert done["result"]["cached"] is False

        # Same checkpoint + same (empty) options: cached.
        job2 = app.request_preview(document["id"], cps[0]["id"], {})
        assert job2["id"] != job["id"]  # a new job row, but its result is a cache hit
        assert app.runner.wait_idle(30)
        assert app.job(job2["id"])["result"]["cached"] is True

        # Different options (viewport_dbu): not a cache hit.
        job3 = app.request_preview(document["id"], cps[0]["id"], {"viewport_dbu": [0, 0, 10, 10]})
        assert app.runner.wait_idle(30)
        done3 = app.job(job3["id"])
        assert done3["status"] == "succeeded"
        assert done3["result"]["cached"] is False
    finally:
        app.stop()


def test_preview_cross_document_checkpoint_id_not_found(tmp_path):
    app, project, doc_a, cps_a = _document_with_checkpoints(tmp_path, "cross", states=(1,))
    try:
        layout_path = Path(project["workspace"]) / "other.gds"
        write_layout(layout_path, 1)
        doc_b = app.catalog.add_live_document(project["id"], "other.gds", {"kind": "saved", "path": str(layout_path)})
        with pytest.raises(ServiceError) as excinfo:
            app.request_preview(doc_b["id"], cps_a[0]["id"], {})
        assert excinfo.value.code == "NOT_FOUND"
        assert excinfo.value.status == 404
    finally:
        app.stop()


def test_preview_oasis_format_refused_before_any_job(tmp_path):
    app, project, document, cps = _document_with_checkpoints(tmp_path, "oasis1", states=(1,), fmt="OASIS")
    try:
        with app.catalog._db() as raw:
            jobs_before = raw.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        with pytest.raises(ServiceError) as excinfo:
            app.request_preview(document["id"], cps[0]["id"], {})
        assert excinfo.value.code == "PREVIEW_UNSUPPORTED_FORMAT"
        assert excinfo.value.status == 409
        with app.catalog._db() as raw:
            jobs_after = raw.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        assert jobs_after == jobs_before
    finally:
        app.stop()


def test_preview_size_over_budget_refused(tmp_path):
    app, project, document, cps = _document_with_checkpoints(tmp_path, "big1", states=(1,))
    try:
        app.budgets = Budgets(max_source_bytes=10)
        with pytest.raises(ServiceError) as excinfo:
            app.request_preview(document["id"], cps[0]["id"], {})
        assert excinfo.value.code == "PREVIEW_TOO_LARGE"
        assert excinfo.value.status == 413
    finally:
        app.stop()


# =========================================================================== #
#                              open in KLayout                                #
# =========================================================================== #
def test_record_then_navigate_holds_opens_and_resumes(tmp_path):
    app, project, document, endpoint, registry = _build_recording(tmp_path, "nav1")
    try:
        cps = _emit_checkpoints(document, endpoint, count=2)
        target = cps[-1]  # latest
        runs_before = app.catalog.list_runs(document["id"])
        first_run_id = runs_before[0]["id"]
        assert runs_before[0]["status"] == "running"
        history_before = Repository.open_readonly(document["store_path"]).history()

        job = app.request_open_in_klayout(document["id"], target["id"],
                                          {"session_id": "klayout-nav1", "confirm_new_tab": True})
        assert app.runner.wait_idle(60)
        done = app.job(job["id"])
        assert done["status"] == "succeeded", done.get("error")

        # Exactly one layout.show_file call, mode "new", under state/cache/history_preview.
        assert len(endpoint.opened) == 1
        assert endpoint.opened[0]["mode"] == "new"
        opened_path = Path(done["result"]["opened_path"])
        assert (app.state.cache_dir / "history_preview") in opened_path.parents

        # Exported bytes equal Repository.export of that checkpoint.
        export_check = tmp_path / "export_check.gds"
        Repository.open_readonly(document["store_path"]).export(target["id"], export_check)
        assert opened_path.read_bytes() == export_check.read_bytes()

        # The first run was drained ("closed") with a navigation note.
        first_run = app.catalog.get_run(first_run_id)
        assert first_run["status"] == "closed"
        assert any("navigation" in n["note"].lower() for n in first_run["notes"])

        # Coordinator resumes within the window: a new run appears, or state is recording/waiting_document.
        # (Generous timeout: under a full-suite run, thread scheduling for the
        # coordinator's own ~1s poll cycle can be delayed by unrelated load.)
        def resumed():
            status = app.supervisor.project_status(project["id"])
            more_runs = len(app.catalog.list_runs(document["id"])) > len(runs_before)
            return status if (status["state"] in ("recording", "waiting_document") or more_runs) else None
        wait_for(resumed, timeout=20)

        # KLayout now shows the history file in the new tab: never recorded.
        endpoint.filename = str(opened_path)
        status = _wait_status(app, project["id"],
                              lambda s: s["state"] == "waiting_document" and s["reason"] == "document_is_service_output",
                              timeout=30)
        assert len(app.catalog.list_documents(project["id"])) == 1  # no document created for the history tab

        # User switches back to the working document.
        endpoint.filename = str(Path(project["workspace"]) / "chip.gds")
        status = _wait_status(app, project["id"], lambda s: s["state"] == "recording", timeout=30)
        assert status["document_id"] == document["id"]
        assert len(app.catalog.list_runs(document["id"])) > len(runs_before)

        # The working document's history store still has all its checkpoints
        # (count unchanged or grown -- new baselines only, nothing removed).
        history_after = Repository.open_readonly(document["store_path"]).history()
        assert len(history_after) >= len(history_before)
        old_ids = {c["id"] for c in history_before}
        current_ids = {c["id"] for c in history_after}
        assert old_ids <= current_ids
    finally:
        app.stop()


def test_restore_appends_checkpoint_and_preserves_all_history(tmp_path):
    app, project, document, endpoint, registry = _build_recording(tmp_path, "restore1")
    try:
        checkpoints = _emit_checkpoints(document, endpoint, count=3)
        target = checkpoints[0]
        before = Repository.open_readonly(document["store_path"]).history()
        before_ids = {item["id"] for item in before}
        expected = tmp_path / "expected.gds"
        Repository.open_readonly(document["store_path"]).export(target["id"], expected)

        # The fake editor must reflect the bytes it will reload. A real KLayout
        # reload derives this state from the restored working file.
        endpoint.state = 0
        result = agent_invoke(app, "restore", {
            "document_id": document["id"],
            "checkpoint_id": target["id"],
            "session_id": "klayout-restore1",
            "reason": "User requested the starting layout",
        }, {project["id"]})
        assert result["restored"] is True
        assert result["restore_of"] == target["id"]
        assert result["history_preserved"] is True
        assert endpoint.opened[-1]["mode"] == "replace"
        assert Path(document["observed_identity"]["path"]).read_bytes() == expected.read_bytes()

        after = Repository.open_readonly(document["store_path"]).history()
        after_ids = {item["id"] for item in after}
        assert before_ids <= after_ids
        restored = next(item for item in after if item["id"] == result["checkpoint_id"])
        assert restored["metadata"]["restore_of"] == target["id"]
        assert restored["metadata"]["operation"] == {
            "method": "vestigraph.restore",
            "reason": "User requested the starting layout",
        }
        assert restored["metadata"]["coverage"] == "exact_file_restore"
        assert restored["id"] not in before_ids
    finally:
        app.stop()


def test_restore_checkpoint_failure_rolls_back_working_file(tmp_path, monkeypatch):
    app, project, document, endpoint, registry = _build_recording(tmp_path, "restore-rollback")
    try:
        checkpoints = _emit_checkpoints(document, endpoint, count=2)
        target = checkpoints[0]
        source_path = Path(document["observed_identity"]["path"])
        before_bytes = source_path.read_bytes()
        before_ids = {item["id"] for item in Repository.open_readonly(document["store_path"]).history()}
        original = Repository.checkpoint

        def fail_restore(self, path, *args, **kwargs):
            if (kwargs.get("metadata") or {}).get("restore_of"):
                raise RepositoryError("synthetic restore commit failure")
            return original(self, path, *args, **kwargs)

        monkeypatch.setattr(Repository, "checkpoint", fail_restore)
        endpoint.state = 0
        job = app.request_restore_in_editor(document["id"], target["id"], {
            "session_id": "klayout-restore-rollback",
            "reason": "Rollback test",
        })
        assert app.runner.wait_idle(60)
        done = app.job(job["id"])
        assert done["status"] == "failed"
        assert source_path.read_bytes() == before_bytes
        after_ids = {item["id"] for item in Repository.open_readonly(document["store_path"]).history()}
        assert before_ids <= after_ids
        assert not any((item.get("metadata") or {}).get("restore_of") == target["id"]
                       for item in Repository.open_readonly(document["store_path"]).history())
    finally:
        app.stop()


def test_navigate_then_record_does_not_stick_paused(tmp_path):
    workspace, history = project_dirs(tmp_path, "nav2")
    layout_path = workspace / "chip.gds"
    write_layout(layout_path, 0)
    registry = FakeRegistry()
    endpoint = Endpoint(layout_path, "127.0.0.1", 8765)
    endpoint.no_document = True
    registry.add("klayout-nav2", port=8765)
    state_root = tmp_path / "state"
    init_state(state_root)
    app = Application.open(state_root, registry=registry, client_factory=make_client_factory({8765: endpoint}),
                           autocapture=True, coordinator_options=FAST).start()
    try:
        project = app.catalog.add_project("nav2", workspace, history, allow_inside_git=True)
        app.supervisor.on_project_added(app.catalog.get_project(project["id"]))
        _wait_status(app, project["id"], lambda s: s["state"] == "waiting_document")

        # Seed a document/checkpoint to navigate to (no coordinator writer held).
        identity = {"kind": "saved", "path": str(layout_path.resolve())}
        document = app.catalog.add_live_document(project["id"], "chip.gds", identity)
        repo = Repository(document["store_path"])
        checkpoint = repo.checkpoint(layout_path, title="seed", metadata={"format": "GDS2"})

        job = app.request_open_in_klayout(document["id"], checkpoint["id"],
                                          {"session_id": "klayout-nav2", "confirm_new_tab": True})
        assert app.runner.wait_idle(60)
        assert app.job(job["id"])["status"] == "succeeded"

        endpoint.no_document = False
        endpoint.filename = str(layout_path)
        status = _wait_status(app, project["id"], lambda s: s["state"] == "recording", timeout=20)
        assert status["state"] != "paused"
    finally:
        app.stop()


def test_tail_save_failure_blocks_navigation_and_coordinator_recovers(tmp_path):
    """navigation_hold must drain the recorder's dirty tail before opening a
    history tab. If that drain's export fails, the open must NOT proceed
    (no layout.show_file call), the job must fail with TAIL_NOT_SAVED, and
    the coordinator must not get stuck in "paused" -- it should keep working
    toward reconnecting/recording once KLink is healthy again."""
    app, project, document, endpoint, registry = _build_recording(tmp_path, "nav6")
    try:
        checkpoint_id = Repository.open_readonly(document["store_path"]).history()[0]["id"]

        # Make the recorder dirty (an unsaved edit observed since the baseline).
        endpoint.emit("shapes_changed", {"n": 1})
        wait_for(lambda: any(e["kind"] == "shapes_changed"
                             for e in Repository.open_readonly(document["store_path"]).events(limit=50)),
                 timeout=10)

        original_call = endpoint.call

        def failing_save(method, params=None, timeout=None):
            if method == "layout.save_file":
                raise RuntimeError("disk full")
            return original_call(method, params, timeout)

        endpoint.call = failing_save

        job = app.request_open_in_klayout(document["id"], checkpoint_id,
                                          {"session_id": "klayout-nav6", "confirm_new_tab": True})
        assert app.runner.wait_idle(30)
        done = app.job(job["id"])
        assert done["status"] == "failed"
        assert done["error"]["code"] == "TAIL_NOT_SAVED"
        details = done["error"].get("details") or {}
        if "recorder_error" in details:
            assert details["recorder_error"] == "EXPORT_FAILED"
        assert endpoint.opened == []          # never reached layout.show_file

        # The coordinator is not stuck "paused": the failed drain already
        # ended the run, so it is working toward reconnecting on its own.
        status = wait_for(
            lambda: (app.supervisor.project_status(project["id"])
                     if app.supervisor.project_status(project["id"])["state"]
                     in ("reconnecting", "waiting_document", "recording") else None),
            timeout=20)
        assert status["state"] != "paused"

        # Restore layout.save_file: a fresh open request now succeeds and navigates.
        endpoint.call = original_call
        _wait_status(app, project["id"], lambda s: s["state"] == "recording", timeout=20)
        job2 = app.request_open_in_klayout(document["id"], checkpoint_id,
                                           {"session_id": "klayout-nav6", "confirm_new_tab": True})
        assert app.runner.wait_idle(30)
        done2 = app.job(job2["id"])
        assert done2["status"] == "succeeded", done2.get("error")
        assert len(endpoint.opened) == 1
        assert endpoint.opened[0]["mode"] == "new"
        opened_path = Path(done2["result"]["opened_path"])
        assert opened_path.is_file()
    finally:
        app.stop()


def test_open_timeout_gives_unknown_and_is_idempotent_then_hard_failure(tmp_path):
    app, project, document, endpoint, registry = _build_recording(tmp_path, "nav3")
    try:
        cps = _emit_checkpoints(document, endpoint, count=1)
        checkpoint_id = cps[-1]["id"]

        endpoint.hang_open = True
        job = app.request_open_in_klayout(document["id"], checkpoint_id,
                                          {"session_id": "klayout-nav3", "confirm_new_tab": True},
                                          request_key="open-req-1")
        assert app.runner.wait_idle(60)
        done = app.job(job["id"])
        assert done["status"] == "unknown"
        assert done["error"]["code"] == "OPEN_UNCONFIRMED"

        # Coordinator resumes (never stuck paused) even after the unconfirmed timeout.
        wait_for(lambda: app.supervisor.project_status(project["id"])["state"] != "paused", timeout=20)

        # Same request_key -> the SAME job, no second navigation attempt.
        job2 = app.request_open_in_klayout(document["id"], checkpoint_id,
                                           {"session_id": "klayout-nav3", "confirm_new_tab": True},
                                           request_key="open-req-1")
        assert job2["id"] == job["id"]
        assert endpoint.opened == []  # never succeeded in opening anything

        # An explicit rejection from show_file -> job failed with OPEN_FAILED.
        endpoint.hang_open = False
        endpoint.fail_open = True
        job3 = app.request_open_in_klayout(document["id"], checkpoint_id,
                                           {"session_id": "klayout-nav3", "confirm_new_tab": True},
                                           request_key="open-req-2")
        assert app.runner.wait_idle(60)
        done3 = app.job(job3["id"])
        assert done3["status"] == "failed"
        assert done3["error"]["code"] == "OPEN_FAILED"
    finally:
        app.stop()


def test_open_validation_errors(tmp_path):
    app, project, document, endpoint, registry = _build_recording(tmp_path, "nav4")
    try:
        cps = _emit_checkpoints(document, endpoint, count=1)
        checkpoint_id = cps[-1]["id"]

        with pytest.raises(ServiceError) as excinfo:
            app.request_open_in_klayout(document["id"], checkpoint_id,
                                        {"session_id": "does-not-exist", "confirm_new_tab": True})
        assert excinfo.value.code == "SESSION_NOT_FOUND"
        assert excinfo.value.status == 404

        with pytest.raises(ServiceError) as excinfo:
            app.request_open_in_klayout(document["id"], checkpoint_id, {"session_id": "klayout-nav4"})
        assert excinfo.value.code == "BAD_REQUEST"

        with pytest.raises(ServiceError) as excinfo:
            app.request_open_in_klayout(document["id"], checkpoint_id,
                                        {"session_id": "klayout-nav4", "confirm_new_tab": True,
                                         "expected_session_instance": "99999999"})
        assert excinfo.value.code == "SESSION_CHANGED"
        assert excinfo.value.status == 409
    finally:
        app.stop()


def test_open_capability_unavailable_without_autocapture(tmp_path):
    state_root = tmp_path / "state"
    init_state(state_root)
    app = open_application(state_root)  # no registry given -> autocapture off
    try:
        workspace, history = project_dirs(tmp_path, "nav5")
        project = app.catalog.add_project("nav5", workspace, history, allow_inside_git=True)
        layout_path = workspace / "chip.gds"
        write_layout(layout_path, 1)
        document = app.catalog.add_live_document(project["id"], "chip.gds", {"kind": "saved", "path": str(layout_path)})
        repo = Repository(document["store_path"])
        checkpoint = repo.checkpoint(layout_path, title="seed", metadata={"format": "GDS2"})
        with pytest.raises(ServiceError) as excinfo:
            app.request_open_in_klayout(document["id"], checkpoint["id"],
                                        {"session_id": "whatever", "confirm_new_tab": True})
        assert excinfo.value.code == "CAPABILITY_UNAVAILABLE"
        assert excinfo.value.status == 503
    finally:
        app.stop()


# =========================================================================== #
#                                sweep_assets                                  #
# =========================================================================== #
def test_sweep_assets_removes_old_downloads_previews_and_stale_history_preview(tmp_path):
    state_root = tmp_path / "state"
    init_state(state_root)
    app = open_application(state_root)
    try:
        cache = app.state.cache_dir

        old_download = cache / "downloads" / "job1"
        old_download.mkdir(parents=True)
        download_file = old_download / "file.bin"
        download_file.write_bytes(b"x")
        old_download_time = time.time() - 2 * 3600  # 2h > ASSET_TTL_S (1h)
        os.utime(download_file, (old_download_time, old_download_time))

        old_preview = cache / "previews" / "sha-1"
        old_preview.mkdir(parents=True)
        preview_file = old_preview / "key.json"
        preview_file.write_bytes(b"{}")
        old_preview_time = time.time() - 8 * 86400  # 8d > PREVIEW_TTL_S (7d)
        os.utime(preview_file, (old_preview_time, old_preview_time))

        history_preview = cache / "history_preview" / "jobX"       # 30 days old: past the 7-day TTL
        history_preview.mkdir(parents=True)
        history_file = history_preview / "chip.gds"
        history_file.write_bytes(b"x")
        very_old_time = time.time() - 30 * 86400
        os.utime(history_file, (very_old_time, very_old_time))
        fresh_preview = cache / "history_preview" / "jobY"         # opened just now: kept
        fresh_preview.mkdir(parents=True)
        (fresh_preview / "chip.gds").write_bytes(b"y")

        outside_marker = tmp_path / "outside_marker.txt"
        outside_marker.write_bytes(b"do not touch")

        removed = app.sweep_assets()
        assert removed == 3
        assert not old_download.exists()
        assert not old_preview.exists()
        assert not history_preview.exists()                          # stale export swept (review finding 8)
        assert fresh_preview.is_dir()
        assert outside_marker.is_file()
    finally:
        app.stop()


def test_closed_clean_document_does_not_leave_a_failed_tail(tmp_path):
    app,project,document,endpoint,registry=_build_recording(tmp_path,"clean-close")
    try:
        record=Repository.open_readonly(document["store_path"]).history()[0]
        endpoint.no_document=True
        _wait_status(app,project["id"],lambda s:s["reason"]=="no_document",timeout=20)
        coord=app.supervisor.coordinators(project["id"])[0]
        assert coord.last_end_error is None
        job=app.request_open_in_klayout(document["id"],record["id"],
            {"session_id":"klayout-clean-close","confirm_new_tab":True})
        assert app.runner.wait_idle(60)
        assert app.job(job["id"])["status"]=="succeeded"
    finally:app.stop()
