"""Command-line interface for Vestigraph."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from . import __version__


def _emit(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vestigraph", description="Local layout history and checkpoint tools")
    parser.add_argument("--repo", default=".vestigraph", metavar="DIR", help="Vestigraph data directory")
    commands = parser.add_subparsers(dest="command", required=True)
    parser.add_argument("--version", action="version", version="%(prog)s " + __version__)

    p = commands.add_parser("setup", help="Manual legacy path: install the KLink KLayout plugin and register automatic history")
    p.add_argument("--port", type=int, default=8787, help="Preferred history web port, not the KLayout RPC port")

    p = commands.add_parser("doctor", help="Check required Python dependencies without connecting to KLayout")
    p.add_argument("--integration", action="store_true", help="Also check the installed KLink plugin and companion registration")

    commands.add_parser("init", help="Initialize a history repository")
    p = commands.add_parser("checkpoint", help="Save a file checkpoint")
    p.add_argument("file", metavar="FILE")
    p.add_argument("--title", default="")
    p.add_argument("--source", default="manual")
    p.add_argument("--segment")

    p = commands.add_parser("history", help="List checkpoints")
    p.add_argument("--limit", type=int, default=50)
    p = commands.add_parser("show", help="Show a checkpoint")
    p.add_argument("id", metavar="ID")
    p = commands.add_parser("events", help="List events")
    p.add_argument("--segment")
    p.add_argument("--limit", type=int, default=200)
    p = commands.add_parser("segments", help="List process segments")
    p.add_argument("--limit", type=int, default=50)
    p = commands.add_parser("segment-start", help="Start a process segment")
    p.add_argument("title", metavar="TITLE")
    p.add_argument("--source", default="mixed")
    p = commands.add_parser("segment-end", help="End a process segment")
    p.add_argument("id", metavar="ID")
    p.add_argument("--status", default="closed")
    p = commands.add_parser("export", help="Export the file from a checkpoint")
    p.add_argument("id", metavar="ID")
    p.add_argument("destination", metavar="DEST")

    p = commands.add_parser("changes", help="List recorded checkpoint changes (ChangeSet v1)")
    p.add_argument("id", metavar="ID")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--cursor")
    p.add_argument("--kind")
    p = commands.add_parser("changes-export", help="Export recorded checkpoint changes as standalone JSONL")
    p.add_argument("id", metavar="ID")
    p.add_argument("destination", metavar="OUTPUT")

    commands.add_parser("fsck", help="Verify history without changing its data (stop writers first)")
    p = commands.add_parser("rebuild-index", help="Recover object index into a new history folder")
    p.add_argument("destination")
    commands.add_parser("stats", help="Show storage statistics")

    p = commands.add_parser("observe", help="Observe a local KLink session")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--idle", type=float, default=5.0)
    p.add_argument("--min-interval", type=float, default=15.0)
    p.add_argument("--max-interval", type=float, default=60.0)
    p.add_argument("--duration", type=float)

    p = commands.add_parser("capabilities", help="Discover registered engineering capabilities")
    p.add_argument("--query", default="")
    p.add_argument("--limit", type=int, default=20)
    choice = p.add_mutually_exclusive_group()
    choice.add_argument("--describe", metavar="NAME")
    choice.add_argument("--mcp-tools", action="store_true", help="Print tool schemas; does not start a server")
    choice.add_argument("--invoke", metavar="NAME")
    p.add_argument("--arguments", default="{}", help="JSON arguments for a registered read-only capability")

    from .service.cli_commands import add_parsers
    add_parsers(commands)
    return parser


def _limit(parser: argparse.ArgumentParser, value: int) -> int:
    if value <= 0:
        parser.error("--limit must be greater than 0")
    return value


def _run(args: argparse.Namespace, parser: argparse.ArgumentParser, *, services=None, skill_services=None) -> Any:
    if args.command in ("setup", "doctor"):
        from vestigraph_backends.vesti_backend_klayout.installation import setup, doctor
        return setup(port=args.port) if args.command == "setup" else (doctor(integration=True) if args.integration else doctor())
    if args.command in ("service", "serve", "companion"):
        from .service.cli_commands import run as run_service
        return run_service(args, services=services, skill_services=skill_services)

    from .store import Repository
    root = Path(args.repo)
    if args.command == "init":
        Repository.init(root, services=services)
        return {"repo": str(root), "initialized": True}
    if args.command in ("history", "show", "events", "segments", "stats", "changes", "changes-export", "capabilities", "fsck", "rebuild-index"):
        repo = Repository.open_readonly(root, services=services)          # never takes the writer lease for a read
    else:
        repo = Repository(root, services=services)

    if args.command in ("fsck", "rebuild-index"):
        from .storage.recovery import inspect_history, rebuild_index
        return inspect_history(repo) if args.command == "fsck" else rebuild_index(repo, args.destination)
    if args.command == "capabilities":
        from .vesti_runtime.repository_capabilities import repository_capabilities
        catalog = repository_capabilities(repo)
        if args.describe:
            return catalog.describe(args.describe)
        if args.mcp_tools:
            return {"tools": catalog.mcp_tools()}
        if args.invoke:
            return catalog.invoke(args.invoke, json.loads(args.arguments))
        return catalog.search(args.query, args.limit)
    if args.command == "checkpoint":
        return repo.checkpoint(args.file, title=args.title, source=args.source, segment_id=args.segment)
    if args.command == "history":
        return repo.history(limit=_limit(parser, args.limit))
    if args.command == "show":
        return repo.get_checkpoint(args.id)
    if args.command == "events":
        return repo.events(segment_id=args.segment, limit=_limit(parser, args.limit))
    if args.command == "segments":
        return repo.segments(limit=_limit(parser, args.limit))
    if args.command == "segment-start":
        return repo.begin_segment(args.title, source=args.source)
    if args.command == "segment-end":
        return repo.close_segment(args.id, status=args.status)
    if args.command == "export":
        return {"checkpoint_id": args.id, "destination": str(repo.export(args.id, args.destination))}
    if args.command == "changes":
        return repo.changes(args.id, limit=_limit(parser, args.limit), cursor=args.cursor, kind=args.kind)
    if args.command == "changes-export":
        return repo.export_changes(args.id, args.destination)
    if args.command == "stats":
        return repo.stats()
    if args.command == "observe":
        from vestigraph_backends.vesti_backend_klayout.capture import observe
        return {"checkpoints": observe(repo, host=args.host, port=args.port, idle_seconds=args.idle,
                                       min_interval=args.min_interval, max_interval=args.max_interval,
                                       duration=args.duration)}
    parser.error(f"Unknown command: {args.command}")


def _utf8_console() -> None:
    # Windows consoles default to the ANSI code page (e.g. cp936), which turns
    # ensure_ascii=False JSON into mojibake. Bytes were always correct; make the
    # display match. Pipes/files already get UTF-8 through PYTHONIOENCODING.
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream.encoding and stream.encoding.lower().replace("-", "") != "utf8":
                stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass


def main(argv: Sequence[str] | None = None, *, services=None, skill_services=None) -> int:
    _utf8_console()
    parser = _parser()
    try:
        args = parser.parse_args(argv)
        result = _run(args, parser, services=services, skill_services=skill_services)
        _emit(result)
        if args.command == "doctor" and not result["ok"]:
            return 1
        if args.command in ("fsck", "rebuild-index"):
            if not result["ok"]:
                return 1
            if not result.get("history_metadata_complete", True):
                return 2
        return 0
    except KeyboardInterrupt:
        print("vestigraph: interrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"vestigraph: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
