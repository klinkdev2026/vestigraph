"""Real-browser acceptance of the panel (Playwright + a locally installed Chromium-based browser).

Skips when Playwright or a browser is unavailable. Everything served is synthetic:
a fake KLayout endpoint and a synthetic legacy history. Screenshots (if any)
go to scratch/ (existing coverage) or test_outputs/ui_m7/ (new M7 navigation
screenshots) and contain no user data.

Navigation model under test (hash-routed, one page for every KLayout window):
    #                          -> windows: every online KLayout window, by port
    #session=<id>              -> window: the layouts open in one window
    #docs                      -> docs: every layout that has history (any project)
    #doc=<id>[&session=<sid>]  -> doc: the saved-version timeline for one layout
"""


from __future__ import annotations


import json


import socket


import threading


import time


from pathlib import Path


import pytest


from tests import gds_fixtures as fx


from tests.autocapture_fakes import FakeEndpoint, FakeRegistry, make_client_factory, wait_for


from tests.service_helpers import init_state, synthetic_repo


from vestigraph.service.application import Application


from vestigraph.service.state import now_iso


from vestigraph.store import Repository


from vestigraph.web.app import create_app


from vestigraph.web.auth import AuthManager


playwright = pytest.importorskip("playwright.sync_api")


uvicorn = pytest.importorskip("uvicorn")


SCREENS = Path(__file__).parents[1] / "scratch" / "browser-screens"


UI_SCREENS = Path(__file__).parents[1] / "test_outputs" / "ui_m7"


UI_R4 = Path(__file__).parents[1] / "test_outputs" / "ui_r4"


@pytest.fixture(scope="module")
def browser():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        launched = None
        for kwargs in ({"channel": "msedge"}, {"channel": "chrome"}, {}):
            try:
                launched = p.chromium.launch(headless=True, **kwargs)
                break
            except Exception:
                continue
        if launched is None:
            pytest.skip("no Chromium-based browser available for Playwright")
        yield launched
        launched.close()


class GdsEndpoint(FakeEndpoint):
    """Fake KLayout whose exports are real (tiny) GDS files so previews can render.

    ``state`` selects the synthetic content: 0 empty TOP, 1 +box, 2 +path, 3 +polygon.
    Also answers layout.show_file so open-in-KLayout can be exercised.
    """

    def __init__(self, host, port, layout_path):
        super().__init__(host, port)
        self.layout_path = Path(layout_path)
        self.state = 0
        self.opened = []
        self.write()

    def write(self):
        db = pytest.importorskip("klayout.db")
        ly = db.Layout()
        ly.dbu = 0.001
        top = ly.create_cell("TOP")
        l1 = ly.layer(101, 0)
        if self.state >= 1:
            top.shapes(l1).insert(db.Box(0, 0, 1000, 500))
        if self.state >= 2:
            top.shapes(l1).insert(db.Path([db.Point(0, 0), db.Point(1000, 500)], 100))
        if self.state >= 3:
            top.shapes(l1).insert(db.Polygon([db.Point(0, 0), db.Point(500, 0), db.Point(500, 500)]))
        ly.write(str(self.layout_path))

    def call(self, method, params=None, timeout=None):
        if method == "layout.save_file":
            self.write()
            Path(params["path"]).write_bytes(self.layout_path.read_bytes())
            self.saves += 1
            return {}
        if method == "layout.show_file":
            self.opened.append(dict(params))
            return {"loaded": params["path"], "type": "open"}
        return super().call(method, params, timeout)


@pytest.fixture
def stack(tmp_path):
    """Real service + real HTTP server on a free loopback port, fake KLayout."""
    workspace = tmp_path / "ws 工作区"
    workspace.mkdir()
    layout = workspace / "demo 芯片.gds"
    registry = FakeRegistry()
    endpoint = GdsEndpoint("127.0.0.1", 8765, layout)
    endpoint.filename = str(layout)
    endpoints = {8765: endpoint}          # mutable: tests may register more windows via add_window()
    registry.add("klayout-8765", port=8765)
    init_state(tmp_path / "state")
    app = Application.open(tmp_path / "state", registry=registry, client_factory=make_client_factory(endpoints),
                           autocapture=True, coordinator_options={"document_poll_s": 0.05, "session_poll_s": 0.05}).start()
    project = app.catalog.add_project("演示项目 demo", workspace, tmp_path / "history", allow_inside_git=True)
    app.catalog.set_policy(project["id"], {"intervals": {"idle_seconds": 1, "min_interval": 1, "max_interval": 2}})
    app.supervisor.on_project_added(app.catalog.get_project(project["id"]))
    legacy = synthetic_repo(tmp_path / "legacy", checkpoints=4, events=3, document_name="旧版 chip.gds")
    legacy_doc = app.catalog.add_history(project["id"], legacy.root)
    auth = AuthManager()
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    web = create_app(app, host="127.0.0.1", port=port, auth=auth)
    server = uvicorn.Server(uvicorn.Config(web, host="127.0.0.1", port=port, log_level="error", access_log=False))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.started
    wait_for(lambda: app.supervisor.project_status(project["id"])["state"] == "recording", timeout=15)
    yield {"app": app, "auth": auth, "base": f"http://127.0.0.1:{port}", "project": project,
           "legacy_doc": legacy_doc, "legacy": legacy, "endpoint": endpoint, "registry": registry,
           "endpoints": endpoints, "workspace": workspace}
    server.should_exit = True
    thread.join(10)
    app.stop()


