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
    if name == "prepare_edit":
        coordinators = [c for c in app.supervisor.coordinators_on(args["session_id"])
                        if c.project["id"] in allowed_projects and c.writable_handle() is not None]
        if not coordinators:
            return {"ok": True, "prepared": False, "reason": "no_active_recording"}
        if len(coordinators) != 1:
            raise ServiceError("SESSION_AMBIGUOUS", "Several recordings own this session.", status=409)
        coordinator = coordinators[0]
        return coordinator.wait_reply(coordinator.request("prepare_edit"), 240)
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
            if len(result["documents"]) == 1:
                result["next_action"] = result["documents"][0]["next_action"]
            elif result["documents"]:
                result["next_action"] = "Choose the document named by the user, then call vestigraph.history."
            else:
                result["next_action"] = "Open or import a document in the local Vestigraph panel."
            result["refinement_opt_in"] = "Skill refinement only: set VESTIGRAPH_EXPERIMENTAL_SKILLS=1 and restart Vestigraph. History does not require this switch."
        return result
    if name == "history":
        document(args["document_id"])
        result = app.checkpoints(args["document_id"], cursor=args["cursor"], limit=200 if args["all"] else 30)
        if args["all"]:
            result = {**result, "items": list(result["items"])}
            while result.get("next_cursor"):
                page = app.checkpoints(args["document_id"], cursor=result["next_cursor"], limit=200)
                result["items"].extend(page["items"])
                result["next_cursor"] = page.get("next_cursor")
        # Agent responses use a bounded, human-readable summary. Full manifests,
        # event payloads and storage metadata remain available from the web UI
        # or an explicitly requested checkpoint detail call.
        summaries = []
        for item in result.get("items", []):
            metadata = item.get("metadata") or item
            operation = metadata.get("operation") or {}
            summaries.append({
                "id": item.get("id"),
                "modified_at": metadata.get("modified_at"),
                "modified_at_basis": metadata.get("modified_at_basis") or "unknown",
                "saved_at": item.get("created_at"),
                "title": item.get("title") or "",
                "source": item.get("source") or "unknown",
                "record": {
                    "operation": operation.get("reason") or operation.get("method"),
                    "note": operation.get("reason") or item.get("title") or "",
                    "coverage": metadata.get("coverage"),
                    "restore_of": metadata.get("restore_of"),
                },
            })
        result = {**result, "items": summaries, "detail_policy":
                  "Set all=true only when the user explicitly asks for the complete checkpoint list; full event payloads and storage metadata remain in the Vestigraph web panel.",
                  "restore_policy":
                  "Restore only after the user explicitly selects a checkpoint. Restore appends a new checkpoint and preserves every existing checkpoint."}
        history_revision = result["history_revision"]
        action = next_call("refine", document_id=args["document_id"],
            from_id="USER_SELECTED_START_ID", to_id="USER_SELECTED_END_ID",
            history_revision=history_revision, title="USER_TITLE", goal="USER_GOAL")
        action["required_before_call"] = "Replace USER_SELECTED_START_ID, USER_SELECTED_END_ID, USER_TITLE and USER_GOAL from the user's selected interval and explanation. Preserve history_revision exactly as returned by this history result; do not invent it."
        action["history_revision_source"] = "history.history_revision"
        return {"history": result, "problems": [],
                "next_action": action if app.experimental_skills else "Review these checkpoint summaries; use the local web panel for detailed comparisons.",
                "instructions": (
                    "Default agent history contains the 30 most recent checkpoints; all=true is only for an explicit user request. "
                    "For full event details and comparisons, "
                    "use the local Vestigraph web panel; do not page history automatically."),
                "next_page": None,
                "on_explicit_restore": next_call("restore", document_id=args["document_id"],
                    checkpoint_id="USER_SELECTED_CHECKPOINT_ID", session_id="ACTIVE_KLAYOUT_SESSION_ID",
                    reason="USER_REASON")}
    if name == "restore":
        document(args["document_id"])
        job = app.request_restore_in_editor(args["document_id"], args["checkpoint_id"], {
            "session_id": args["session_id"],
            "expected_session_instance": args["expected_session_instance"],
            "reason": args["reason"],
        })
        if not app.runner.wait_idle(300):
            raise ServiceError("RESTORE_TIMEOUT", "The restore job did not finish in time.", status=503)
        done = app.job(job["id"], allowed_projects)
        if done["status"] != "succeeded":
            error = done.get("error") or {}
            raise ServiceError(error.get("code", "RESTORE_FAILED"), error.get("message", "Restore failed."),
                               status=409, next_action=error.get("next_action"))
        return {"restored": True, **done["result"],
                "next_action": next_call("history", document_id=args["document_id"])}
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
