"""Local authenticated skill workflow; never opens another service writer."""
import hashlib
import os
from pathlib import Path
import tempfile
import uuid

from ..agent_contract import bind, next_call
from .errors import ServiceError, bad_request, not_found
from .state import is_within


def submit_next_action(item):
    return {
        "tool": "vestigraph.submit",
        "arguments": {"skill_id": item["id"], "expected_revision": item["revision"]},
        "required_before_call": "Write the body from the frozen evidence and the user's explanation; do not submit placeholder text.",
    }


def skill_result(item):
    return {"skill": item, "problems": [], "next_action": submit_next_action(item),
        "instructions": "Keep observations, user intent and inference separate. Do not infer action order from saved files. Do not execute attachments or upload evidence. Submit a draft only."}


def placeholder_body(body):
    text = body.strip().lower()
    return (text in {"replace with instructions derived from the selected evidence and user explanation.",
                     "write derived instructions here", "todo", "tbd"}
            or "replace with instructions derived" in text)


def invoke(app, name, arguments, allowed_projects):
    args = bind(name, arguments)
    def project(pid):
        if pid not in allowed_projects:
            raise not_found("Project", "Call vestigraph.guide with {}.")
    def document(did):
        item = app.catalog.get_document(did)
        project(item["project_id"])
        return item
    def skill(sid):
        item = app.skills.get(sid)
        project(item["project_id"])
        return item
    if name == "guide":
        pid = args["project_id"]
        if pid is not None:
            project(pid)
        projects = [{"id": p["id"], "name": p["name"],
                     "next_action": next_call("guide", project_id=p["id"])}
                    for p in app.catalog.list_projects() if p["id"] in allowed_projects]
        result = {"available": True, "skills_enabled": app.experimental_skills,
                  "projects": projects, "problems": [],
                  "next_action": "Choose the user's project from projects, then call vestigraph.guide with that project_id."}
        if pid is None:
            if len(projects) == 1:
                result["next_action"] = projects[0]["next_action"]
            elif projects:
                result["problems"] = ["Choose the user's project from projects; do not guess."]
                result["next_action"] = "Ask the user which listed project to use, then call vestigraph.guide with that project_id."
            else:
                result["next_action"] = "Open the local Vestigraph panel and import or record a document."
            return result
        result["documents"] = [{"id": d["id"], "name": d["name"],
                               "next_action": next_call("history", document_id=d["id"])}
                              for d in app.documents(pid)]
        if app.experimental_skills:
            result["pending"] = app.skills.list(pid, args["before"], "awaiting_agent")
            for item in result["pending"]["items"]:
                item["next_action"] = next_call("skill", skill_id=item["id"])
            more = result["pending"]["next_before"]
            if more:
                result["next_page"] = next_call("guide", project_id=pid, before=more)
            choices = result["pending"]["items"]
            if len(choices) == 1:
                result["next_action"] = choices[0]["next_action"]
            elif choices:
                result["problems"] = ["Several pending requests exist. Choose the request named by the user."]
                result["next_action"] = "Ask the user which pending request to use, then call vestigraph.skill with that skill_id."
            elif len(result["documents"]) == 1:
                result["next_action"] = result["documents"][0]["next_action"]
            elif result["documents"]:
                result["problems"] = ["Several documents exist. Choose the document named by the user."]
                result["next_action"] = "Ask the user which listed document to use, then call vestigraph.history with that document_id."
            else:
                result["next_action"] = "Open or import a local document in Vestigraph, then call vestigraph.guide with this project_id."
        else:
            result["next_action"] = "To opt in, set VESTIGRAPH_EXPERIMENTAL_SKILLS=1 and restart Vestigraph, then call vestigraph.guide with {}."
        return result
    if name == "history":
        document(args["document_id"])
        result = app.checkpoints(args["document_id"], cursor=args["cursor"], limit=30)
        history_revision = result["history_revision"]
        action = next_call("refine", document_id=args["document_id"],
            from_id="USER_SELECTED_START_ID", to_id="USER_SELECTED_END_ID",
            history_revision=history_revision, title="USER_TITLE", goal="USER_GOAL")
        action["required_before_call"] = "Replace USER_SELECTED_START_ID, USER_SELECTED_END_ID, USER_TITLE and USER_GOAL from the user's selected interval and explanation. Preserve history_revision exactly as returned by this history result; do not invent it."
        action["history_revision_source"] = "history.history_revision"
        return {"history": result, "problems": [],
                "next_action": action,
                "instructions": "Replace placeholders with the user-selected interval and explanation. Do not guess.",
                "next_page": next_call("history", document_id=args["document_id"], cursor=result["next_cursor"]) if result.get("next_cursor") else None}
    if not app.experimental_skills:
        raise ServiceError("EXPERIMENTAL_DISABLED", "Skill refinement is disabled.", status=409,
                           next_action="Set VESTIGRAPH_EXPERIMENTAL_SKILLS=1 and restart Vestigraph; call vestigraph.guide with {}.")
    if name == "refine":
        document(args["document_id"])
        selection = {k: args[k] for k in ("from_id", "to_id", "history_revision")}
        intent = {k: args[k] for k in ("title", "goal", "rationale", "applicability", "parameters", "success_criteria", "domain")}
        # Stable key survives lost responses and new MCP sessions. Explicitly
        # selecting a different window or intent creates a different request.
        from .catalog import fingerprint
        key = str(uuid.uuid5(uuid.NAMESPACE_URL, fingerprint(args)))
        return skill_result(app.skills.create(args["document_id"], intent,
                            {"provider": "history-window", "selection": selection}, key))
    item = skill(args["skill_id"])
    if name == "skill":
        return skill_result(item)
    if item["revision"] != args["expected_revision"]:
        raise ServiceError("SKILL_REVISION_CONFLICT", "The local skill has changed. Read it before submitting or exporting.", status=409,
                           next_action=next_call("skill", skill_id=item["id"]))
    if name == "submit":
        if placeholder_body(args["body"]):
            raise bad_request("Submit derived skill instructions, not placeholder text.",
                              submit_next_action(item))
        updated = app.skills.revise(item["id"], args["expected_revision"], args["body"], args["files"],
                                    actor="local-agent", validation_note=args["validation_note"], check_structure=True)
        return {"skill_id": updated["id"], "revision": updated["revision"], "state": updated["state"],
                "verification": updated["verification"], "problems": [], "next_action": "done",
                "on_user_request": next_call("export", skill_id=updated["id"], expected_revision=updated["revision"])}
    data, mime, filename = app.skills.export(item["id"], args["format"], item["revision"])
    digest = hashlib.sha256(data).hexdigest()
    suffix = ".zip" if args["format"] == "agent-skill" else ".json"
    folder = app.state.root / "exports"
    folder.mkdir(parents=True, exist_ok=True)
    if folder.is_symlink() or not is_within(folder, app.state.root):
        raise bad_request("The export directory is not a safe local service path.",
                          "Choose a fresh service state directory and retry the export.")
    target = folder / (item["id"] + "-r" + str(item["revision"]) + "-" + digest + suffix)
    if not is_within(target, folder):
        raise bad_request("The export destination is outside the local export directory.",
                          "Choose a fresh service export directory and retry.")
    if not target.exists():
        with tempfile.TemporaryDirectory(prefix="export-", dir=folder) as temporary:
            staged = Path(temporary) / "asset"
            staged.write_bytes(data)
            try:
                os.link(staged, target)
            except FileExistsError:
                pass
    if target.is_symlink() or hashlib.sha256(target.read_bytes()).hexdigest() != digest:
        raise bad_request("The export destination contains different data.", "Keep the existing export for inspection and choose a fresh service export directory.")
    return {"path": str(target), "sha256": digest, "revision": item["revision"],
            "local_only": True, "problems": [], "next_action": "done"}
