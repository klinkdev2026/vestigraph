"""A transparent local scaffold; it never claims that an AI inferred procedures."""
import json

class EvidenceDraft:
    id = "evidence-draft"
    api_version = 1
    label = "Evidence outline (no AI)"

    def generate(self, evidence, intent):
        parts = ["# " + intent["title"], "",
                 "## Goal", intent["goal"], "",
                 "## Applicability", intent.get("applicability") or "Not specified.",
                 "", "## Explanation supplied by the user",
                 intent.get("rationale") or "No design rationale was supplied.",
                 "", "## Parameters", intent.get("parameters") or "Not specified.",
                 "", "## Success criteria", intent.get("success_criteria") or "Not specified.",
                 "", "## Evidence"]
        for item in evidence.get("artifacts", []):
            parts.append("- " + item.get("title", item.get("id", "Artifact")) +
                         " [" + item.get("id", "") + "]")
        for item in evidence.get("changes", []):
            parts.append("- " + json.dumps(item, ensure_ascii=False, sort_keys=True))
        parts += ["", "## Procedure to derive",
                  "Use the attached evidence and user explanation to write actionable steps. "
                  "Separate measured facts, user intent, and inferences. "
                  "Do not infer a missing operation sequence from saved endpoints.",
                  "", "## Validation", "Reproduction has not been verified."]
        return {"body": "\n".join(parts), "kind": "outline"}


class ContractValidator:
    id = "skill-contract"
    api_version = 1
    label = "Content structure only"

    def validate(self, skill):
        return {"status": "passed" if skill["body"].strip() else "failed",
                "scope": "document_structure", "message": "Checked that the skill body is nonempty.",
                "limitations": ["No execution, replay, domain-rule or outcome validation."]}
