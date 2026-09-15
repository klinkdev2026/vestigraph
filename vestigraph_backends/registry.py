"""Explicit lazy factories. Registration never imports or probes a provider."""
from collections.abc import Callable
from vestigraph_backends.base import EditorBackend
from vestigraph_backends.types import API_VERSION, BackendError, _identifier


class BackendRegistry:
    def __init__(self):
        self._factories: dict[str, Callable[[], EditorBackend]] = {}

    def register(self, backend_id: str, factory: Callable[[], EditorBackend]) -> None:
        _identifier(backend_id)
        if backend_id in self._factories or not callable(factory):
            raise ValueError("Duplicate or invalid backend factory.")
        self._factories[backend_id] = factory

    @property
    def backend_ids(self):
        return tuple(self._factories)

    def create(self, backend_id: str) -> EditorBackend:
        factory = self._factories.get(backend_id)
        if factory is None:
            raise BackendError("unknown_backend", outcome="not_started")
        try:
            backend = factory()
        except BackendError:
            raise
        except ImportError:
            raise BackendError("dependency_unavailable", outcome="not_started") from None
        except Exception:
            raise BackendError("provider_initialization_failed", outcome="not_started") from None
        if (not isinstance(backend, EditorBackend) or getattr(backend, "backend_id", None) != backend_id
                or type(getattr(backend, "api_version", None)) is not int or backend.api_version != API_VERSION):
            # Factories must be connection-free; no side-effecting close on an incompatible object.
            raise BackendError("incompatible_backend", outcome="not_started")
        return backend


def default_registry() -> BackendRegistry:
    """Explicit built-in registration; no dependency import until create()."""
    registry = BackendRegistry()
    registry.register("klayout", _klayout_factory)
    return registry


def _klayout_factory():
    from vestigraph_backends.vesti_backend_klayout import create_backend
    return create_backend()


def default_renderer():
    # Separate from editor creation: offline rendering must never open a connection.
    from vestigraph_backends.vesti_backend_klayout.vesti_klayout_rendering.adapter import KLayoutRenderer
    return KLayoutRenderer()


def legacy_capabilities():
    """Compatibility capability envelope for the existing single-provider UI."""
    from vestigraph_backends.vesti_backend_klayout.capabilities import legacy_capabilities as probe
    return probe()


def configured_registry(*, backend_registry=None, session_registry=None, client_factory=None):
    """Composition/old-injection boundary. Production code receives only factories."""
    if backend_registry is not None:
        if session_registry is not None or client_factory is not None:
            raise ValueError("Do not mix a backend registry with old endpoint injection.")
        return backend_registry
    registry = BackendRegistry()
    def create():
        from vestigraph_backends.vesti_backend_klayout.adapter import KLayoutBackend
        return KLayoutBackend(client_factory=client_factory, session_registry=session_registry)
    registry.register("klayout", create)
    return registry
