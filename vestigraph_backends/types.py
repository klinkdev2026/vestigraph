"""Backend API v1: IDs are opaque; a software session is not a history ID."""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
import json
import math
from pathlib import Path
from typing import Protocol

API_VERSION = 1
MAX_EVENT_BYTES = 16 * 1024


class BackendError(RuntimeError):
    """Stable local reason, never a raw vendor error message."""
    def __init__(self, reason_code: str, *, retryable=False, outcome="failed"):
        if not isinstance(reason_code, str) or not reason_code or len(reason_code) > 64 or not all(
                c in "abcdefghijklmnopqrstuvwxyz_0123456789" for c in reason_code):
            raise ValueError("Invalid backend reason code.")
        if outcome not in ("not_started", "failed", "unknown"):
            raise ValueError("Invalid backend outcome.")
        super().__init__(reason_code)
        self.reason_code, self.retryable, self.outcome = reason_code, bool(retryable), outcome


class Availability(str, Enum):
    ONLINE = "online"
    OFFLINE = "offline"
    UNKNOWN = "unknown"


class EventKind(str, Enum):
    DOCUMENT_CHANGED = "document_changed"
    CONTENT_CHANGED = "content_changed"
    SAVE_NOTICE = "save_notice"
    OPERATION_STARTED = "operation_started"
    OPERATION_FINISHED = "operation_finished"
    SELECTION_CHANGED = "selection_changed"
    PROGRESS = "progress"
    CONNECTION_LOST = "connection_lost"


def _identifier(value):
    if not isinstance(value, str) or not value or len(value) > 256 or any(ord(c) < 32 for c in value):
        raise ValueError("Expected a bounded opaque identifier.")


def _reject_json_constant(_):
    raise ValueError("Nonfinite JSON.")


@dataclass(frozen=True)
class SessionDescriptor:
    backend_id: str
    session_instance_id: str
    display_name: str
    availability: Availability = Availability.UNKNOWN
    private_handle: object = field(default=None, repr=False, compare=False)
    alias: str | None = None
    public_metadata_json: str = field(default="{}", repr=False)
    legacy_instance_aliases: tuple[str, ...] = ()

    def __post_init__(self):
        for value in (self.backend_id, self.session_instance_id, self.display_name):
            _identifier(value)
        if self.alias is not None:
            _identifier(self.alias)
        _metadata(self.public_metadata_json)
        for value in self.legacy_instance_aliases:
            _identifier(value)

    @property
    def key(self):
        return self.backend_id, self.session_instance_id


@dataclass(frozen=True)
class DocumentRef:
    backend_id: str
    session_instance_id: str
    document_instance_id: str

    def __post_init__(self):
        for value in (self.backend_id, self.session_instance_id, self.document_instance_id):
            _identifier(value)


@dataclass(frozen=True)
class DocumentDescriptor:
    ref: DocumentRef
    display_name: str
    active: bool
    source_path: str | None = None
    source_uri: str | None = None
    format_hint: str | None = None
    identity_strength: str = "unknown"
    view_name: str | None = None
    evidence_json: str = field(default="{}", repr=False)
    ownership_key: str | None = None

    def __post_init__(self):
        _metadata(self.evidence_json)
        if self.ownership_key is not None:
            _identifier(self.ownership_key)


@dataclass(frozen=True)
class DocumentState:
    ref: DocumentRef
    revision: str | None = None
    identity_strength: str = "unknown"
    diagnostics: tuple[str, ...] = ()
    view_name: str | None = None


@dataclass(frozen=True)
class Capability:
    supported: bool = False
    available: bool = False
    reason_code: str | None = "unsupported"

    def __post_init__(self):
        if type(self.supported) is not bool or type(self.available) is not bool:
            raise ValueError("Capability flags must be booleans.")
        if self.available and not self.supported:
            raise ValueError("Unsupported capability cannot be available.")


@dataclass(frozen=True)
class BackendCapabilities:
    events: Capability = Capability()
    export_formats: tuple[str, ...] = ()
    export: Capability = Capability()
    screenshot: Capability = Capability()
    open_snapshot: Capability = Capability()
    stable_document_handle: Capability = Capability()
    atomic_export: Capability = Capability()
    atomic_screenshot_binding: Capability = Capability()
    cancel_export: Capability = Capability()
    native_project_restore: Capability = Capability()
    open_formats: tuple[str, ...] = ()

    def __post_init__(self):
        for name in ("events", "export", "screenshot", "open_snapshot", "stable_document_handle",
                     "atomic_export", "atomic_screenshot_binding", "cancel_export", "native_project_restore"):
            if not isinstance(getattr(self, name), Capability):
                raise ValueError("Expected a typed capability.")
        for formats in (self.export_formats, self.open_formats):
            if not isinstance(formats, tuple) or len(formats) > 64:
                raise ValueError("Expected a bounded tuple of formats.")
            for value in formats:
                _identifier(value)


class CancelToken(Protocol):
    def is_set(self) -> bool: ...


