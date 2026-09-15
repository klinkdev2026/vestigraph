"""HTTP adapter for skill assets; same authentication and scope as history."""
from fastapi import APIRouter, Depends, Request, Response
from pydantic import Field, StrictInt
from . import schemas
from .routes import (app_of, request_id_of, require_session, require_write,
                     _document_in_scope, _project_in_scope)

def require_experimental_skills(request: Request, session=Depends(require_session)):
    if not app_of(request).experimental_skills:
        from ..service.errors import ServiceError
        raise ServiceError("EXPERIMENTAL_DISABLED", "Skill refinement is experimental and disabled.", status=409,
                           next_action="Set VESTIGRAPH_EXPERIMENTAL_SKILLS=1 and restart the service to opt in.")


router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_experimental_skills)])

class SkillCreate(schemas.Strict):
    intent: dict
    source: dict

class SkillRevision(schemas.Strict):
    expected_revision: StrictInt = Field(ge=1)
    body: str = Field(min_length=1, max_length=65536)
    files: dict[str,str] = Field(default_factory=dict)
    intent: dict | None = None
    actor: str = Field(default="user",min_length=1,max_length=100)
    validation_note: str = Field(default="",max_length=4000)

class SkillAction(schemas.Strict):
    expected_revision: StrictInt = Field(ge=1)
    provider: str = Field(default="",max_length=128)

def scoped(request, sid):
    item = app_of(request).skills.get(sid)
    _project_in_scope(request, item["project_id"])
    return item

@router.get("/skill-options")
def options(request: Request, session=Depends(require_session)):
    return schemas.ok(app_of(request).skills.options(),request_id_of(request))

@router.get("/projects/{pid}/skills")
def listing(pid: str, request: Request, before: int | None=None, state: str | None=None,
            limit: int=30, session=Depends(require_session)):
    _project_in_scope(request,pid)
    return schemas.ok(app_of(request).skills.list(pid,before,state,limit),request_id_of(request))

@router.post("/documents/{did}/skills",status_code=201)
def create(did: str, body: SkillCreate, request: Request, write=Depends(require_write)):
    _document_in_scope(request,did)
    item=app_of(request).skills.create(did,body.intent,body.source,write["request_key"])
    return schemas.ok(item,request_id_of(request))

@router.get("/skills/{sid}")
def get(sid: str, request: Request, revision: int | None=None, session=Depends(require_session)):
    scoped(request,sid)
    return schemas.ok(app_of(request).skills.get(sid,revision),request_id_of(request))

@router.put("/skills/{sid}")
def revise(sid: str, body: SkillRevision, request: Request, write=Depends(require_write)):
    scoped(request,sid)
    return schemas.ok(app_of(request).skills.revise(sid,**body.model_dump()),request_id_of(request))

@router.post("/skills/{sid}/publish")
def publish(sid: str, body: SkillAction, request: Request, write=Depends(require_write)):
    scoped(request,sid)
    return schemas.ok(app_of(request).skills.publish(sid,body.expected_revision),request_id_of(request))

@router.post("/skills/{sid}/generate",status_code=202)
def generate(sid: str, body: SkillAction, request: Request, write=Depends(require_write)):
    scoped(request,sid)
    job=app_of(request).skills.request(sid,body.expected_revision,body.provider,"skill_generate",write["request_key"])
    return schemas.ok({"job_id":job["id"]},request_id_of(request))

@router.post("/skills/{sid}/validate",status_code=202)
def validate(sid: str, body: SkillAction, request: Request, write=Depends(require_write)):
    scoped(request,sid)
    job=app_of(request).skills.request(sid,body.expected_revision,body.provider,"skill_validate",write["request_key"])
    return schemas.ok({"job_id":job["id"]},request_id_of(request))

@router.get("/skills/{sid}/export")
def export(sid: str, request: Request, format: str="agent-skill", revision: int | None=None, session=Depends(require_session)):
    scoped(request,sid)
    data,mime,name=app_of(request).skills.export(sid,format,revision)
    # Download names are adapter output, never an HTTP header injection channel.
    import re
    from ..service.errors import bad_request
    if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,160}",name) or "\r" in mime or "\n" in mime:
        raise bad_request("Invalid skill exporter response.")
    return Response(data,media_type=mime,headers={"Content-Disposition":'attachment; filename="'+name+'"',"Cache-Control":"no-store"})


class SkillCall(schemas.Strict):
    name: str = Field(min_length=1,max_length=128)
    arguments: dict = Field(default_factory=dict)

@router.get("/skill-capabilities")
def capabilities(request: Request, session=Depends(require_session)):
    from ..vesti_skills.capabilities import skill_capabilities
    from .routes import scope
    registry=skill_capabilities(app_of(request),scope(request))
    return schemas.ok({
        "tools": registry.mcp_tools(),
        "transport": "compatibility_authenticated_http_dispatch",
        "public_agent_path": "klink_mcp",
        "next_action": "Use KLink MCP: start with klink.status, discover the vestigraph domain, then follow vestigraph.guide next_action.",
    }, request_id_of(request))

@router.post("/skill-capabilities/invoke")
def invoke(body: SkillCall, request: Request, write=Depends(require_write)):
    from ..vesti_skills.capabilities import skill_capabilities
    from .routes import scope
    from ..service.errors import bad_request
    registry=skill_capabilities(app_of(request),scope(request))
    try:
        data=registry.invoke(body.name,body.arguments)
    except (ValueError,TypeError) as exc:
        raise bad_request(str(exc)) from exc
    return schemas.ok(data,request_id_of(request))
