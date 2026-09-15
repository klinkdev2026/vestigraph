"""Offline provider/packaging checks. Never connects to a user's editor."""
import ast
import base64
import importlib
import json
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from vestigraph_backends import EditorBackend
from vestigraph_backends.registry import default_registry, default_renderer
from vestigraph_backends.types import (
    BackendError,
    EventKind,
    ExportRequest,
    OpenRequest,
    ScreenshotRequest,
    SessionDescriptor,
    MAX_EVENT_BYTES,
)
from vestigraph_backends.vesti_backend_klayout.adapter import KLayoutBackend
from vestigraph_backends.vesti_backend_klayout.protocol import CHANNELS
from tests.backend_fakes import image


class Endpoint:
    def __init__(self, root):
        self.filename = str(root / "working.gds")
        self.handlers = {}
        self.calls = []
        self.closed = 0
        self.connected = 0
        self.cell = "TOP"
        self.on_export = lambda: None
        self.open_mode = "success"
        self.failure = None
        self.png = {"data_url": "data:image/png;base64," + base64.b64encode(image()).decode()}

    def connect(self):
        self.connected += 1

    def close(self):
        self.closed += 1

    def on(self, channel, callback):
        self.handlers[channel] = callback

    def off(self, channel, callback):
        if self.handlers.get(channel) is callback:
            del self.handlers[channel]

    def subscribe(self, channels):
        return {"accepted": channels}

    def call(self, method, params, timeout):
        self.calls.append((method, params))
        if self.failure is not None:
            raise self.failure
        if method == "view.list_tabs":
            return {"current_index": 0, "tabs": [{"index": 0, "cellviews": [{
                "index": 0, "filename": self.filename, "active_cell": self.cell, "is_active": True}]}]}
        if method == "events.channels":
            return {"channels": list(CHANNELS)}
        if method == "layout.save_file":
            Path(params["path"]).write_bytes(b"snapshot bytes")
            self.on_export()
            return {"file_size": 14}
        if method == "view.screenshot":
            return self.png
        if method == "layout.show_file":
            if self.open_mode == "timeout":
                raise TimeoutError("secret design path")
            if self.open_mode == "fail":
                return {"success": False}
            if self.open_mode == "success":
                self.filename = params["path"]
            return {}
        if method == "meta.ping":
            return {}
        raise AssertionError(method)


@pytest.fixture
def connected(tmp_path):
    endpoint = Endpoint(tmp_path)
    backend = KLayoutBackend(client_factory=lambda **_: endpoint)
    session = SessionDescriptor("klayout", "session-instance", "KLayout",
                                private_handle={"host": "127.0.0.1", "port": 8765})
    backend.connect(session)
    ref = backend.list_documents()[0].ref
    yield backend, endpoint, ref, session
    backend.close()


def test_factory_is_explicit_connection_free_and_declared():
    registry = default_registry()
    assert registry.backend_ids == ("klayout",)
    backend = registry.create("klayout")
    assert isinstance(backend, EditorBackend)
    assert (backend.backend_id, backend.display_name, backend.api_version) == ("klayout", "KLayout", 1)
    assert not backend.capabilities().export.available
    assert not backend.capabilities().atomic_export.supported


