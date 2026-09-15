"""KLayout host/port recording entry for CLI and desktop UI.

Endpoint composition delegates all recording to the shared capture runtime.
"""
import json
import uuid
from vestigraph.capture_runtime import Observer, run_observer, QUEUE_SIZE
from vestigraph.capture_errors import CaptureError, DOCUMENT_CHANGED
from vestigraph_backends.types import SessionDescriptor, BackendEvent
from vestigraph_backends.vesti_backend_klayout.adapter import KLayoutBackend, _event
from vestigraph_backends.vesti_backend_klayout.protocol import (
    CHANNELS,
    _validate_endpoint,
    _client_factory,
)


class KLayoutObserver(Observer):
    def __init__(self, repo, client, host, port, *, expected=None, **options):
        host, port = _validate_endpoint(host, port)
        self.expected_document = expected
        backend = KLayoutBackend(client_factory=lambda **_: client)
        session = SessionDescriptor("klayout", uuid.uuid4().hex, "KLayout",
                                    private_handle={"host": host, "port": port})
        super().__init__(repo, backend, session, **options)

    def _select_document(self):
        selected = super()._select_document()
        if self.expected_document is not None:
            identity = json.loads(selected.evidence_json)
            identity["filename"] = selected.source_path
            if any(identity.get(key) != self.expected_document.get(key)
                   for key in ("tab_index", "cellview_index", "filename")):
                raise CaptureError(DOCUMENT_CHANGED, "Active document changed before recording started.")
        return selected

    def consume(self, event, data=None):
        return super().consume(event if isinstance(event, BackendEvent) else _event(event, data))

    def callback(self, channel):
        return lambda data: self.enqueue(_event(channel, data))


def observe(repo, host="127.0.0.1", port=8765, idle_seconds=5,
            min_interval=15, max_interval=60, duration=None, stop_event=None,
            client_factory=None, *, capture_context=None, on_status=None, commands=None,
            expected_document=None, cancel_event=None, durable_capture=False, spool_options=None,
            observer_factory=None, queue_size=None):
    host, port = _validate_endpoint(host, port)
    if observer_factory is None:
        observer_factory = KLayoutObserver
    if queue_size is None:
        queue_size = QUEUE_SIZE
    if expected_document is not None and not isinstance(expected_document, dict):
        raise ValueError("expected_document must be a dict.")
    if capture_context is not None and not isinstance(capture_context, dict):
        raise ValueError("capture_context must be a dict of ids.")
    if on_status is not None and not callable(on_status):
        raise ValueError("on_status must be callable.")
    observer = observer_factory(repo, _client_factory(client_factory)(host=host, port=port), host, port,
                                context=capture_context, on_status=on_status, expected=expected_document,
                                cancel_event=cancel_event, durable_capture=durable_capture,
                                spool_options=spool_options, queue_size=queue_size)
    return run_observer(observer, idle_seconds=idle_seconds, min_interval=min_interval,
                        max_interval=max_interval, duration=duration, stop_event=stop_event, commands=commands)
