"""Synchronous interface; caller owns scheduling, history, spool and permissions."""
from abc import ABC, abstractmethod
from typing import Callable, ClassVar, Mapping
from vestigraph_backends.types import (
    API_VERSION,
    Availability,
    BackendCapabilities,
    BackendEvent,
    DocumentDescriptor,
    DocumentRef,
    DocumentState,
    ExportReceipt,
    ExportRequest,
    OpenReceipt,
    OpenRequest,
    ScreenshotRequest,
    ScreenshotResult,
    SessionDescriptor,
)


class Subscription(ABC):
    @abstractmethod
    def close(self) -> None:
        """Idempotent; after return no callbacks from this subscription may run."""


class EditorBackend(ABC):
    api_version: ClassVar[int] = API_VERSION
    backend_id: ClassVar[str]

    @abstractmethod
    def discover(self, config: Mapping[str, object]) -> tuple[SessionDescriptor, ...]:
        """Read authorized local discovery configuration; never start software."""

    @abstractmethod
    def connect(self, session: SessionDescriptor) -> None: ...

    @abstractmethod
    def close(self) -> None:
        """Release this session, never close the user's editor."""

    @abstractmethod
    def health(self) -> Availability: ...

    @abstractmethod
    def capabilities(self, document: DocumentRef | None = None) -> BackendCapabilities: ...

    @abstractmethod
    def list_documents(self) -> tuple[DocumentDescriptor, ...]: ...

    @abstractmethod
    def inspect_document(self, document: DocumentRef) -> DocumentState: ...

    @abstractmethod
    def observe(self, callback: Callable[[BackendEvent], None]) -> Subscription: ...

    @abstractmethod
    def export_snapshot(self, document: DocumentRef, request: ExportRequest) -> ExportReceipt:
        """Only write allocated destination; no store commit, scan or hash."""

    @abstractmethod
    def screenshot(self, document: DocumentRef, request: ScreenshotRequest) -> ScreenshotResult: ...

    @abstractmethod
    def open_snapshot(self, request: OpenRequest) -> OpenReceipt: ...

    def flush_observed_changes(self, document: DocumentRef) -> None:
        """Optional event barrier; providers without delayed notifications need no work."""
