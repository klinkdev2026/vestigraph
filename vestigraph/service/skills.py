"""Skill application service shared by the web UI and registered agent tools."""
import copy
import re
from ..vesti_skills.catalog import SkillCatalog, encoded
from ..vesti_skills.contracts import default_services
from ..vesti_skills.draft import EvidenceDraft
from .catalog import fingerprint
from .errors import bad_request, conflict


def intent_of(value):
    allowed = {"title", "goal", "rationale", "applicability", "parameters", "success_criteria", "domain"}
    if not isinstance(value, dict) or set(value) - allowed:
        raise bad_request("Invalid skill explanation fields.")
    result = {}
    for key in allowed:
        text = value.get(key, "")
        limit = 200 if key in ("title", "domain") else 4000
        if not isinstance(text, str) or len(text) > limit:
            raise bad_request("Invalid or oversized skill explanation: " + key)
        result[key] = text.strip()
    if not result["title"] or not result["goal"]:
        raise bad_request("Skill name and goal are required.")
    return result


def content_of(body, files):
    if not isinstance(body, str) or not body.strip() or len(body.encode("utf-8")) > 65536:
        raise bad_request("Skill instructions must be nonempty and at most 64 KiB.")
    if not isinstance(files, dict) or len(files) > 20:
        raise bad_request("A skill may have at most 20 text supporting files.")
    used = set()
    for name, text in files.items():
        if (not isinstance(name, str) or len(name) > 160
                or not re.fullmatch(r"(scripts|references|assets)/[a-zA-Z0-9_./-]+", name)
                or any(part in ("", ".", "..") for part in name.split("/"))
                or name.lower() == "references/vestigraph.json"
                or name.lower() in used or not isinstance(text, str)):
            raise bad_request("Supporting files need unique safe relative paths under scripts, references or assets.")
        used.add(name.lower())
    if len(encoded(files).encode()) > 128 * 1024:
        raise bad_request("Supporting text files exceed 128 KiB.")
    return body.strip(), dict(files)


