"""/api/v1 routes. Thin: validate, check auth/CSRF/scope, call Application, wrap."""
from __future__ import annotations

import asyncio
import uuid
from concurrent.futures import ThreadPoolExecutor
import threading

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import FileResponse, JSONResponse

from ..service.application import Application
from ..service.errors import ServiceError, bad_request, forbidden, unauthorized
from . import schemas
from .auth import AuthManager

router = APIRouter(prefix="/api/v1")


def request_id_of(request: Request) -> str:
    return request.state.request_id


def app_of(request: Request) -> Application:
    return request.app.state.application


def auth_of(request: Request) -> AuthManager:
    return request.app.state.auth


def require_session(request: Request) -> dict:
    session = auth_of(request).session(request.cookies.get(request.app.state.cookie_name))
    if session is None:
        raise unauthorized()
    return session


def require_origin(request: Request):
    origin = request.headers.get("origin")
    if origin is None or origin not in getattr(request.app.state, "origins", {request.app.state.origin}):
        raise forbidden("Cross-origin or missing Origin header.", "Use the local Vestigraph page itself.",
                        details={"expected_origin": request.app.state.origin})


def require_write(request: Request, session: dict = Depends(require_session)) -> dict:
    """Writes need same-origin + CSRF token; returns the idempotency key (may be None)."""
    require_origin(request)
    if not auth_of(request).check_csrf(session, request.headers.get("x-csrf-token")):
        raise forbidden("CSRF token missing or invalid.", "Reload the page to refresh your session.")
    key = request.headers.get("x-request-id")
    if key is not None:
        try:
            uuid.UUID(key)
        except ValueError:
            raise bad_request("X-Request-ID must be a UUID.", "Generate a UUID per user action.") from None
    return {"session": session, "request_key": key}


def scope(request: Request) -> set:
    """Project ids the session may touch.

    Single local user in this version, so this is EVERY registered project; it is the one
    place to narrow when sessions ever get per-project rights. Not an authorization check yet."""
    return {p["id"] for p in app_of(request).catalog.list_projects()}


def _document_in_scope(request: Request, document_id: str) -> dict:
    document = app_of(request).catalog.get_document(document_id)
    if document["project_id"] not in scope(request):
        raise ServiceError("NOT_FOUND", "Document not found in the current scope.", status=404,
                           next_action="Refresh the project list.")
    return document


def _project_in_scope(request: Request, project_id: str):
    if project_id not in scope(request):
        raise ServiceError("NOT_FOUND", "Project not found in the current scope.", status=404,
                           next_action="Refresh the project list.")


def _segment_filter(value):
    if value is None:
        return None
    if not isinstance(value, str) or not 1 <= len(value) <= 128:
        raise bad_request("segment_id is malformed.")
    return value


# ------------------------------------------------------------------ auth --
@router.post("/auth/bootstrap")
def auth_bootstrap(request: Request, response: Response):
    require_origin(request)
    header = request.headers.get("authorization", "")
    token = header[7:] if header.lower().startswith("bearer ") else ""
    session = auth_of(request).exchange(token)
    if session is None:
        raise ServiceError("BOOTSTRAP_INVALID", "The one-time link is invalid or expired.", status=401,
                           next_action="Restart the service or run it again to print a fresh link.")
    response.set_cookie(request.app.state.cookie_name, session["session_id"], httponly=True, samesite="strict",
                        secure=False, path="/", max_age=int(auth_of(request).session_ttl))
    data = {"csrf_token": session["csrf_token"], "expires_at": session["expires_at"],
            "capabilities": app_of(request).capabilities()}
    return schemas.ok(data, request_id_of(request), "Call GET /api/v1/status")


@router.post("/auth/issue-link")
def auth_issue_link(request: Request):
    """A local launcher (the KLayout plugin) trades the owner-only control secret for a sign-in link."""
    import hmac
    secret = getattr(request.app.state, "control_secret", None)
    presented = request.headers.get("x-control-secret", "")
    if not secret or not presented or not presented.isascii() or not hmac.compare_digest(secret, presented):
        raise ServiceError("CONTROL_SECRET_INVALID", "Control secret missing or wrong.", status=401,
                           next_action="Only the local launcher that started the service can request links.")
    token = auth_of(request).issue_bootstrap()
    link = f"{request.app.state.origin}/#bootstrap={token}"
    return schemas.ok({"link": link, "expires_in_s": int(auth_of(request).bootstrap_ttl)}, request_id_of(request))


