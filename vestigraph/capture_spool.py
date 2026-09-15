"""Crash-resumable capture copies. This is not a replacement for the object store."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import threading
import uuid

from . import history_order
from .store import RepositoryError, _metadata, _now

GiB = 1024 ** 3
DONE = ("committed", "unchanged")


def capture_suffix(metadata, *, formats=None):
    """Old reservations default to .gds; new ones persist their validated suffix."""
    from .vesti_formats.registry import FORMATS
    formats = formats if formats is not None else FORMATS.copy(frozen=True)
    suffix = metadata.get("spool_suffix", ".gds")
    allowed = {ext for item in formats.describe() for ext in item["extensions"]}
    if suffix not in allowed:
        raise SpoolError("Invalid capture file extension.")
    return suffix


class SpoolError(RepositoryError):
    pass


class SpoolFull(SpoolError):
    pass


class CaptureQueueBlocked(SpoolError):
    """Accepted backlog or an export of unknown completeness needs inspection."""


class AcceptancePending(CaptureQueueBlocked):
    """Export verified on disk, but its database acceptance must be retried."""


def _exists(db):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='capture_queue'").fetchone() is not None


def _stats(db, *, max_items=3, max_bytes=8*GiB, max_file_bytes=2*GiB):
    disabled = {"enabled": False, "pending_count": 0, "pending_bytes": 0, "processing": False,
                "blocked_count": 0, "last_captured_at": None, "last_completed_at": None,
                "last_error": None, "capacity_count": max_items, "capacity_bytes": max_bytes, "full": False}
    if not _exists(db):
        return disabled
    # The active index bounds status cost by outstanding work, not checkpoint count.
    rows = db.execute("SELECT state,size,reserved_bytes,error,created_at FROM capture_queue "
                      "WHERE reserved_bytes>0").fetchall()
    pending = [r for r in rows if r["state"] not in (*DONE, "quarantined")]
    last = db.execute("SELECT metadata FROM capture_queue WHERE sha256 IS NOT NULL ORDER BY ordinal DESC LIMIT 1").fetchone()
    done = db.execute("SELECT metadata FROM capture_queue WHERE state IN ('committed','unchanged') "
                      "ORDER BY ordinal DESC LIMIT 1").fetchone()
    used = sum(r["reserved_bytes"] for r in rows)
    return dict(disabled, enabled=True, pending_count=len(pending),
                pending_bytes=sum(r["size"] for r in pending),
                reserved_bytes=used, processing=any(r["state"] == "processing" for r in pending),
                blocked_count=sum(r["state"] == "blocked" for r in pending),
                quarantined_count=sum(r["state"] == "quarantined" for r in rows),
                quarantined_bytes=sum(r["size"] for r in rows if r["state"] == "quarantined"),
                last_error=next((r["error"] for r in reversed(rows) if r["error"]), None),
                last_captured_at=(json.loads(last[0]).get("captured_at") if last else None),
                last_completed_at=(json.loads(done[0]).get("completed_at") if done else None),
                # Quarantined copies keep their bytes reserved (they are still on disk) but do not
                # take one of the item slots: three failed exports must not stop recording for good.
                full=len([r for r in rows if r["state"] != "quarantined"]) >= max_items
                     or used + max_file_bytes > max_bytes)


def pending_stats(repo):
    """Read-only status, including old histories. Never initializes queue schema."""
    with repo._connect() as db:
        return _stats(db)


def blocked_head(repo):
    """The retained copy that stops the organizer, or None. Read-only; no schema is created.

    Only a BLOCKED head (an accepted copy whose organizing failed) is reported: a 'writing'
    head is settled by ``Spool.recover()`` when the next lease owner starts."""
    with repo._connect() as db:
        if not _exists(db):
            return None
        row = db.execute("SELECT id,state,error,created_at FROM capture_queue "
                         "WHERE state NOT IN ('committed','unchanged','quarantined') ORDER BY ordinal LIMIT 1").fetchone()
    if row is None or row["state"] != "blocked":
        return None
    return {"id": row["id"], "state": row["state"], "error": row["error"], "created_at": row["created_at"]}


class Spool:
    def __init__(self, repo, *, max_items=3, max_bytes=8*GiB, max_file_bytes=2*GiB, min_free_bytes=GiB):
        for key, value in dict(max_items=max_items, max_bytes=max_bytes,
                               max_file_bytes=max_file_bytes, min_free_bytes=min_free_bytes).items():
            if isinstance(value, bool) or not isinstance(value, int) or value < (0 if key == "min_free_bytes" else 1):
                raise ValueError(f"{key} must be a positive integer (min_free_bytes may be zero).")
        if max_file_bytes > max_bytes:
            raise ValueError("max_file_bytes cannot exceed max_bytes.")
        self.repo, self.root = repo, repo.root / "capture-spool"
        self.max_items, self.max_bytes = max_items, max_bytes
        self.max_file_bytes, self.min_free_bytes = max_file_bytes, min_free_bytes
        self.lock = threading.RLock()
        self._writer()
        if repo.format != 2:
            raise SpoolError("Durable capture requires format 2.")
        if self.root.is_symlink() or self.root.resolve().parent != repo.root.resolve():
            raise SpoolError("Capture spool must be a local directory inside this history.")
        self.root.mkdir(exist_ok=True)
        repo.sync_dir(repo.root)
        with repo._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS capture_queue (
                ordinal INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
                state TEXT NOT NULL, size INTEGER NOT NULL DEFAULT 0,
                reserved_bytes INTEGER NOT NULL, sha256 TEXT,
                metadata TEXT NOT NULL, checkpoint_id TEXT, error TEXT, created_at TEXT NOT NULL)""")
            db.execute("CREATE INDEX IF NOT EXISTS capture_queue_active ON capture_queue(ordinal) WHERE reserved_bytes>0")
            db.execute("CREATE INDEX IF NOT EXISTS capture_queue_pending ON capture_queue(ordinal) "
                       "WHERE state NOT IN ('committed','unchanged','quarantined')")

            self._reconcile_orphans(db)

    def _reconcile_orphans(self, db):
        """Account old pre-INSERT orphans; unknown copies are never auto-published/deleted."""
        formats = self.repo.services.formats
        allowed = {ext for item in formats.describe() for ext in item["extensions"]}
        # Repair older max-file reservations and recount copies that a timed-out exporter
        # may have finished writing after quarantine. Never auto-publish these bytes.
        retained = db.execute("SELECT id,metadata FROM capture_queue WHERE state='quarantined'").fetchall()
        for row in retained:
            path = self._path(row["id"], json.loads(row["metadata"]))
            size = path.stat().st_size if path.exists() else 0
            db.execute("UPDATE capture_queue SET size=?,reserved_bytes=? WHERE id=?", (size, size, row["id"]))
        for path in self.root.iterdir():
            if path.suffix not in allowed:
                continue
            capture_id = path.stem
            orphan_meta = {"spool_suffix": path.suffix}
            path = self._path(capture_id, orphan_meta)
            if db.execute("SELECT 1 FROM capture_queue WHERE id=?", (capture_id,)).fetchone():
                continue
            size = path.stat().st_size
            db.execute("INSERT INTO capture_queue(id,state,size,reserved_bytes,metadata,error,created_at) "
                       "VALUES(?,?,?,?,?,?,?)",
                       (capture_id, "quarantined", size, max(1, size), json.dumps(orphan_meta),
                        "orphan_export: no reservation record; retained and counted against capacity", _now()))

    def _writer(self):
        if not self.repo.writer_held or self.repo.readonly:
            raise SpoolError("Capture spool writes require the history writer lease.")

    def _path(self, capture_id, metadata=None):
        if not isinstance(capture_id, str) or not re.fullmatch("[0-9a-f]{32}", capture_id):
            raise SpoolError("Invalid capture id.")
        if metadata is None:
            with self.repo._connect() as db:
                row = db.execute("SELECT metadata FROM capture_queue WHERE id=?", (capture_id,)).fetchone()
            metadata = json.loads(row[0]) if row else {}
        path = self.root / (capture_id + capture_suffix(metadata, formats=self.repo.services.formats))
        # Windows resolution of a concurrently unlinked file can transiently
        # name a different parent. Serialize these short checks with cleanup;
        # do not hold this lock while accepting/hashing a GB-sized export.
        with self.lock:
            if path.is_symlink() or path.resolve().parent != self.root.resolve():
                raise SpoolError("Capture copy is not a regular owned file.")
        return path

    def _row(self, row):
        if row is None:
            return None
        out = dict(row)
        out["metadata"] = json.loads(out["metadata"])
        out["path"] = str(self._path(out["id"], out["metadata"]))
        return out

    def get(self, capture_id):
        self._path(capture_id)
        with self.repo._connect() as db:
            row = db.execute("SELECT * FROM capture_queue WHERE id=?", (capture_id,)).fetchone()
        if row is None:
            raise SpoolError("Capture item not found.")
        return self._row(row)

    def reserve(self, metadata):
        self._writer()
        with self.lock, self.repo._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._reconcile_orphans(db)
            first = db.execute("SELECT state FROM capture_queue WHERE state NOT IN "
                               "('committed','unchanged','quarantined') ORDER BY ordinal LIMIT 1").fetchone()
            if first and first[0] == "blocked":
                raise CaptureQueueBlocked("Capture organization is blocked; retry or discard the retained copy.")
            rows = db.execute("SELECT reserved_bytes, state FROM capture_queue WHERE reserved_bytes>0").fetchall()
            used = sum(r[0] for r in rows)
            slots = sum(1 for r in rows if r[1] != "quarantined")     # see _stats: quarantined = bytes only
            if (slots >= self.max_items or used + self.max_file_bytes > self.max_bytes
                    or shutil.disk_usage(self.root).free < self.min_free_bytes + self.max_file_bytes):
                raise SpoolFull("Capture queue is full or free disk reserve is low; new capture deferred.")
            meta = json.loads(_metadata(metadata))
            previous = db.execute("SELECT id FROM capture_queue WHERE state NOT IN ('committed','unchanged','quarantined') "
                                  "ORDER BY ordinal DESC LIMIT 1").fetchone()
            head = history_order.head(db)
            # Never accept caller overrides of ordering.
            meta["predecessor_capture_id"] = previous[0] if previous else None
            meta["expected_head_id"] = head["id"] if head else None
            capture_id = uuid.uuid4().hex
            path = self._path(capture_id, meta)
            db.execute("INSERT INTO capture_queue(id,state,reserved_bytes,metadata,created_at) VALUES(?,?,?,?,?)",
                       (capture_id, "writing", self.max_file_bytes, _metadata(meta), _now()))
        # Reserve durably BEFORE creating a file; crashes cannot leave unbudgeted raw files.
        with path.open("xb") as stream:
            stream.flush()
            os.fsync(stream.fileno())
        self.repo.sync_dir(self.root)
        return self.get(capture_id)

    def _receipt_path(self, capture_id):
        path = self._path(capture_id).with_suffix(".verified.json")
        if path.is_symlink():
            raise SpoolError("Verification receipt cannot be a symlink.")
        return path

    def _read_receipt(self, capture_id):
        path = self._receipt_path(capture_id)
        if not path.exists():
            return None
        if path.stat().st_size > 16384:
            raise SpoolError("Verification receipt exceeds its metadata bound.")
        try:
            receipt = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, UnicodeError) as exc:
            raise SpoolError("Verification receipt is incomplete or malformed.") from exc
        if (not isinstance(receipt, dict) or receipt.get("capture_id") != capture_id
                or not isinstance(receipt.get("sha256"), str)
                or not re.fullmatch("[0-9a-f]{64}", receipt["sha256"])
                or not isinstance(receipt.get("captured_at"), str)
                or "export_ms" not in receipt
                or type(receipt.get("size")) is not int or receipt["size"] <= 0):
            raise SpoolError("Verification receipt is malformed.")
        return receipt

    def _persist_receipt(self, capture_id, size, digest, meta):
        receipt = self._read_receipt(capture_id)
        if receipt is not None:
            if receipt["size"] != size or receipt["sha256"] != digest:
                raise SpoolError("Verified export bytes changed before database acceptance.")
            return receipt
        receipt = {"capture_id": capture_id, "size": size, "sha256": digest,
                   "captured_at": meta["captured_at"], "export_ms": meta["export_ms"]}
        with self._receipt_path(capture_id).open("x", encoding="utf-8") as stream:
            json.dump(receipt, stream, ensure_ascii=True)
            stream.flush()
            os.fsync(stream.fileno())
        self.repo.sync_dir(self.root)
        return receipt

    def accept(self, capture_id, *, export_ms=None, checkpoint_metadata=None):
        self._writer()
        from .capture_noop import NoopProbe, same_document
        import time
        started = time.monotonic()
        row = self.get(capture_id)
        if row["state"] != "writing":
            raise SpoolError("Only a completed writing item may be accepted.")
        path = self._path(capture_id)
        before = path.stat()
        if not 0 < before.st_size <= self.max_file_bytes:
            self.block(capture_id, "Export size exceeds the configured per-file limit or is empty.")
            raise SpoolError("Export size is outside its reservation; retained but not accepted.")
        meta = row["metadata"]
        if checkpoint_metadata is not None:
            meta["checkpoint_metadata"] = json.loads(_metadata(checkpoint_metadata))
        parent, probe = None, None
        if meta.get("source", "system") != "manual":
            history = self.repo.history(1)
            if history and same_document(history[0], meta) and history[0]["size"] == before.st_size:
                parent = history[0]
                try:
                    probe = NoopProbe(self.repo, parent, before.st_size)
                except Exception:
                    probe = None  # unknown/corrupt template is a cache miss, never a no-op proof
        digest = hashlib.sha256()
        normalized_equal = False
        try:
            with path.open("r+b") as stream:
                stream.flush()
                os.fsync(stream.fileno())
                while chunk := stream.read(4*1024*1024):
                    digest.update(chunk)
                    if probe is not None:
                        try:
                            probe.update(chunk)
                        except Exception:
                            probe.close()
                            probe = None
            if probe is not None:
                normalized_equal = probe.matches(before.st_size)
        finally:
            if probe is not None:
                probe.close()
        after = path.stat()
        if (before.st_size, before.st_mtime_ns, before.st_ino, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ino, after.st_ctime_ns):
            self.block(capture_id, "Export was still changing during acceptance.")
            raise SpoolError("Export changed during acceptance; not accepted.")
        self.repo.sync_dir(self.root)
        raw_hash = digest.hexdigest()
        basis = ("raw_sha256" if parent and parent["sha256"] == raw_hash else
                 "verified_timestamp_template" if normalized_equal else None)
        meta["captured_at"], meta["export_ms"] = _now(), export_ms
        meta["accepted_stat"] = [after.st_size, after.st_mtime_ns, after.st_ino, after.st_ctime_ns]
        meta["accept_ms"] = round((time.monotonic() - started) * 1000, 1)
        receipt = self._persist_receipt(capture_id, after.st_size, raw_hash, meta)
        meta["captured_at"], meta["export_ms"] = receipt["captured_at"], receipt["export_ms"]
        try:
            skip = self._commit_acceptance(capture_id, after.st_size, raw_hash, meta, parent, basis)
            if skip:
                try:
                    self.cleanup_done()
                except OSError:
                    pass
            return self.get(capture_id)
        except sqlite3.DatabaseError as exc:
            # The fsynced receipt survives a process restart; do NOT call this an export failure.
            raise AcceptancePending("Verified export retained; database acceptance pending. "
                                    "It will be retried when the database is available.") from exc

    def _commit_acceptance(self, capture_id, size, raw_hash, meta, parent, basis):
        with self.repo._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            # A compare result against an old head cannot elide a new version.
            head = history_order.head(db)
            first = db.execute("SELECT id FROM capture_queue WHERE state NOT IN "
                               "('committed','unchanged','quarantined') ORDER BY ordinal LIMIT 1").fetchone()
            expected = meta.get("expected_head_id")
            predecessor = meta.get("predecessor_capture_id")
            if predecessor:
                prev = db.execute("SELECT state,checkpoint_id FROM capture_queue WHERE id=?", (predecessor,)).fetchone()
                expected = prev["checkpoint_id"] if prev and prev["state"] in DONE else object()
            skip = bool(basis and parent and head and head["id"] == parent["id"] == expected
                        and first and first[0] == capture_id)
            state, checkpoint_id = ("unchanged", parent["id"]) if skip else ("ready", None)
            if skip:
                meta["completed_at"] = _now()
                meta["dedupe"] = {"basis": basis, "checkpoint_id": checkpoint_id,
                                  "raw_bytes_equal": basis == "raw_sha256",
                                  "normalized_sha256": (parent.get("manifest") or {}).get("normalized_sha256"),
                                  "note": "Automatic no-op receipt; timestamp-only raw bytes are not a new restorable version."}
            updated = db.execute("UPDATE capture_queue SET state=?,size=?,reserved_bytes=?,sha256=?,"
                                 "metadata=?,checkpoint_id=?,error=NULL WHERE id=? AND state='writing'",
                                 (state, size, size, raw_hash,
                                  _metadata(meta), checkpoint_id, capture_id))
            if updated.rowcount != 1:
                raise SpoolError("Capture state changed during acceptance.")
        return skip

    def ready_item(self):
        with self.repo._connect() as db:
            row = db.execute("SELECT * FROM capture_queue WHERE state NOT IN ('committed','unchanged','quarantined') "
                             "ORDER BY ordinal LIMIT 1").fetchone()
        return self._row(row)

    def mark_processing(self, capture_id):
        self._writer()
        with self.lock, self.repo._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            first = db.execute("SELECT id,state FROM capture_queue WHERE state NOT IN ('committed','unchanged','quarantined') "
                               "ORDER BY ordinal LIMIT 1").fetchone()
            if not first or first["id"] != capture_id or first["state"] != "ready":
                raise SpoolError("Only the first ready copy can be organized.")
            db.execute("UPDATE capture_queue SET state='processing',error=NULL WHERE id=?", (capture_id,))
        return self.get(capture_id)

    def retry(self, capture_id, error=None):
        self._writer()
        with self.lock, self.repo._connect() as db:
            cur = db.execute("UPDATE capture_queue SET state='ready',error=? WHERE id=? AND sha256 IS NOT NULL "
                             "AND state IN ('processing','blocked')", (error, capture_id))
            if cur.rowcount != 1:
                raise SpoolError("Only an accepted retained copy can be retried.")
        return self.get(capture_id)

    def block(self, capture_id, error):
        self._writer()
        path = self._path(capture_id)
        size = path.stat().st_size if path.exists() else 0
        with self.lock, self.repo._connect() as db:
            cur = db.execute("UPDATE capture_queue SET state=CASE WHEN sha256 IS NULL THEN 'quarantined' ELSE 'blocked' END,"
                             "error=?,size=?,reserved_bytes=? "
                             "WHERE id=? AND state NOT IN ('committed','unchanged')",
                             (str(error)[:500], size, size, capture_id))
            if cur.rowcount != 1:
                raise SpoolError("Cannot block a missing or completed capture.")
        return self.get(capture_id)

    def discard(self, capture_id, note="discarded by user"):
        """Give up a retained copy (blocked or quarantined): remove its file and receipt and
        release its reservation. The row stays as a terminal 'quarantined' record with the
        reason, so the history keeps saying that an export existed and was dropped."""
        self._writer()
        with self.lock, self.repo._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT state,error FROM capture_queue WHERE id=?", (capture_id,)).fetchone()
            if row is None or row["state"] not in ("blocked", "quarantined"):
                raise SpoolError("Only a retained (blocked or quarantined) copy can be discarded.")
            previous = row["error"] or ""
            db.execute("UPDATE capture_queue SET state='quarantined', reserved_bytes=0, error=? WHERE id=?",
                       ((note + ": " + previous)[:500], capture_id))
            self._path(capture_id).unlink(missing_ok=True)
            self._receipt_path(capture_id).unlink(missing_ok=True)
            self.repo.sync_dir(self.root)
        return self.get(capture_id)

    def cleanup_done(self):
        self._writer()
        with self.lock, self.repo._connect() as db:
            rows = db.execute("SELECT id FROM capture_queue WHERE state IN ('committed','unchanged') "
                              "AND reserved_bytes>0").fetchall()
            for row in rows:
                self._path(row["id"]).unlink(missing_ok=True)
                self._receipt_path(row["id"]).unlink(missing_ok=True)
                self.repo.sync_dir(self.root)
                db.execute("UPDATE capture_queue SET reserved_bytes=0 WHERE id=?", (row["id"],))

    def recover(self):
        """Only a new lease owner calls this, before starting an organizer."""
        self._writer()
        with self.lock, self.repo._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._reconcile_orphans(db)
            db.execute("UPDATE capture_queue SET state='ready' WHERE state='processing'")
            writing = [r[0] for r in db.execute("SELECT id FROM capture_queue WHERE state='writing' ORDER BY ordinal")]
        # Rehash verified-but-uncommitted files without holding a spool/SQLite write lock.
        for capture_id in writing:
            try:
                if self._read_receipt(capture_id) is not None:
                    self.accept(capture_id)
                else:
                    self.block(capture_id, "incomplete_export: export completion unknown")
            except AcceptancePending:
                raise
            except (SpoolError, OSError) as exc:
                self.block(capture_id, "verification_recovery_failed: " + str(exc)[:400])
        with self.repo._connect() as db:
            rows = db.execute("SELECT id FROM capture_queue WHERE state='ready'").fetchall()
            for row in rows:
                if not self._path(row["id"]).is_file():
                    db.execute("UPDATE capture_queue SET state='blocked',error='Accepted raw copy is missing' WHERE id=?",
                               (row["id"],))

    def stats(self):
        with self.repo._connect() as db:
            result = _stats(db, max_items=self.max_items, max_bytes=self.max_bytes, max_file_bytes=self.max_file_bytes)
        if shutil.disk_usage(self.root).free < self.min_free_bytes + self.max_file_bytes:
            result["full"] = True
        return result
