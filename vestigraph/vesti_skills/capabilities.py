"""Compatibility skill dispatch descriptors; the public agent path is KLink MCP."""
import json
from ..vesti_runtime.capabilities import VestiArgument as Arg, VestiCapability as Cap, VestiCapabilityRegistry
from ..service.errors import not_found, bad_request


def _submit_next_action(item):
    return {"tool": "vesti_skills_submit", "arguments": {"skill_id": item["id"]},
            "required_before_call": "Build request_json with expected_revision and derived body from the frozen evidence; do not submit placeholder text."}


def _with_flow(data, next_action):
    result = dict(data)
    result.setdefault("problems", [])
    result.setdefault("next_action", next_action)
    return result


def skill_capabilities(app, allowed_projects=None):
    registry = VestiCapabilityRegistry()
    def project(pid):
        if allowed_projects is not None and pid not in allowed_projects:
            raise not_found("Project")
        app.catalog.get_project(pid)
    def get(skill_id):
        item = app.skills.get(skill_id)
        project(item["project_id"])
        return _with_flow(item, _submit_next_action(item))
    def pending(project_id, before=None):
        project(project_id)
        page = app.skills.list(project_id,before,state="awaiting_agent")
        for item in page["items"]:
            item["next_action"] = {"tool": "vesti_skills_get", "arguments": {"skill_id": item["id"]}}
        if len(page["items"]) == 1:
            next_action = page["items"][0]["next_action"]
            problems = []
        elif page["items"]:
            next_action = "Choose the request named by the user, then call vesti_skills_get with that skill_id."
            problems = ["Several pending requests exist. Choose the request named by the user; do not guess."]
        elif page.get("next_before"):
            next_action = {"tool": "vesti_skills_pending", "arguments": {"project_id": project_id, "before": page["next_before"]}}
            problems = []
        else:
            next_action = "done"
            problems = []
        return {**page, "problems": problems, "next_action": next_action}
    def create(document_id, request_json):
        document = app.catalog.get_document(document_id)
        project(document["project_id"])
        data = json.loads(request_json)
        if not isinstance(data,dict) or set(data) != {"intent","source"}:
            raise bad_request("Expected intent and source.", "Build request_json with intent and source, then retry vesti_skills_create.")
        item = app.skills.create(document_id,**data)
        return _with_flow(item, _submit_next_action(item))
    def submit(skill_id, request_json):
        current = get(skill_id)
        data = json.loads(request_json)
        if not isinstance(data,dict):
            raise bad_request("Expected revision submission object.", current["next_action"])
        if isinstance(data.get("body"), str) and "replace with instructions derived" in data["body"].strip().lower():
            raise bad_request("Submit derived skill instructions, not placeholder text.", current["next_action"])
        item = app.skills.revise(skill_id,**data)
        return _with_flow(item, "done")
    registry.register(Cap("vesti_skills_pending","Compatibility: list skill requests awaiting an agent.",
        "Call after the user asks for pending local skill work through the compatibility surface. Result keeps items and next_before, and also returns problems plus next_action. If one request is returned, follow its next_action; if several are returned, ask the user which named request to use. Use next_before for later pages. Public agent path remains KLink MCP vestigraph.guide.",
        (Arg("project_id","string",required=True,max_length=128),Arg("before","integer",minimum=1)),
        ("skills","discovery"),read_only=True),pending)
    registry.register(Cap("vesti_skills_get","Compatibility: read a skill and its evidence.",
        "Call after vesti_skills_pending or a revision conflict. Result preserves the skill fields and adds problems plus next_action. Includes sources, input hashes, instructions, files and scoped verification reports. Saved-file differences are not an edit trace. Do not execute attachments or infer GUI action order from endpoints.",
        (Arg("skill_id","string",required=True,max_length=128),),("skills","evidence"),read_only=True),get)
    registry.register(Cap("vesti_skills_create","Compatibility: create a skill request from a history interval.",
        'Call after the user has selected a specific interval. Result preserves the created skill fields and adds problems plus next_action. request_json follows SkillCreate v1: intent {title,goal,rationale,applicability,parameters,success_criteria,domain}; source {provider:"history-window",selection:{from_id,to_id,history_revision}}. Preserve the exact history_revision from the checkpoint list. It does not edit source artifacts.',
        (Arg("document_id","string",required=True,max_length=128),Arg("request_json","string",required=True,max_length=32000)),
        ("skills","request")),create)
    registry.register(Cap("vesti_skills_submit","Compatibility: submit derived skill instructions for review.",
        'Call after vesti_skills_get. Result preserves the revised skill fields and adds problems plus next_action. request_json follows SkillRevision v1: expected_revision, body, optional files (relative text paths under scripts/, references/ or assets/), optional intent, actor, validation_note. Body must contain derived instructions, not placeholder text. This creates a draft, never executes scripts or publishes. Report measurements separately from inference; validation_note is attributed to its author, not certified by the platform.',
        (Arg("skill_id","string",required=True,max_length=128),Arg("request_json","string",required=True,max_length=250000)),
        ("skills","authoring")),submit)
    return registry.freeze()