@router.get("/auth/session")
def auth_session(request: Request, session: dict = Depends(require_session)):
    return schemas.ok({"csrf_token": session["csrf_token"], "expires_at": session["expires_at"],
                       "capabilities": app_of(request).capabilities()}, request_id_of(request))


@router.post("/auth/logout")
def auth_logout(request: Request, response: Response, write: dict = Depends(require_write)):
    auth_of(request).revoke(write["session"]["session_id"])
    response.delete_cookie(request.app.state.cookie_name, path="/")
    return schemas.ok({"logged_out": True, "recording_affected": False}, request_id_of(request))


# ---------------------------------------------------------------- status --
@router.get("/status")
def status(request: Request, session: dict = Depends(require_session)):
    return schemas.ok(app_of(request).status(), request_id_of(request))


@router.get("/sessions")
def sessions(request: Request, session: dict = Depends(require_session)):
    return schemas.ok({"items": app_of(request).sessions()}, request_id_of(request))


# -------------------------------------------------------------- projects --
@router.get("/projects")
def projects(request: Request, session: dict = Depends(require_session)):
    return schemas.ok({"items": app_of(request).projects(), "next_cursor": None, "window_id": None},
                      request_id_of(request))


@router.get("/projects/{pid}/documents")
def project_documents(pid: str, request: Request, session: dict = Depends(require_session)):
    _project_in_scope(request, pid)
    return schemas.ok({"items": app_of(request).documents(pid), "next_cursor": None, "window_id": None},
                      request_id_of(request))


@router.get("/projects/{pid}/status")
def project_status(pid: str, request: Request, session: dict = Depends(require_session)):
    _project_in_scope(request, pid)
    return schemas.ok(app_of(request).project_status(pid), request_id_of(request))


@router.put("/projects/{pid}/policy")
def project_policy(pid: str, body: schemas.PolicyUpdate, request: Request, write: dict = Depends(require_write)):
    _project_in_scope(request, pid)
    data = app_of(request).set_policy(pid, body.updates(), body.expected_policy_version)
    return schemas.ok(data, request_id_of(request))


@router.post("/projects/{pid}/pause", status_code=202)
def project_pause(pid: str, request: Request, write: dict = Depends(require_write),
                  body: schemas.PauseRequest | None = None):
    _project_in_scope(request, pid)
    job = app_of(request).pause(pid, body.reason if body else None, session_id=body.session_id if body else None)
    return schemas.ok({"job_id": job["id"], "status": job["status"]}, request_id_of(request), f"GET /api/v1/jobs/{job['id']}")


@router.post("/projects/{pid}/resume", status_code=202)
def project_resume(pid: str, request: Request, write: dict = Depends(require_write),
                   body: schemas.ResumeRequest | None = None):
    _project_in_scope(request, pid)
    job = app_of(request).resume(pid, session_id=body.session_id if body else None)
    return schemas.ok({"job_id": job["id"], "status": job["status"]}, request_id_of(request), f"GET /api/v1/jobs/{job['id']}")


@router.put("/projects/{pid}/storage", status_code=202)
def project_storage(pid: str, body: schemas.RelocateRequest, request: Request, write: dict = Depends(require_write)):
    """Move the whole history of a project to another folder (drain, copy, verify, switch, remove old)."""
    _project_in_scope(request, pid)
    job = app_of(request).request_relocate(pid, body.history_root, write["request_key"],
                                           allow_inside_git=body.allow_inside_git)
    return schemas.ok({"job_id": job["id"], "status": job["status"]}, request_id_of(request), f"GET /api/v1/jobs/{job['id']}")


_DIALOG_SLOT = threading.Lock()
_DIALOG_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="vestigraph-dialog")


@router.post("/projects/{pid}/storage/browse")
async def project_storage_browse(pid: str, body: schemas.BrowseRequest, request: Request,
                                 write: dict = Depends(require_write)):
    """Open a native folder dialog on this machine's desktop (a browser cannot read folder paths).

    Waits until the user answers; returns {"path": str|None, "cancelled": bool}. The wait happens
    on a dedicated single thread, never on the shared request pool: a dialog left open for
    minutes must not hold a pool worker hostage and stall every other request."""
    _project_in_scope(request, pid)
    from ..service import dialogs
    status = app_of(request).project_status(pid)
    loop = asyncio.get_running_loop()
    if not _DIALOG_SLOT.acquire(blocking=False):
        raise ServiceError("DIALOG_BUSY", "A folder dialog is already open.", status=409,
                           next_action="Finish the open dialog or type the path.")
    try:
        future = _DIALOG_EXECUTOR.submit(dialogs.pick_directory, status.get("history_root"), body.title)
    except BaseException:
        _DIALOG_SLOT.release()
        raise
    # Release on actual subprocess completion, even if the HTTP request is cancelled.
    future.add_done_callback(lambda _: _DIALOG_SLOT.release())
    chosen = await asyncio.wrap_future(future, loop=loop)
    return schemas.ok({"path": chosen, "cancelled": chosen is None}, request_id_of(request))


