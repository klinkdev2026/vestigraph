"""Offline inspection and recovery into a NEW history; never delete source evidence."""
import hashlib
import json
from pathlib import Path
import re
import shutil
import sqlite3

from .packs import scan_pack, LAYOUT_OBJECT_LIMIT
from .object_access import ObjectIndex
from .engine import export_v2
from ..store import Repository, RepositoryError, WRITER_LOCK
from ..filelock import FileLock


class _Sink:
    def write(self, data):
        return len(data)


def _pack_files(root):
    for folder in (root / "packs", root / "quarantine" / "packs"):
        for path in sorted(folder.glob("*.pack")):
            if re.fullmatch(r"[0-9a-f]{32}", path.stem):
                if path.is_symlink() or not path.resolve().is_relative_to(root):
                    raise RepositoryError("Recovery refuses a pack outside the history.")
                yield path


def _inspect(repo):
    report = {"ok": True, "objects_checked": 0, "checkpoints_checked": 0,
              "packs_checked": 0, "issues": [], "issue_count": 0, "unindexed_packs": []}
    def issue(kind, identifier, exc):
        report["ok"] = False
        report["issue_count"] += 1
        if len(report["issues"]) < 100:
            report["issues"].append({"kind": kind, "id": identifier, "message": str(exc)[:500]})
    with repo._connect() as db:
        check = db.execute("PRAGMA quick_check").fetchone()[0]
        if check != "ok":
            issue("database", "index", check)
        for path in _pack_files(repo.root):
            known = db.execute("SELECT 1 FROM packs WHERE id=?", (path.stem,)).fetchone()
            if not known and len(report["unindexed_packs"]) < 100:
                report["unindexed_packs"].append(str(path.relative_to(repo.root)))
            try:
                for _ in scan_pack(path, registry=repo.services.codecs.registry):
                    pass
                report["packs_checked"] += 1
            except (OSError, ValueError) as exc:
                issue("pack", path.stem, exc)
        index = ObjectIndex(repo)
        try:
            for row in db.execute("SELECT hash FROM objects"):
                try:
                    index.get(row[0], LAYOUT_OBJECT_LIMIT)
                    report["objects_checked"] += 1
                except (OSError, ValueError, RuntimeError) as exc:
                    issue("object", row[0], exc)
            for row in db.execute("SELECT * FROM checkpoints ORDER BY ordinal"):
                try:
                    export_v2(repo, repo._row(row), _Sink())
                    report["checkpoints_checked"] += 1
                except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
                    issue("checkpoint", row["id"], exc)
        finally:
            index.close()
    return report


def inspect_history(repo):
    if repo.format != 2:
        raise RepositoryError("Integrity inspection currently requires storage format 2.")
    with FileLock(repo.root / WRITER_LOCK, {"owner": "vestigraph integrity inspection"}):
        return _inspect(repo)


def rebuild_index(repo, destination):
    """Copy surviving metadata/packs and rebuild object rows, with a verification report.

    Pack records cannot recover lost checkpoint titles, manifests, timestamps or
    evidence. Those require a database backup. Unindexed bytes remain preserved.
    """
    destination = Path(destination).expanduser().resolve()
    if destination.exists() or destination.is_relative_to(repo.root) or repo.root.is_relative_to(destination):
        raise RepositoryError("Recovery destination must be a new folder outside the source history.")
    if repo.format != 2:
        raise RepositoryError("Index recovery currently requires storage format 2.")
    with FileLock(repo.root / WRITER_LOCK, {"owner": "vestigraph index recovery"}):
        destination.mkdir(parents=True)
        marker = destination / "recovery-incomplete.json"
        marker.write_text(json.dumps({"source": str(repo.root)}), encoding="utf-8")
        # Copy all auxiliary evidence, but SQLite through its online backup API.
        for source in repo.root.iterdir():
            if source.name in ("index.sqlite3", "index.sqlite3-wal", "index.sqlite3-shm", WRITER_LOCK, "tmp"):
                continue
            if source.is_symlink():
                raise RepositoryError("Recovery refuses symlinked history content.")
            target = destination / source.name
            if source.is_dir():
                for child in source.rglob("*"):
                    if child.is_symlink():
                        raise RepositoryError("Recovery refuses symlinked history content.")
                shutil.copytree(source, target)
            else:
                shutil.copy2(source, target)
        (destination / "tmp").mkdir()
        (destination / "packs").mkdir(exist_ok=True)
        with repo._connect() as source, sqlite3.connect(destination / "index.sqlite3") as target:
            source.backup(target)
        recovered = Repository(destination, services=repo.services)
        registry = repo.services.codecs.registry
        missing_metadata = []
        with recovered._connect() as db:
            db.execute("DELETE FROM objects")
            db.execute("DELETE FROM packs")
            for source in _pack_files(repo.root):
                target = recovered.packs_dir / source.name
                if target.exists():
                    def digest(path):
                        h = hashlib.sha256()
                        with path.open("rb") as stream:
                            for chunk in iter(lambda: stream.read(1024*1024), b""):
                                h.update(chunk)
                        return h.digest()
                    if digest(source) != digest(target):
                        raise RepositoryError("Conflicting pack copies; preserve both and inspect before recovery.")
                else:
                    shutil.copy2(source, target)
                db.execute("INSERT OR IGNORE INTO packs VALUES(?,?,?,?)",
                           (source.stem, source.stem, target.stat().st_size, "recovered"))
                if not db.execute("SELECT 1 FROM checkpoints WHERE id=?", (source.stem,)).fetchone():
                    if source.stem not in missing_metadata:
                        missing_metadata.append(source.stem)
                for row in scan_pack(target, registry=registry):
                    db.execute("INSERT INTO objects(hash,offset,raw_size,stored_size,codec,depth,base_hash,"
                               "base_raw_size,stored_sha256,pack) VALUES(?,?,?,?,?,?,?,?,?,?) "
                               "ON CONFLICT(hash) DO UPDATE SET offset=excluded.offset,raw_size=excluded.raw_size,"
                               "stored_size=excluded.stored_size,codec=excluded.codec,depth=excluded.depth,"
                               "base_hash=excluded.base_hash,base_raw_size=excluded.base_raw_size,"
                               "stored_sha256=excluded.stored_sha256,pack=excluded.pack "
                               "WHERE excluded.depth < objects.depth", (*row, source.stem))
        report = _inspect(recovered)
        report.update(destination=str(destination), checkpoint_metadata_missing_for_packs=missing_metadata,
                      history_metadata_complete=not missing_metadata)
        (destination / "recovery-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        if report["ok"]:
            marker.unlink()
        return report
