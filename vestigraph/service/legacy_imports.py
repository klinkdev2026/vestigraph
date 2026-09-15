"""Legacy files -> confirmed, resumable import plans. No source files are modified."""
import hashlib
import json
import os
import re
import threading
import uuid
from pathlib import Path
from datetime import datetime
import time

from ..store import Repository, RepositoryError, SaveCancelled
from ..history_order import writer
from .errors import ServiceError, bad_request
from .state import now_iso

MAX_ITEMS = 200
MAX_PLANS = 32
MAX_FILE_BYTES = 16 * 1024**3


class LegacyImports:
    def __init__(self, app):
        self.app = app
        self.root = app.state.root / "imports"
        self.lock = threading.RLock()
        app.runner.register("legacy_import_plan", "heavy", self.prepare_job)
        app.runner.register("legacy_import", "coordinate", self.import_job)

    def document(self, did):
        doc = self.app.catalog.get_document(did)
        if doc["read_only"]:
            raise ServiceError("DOCUMENT_READ_ONLY", "This history is read-only.", status=409)
        return doc

    def path(self, did, bid):
        self.app.catalog.get_document(did)
        if not all(isinstance(v, str) and re.fullmatch(r"[0-9a-f]{32}", v) for v in (did, bid)):
            raise bad_request("Invalid import identity.")
        return self.root / did / (bid + ".json")

    def _read(self, did, bid):
        p = self.path(did, bid)
        if not p.is_file():
            raise ServiceError("NOT_FOUND", "Import plan not found.", status=404)
        if p.stat().st_size > 2 * 1024**2:
            raise bad_request("Import plan is oversized.")
        plan = json.loads(p.read_text(encoding="utf-8"))
        if plan["status"] in ("running", "ready") and plan.get("active_job_id"):
            job = self.app.catalog.get_job(plan["active_job_id"])
            if job["status"] not in ("queued", "running"):
                plan["status"] = "paused"
        return plan

    def _write(self, plan):
        p = self.path(plan["document_id"], plan["id"])
        p.parent.mkdir(parents=True, exist_ok=True)
        temporary = p.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(plan, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, p)

    @staticmethod
    def public(plan):
        return {**{k: v for k, v in plan.items() if k != "items"},
                "items": [{k: v for k, v in item.items() if k != "path"} for item in plan["items"]]}

    def get(self, did, bid):
        with self.lock:
            return self.public(self._read(did, bid))

    def list(self, did):
        self.app.catalog.get_document(did)
        folder = self.root / did
        with self.lock:
            paths = sorted(folder.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:MAX_PLANS]
            plans = [self._read(did, p.stem) for p in paths]
            return [{**{k: v for k, v in p.items() if k != "items"},
                     "total": len(p["items"]), "completed": sum(bool(i["checkpoint_id"]) for i in p["items"])} for p in plans]

    def prepare(self, did, paths, request_key=None):
        doc = self.document(did)
        if not isinstance(paths, (list, tuple)) or not 1 <= len(paths) <= MAX_ITEMS:
            raise bad_request(f"Select between 1 and {MAX_ITEMS} files.")
        repo = Repository.open_readonly(doc["store_path"], services=self.app.services)
        if repo.format != 2 or not repo.history(1):
            raise bad_request("Save a current version in a format-2 history before importing older files.")
        paths = [str(Path(p).expanduser().resolve()) for p in paths]
        if any(len(p) > 4096 for p in paths):
            raise bad_request("File path is too long.")
        with self.lock:
            if len(list((self.root / did).glob("*.json"))) >= MAX_PLANS:
                raise bad_request("Remove an old import plan before creating another.")
        job, _ = self.app.runner.submit(doc["project_id"], "legacy_import_plan", "document", did,
                                        {"paths": paths}, request_key=request_key)
        return job

    def prepare_job(self, job):
        did = job["target_id"]
        doc = self.document(did)
        repo = Repository.open_readonly(doc["store_path"], services=self.app.services)
        from .. import history_order
        with repo._connect() as db:
            if history_order.enabled(db):
                row = db.execute("SELECT checkpoint_id FROM history_timeline ORDER BY position LIMIT 1").fetchone()
            else:
                row = db.execute("SELECT id FROM checkpoints ORDER BY ordinal LIMIT 1").fetchone()
        if row is None:
            raise bad_request("The history has no current version.")
        plan = {"id": job["id"], "document_id": did, "created_at": now_iso(), "status": "draft",
                "before_id": row[0], "cancel_requested": False, "items": []}
        paths = sorted(job["payload"]["paths"], key=lambda s: [int(x) if x.isdigit() else x.casefold() for x in re.split(r"(\d+)", Path(s).name)])
        seen = {}
        for n, path in enumerate(paths):
            item = {"id": uuid.uuid4().hex, "path": path, "name": Path(path).name,
                    "title": Path(path).stem, "note": "", "historical_at": "", "checkpoint_id": None,
                    "error": None, "sha256": None, "size": None, "mtime_ns": None}
            try:
                if Path(path).suffix.lower() not in (".gds", ".gds2", ".oas", ".oasis"):
                    raise ValueError("Select GDS or OASIS files.")
                digest, st = self.fingerprint(Path(path))
                item.update(sha256=digest, size=st.st_size, mtime_ns=st.st_mtime_ns,
                            duplicate_of=seen.get(digest))
                seen.setdefault(digest, item["id"])
            except (OSError, ValueError) as exc:
                item["error"] = str(exc)[:500]
            plan["items"].append(item)
            self.app.catalog.update_job(job["id"], progress=(n+1)/len(paths))
        with self.lock:
            self._write(plan)
        return {"plan_id": plan["id"]}, None

    def fingerprint(self, path):
        before = path.stat()
        if not path.is_file() or not 0 < before.st_size <= MAX_FILE_BYTES:
            raise ValueError("Expected a nonempty file no larger than 16 GiB.")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            while True:
                if self.app.runner.stopping.is_set():
                    raise ServiceError("SERVICE_STOPPED", "Import interrupted by shutdown.", status=409)
                block = stream.read(4 * 1024**2)
                if not block:
                    break
                digest.update(block)
            after = os.fstat(stream.fileno())
        stamp = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns)
        if stamp(before) != stamp(opened) or stamp(opened) != stamp(after) or stamp(after) != stamp(path.stat()):
            raise ValueError("Source changed while preparing the plan; select it again.")
        return digest.hexdigest(), after

    def confirm(self, did, bid, before_id, items, request_key=None):
        doc = self.document(did)
        repo = Repository.open_readonly(doc["store_path"], services=self.app.services)
        repo.get_checkpoint(before_id)
        with self.lock:
            plan = self._read(did, bid)
            if items is not None and (not isinstance(items, list) or any(not isinstance(i, dict) for i in items)):
                raise bad_request("Invalid import items.")
            if request_key and plan.get("active_job_id"):
                previous_job = self.app.catalog.get_job(plan["active_job_id"])
                if previous_job.get("request_key") == request_key:
                    expected = [{k: i[k] for k in ("id", "title", "note", "historical_at")} for i in plan["items"]]
                    supplied = [{k: i.get(k, next((v[k] for v in plan["items"] if v["id"] == i.get("id")), "")) for k in ("id", "title", "note", "historical_at")} for i in items] if isinstance(items, list) else None
                    if before_id != plan["before_id"] or (supplied is not None and supplied != expected):
                        raise bad_request("This request ID was used with a different import order.")
                    return previous_job
            if plan["status"] == "ready" and plan.get("active_job_id"):
                raise bad_request("The import is already queued.")
            if plan["status"] not in ("draft", "ready", "paused", "failed", "completed"):
                raise bad_request("The import is running.")
            if plan["status"] == "draft":
                known = {i["id"]: i for i in plan["items"]}
                if not isinstance(items, list) or not 1 <= len(items) <= MAX_ITEMS:
                    raise bad_request("Confirm a nonempty file order.")
                selected, ids = [], set()
                for given in items:
                    if not isinstance(given, dict) or given.get("id") not in known or given["id"] in ids:
                        raise bad_request("Invalid or duplicate import item.")
                    item = dict(known[given["id"]])
                    if item["error"] or not item["sha256"]:
                        raise bad_request("Remove unreadable files before importing.")
                    for key, limit in (("title", 200), ("note", 2000), ("historical_at", 64)):
                        value = given.get(key, item[key])
                        if not isinstance(value, str) or len(value) > limit or (key=="title" and not value.strip()):
                            raise bad_request("Invalid import label or date.")
                        if key == "historical_at" and value:
                            try:
                                datetime.fromisoformat(value.replace("Z", "+00:00"))
                            except ValueError:
                                raise bad_request("Historical time must be an ISO date or date-time.") from None
                        item[key] = value
                    ids.add(item["id"])
                    selected.append(item)
                plan.update(items=selected, before_id=before_id, status="ready")
            elif before_id != plan["before_id"] or items is not None:
                raise bad_request("A confirmed import can only resume its original order.")
            plan["cancel_requested"] = False
            self._write(plan)
            try:
                job, _ = self.app.runner.submit(doc["project_id"], "legacy_import", "document", did,
                                                {"plan_id": bid}, request_key=request_key)
            except Exception as exc:
                plan.update(status="paused", error=str(exc)[:500])
                self._write(plan)
                raise
            plan["active_job_id"] = job["id"]
            self._write(plan)
        return job

    def cancel(self, did, bid):
        with self.lock:
            plan = self._read(did, bid)
            plan["cancel_requested"] = True
            self._write(plan)
        return self.public(plan)

    def delete(self, did, bid):
        with self.lock:
            plan = self._read(did, bid)
            if plan["status"] in ("running", "ready"):
                raise bad_request("Cancel and wait for the import before removing its plan.")
            self.path(did, bid).unlink()
        return {"removed": True, "versions_preserved": True}

    def import_job(self, job):
        did, bid = job["target_id"], job["payload"]["plan_id"]
        self.document(did)
        held, repo = [], None
        with self.lock:
            plan = self._read(did, bid)
            plan.pop("error", None)
            plan["status"] = "running"
            self._write(plan)
        try:
            # Serialized with relocation/navigation. Freeze recorders while holding the store lease.
            for coordinator in self.app.supervisor.coordinators(job["project_id"]):
                held.append(coordinator)
                coordinator.wait_reply(coordinator.request("navigation_hold"), 300)
            doc = self.document(did)
            repo = Repository(doc["store_path"], services=self.app.services)
            with writer(repo):
                for index in range(len(plan["items"])):
                    with self.lock:
                        plan = self._read(did, bid)
                    if plan["cancel_requested"] or self.app.runner.stopping.is_set():
                        plan["status"] = "paused"
                        break
                    item = plan["items"][index]
                    try:
                        result = repo.import_checkpoint(item["path"], before_id=plan["before_id"],
                            batch_id=bid, item_id=item["id"], expected_sha256=item["sha256"],
                            filename=item["name"], title=item["title"],
                            metadata={"legacy_import": {"observed_mtime_ns": item["mtime_ns"],
                                      "historical_at": item["historical_at"] or None, "note": item["note"]}},
                            cancel=_Cancelled(self, did, bid))
                        item.update(checkpoint_id=result["id"], error=None)
                    except (OSError, RepositoryError) as exc:
                        item["error"] = str(exc)[:500]
                        plan["status"] = "paused" if isinstance(exc, SaveCancelled) else "failed"
                        break
                    finally:
                        with self.lock:
                            plan["cancel_requested"] = self._read(did, bid)["cancel_requested"]
                            self._write(plan)
                    self.app.catalog.update_job(job["id"], progress=(index+1)/len(plan["items"]))
                else:
                    plan["status"] = "completed"
        except Exception as exc:
            plan.update(status="failed", error=str(exc)[:500])
        finally:
            try:
                with self.lock:
                    plan["cancel_requested"] = self._read(did, bid)["cancel_requested"]
                    self._write(plan)
            finally:
                release_error = None
                for coordinator in held:
                    try:
                        coordinator.wait_reply(coordinator.request("navigation_release"), 60)
                    except Exception as exc:
                        release_error = exc
                self.app.supervisor.bump()
                if release_error is not None:
                    raise release_error
        return {"plan_id": bid, "status": plan["status"],
                "completed": sum(bool(i["checkpoint_id"]) for i in plan["items"]),
                "total": len(plan["items"])}, None


class _Cancelled:
    def __init__(self, owner, did, bid):
        self.owner, self.did, self.bid = owner, did, bid
        self.checked, self.cancelled = 0.0, False

    def is_set(self):
        if self.owner.app.runner.stopping.is_set():
            return True
        if time.monotonic() - self.checked > 0.2:
            with self.owner.lock:
                self.cancelled = self.owner._read(self.did, self.bid)["cancel_requested"]
            self.checked = time.monotonic()
        return self.cancelled