@router.post("/documents/{did}/milestones", status_code=202)
def document_milestone(did: str, body: schemas.MilestoneRequest, request: Request,
                       write: dict = Depends(require_write)):
    _document_in_scope(request, did)
    job = app_of(request).milestone(did, body.title, write["request_key"])
    return schemas.ok({"job_id": job["id"], "status": job["status"]}, request_id_of(request), f"GET /api/v1/jobs/{job['id']}")


# --------------------------------------------------------------- history --
@router.post("/documents/{did}/save/cancel")
def document_cancel_save(did: str, request: Request, write: dict = Depends(require_write)):
    _document_in_scope(request, did)
    return schemas.ok(app_of(request).cancel_save(did), request_id_of(request))


@router.get("/documents/{did}/checkpoints")
def document_checkpoints(did: str, request: Request, session: dict = Depends(require_session),
                         cursor: str | None = None, limit: int | None = None, segment_id: str | None = None):
    _document_in_scope(request, did)
    data = app_of(request).checkpoints(did, cursor, limit, _segment_filter(segment_id))
    return schemas.ok(data, request_id_of(request))


@router.get("/documents/{did}/capture")
def document_capture_queue(did: str, request: Request, session: dict = Depends(require_session)):
    _document_in_scope(request, did)
    return schemas.ok(app_of(request).capture_queue(did), request_id_of(request))


@router.post("/documents/{did}/capture/discard")
def document_capture_discard(did: str, request: Request, write: dict = Depends(require_write),
                             body: schemas.CaptureDiscardRequest | None = None):
    """Drop a retained copy (the blocked head, or ``capture_id``) so recording can continue."""
    _document_in_scope(request, did)
    capture_id = body.capture_id if body is not None else None
    return schemas.ok(app_of(request).discard_capture(did, capture_id), request_id_of(request))


@router.post("/documents/{did}/capture/retry")
def document_capture_retry(did: str, request: Request, write: dict = Depends(require_write)):
    _document_in_scope(request, did)
    return schemas.ok(app_of(request).retry_capture(did), request_id_of(request))


@router.get("/documents/{did}/segments")
def document_segments(did: str, request: Request, session: dict = Depends(require_session),
                      cursor: str | None = None, limit: int | None = None):
    _document_in_scope(request, did)
    return schemas.ok(app_of(request).segments(did, cursor, limit), request_id_of(request))


@router.get("/documents/{did}/events")
def document_events(did: str, request: Request, session: dict = Depends(require_session),
                    cursor: str | None = None, limit: int | None = None, segment_id: str | None = None):
    _document_in_scope(request, did)
    return schemas.ok(app_of(request).events(did, cursor, limit, _segment_filter(segment_id)), request_id_of(request))


@router.get("/documents/{did}/events/{seq}")
def document_event(did: str, seq: int, request: Request, session: dict = Depends(require_session)):
    _document_in_scope(request, did)
    return schemas.ok(app_of(request).event(did, seq), request_id_of(request))


@router.get("/documents/{did}/checkpoints/{cid}")
def document_checkpoint(did: str, cid: str, request: Request, session: dict = Depends(require_session)):
    _document_in_scope(request, did)
    return schemas.ok(app_of(request).checkpoint(did, cid), request_id_of(request))


@router.get("/documents/{did}/checkpoints/{cid}/changes")
def document_checkpoint_changes(did: str, cid: str, request: Request, session: dict = Depends(require_session),
                                limit: int | None = None, cursor: str | None = None, kind: str | None = None):
    _document_in_scope(request, did)
    data = app_of(request).changes(did, cid, limit=limit, cursor=cursor, kind=kind)
    return schemas.ok(data, request_id_of(request))


@router.put("/documents/{did}/checkpoints/{cid}/title")
def rename_checkpoint(did: str, cid: str, body: schemas.RenameCheckpoint, request: Request,
                      write: dict = Depends(require_write)):
    _document_in_scope(request, did)
    data = app_of(request).rename_checkpoint(did, cid, body.title, body.expected_revision,
                                            body.actor, write["request_key"])
    return schemas.ok(data, request_id_of(request))


