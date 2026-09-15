"""Synthetic public agent workflow check for installed Vestigraph builds.

Run with: python -I tools/check_agent_flow.py
It creates only temporary synthetic text history and never reads user skill/history data.
"""
from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from vestigraph.service.agent import invoke
from vestigraph.service.application import Application
from vestigraph.service.catalog import Catalog
from vestigraph.service.state import ServiceState
from vestigraph.store import Repository
from vestigraph.vesti_skills.contracts import default_services


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def run(state_root: str | Path | None = None, *, keep: bool = False) -> dict:
    """Run the guide/history/refine/skill/submit/export flow on synthetic data."""
    manager = None if state_root is not None else tempfile.TemporaryDirectory(prefix="vestigraph-agent-flow-")
    root = Path(state_root if state_root is not None else manager.name).resolve()
    state = root / "state"
    workspace = root / "workspace"
    history_root = root / "project-history"
    source_root = root / "source-history"
    workspace.mkdir(parents=True, exist_ok=True)
    Catalog.init(ServiceState.init(state))
    app = Application.open(state, acquire_instance=False, skill_services=default_services())
    try:
        project = app.catalog.add_project("Synthetic agent flow", workspace, history_root, allow_inside_git=True)
        source_repo = Repository.init(source_root, services=app.services)
        specimen = root / "specimen.txt"
        _write(specimen, "boundary=old\n")
        first = source_repo.checkpoint(specimen)
        _write(specimen, "boundary=new\n")
        second = source_repo.checkpoint(specimen)
        document = app.catalog.add_history(project["id"], source_repo.root, read_only=True)
        allowed = {project["id"]}
        guide = invoke(app, "guide", {}, allowed)
        history = invoke(app, "history", {"document_id": document["id"]}, allowed)
        request = {
            "document_id": document["id"],
            "from_id": first["id"],
            "to_id": second["id"],
            "history_revision": source_repo.history_revision(),
            "title": "Synthetic boundary update",
            "goal": "Document the boundary update procedure",
            "rationale": "Synthetic installed-build flow check.",
        }
        refined = invoke(app, "refine", request, allowed)
        skill = invoke(app, "skill", {"skill_id": refined["skill"]["id"]}, allowed)
        submitted = invoke(app, "submit", {
            "skill_id": skill["skill"]["id"],
            "expected_revision": skill["skill"]["revision"],
            "body": "# Procedure\nUpdate the synthetic boundary value and verify the saved text content.",
            "validation_note": "Synthetic structure check only.",
        }, allowed)
        exported = invoke(app, "export", {
            "skill_id": skill["skill"]["id"],
            "expected_revision": submitted["revision"],
        }, allowed)
        result = {
            "ok": True,
            "project_id": project["id"],
            "document_id": document["id"],
            "skill_id": skill["skill"]["id"],
            "submitted_revision": submitted["revision"],
            "export_sha256": exported["sha256"],
            "export_path": exported["path"] if keep or state_root is not None else None,
            "next_actions": [guide["next_action"], history["next_action"], skill["next_action"], submitted["next_action"], exported["next_action"]],
        }
        return result
    finally:
        app.stop()
        if manager is not None and not keep:
            manager.cleanup()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a synthetic Vestigraph public agent workflow check.")
    parser.add_argument("--state-root", help="Directory for synthetic state/history. Defaults to a temporary directory.")
    parser.add_argument("--keep", action="store_true", help="Keep the temporary directory and report the export path.")
    args = parser.parse_args(argv)
    print(json.dumps(run(args.state_root, keep=args.keep), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
