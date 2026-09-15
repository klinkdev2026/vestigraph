"""Real local store + simulated RPC endpoint. No running editor is contacted."""


from pathlib import Path


import threading


from unittest.mock import patch


import pytest


from vestigraph_backends.vesti_backend_klayout import capture as capture


from vestigraph.store import Repository


class Endpoint:
    def __init__(self):
        self.handlers = {}
        self.filename = "test.gds"
        self.cell = "TOP"
        self.saves = self.pings = 0
        self.closed = False
        self.on_ping = self.on_save = lambda endpoint: None
        self.channels = list(capture.CHANNELS)

    def connect(self):
        return self

    def close(self):
        self.closed = True

    def on(self, name, handler):
        self.handlers[name] = handler

    def subscribe(self, names):
        return {"accepted": names}

    def emit(self, name="shapes_changed", payload=None):
        self.handlers[name]({} if payload is None else payload)

    def call(self, method, params=None, timeout=None):
        if method == "events.channels":
            return {"channels": self.channels}
        if method == "view.list_tabs":
            return {"current_index": 0, "tabs": [{"index": 0, "cellviews": [
                {"index": 2, "filename": self.filename, "active_cell": self.cell, "is_active": True},
            ]}]}
        if method == "layout.save_file":
            assert params["cellview_index"] == 2
            assert Path(params["path"]).exists()  # Caller owns the reserved file.
            self.saves += 1
            self.on_save(self)
            Path(params["path"]).write_bytes(f"synthetic export {self.saves}".encode())
            return {}
        if method == "meta.ping":
            self.pings += 1
            self.on_ping(self)
            return {}
        raise AssertionError(f"Unexpected RPC {method}")


@pytest.fixture
def harness(tmp_path):
    repo, endpoint, stopped = Repository.init(tmp_path / "history"), Endpoint(), threading.Event()

    def run(**kwargs):
        options = dict(duration=2, idle_seconds=.005, min_interval=.005,
                       max_interval=.02, stop_event=stopped,
                       client_factory=lambda **unused: endpoint)
        options.update(kwargs)
        return capture.observe(repo, **options)

    return repo, endpoint, stopped, run


__all__ = [name for name in globals() if not name.startswith("__")]
