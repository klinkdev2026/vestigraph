"""Agent calls share local browser authentication, CSRF and project scope."""
from fastapi import APIRouter, Depends, Request
from pydantic import Field, ValidationError
from .schemas import Strict, ok
from .routes import app_of, request_id_of, require_write
from ..service.errors import ServiceError, bad_request
from ..agent_contract import next_call

router = APIRouter(prefix="/api/v1/agent")

class Call(Strict):
    name: str = Field(min_length=1, max_length=32)
    arguments: dict = Field(default_factory=dict)

@router.post("/invoke")
def invoke(body: Call, request: Request, write=Depends(require_write)):
    from .routes import scope
    from ..service.agent import invoke as dispatch
    try:
        result = dispatch(app_of(request), body.name, body.arguments, scope(request))
    except ValidationError as exc:
        raise bad_request("Invalid tool arguments: " + "; ".join(
            ".".join(map(str, e["loc"])) + ": " + e["msg"] for e in exc.errors(include_input=False)),
            "Read the tool inputSchema, correct the named fields and retry.") from None
    except ValueError:
        raise bad_request("Invalid tool or arguments.", "Call vestigraph.guide with {}.") from None
    except ServiceError as exc:
        if not exc.next_action:
            exc.next_action = next_call("skill", skill_id=body.arguments["skill_id"]) if body.arguments.get("skill_id") else next_call("guide")
        raise
    return ok(result, request_id_of(request))