def test_provider_factory_renderer_and_legacy_imports_without_vendor_dependencies(tmp_path):
    root = Path(__file__).resolve().parents[1]
    script = """
import importlib.abc, sys
class Ban(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, *args):
        if fullname.split('.')[0] in ('klink', 'klayout'):
            raise ModuleNotFoundError(fullname)
sys.meta_path.insert(0, Ban())
from vestigraph_backends.registry import default_registry, default_renderer
backend = default_registry().create('klayout')
assert backend.backend_id == 'klayout'
assert not default_renderer().capabilities().available
import vestigraph_backends.vesti_backend_klayout.capture, vestigraph.service.application, vestigraph_backends.vesti_backend_klayout.companion
assert not any(n.split('.')[0] in ('klink', 'klayout') for n in sys.modules)
"""
    result = subprocess.run([sys.executable, "-c", script], cwd=root, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_discovery_is_read_only_loopback_and_pid_scoped(tmp_path):
    class Registry:
        pid = 10
        def list_sessions(self, include_stale):
            return [{"session_id": "window", "pid": self.pid, "rpc_port": 8765},
                    {"session_id": "remote", "host": "10.0.0.1", "rpc_port": 8765}]
    registry = Registry()
    def forbidden(**_):
        raise AssertionError("discovery must not connect")
    backend = KLayoutBackend(session_registry=registry, client_factory=forbidden)
    first = backend.discover({})
    assert len(first) == 1 and first[0].backend_id == "klayout"
    registry.pid = 11
    assert backend.discover({})[0].key != first[0].key


def test_export_once_no_scan_and_best_effort_receipt(connected, tmp_path):
    backend, endpoint, ref, _ = connected
    path = tmp_path / "reserved.gds"
    path.touch()
    receipt = backend.export_snapshot(ref, ExportRequest(path, "GDS2", time.monotonic()+10))
    assert receipt.completed and receipt.document == ref and receipt.observed_revision is None
    assert receipt.consistency == "best_effort" and receipt.artifact_role == "exchange_snapshot"
    assert path.read_bytes() == b"snapshot bytes"
    assert sum(method == "layout.save_file" for method, _ in endpoint.calls) == 1
    assert all(method != "layout.file_info" for method, _ in endpoint.calls)


def test_export_refuses_source_overwrite_cancel_and_non_gds(connected, tmp_path):
    backend, endpoint, ref, _ = connected
    token = threading.Event()
    token.set()
    with pytest.raises(BackendError, match="cancelled"):
        backend.export_snapshot(ref, ExportRequest(tmp_path/"new.gds", "GDS2", time.monotonic()+10, token))
    with pytest.raises(BackendError, match="source_overwrite_refused"):
        backend.export_snapshot(ref, ExportRequest(Path(endpoint.filename), "GDS2", time.monotonic()+10))
    with pytest.raises(BackendError, match="unsupported_format"):
        backend.export_snapshot(ref, ExportRequest(tmp_path/"new.oas", "OASIS", time.monotonic()+10))
    assert not any(method == "layout.save_file" for method, _ in endpoint.calls)


def test_document_switch_and_reconnect_do_not_reuse_ref(connected, tmp_path):
    backend, endpoint, ref, session = connected
    endpoint.filename = str(tmp_path/"other.gds")
    with pytest.raises(BackendError, match="identity_changed"):
        backend.inspect_document(ref)
    backend.connect(session)
    fresh = backend.list_documents()[0].ref
    assert fresh != ref
    with pytest.raises(BackendError, match="identity_changed"):
        backend.inspect_document(ref)


def test_export_cell_switch_rejects_binding(connected, tmp_path):
    backend, endpoint, ref, _ = connected
    endpoint.on_export = lambda: setattr(endpoint, "cell", "OTHER")
    with pytest.raises(BackendError, match="identity_changed"):
        backend.export_snapshot(ref, ExportRequest(tmp_path/"reserved.gds", "GDS2", time.monotonic()+10))


def test_neutral_events_are_bounded_and_close_stops_old_callbacks(connected):
    backend, endpoint, ref, _ = connected
    events = []
    subscription = backend.observe(events.append)
    old_handler = endpoint.handlers["shapes_changed"]
    old_handler({"caused_by": [{"method": "layout.save_file"}]})
    assert events[-1].kind is EventKind.SAVE_NOTICE and events[-1].ref is None
    old_handler({"many": "x" * (MAX_EVENT_BYTES * 2)})
    assert events[-1].kind is EventKind.CONTENT_CHANGED
    assert json.loads(events[-1].vendor_evidence_json)["truncated"]
    subscription.close()
    subscription.close()
    old_handler({})
    assert len(events) == 2
    fresh = backend.observe(events.append)
    late_handler = endpoint.handlers["shapes_changed"]
    backend.close()
    late_handler({})
    assert len(events) == 2
    fresh.close()


def test_screenshot_bound_and_invalid_bytes_are_not_success(connected):
    backend, endpoint, ref, _ = connected
    result = backend.screenshot(ref, ScreenshotRequest(time.monotonic()+10))
    assert result.status == "success" and result.png == image()
    endpoint.png = {"data_url": "data:image/png;base64,AAAA"}
    result = backend.screenshot(ref, ScreenshotRequest(time.monotonic()+10))
    assert result.reason_code == "invalid_image" and result.png is None
    result = backend.screenshot(ref, ScreenshotRequest(time.monotonic()+10, expected_revision="known"))
    assert result.status == "unavailable" and result.reason_code == "revision_unavailable"


@pytest.mark.parametrize("mode,outcome", [("success", "completed"), ("unconfirmed", "unknown"),
                                        ("timeout", "unknown"), ("fail", "failed")])
def test_open_requires_confirmed_document_and_never_overwrites(connected, tmp_path, mode, outcome):
    backend, endpoint, _, _ = connected
    endpoint.open_mode = mode
    receipt = backend.open_snapshot(OpenRequest(tmp_path/"history.gds", "GDS2", time.monotonic()+10))
    assert receipt.outcome == outcome
    params = [params for method, params in endpoint.calls if method == "layout.show_file"]
    assert len(params) == 1 and params[0]["mode"] == "new" and params[0]["keep_position"] is False


def test_post_dispatch_deadline_is_unknown_not_not_started(connected, tmp_path, monkeypatch):
    from vestigraph_backends.vesti_backend_klayout import adapter as adapter
    backend, endpoint, ref, _ = connected
    clock = [1.]
    monkeypatch.setattr(adapter.time, "monotonic", lambda: clock[0])
    endpoint.on_export = lambda: clock.__setitem__(0, 20.)
    with pytest.raises(BackendError) as error:
        backend.export_snapshot(ref, ExportRequest(tmp_path/"history.gds", "GDS2", 10.))
    assert error.value.reason_code == "timeout" and error.value.outcome == "unknown"


def test_errors_never_expose_vendor_exception_text(connected):
    backend, endpoint, ref, _ = connected
    endpoint.failure = TimeoutError("secret_path_and_token")
    with pytest.raises(BackendError) as error:
        backend.inspect_document(ref)
    assert error.value.reason_code == "timeout"
    assert "secret" not in str(error.value)


def test_vendor_imports_and_rpc_calls_are_owned_by_provider():
    root = Path(__file__).resolve().parents[1]
    for path in (root / "vestigraph").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(alias.name.split(".")[0] not in ("klink", "klayout") for alias in node.names), path
            if isinstance(node, ast.ImportFrom):
                assert (node.module or "").split(".")[0] not in ("klink", "klayout"), path
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "call" and node.args and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)):
                assert not node.args[0].value.startswith(("view.", "layout.", "events.", "meta.")), path