@router.get("/documents/{did}/checkpoints/{cid}/thumbnail")
def checkpoint_thumbnail(did: str, cid: str, request: Request, session: dict = Depends(require_session)):
    _document_in_scope(request, did)
    app_of(request).checkpoint(did, cid)
    item = app_of(request).thumbnail(did, cid)
    if item is None:
        return Response(status_code=204, headers={"Cache-Control": "no-store"})
    png, info = item
    return Response(png, media_type="image/png", headers={
        "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff",
        "X-Vestigraph-Image-Binding": info.get("binding", "saved_file"),
        "X-Vestigraph-Image-Source": info.get("source", "capture"),
    })


@router.post("/documents/{did}/checkpoints/{cid}/thumbnail", status_code=202)
def generate_checkpoint_thumbnail(did: str, cid: str, request: Request, write: dict = Depends(require_write)):
    _document_in_scope(request, did)
    job = app_of(request).saved_thumbnails.request(did, cid, write["request_key"])
    return schemas.ok({"job_id": job["id"], "status": job["status"]}, request_id_of(request))


@router.get("/documents/{did}/checkpoints/{cid}/thumbnail-status")
def checkpoint_thumbnail_status(did: str, cid: str, request: Request, session: dict = Depends(require_session)):
    _document_in_scope(request, did)
    return JSONResponse(schemas.ok(app_of(request).thumbnail_status(did, cid), request_id_of(request)),
                        headers={"Cache-Control": "private, no-store"})


@router.post("/documents/{did}/changes/summaries")
def document_change_summaries(did: str, body: schemas.ChangeSummariesRequest, request: Request,
                              session: dict = Depends(require_session)):
    """Multi-version summary batch (docs/SPEC_PERFORMANCE_READ_ACCESS.md §4.4): POST because
    of the body, but it is a read -- require_session, not require_write/CSRF."""
    _document_in_scope(request, did)
    data = app_of(request).change_summaries(did, body.checkpoint_ids)
    return schemas.ok(data, request_id_of(request))


@router.post("/documents/{did}/evidence")
def document_evidence(did: str, body: schemas.ChangeSummariesRequest, request: Request,
                      session: dict = Depends(require_session)):
    """Read-only ordered context envelope; payload and event reads remain explicit."""
    _document_in_scope(request, did)
    return schemas.ok(app_of(request).evidence_package(did, body.checkpoint_ids), request_id_of(request))


@router.get("/documents/{did}/annotations")
def document_annotations(did: str, request: Request, session: dict = Depends(require_session),
                         cursor: str | None = None, limit: int | None = None,
                         target_type: str | None = None, target_id: str | None = None):
    _document_in_scope(request, did)
    if target_type is not None and target_type not in ("checkpoint", "segment"):
        raise bad_request("target_type must be checkpoint or segment.")
    return schemas.ok(app_of(request).annotations(did, cursor, limit, target_type, target_id), request_id_of(request))


@router.post("/documents/{did}/annotations", status_code=201)
def document_annotate(did: str, body: schemas.AnnotationRequest, request: Request,
                      write: dict = Depends(require_write)):
    _document_in_scope(request, did)
    data = app_of(request).annotate(did, body.target_type, body.target_id, body.text, request_key=write["request_key"])
    return schemas.ok(data, request_id_of(request))


# ------------------------------------------------------------------ jobs --
def _accepted(request: Request, job: dict):
    return JSONResponse(status_code=202, content=schemas.ok(
        {"job_id": job["id"], "status": job["status"]}, request_id_of(request), f"GET /api/v1/jobs/{job['id']}"))


@router.post("/documents/{did}/checkpoints/{cid}/downloads")
def checkpoint_download(did: str, cid: str, request: Request, write: dict = Depends(require_write)):
    _document_in_scope(request, did)
    return _accepted(request, app_of(request).request_download(did, cid, write["request_key"]))


@router.post("/documents/{did}/checkpoints/{cid}/preview")
def checkpoint_preview(did: str, cid: str, request: Request, write: dict = Depends(require_write),
                       body: schemas.PreviewRequest | None = None):
    _document_in_scope(request, did)
    payload = body.model_dump() if body else {}
    return _accepted(request, app_of(request).request_preview(did, cid, payload, write["request_key"]))


@router.post("/documents/{did}/diffs")
def document_diff(did: str, body: schemas.DiffRequest, request: Request, write: dict = Depends(require_write)):
    _document_in_scope(request, did)
    return _accepted(request, app_of(request).request_diff(did, body.from_id, body.to_id, write["request_key"]))


