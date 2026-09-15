"""Fakes for M2 automatic-recording tests: no real registry, no real KLayout.

Pattern follows tests/test_capture_safety.py's ``Endpoint``: one shared state
object per simulated KLayout session/port acts as BOTH the "session record"
data and the fake RPC client (``connect()`` returns itself). The Coordinator's
own probe client and the recorder's client are different Python objects in
production, but here they resolve to the SAME ``FakeEndpoint`` instance so a
test can flip ``filename``/``healthy``/``no_document`` and have both code
paths observe the change immediately -- exactly like a real KLayout session
whose state changes between two RPC calls.
"""
from __future__ import annotations

from pathlib import Path
import threading
import time

from vestigraph_backends.vesti_backend_klayout import capture as capture


class FakeRegistry:
    """Stand-in for klink.mcp.session_registry.SessionRegistry. Mutable, thread-safe."""

    def __init__(self):
        self._lock = threading.Lock()
        self._sessions = {}

    def add(self, session_id, *, host="127.0.0.1", port=8765, pid=4242):
        record = {"session_id": session_id, "host": host, "rpc_port": port,
                  "pid": pid, "last_seen": time.time()}
        with self._lock:
            self._sessions[session_id] = record
        return record

    def remove(self, session_id):
        with self._lock:
            self._sessions.pop(session_id, None)

    def list_sessions(self, include_stale=False):
        with self._lock:
            # The real registry already filters staleness itself; a test that
            # wants "offline" simply removes the record instead of flagging it.
            return [dict(record) for record in self._sessions.values()]


class FakeEndpoint:
    """Shared simulated KLink RPC endpoint for one host:port.

    Set ``healthy = False`` to simulate a dropped connection (connect() and
    call() both raise ConnectionError). Set ``no_document`` to simulate a
    KLayout window with nothing open. Set ``filename`` to the observed active
    document path (``None``/``""`` means unsaved).
    """

    def __init__(self, host="127.0.0.1", port=8765):
        self.host, self.port = host, port
        self.healthy = True
        self.no_document = False
        self.filename = None
        self.cell = "TOP"
        self.channels = list(capture.CHANNELS)
        self.handlers = {}
        self.saves = 0
        self.pings = 0
        self.close_count = 0
        self.export_bytes = None  # None (default) -> distinct bytes per save; set to hold content constant
        self._lock = threading.RLock()

    # --- connection lifecycle ------------------------------------------------
    def connect(self):
        with self._lock:
            if not self.healthy:
                raise ConnectionError(f"fake endpoint {self.host}:{self.port} is unreachable")
            return self

    def close(self):
        with self._lock:
            self.close_count += 1

    def on(self, name, handler):
        with self._lock:
            self.handlers[name] = handler

    def subscribe(self, names):
        return {"accepted": list(names)}

    def emit(self, name="shapes_changed", payload=None):
        """Deliver one event to whatever recorder is currently subscribed.

        Raises KeyError if nothing has registered a handler yet -- callers
        should wait for the "recording" state before emitting.
        """
        with self._lock:
            handler = self.handlers[name]
        handler({} if payload is None else payload)

    # --- RPC surface -----------------------------------------------------------
    def call(self, method, params=None, timeout=None):
        with self._lock:
            if not self.healthy:
                raise ConnectionError(f"fake endpoint {self.host}:{self.port} is unreachable")
            if method == "events.channels":
                return {"channels": list(self.channels)}
            if method == "view.list_tabs":
                if self.no_document:
                    return {"current_index": None, "tabs": []}
                tabs = [{"index": 0, "cellviews": [
                    {"index": 2, "filename": self.filename, "active_cell": self.cell, "is_active": True},
                ]}]
                # Other (non-current) tabs, e.g. a second copy of the same file -> ambiguous ownership.
                for offset, name in enumerate(getattr(self, "extra_tabs", []) or [], start=1):
                    tabs.append({"index": offset, "cellviews": [
                        {"index": 0, "filename": name, "active_cell": "TOP", "is_active": True}]})
                return {"current_index": 0, "tabs": tabs}
            if method == "layout.file_info":
                # What the exported FILE contains, independent of tabs (mirrors klink's file_info).
                return {"top_cells": list(getattr(self, "top_cells", None) or ["TOP"]), "layers": []}
            if method == "layout.save_file":
                path = Path(params["path"])
                assert params["cellview_index"] == 2
                assert path.exists(), "caller must reserve the temp file before calling layout.save_file"
                self.saves += 1
                content = self.export_bytes
                if content is None:
                    content = f"synthetic export {self.port}-{self.saves}".encode()
                path.write_bytes(content)
                return {}
            if method == "meta.ping":
                self.pings += 1
                return {}
            raise AssertionError(f"unexpected RPC {method}")


def make_client_factory(endpoints: dict):
    """Return a ``client_factory(host=..., port=...)`` backed by ``endpoints`` (port -> FakeEndpoint).

    Missing ports are created lazily so a test may either pre-populate
    ``endpoints`` or let the first probe create the entry.
    """

    def factory(host="127.0.0.1", port=8765, **_ignored):
        endpoint = endpoints.get(port)
        if endpoint is None:
            endpoint = endpoints[port] = FakeEndpoint(host, port)
        return endpoint

    return factory


def wait_for(predicate, timeout=10.0, step=0.02, status_fn=None):
    """Poll ``predicate()`` (no args) until it returns something truthy; return that value.

    Never sleeps a fixed duration for correctness: raises AssertionError with
    the last observed status (via ``status_fn()``, if given) on timeout.
    """
    deadline = time.monotonic() + timeout
    while True:
        result = predicate()
        if result:
            return result
        if time.monotonic() >= deadline:
            extra = ""
            if status_fn is not None:
                try:
                    extra = f" last observed: {status_fn()!r}"
                except Exception as exc:  # noqa: BLE001 - diagnostics must never mask the real failure
                    extra = f" (status_fn raised {exc!r})"
            raise AssertionError(f"condition not satisfied within {timeout}s.{extra}")
        time.sleep(step)
