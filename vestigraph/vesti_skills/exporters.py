"""Export formats are adapters; SKILL.md is not the platform's storage format."""
import io
import json
import zipfile

class JsonSkill:
    id = "vestigraph-json"
    api_version = 1
    label = "Vestigraph skill JSON"
    def export(self, skill):
        return (json.dumps(skill, ensure_ascii=False, indent=2).encode("utf-8"),
                "application/json", "vestigraph-skill.json")


class MarkdownSkill:
    id = "agent-skill"
    api_version = 1
    label = "Agent SKILL.md bundle"

    def export(self, skill):
        name = "vestigraph-" + skill["id"][:12]
        # The target Agent skill validator forbids angle brackets in descriptions.
        # JSON quoting below handles escaping; provenance retains the original goal.
        description = skill["intent"]["goal"].replace("<", "").replace(">", "")[:1000] or "Reusable engineering procedure"
        markdown = ("---\nname: " + name + "\ndescription: " +
                    json.dumps(description, ensure_ascii=False) + "\n---\n\n" +
                    skill["body"] + "\n\nSource and verification: "
                    "[Vestigraph provenance](references/vestigraph.json).\n")
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            def write(path, text):
                info = zipfile.ZipInfo(path)
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, text)
            write(name + "/SKILL.md", markdown)
            write(name + "/references/vestigraph.json",
                             json.dumps(skill, ensure_ascii=False, indent=2))
            for path, text in sorted(skill.get("files", {}).items()):
                write(name + "/" + path, text)
        return out.getvalue(), "application/zip", name + ".zip"