def _deadline(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("Deadline must be a finite monotonic timestamp.")


@dataclass(frozen=True)
class ExportRequest:
    destination: Path
    format: str
    deadline: float
    cancel: CancelToken | None = field(default=None, repr=False, compare=False)
    include_structure_summary: bool = False

    def __post_init__(self):
        if not isinstance(self.destination, Path) or not self.destination.is_absolute():
            raise ValueError("Core must allocate an absolute export destination.")
        _identifier(self.format)
        _deadline(self.deadline)


@dataclass(frozen=True)
class ExportReceipt:
    document: DocumentRef
    actual_format: str
    artifact_role: str
    completed: bool
    reported_size: int | None
    observed_revision: str | None
    started_at: str
    finished_at: str
    consistency: str = "best_effort"
    limitations: tuple[str, ...] = ()
    evidence_json: str = field(default="{}", repr=False)
    top_cells: tuple[str, ...] | None = None

    def __post_init__(self):
        if not isinstance(self.document, DocumentRef) or type(self.completed) is not bool:
            raise ValueError("Export must identify its document and completion explicitly.")
        _identifier(self.actual_format)
        if self.artifact_role not in ("exchange_snapshot", "file_snapshot"):
            raise ValueError("This contract only supports single-file exchange snapshots.")
        _metadata(self.evidence_json)
        if self.reported_size is not None and (type(self.reported_size) is not int or self.reported_size < 0):
            raise ValueError("Invalid exported size.")


@dataclass(frozen=True)
class ScreenshotRequest:
    deadline: float
    width: int = 1280
    height: int = 720
    max_bytes: int = 2 * 1024 * 1024
    expected_revision: str | None = None

    def __post_init__(self):
        _deadline(self.deadline)
        for value, limit in ((self.width, 1920), (self.height, 1080), (self.max_bytes, 2*1024*1024)):
            if type(value) is not int or not 1 <= value <= limit:
                raise ValueError("Screenshot budget exceeds the core contract.")


@dataclass(frozen=True)
class ScreenshotResult:
    status: str
    document: DocumentRef
    captured_at: str
    png: bytes | None = field(default=None, repr=False)
    observed_revision: str | None = None
    binding: str = "best_effort_after_export"
    view_description: str = "current_viewport"
    reason_code: str | None = None

    def __post_init__(self):
        if self.status not in ("success", "unavailable", "failed", "cancelled"):
            raise ValueError("Invalid screenshot status.")
        if self.status == "success":
            if not isinstance(self.png, bytes) or not 0 < len(self.png) <= 2*1024*1024 or self.reason_code is not None:
                raise ValueError("Screenshot success requires bounded bytes.")
        elif self.png is not None or not self.reason_code:
            raise ValueError("No empty screenshot success or failure with image bytes.")


@dataclass(frozen=True)
class OpenRequest:
    path: Path
    format: str
    deadline: float
    mode: str = "new"
    artifact_role: str = "exchange_snapshot"

    def __post_init__(self):
        _deadline(self.deadline)
        if not isinstance(self.path, Path) or not self.path.is_absolute() or self.mode != "new":
            raise ValueError("Open must target an absolute file in a new document.")
        if self.artifact_role not in ("exchange_snapshot", "file_snapshot"):
            raise BackendError("native_project_restore_unsupported", outcome="not_started")


@dataclass(frozen=True)
class OpenReceipt:
    outcome: str
    document: DocumentRef | None = None
    reason_code: str | None = None

    def __post_init__(self):
        if self.outcome not in ("completed", "failed", "unknown"):
            raise ValueError("Invalid open outcome.")
        if self.outcome == "completed" and self.document is None:
            raise ValueError("Open completion must identify its document.")


@dataclass(frozen=True)
class BackendEvent:
    kind: EventKind
    source: str
    observed_at: str
    ref: DocumentRef | None = None
    revision: str | None = None
    coverage: str = "best_effort"
    # Store bounded immutable JSON; callers cannot grow a payload after validation.
    details_json: str = "{}"
    vendor_evidence_json: str | None = field(default=None, repr=False)
    record_kind: str | None = None
    affects_content: bool | None = None

    def __post_init__(self):
        if not isinstance(self.kind, EventKind):
            raise ValueError("Expected a neutral event kind.")
        for value in (self.source, self.observed_at, self.coverage):
            _identifier(value)
        if self.revision is not None:
            _identifier(self.revision)
        if self.record_kind is not None:
            _identifier(self.record_kind)
        if self.affects_content is not None and type(self.affects_content) is not bool:
            raise ValueError("Invalid content-impact flag.")
        total = 0
        for payload in (self.details_json, self.vendor_evidence_json):
            if payload is None:
                continue
            if not isinstance(payload, str) or len(payload) > MAX_EVENT_BYTES:
                raise ValueError("Oversized event.")
            total += len(payload.encode("utf-8"))
            if total > MAX_EVENT_BYTES:
                raise ValueError("Oversized event.")
            try:
                value = json.loads(payload, parse_constant=_reject_json_constant)
            except (ValueError, RecursionError) as exc:
                raise ValueError("Invalid event JSON.") from exc
            if not isinstance(value, dict):
                raise ValueError("Event evidence must be an object.")


def _metadata(value):
    if not isinstance(value, str) or len(value.encode("utf-8")) > MAX_EVENT_BYTES:
        raise ValueError("Invalid or oversized metadata.")
    try:
        body = json.loads(value, parse_constant=_reject_json_constant)
    except (ValueError, RecursionError) as exc:
        raise ValueError("Invalid metadata JSON.") from exc
    if not isinstance(body, dict):
        raise ValueError("Metadata must be an object.")
