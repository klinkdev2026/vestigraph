"""Response envelopes and request bodies for the /api/v1 contract."""
from __future__ import annotations
from typing import Any, Optional
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from ..service.errors import ServiceError


def ok(data: Any, request_id: str, next_action: str = "done") -> dict:
    return {"ok": True, "data": data, "request_id": request_id, "next_action": next_action}


def fail(error: ServiceError, request_id: str) -> dict:
    return {"ok": False, "error": error.to_dict(), "request_id": request_id}


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RenameCheckpoint(Strict):
    title: str = Field(min_length=1, max_length=200)
    expected_revision: StrictInt = Field(ge=0)
    actor: str = "user"


class PolicyUpdate(Strict):
    enabled: Optional[bool] = None
    session_selector: Optional[dict] = None
    allow_unsaved: Optional[bool] = None
    intervals: Optional[dict] = None
    expected_policy_version: Optional[int] = None

    def updates(self) -> dict:
        return {k: v for k, v in self.model_dump().items()
                if k != "expected_policy_version" and v is not None}


class PauseRequest(Strict):
    reason: Optional[str] = Field(default=None, max_length=200)
    session_id: Optional[str] = Field(default=None, max_length=200)


class ResumeRequest(Strict):
    session_id: Optional[str] = Field(default=None, max_length=200)


class MilestoneRequest(Strict):
    title: str = Field(min_length=1, max_length=200)


class AnnotationRequest(Strict):
    target_type: str = Field(pattern="^(checkpoint|segment)$")
    target_id: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=4000)


class PreviewRequest(Strict):
    top_cell: Optional[str] = Field(default=None, max_length=200)
    viewport_dbu: Optional[list] = None
    layers: Optional[list] = None


class OpenInKLayoutRequest(Strict):
    session_id: str = Field(min_length=1, max_length=200)
    expected_session_instance: Optional[str] = Field(default=None, max_length=200)
    confirm_new_tab: bool = True


class OpenInEditorRequest(OpenInKLayoutRequest):
    backend_id: str = Field(min_length=1, max_length=128)


class DiffRequest(Strict):
    from_id: str = Field(alias="from", min_length=1, max_length=128)
    to_id: str = Field(alias="to", min_length=1, max_length=128)
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class RelocateRequest(Strict):
    history_root: str = Field(min_length=1, max_length=1000)
    allow_inside_git: bool = False


class BrowseRequest(Strict):
    title: str = Field(default="Choose the folder for layout history", min_length=1, max_length=200)


class CaptureDiscardRequest(Strict):
    capture_id: str | None = Field(default=None, min_length=1, max_length=128)


class ChangeSummariesRequest(Strict):
    checkpoint_ids: list[str] = Field(min_length=1, max_length=100)


class ImportItem(Strict):
    id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=200)
    note: str = Field(default="", max_length=2000)
    historical_at: str = Field(default="", max_length=64)


class ImportConfirmation(Strict):
    before_id: str = Field(min_length=1, max_length=128)
    items: Optional[list[ImportItem]] = Field(default=None, max_length=200)