class Skills:
    def __init__(self, app, services=None):
        self.app = app
        self.services = services or default_services()
        self.catalog = SkillCatalog(app.catalog)
        app.runner.register("skill_generate", "heavy", self._generate)
        app.runner.register("skill_validate", "heavy", self._validate)

    def options(self):
        return {"schema_version": 1, "agent_handoff": True, **self.services.describe()}

    def list(self, project_id, before=None, state=None, limit=30):
        self.app.catalog.get_project(project_id)
        return self.catalog.list(project_id, before, state, limit)

    def get(self, sid, revision=None):
        return self.catalog.get(sid, revision)

    def create(self, document_id, intent, source, request_key=None):
        document = self.app.catalog.get_document(document_id)
        intent = intent_of(intent)
        if (not isinstance(source, dict) or set(source) != {"provider", "selection"}
                or not isinstance(source["provider"],str) or not isinstance(source["selection"],dict)):
            raise bad_request("A source provider and selection are required.")
        provider = self.services.sources.get(source["provider"])
        if provider is None:
            raise bad_request("The selected evidence source provider is not registered.")
        evidence = provider.collect(self.app, document, source["selection"])
        if not isinstance(evidence, dict):
            raise bad_request("Evidence provider returned an invalid bundle.")
        # Enforce a common provenance boundary even for domain-specific collectors.
        encoded(evidence)
        bundle = {"provider": provider.id, "selection": copy.deepcopy(source["selection"]),
                  "document_id": document_id, "data": evidence, "digest": fingerprint(evidence)}
        outline = EvidenceDraft().generate(evidence, intent)
        body, files = content_of(outline["body"], {})
        payload = {"intent": intent, "sources": bundle, "body": body, "files": files,
                   "state": "awaiting_agent", "body_kind": "outline",
                   "generation": {"provider": None, "actor": "user-request", "mode": "agent_handoff"},
                   "verification": {"status": "not_verified", "reports": []}}
        return self.catalog.create(document["project_id"], document_id, payload, request_key)

    def revise(self, sid, expected_revision, body, files=None, intent=None, actor="user", validation_note="", *, check_structure=False):
        body, files = content_of(body, {} if files is None else files)
        if not isinstance(actor, str) or not 1 <= len(actor) <= 100:
            raise bad_request("Invalid skill author.")
        if not isinstance(validation_note, str) or len(validation_note) > 4000:
            raise bad_request("Validation note must be at most 4000 characters.")
        updated_intent = intent_of(intent) if intent is not None else None
        structure_report = None
        if check_structure:
            from ..vesti_skills.draft import ContractValidator
            structure_report = ContractValidator().validate({"body": body, "files": files})
            if structure_report["status"] != "passed":
                raise bad_request("Skill structure check failed.", "Read the selected request and submit nonempty instructions.")
        def update(item):
            item.update(body=body, files=files, state="draft", body_kind="instructions")
            if updated_intent is not None:
                item["intent"] = updated_intent
            item["generation"] = {**item["generation"], "actor": actor, "mode": "submitted"}
            item["verification"] = {"status": "not_verified", "reports": []}
            if validation_note.strip():
                item["verification"] = {"status": "reported", "reports": [{
                    "status": "reported", "scope": "author_statement", "actor": actor,
                    "message": validation_note.strip(), "content_digest": fingerprint({"body":body,"files":files}),
                    "source_digest": item["sources"]["digest"]}]}
            if structure_report is not None:
                report = dict(structure_report, provider="skill-contract", revision=expected_revision + 1,
                              content_digest=fingerprint({"body": body, "files": files}),
                              source_digest=item["sources"]["digest"])
                item["verification"] = {"status": "checked", "reports": [report, *item["verification"]["reports"]]}
        return self.catalog.update(sid, expected_revision, update)

    def publish(self, sid, expected_revision):
        def update(item):
            if item["state"] == "awaiting_agent" or item["body_kind"] == "outline":
                raise bad_request("Complete and save the skill instructions before publishing.")
            item["state"] = "published"
        return self.catalog.update(sid, expected_revision, update)

    def request(self, sid, expected_revision, provider_id, kind, request_key=None):
        item = self.get(sid)
        if item["revision"] != expected_revision:
            raise conflict("SKILL_REVISION_CONFLICT", "Reload the latest skill before starting this task.")
        providers = self.services.generators if kind == "skill_generate" else self.services.validators
        if provider_id not in providers:
            raise bad_request("The selected skill provider is not registered.")
        job, _ = self.app.runner.submit(item["project_id"], kind, "skill", sid,
            {"revision":expected_revision, "provider":provider_id}, request_key=request_key)
        return job

    def _generate(self, job):
        current = self.get(job["target_id"], job["payload"]["revision"])
        provider = self.services.generators[job["payload"]["provider"]]
        result = provider.generate(copy.deepcopy(current["sources"]["data"]), copy.deepcopy(current["intent"]))
        body, files = content_of(result.get("body"), result.get("files", {}))
        def update(item):
            item.update(body=body, files=files, state="draft",
                        body_kind="outline" if result.get("kind") == "outline" else "instructions",
                        generation={"provider":provider.id,"actor":provider.id,"mode":"provider"},
                        verification={"status":"not_verified","reports":[]})
        item = self.catalog.update(current["id"], current["revision"], update)
        return {"skill_id":item["id"],"revision":item["revision"]}, None

    def _validate(self, job):
        current = self.get(job["target_id"], job["payload"]["revision"])
        provider = self.services.validators[job["payload"]["provider"]]
        report = provider.validate(copy.deepcopy(current))
        if not isinstance(report, dict) or report.get("status") not in ("passed","failed","not_verified") or not isinstance(report.get("scope"), str):
            raise bad_request("Validator must report its status and validation scope.")
        encoded(report)
        report.update(provider=provider.id, revision=current["revision"],
                      content_digest=fingerprint({"body":current["body"],"files":current["files"]}),
                      source_digest=current["sources"]["digest"])
        def update(item):
            item["verification"] = {"status": "checked", "reports": [report]}
        item = self.catalog.update(current["id"],current["revision"],update)
        return {"skill_id":item["id"],"revision":item["revision"],"report":report}, None

    def export(self, sid, exporter_id, revision=None):
        provider = self.services.exporters.get(exporter_id)
        if provider is None:
            raise bad_request("The selected skill exporter is not registered.")
        return provider.export(self.get(sid,revision))