def test_navigation_barrier_does_not_filter_transient_coordinator_states():
    from types import SimpleNamespace
    from vestigraph.service.application import Application
    # No status() call is permitted here: the state can change after it returns.
    coordinators = [object(), object(), object()]
    selected = []
    def coordinators_on(session):
        selected.append(session)
        return coordinators
    app = object.__new__(Application)
    app.supervisor = SimpleNamespace(coordinators_on=coordinators_on)
    assert app._coordinators_on("chosen-session") is coordinators
    assert selected == ["chosen-session"]


def test_malformed_document_list_is_a_typed_error(connected, monkeypatch):
    backend, endpoint, _, _ = connected
    monkeypatch.setattr(endpoint, "call", lambda *a, **k: {"tabs": [{"index": 0, "cellviews": None}]})
    with pytest.raises(BackendError, match="invalid_response"):
        backend.list_documents()


def test_repeated_subscriptions_do_not_accumulate_vendor_handlers(connected):
    backend, endpoint, _, _ = connected
    for _ in range(100):
        subscription = backend.observe(lambda event: None)
        assert len(endpoint.handlers) == len(CHANNELS)
        subscription.close()
        assert not endpoint.handlers


def test_client_without_off_requires_reconnection_after_subscription(connected):
    backend, endpoint, _, _ = connected
    endpoint.off = None
    subscription = backend.observe(lambda event: None)
    subscription.close()
    with pytest.raises(BackendError, match="reconnect_required"):
        backend.observe(lambda event: None)


def test_two_untitled_layouts_cannot_inherit_each_others_identity(connected,monkeypatch):
    backend,endpoint,ref,session=connected
    def tabs(names):
        return {"current_index":0,"tabs":[{"index":i,"cellviews":[{"index":0,"filename":None,
            "active_cell":name,"is_active":True}]} for i,name in enumerate(names)]}
    response={"data":tabs(["FIRST"])};original=endpoint.call
    monkeypatch.setattr(endpoint,"call",lambda method,params,timeout:response["data"] if method=="view.list_tabs" else original(method,params,timeout))
    first=backend.list_documents()[0]
    response["data"]=tabs(["FIRST","SECOND"])
    with pytest.raises(BackendError,match="document_ambiguous"):
        backend.list_documents()
    response["data"]=tabs(["SECOND"])
    second=backend.list_documents()[0]
    assert first.ref!=second.ref and first.ownership_key!=second.ownership_key
    with pytest.raises(BackendError):backend.inspect_document(first.ref)