def sign_in(browser, stack, viewport=None):
    # bypass_csp only affects Playwright's own eval helpers; the CSP header is asserted in test_web_static.
    context = browser.new_context(viewport=viewport or {"width": 1280, "height": 900}, locale="zh-CN", bypass_csp=True)
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
    token = stack["auth"].issue_bootstrap()
    page.goto(f"{stack['base']}/#bootstrap={token}")
    page.wait_for_selector("#main:not([hidden])", timeout=15000)
    assert "bootstrap" not in page.url                       # token left the address bar
    page.errors = errors
    return context, page


def add_window(stack, port, filename, state=0):
    """Register a second/third fake KLayout window in the same project's workspace and wait
    for the automatic recorder to reach the ``recording`` state for it."""
    layout = stack["workspace"] / filename
    endpoint = GdsEndpoint("127.0.0.1", port, layout)
    endpoint.state = state
    endpoint.filename = str(layout)
    stack["endpoints"][port] = endpoint
    stack["registry"].add(f"klayout-{port}", port=port)

    def recording_now():
        for session in stack["app"].sessions():
            if session["port"] == port:
                rec = session["recording"][0] if session["recording"] else None
                return bool(rec and rec["state"] == "recording")
        return False

    wait_for(recording_now, timeout=20)
    return endpoint


def live_document(stack, filename_fragment=None):
    docs = [d for d in stack["app"].catalog.list_documents(stack["project"]["id"]) if d["origin"] == "live"]
    if filename_fragment is None:
        return docs[0]
    return next(d for d in docs if filename_fragment in d["name"])


@pytest.fixture
def stack_no_delta(tmp_path):
    """A minimal service + HTTP server with the storage_delta capability forced OFF (see
    Application.open(storage_delta=...)/Supervisor's autocapture-style override), so the
    "install the delta encoder" hint can be exercised regardless of whether bsdiff4 actually
    happens to be installed in the environment running this test. No fake KLayout window is
    needed: only a static imported (read-only) history is navigated."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    init_state(tmp_path / "state")
    app = Application.open(tmp_path / "state", storage_delta=False).start()
    project = app.catalog.add_project("p", workspace, tmp_path / "history", allow_inside_git=True)
    legacy = synthetic_repo(tmp_path / "legacy", checkpoints=2, events=1, document_name="chip.gds")
    legacy_doc = app.catalog.add_history(project["id"], legacy.root)
    auth = AuthManager()
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    web = create_app(app, host="127.0.0.1", port=port, auth=auth)
    server = uvicorn.Server(uvicorn.Config(web, host="127.0.0.1", port=port, log_level="error", access_log=False))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.started
    assert app.capabilities()["storage_delta"] is False
    yield {"app": app, "auth": auth, "base": f"http://127.0.0.1:{port}", "project": project, "legacy_doc": legacy_doc}
    server.should_exit = True
    thread.join(10)
    app.stop()


def _open_recording_document(page):
    """Drive the panel from sign-in to the live document's timeline (state.documentId bound)."""
    page.wait_for_function("document.querySelectorAll('.window-card').length === 1", timeout=15000)
    page.locator(".window-card").first.locator("button", has_text="进入").click()
    page.wait_for_function("!document.getElementById('view-window').hidden", timeout=10000)
    page.wait_for_selector("#window-documents .item", timeout=10000)
    page.click("#window-documents .item")
    page.wait_for_selector("#checkpoints .item", timeout=10000)
    page.wait_for_function("document.getElementById('state-pill').dataset.state === 'recording'", timeout=15000)


def _freeze_coordinator(app, project):
    """Pause this window's coordinator (session scope) so its own loop stops touching state,
    then return it for direct ._set() injection."""
    coordinator = app.supervisor.coordinator(project["id"])
    coordinator.wait_reply(coordinator.request("pause", reason="frozen-for-test", scope="session"), 30)
    return coordinator


def _unfreeze_coordinator(coordinator):
    if coordinator is not None:
        coordinator.wait_reply(coordinator.request("resume"), 30)


__all__ = [name for name in globals() if not name.startswith("__")]