@router.post("/documents/{did}/checkpoints/{cid}/open-in-klayout")
def checkpoint_open(did: str, cid: str, body: schemas.OpenInKLayoutRequest, request: Request,
                    write: dict = Depends(require_write)):
    _document_in_scope(request, did)
    return _accepted(request, app_of(request).request_open_in_klayout(did, cid, body.model_dump(), write["request_key"]))


@router.post("/documents/{did}/checkpoints/{cid}/open-in-editor")
def checkpoint_open_editor(did: str, cid: str, body: schemas.OpenInEditorRequest, request: Request,
                           write: dict = Depends(require_write)):
    _document_in_scope(request, did)
    return _accepted(request, app_of(request).request_open_in_editor(
        did, cid, body.model_dump(), write["request_key"]))


@router.get("/jobs/{jid}")
def job(jid: str, request: Request, session: dict = Depends(require_session)):
    return schemas.ok(app_of(request).job(jid, scope(request)), request_id_of(request))


@router.api_route("/jobs/{jid}/asset", methods=["GET", "HEAD"])
def job_asset(jid: str, request: Request, session: dict = Depends(require_session)):
    path, filename, content_type = app_of(request).job_asset(jid, scope(request))
    # RFC 5987 filename*; the plain filename falls back to ASCII-only characters.
    ascii_name = "".join(ch if 32 < ord(ch) < 127 and ch not in '";\\' else "_" for ch in filename) or "file"
    from urllib.parse import quote
    disposition = f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename, safe='')}"
    return FileResponse(path, media_type=content_type, headers={"Content-Disposition": disposition,
                                                                "Cache-Control": "no-store"})

# ------------------------------------------------------- legacy version imports --
@router.post("/documents/{did}/history-imports/browse")
async def history_import_browse(did: str, body: schemas.BrowseRequest, request: Request,
                                write: dict = Depends(require_write)):
    _document_in_scope(request, did)
    application = app_of(request)
    document = application.legacy_imports.document(did)
    from ..service import dialogs
    project = application.catalog.get_project(document["project_id"])
    if not _DIALOG_SLOT.acquire(blocking=False):
        raise ServiceError("DIALOG_BUSY", "A file or folder dialog is already open.", status=409)
    try:
        future = _DIALOG_EXECUTOR.submit(dialogs.pick_files, project.get("workspace"), body.title)
    except BaseException:
        _DIALOG_SLOT.release()
        raise
    future.add_done_callback(lambda _: _DIALOG_SLOT.release())
    paths = await asyncio.wrap_future(future)
    if not paths:
        return schemas.ok({"cancelled": True}, request_id_of(request))
    job = application.legacy_imports.prepare(did, paths, write["request_key"])
    return schemas.ok({"job_id": job["id"], "cancelled": False}, request_id_of(request))


@router.get("/documents/{did}/history-imports")
def history_import_list(did: str, request: Request, session: dict = Depends(require_session)):
    _document_in_scope(request, did)
    return schemas.ok({"items": app_of(request).legacy_imports.list(did)}, request_id_of(request))


@router.get("/documents/{did}/history-imports/{bid}")
def history_import_get(did: str, bid: str, request: Request, session: dict = Depends(require_session)):
    _document_in_scope(request, did)
    return schemas.ok(app_of(request).legacy_imports.get(did, bid), request_id_of(request))


@router.post("/documents/{did}/history-imports/{bid}/run", status_code=202)
def history_import_run(did: str, bid: str, body: schemas.ImportConfirmation, request: Request,
                        write: dict = Depends(require_write)):
    _document_in_scope(request, did)
    job = app_of(request).legacy_imports.confirm(did, bid, body.before_id,
            [item.model_dump() for item in body.items] if body.items is not None else None,
            request_key=write["request_key"])
    return schemas.ok({"job_id": job["id"], "status": job["status"]}, request_id_of(request))


@router.post("/documents/{did}/history-imports/{bid}/cancel")
def history_import_cancel(did: str, bid: str, request: Request, write: dict = Depends(require_write)):
    _document_in_scope(request, did)
    return schemas.ok(app_of(request).legacy_imports.cancel(did, bid), request_id_of(request))


@router.delete("/documents/{did}/history-imports/{bid}")
def history_import_delete(did: str, bid: str, request: Request, write: dict = Depends(require_write)):
    _document_in_scope(request, did)
    return schemas.ok(app_of(request).legacy_imports.delete(did, bid), request_id_of(request))
