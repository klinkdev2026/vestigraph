"""Streaming, immutable file checkpoints and transactional local process history.

The SQLite database is authoritative metadata; objects are immutable compressed
1 MiB chunks. This is a byte store, not a geometry parser or a signoff system.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import time
import uuid
import zlib

from .filelock import FileLock, LockHeld
from . import history_order
from .storage import engine as _engine
from .storage.engine import StorageError

CHUNK_SIZE = 1024 * 1024
FORMATS = ("1", "2")
V2_STATEMENTS = (
    '''CREATE TABLE IF NOT EXISTS packs (
        id TEXT PRIMARY KEY, checkpoint_id TEXT NOT NULL,
        size INTEGER NOT NULL, created_at TEXT NOT NULL)''',
    '''CREATE TABLE IF NOT EXISTS objects (
        hash TEXT PRIMARY KEY, pack TEXT NOT NULL REFERENCES packs(id),
        offset INTEGER NOT NULL, raw_size INTEGER NOT NULL, stored_size INTEGER NOT NULL,
        codec INTEGER NOT NULL, depth INTEGER NOT NULL, base_hash TEXT,
        base_raw_size INTEGER NOT NULL, stored_sha256 TEXT NOT NULL)''',
    '''CREATE TABLE IF NOT EXISTS changesets (
        checkpoint_id TEXT PRIMARY KEY REFERENCES checkpoints(id), root TEXT NOT NULL)''',
)
V2_SCHEMA = ";\n".join(V2_STATEMENTS) + ";"
WRITER_LOCK = "writer.lock"
TRANSIENT_WAIT_S = 30.0
MAX_PAYLOAD = 256 * 1024
_IN_CHUNK = 500                  # ids per IN (...) list in batch lookups
CAPTURE_TMP_MAX_AGE_S = 6 * 3600  # tmp/capture-* older than this can no longer have a writer
SOURCES = {"manual", "automation", "mixed", "unknown", "system"}
_HASH = re.compile(r"^[0-9a-f]{64}$")


class RepositoryError(RuntimeError):
    """Expected repository failure with a user-actionable message."""


class CursorError(RepositoryError):
    """The caller's paging cursor/limit is invalid (a request error, not a damaged history)."""


class RepositoryDataError(RepositoryError):
    """Persisted metadata is malformed, distinct from a missing version."""


class SaveCancelled(RepositoryError):
    """prepare()/checkpoint() stopped because `cancel` was set; nothing was published."""


def _now():
    return datetime.now(timezone.utc).isoformat()


def _json(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise RepositoryError("Metadata must be finite JSON values; correct the input and retry.") from exc


def _metadata(value):
    if value is not None and not isinstance(value, dict):
        raise RepositoryError("Metadata/payload must be a JSON object; pass a dict.")
    encoded = _json(value if value is not None else {})
    if len(encoded.encode("utf-8")) > MAX_PAYLOAD:
        raise RepositoryError("Metadata/payload exceeds 256 KiB; summarize it explicitly and retry.")
    return encoded


def _source(value):
    if value not in SOURCES:
        raise RepositoryError("Source must be manual, automation, mixed, unknown or system.")
    return value


def _limit(value):
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 10000:
        raise RepositoryError("Limit must be an integer between 1 and 10000; adjust --limit.")
    return value


def _sync_dir(path):
    # Windows has no portable directory fsync. SQLite FULL + flushed object files
    # protect normal process crashes; sudden power-loss durability is FS-dependent.
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _stamp(stat):
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


class Repository:
    def __init__(self, root, *, readonly=False, services=None):
        from .vesti_runtime.services import VestiRepositoryServices
        if services is not None and not isinstance(services, VestiRepositoryServices):
            raise ValueError("services must be VestiRepositoryServices")
        self._services = services if services is not None else VestiRepositoryServices()
        self.root = Path(root).expanduser().resolve()
        self.database = self.root / "index.sqlite3"
        self.readonly = bool(readonly)
        self._lease = None
        self._open_prepare = None       # at most one un-finished PreparedSnapshot per Repository
        self.format = None
        if not self.database.is_file():
            raise RepositoryError("Repository is not initialized; run vestigraph --repo PATH init first.")
        try:
            with self._connect() as db:
                version = db.execute("SELECT value FROM config WHERE key='format_version'").fetchone()
                if version is None or version[0] not in FORMATS:
                    raise RepositoryError("Unsupported repository format; use a compatible Vestigraph version.")
                self.format = int(version[0])
        except sqlite3.DatabaseError as exc:
            raise RepositoryError("Cannot read repository metadata; check the path or restore a complete backup.") from exc

    @property
    def services(self):
        return self._services

    # v2 layout helpers (docs/STORAGE_V2_FORMAT.md §1)
    @property
    def objects_dir(self):
        return self.root / "objects"

    @property
    def tmp_dir(self):
        return self.root / "tmp"

    @property
    def packs_dir(self):
        return self.root / "packs"

    def loose_path(self, digest):
        return self._object_path(digest)

    def pack_path(self, pack_id):
        if not isinstance(pack_id, str) or not re.fullmatch(r"[0-9a-f]{32}", pack_id):
            raise RepositoryError("Invalid pack id; restore metadata from a trusted backup.")
        return self.packs_dir / (pack_id + ".pack")

    @staticmethod
    def sync_dir(path):
        _sync_dir(path)

    @classmethod
    def init(cls, root, *, storage_format=2, services=None):
        from .vesti_runtime.services import VestiRepositoryServices
        if services is not None and not isinstance(services, VestiRepositoryServices):
            raise ValueError("services must be VestiRepositoryServices")
        root = Path(root).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        if (root / "index.sqlite3").exists():
            return cls(root)  # Never reset or silently migrate an existing database.
        if storage_format not in (1, 2):
            raise RepositoryError("storage_format must be 1 or 2.")
        for name in ("objects", "tmp") + (("packs",) if storage_format == 2 else ()):
            (root / name).mkdir(exist_ok=True)
        with sqlite3.connect(root / "index.sqlite3", timeout=30) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            db.executescript('''
                CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                INSERT OR IGNORE INTO config VALUES ('format_version', '%d');''' % storage_format + (
                V2_SCHEMA if storage_format == 2 else "") + '''
                CREATE TABLE IF NOT EXISTS segments (
                    ordinal INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
                    title TEXT NOT NULL, source TEXT NOT NULL, started_at TEXT NOT NULL,
                    ended_at TEXT, status TEXT NOT NULL, metadata TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
                    kind TEXT NOT NULL, source TEXT NOT NULL,
                    segment_id TEXT REFERENCES segments(id), payload TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS event_segment ON events(segment_id, seq);
                CREATE TABLE IF NOT EXISTS checkpoints (
                    ordinal INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
                    parent_id TEXT REFERENCES checkpoints(id),
                    segment_id TEXT REFERENCES segments(id), title TEXT NOT NULL,
                    source TEXT NOT NULL, created_at TEXT NOT NULL, filename TEXT NOT NULL,
                    size INTEGER NOT NULL, sha256 TEXT NOT NULL,
                    manifest TEXT NOT NULL, metadata TEXT NOT NULL);
            ''')
        db.close()
        return cls(root, services=services)

    @classmethod
    def open_readonly(cls, root, *, services=None):
        """Browse an existing history without any write: no lease, no metadata update.

        SQLite may still create its own empty -wal/-shm sidecars next to a
        WAL-mode database; the database file and objects are never modified.
        """
        return cls(root, readonly=True, services=services)

    def _open_connection(self):
        if self.readonly:
            # SQLite URI: percent, question mark and hash are the only reserved characters.
            escaped = self.database.as_posix().replace("%", "%25").replace("?", "%3F").replace("#", "%23")
            uri = "file:" + escaped + "?mode=ro"
            db = sqlite3.connect(uri, uri=True, timeout=30)
        else:
            db = sqlite3.connect(self.database, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        if not self.readonly:
            db.execute("PRAGMA synchronous=FULL")
        return db

    @contextmanager
    def _connect(self):
        db = self._open_connection()
        try:
            with db:
                yield db
        finally:
            db.close()

    # ------------------------------------------------------------ writer lease --
    def acquire_writer(self, owner="vestigraph"):
        """Hold the process-level exclusive writer lease until release_writer()."""
        if self.readonly:
            raise RepositoryError("Repository was opened read-only; open it normally to write.")
        if self._lease is not None:
            return self
        lease = FileLock(self.root / WRITER_LOCK, {"owner": str(owner)})
        try:
            lease.acquire()
        except LockHeld as exc:
            raise RepositoryError(
                f"Another writer holds this history: {exc}. Stop that recorder/service first; "
                "reading the history is still allowed.") from exc
        self._lease = lease
        try:
            self._check_writer_target()
            if self.format == 2:
                self.cleanup_orphans()
        except BaseException:
            self.release_writer()
            raise
        return self

    def cleanup_orphans(self, *, force=False):
        """Remove temporary leftovers and quarantine packs no transaction references.
        Only under the writer lease (nobody else can be mid-prepare); never touches other files."""
        if self._lease is None and not force:
            return []
        removed = []
        # Temporary editor exports (capture-*) that a timed-out RPC may still have been writing
        # are kept by the recorder; nothing writes one for hours, so age them out here.
        cutoff = time.time() - CAPTURE_TMP_MAX_AGE_S
        for path in self.tmp_dir.glob("capture-*"):
            try:
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed.append(str(path))
            except OSError:
                pass
        if self.format != 2:
            return removed
        for path in list(self.tmp_dir.glob("vg2-*.pack")) + list(self.tmp_dir.glob("vg2-*.work.sqlite3")):
            try:
                path.unlink()
                removed.append(str(path))
            except OSError:
                pass
        with self._connect() as db:
            known = {row[0] for row in db.execute("SELECT id FROM packs")}
        for path in self.packs_dir.glob("*.pack"):
            if path.stem not in known and re.fullmatch(r"[0-9a-f]{32}", path.stem):
                quarantine = self.root / "quarantine" / "packs"
                if quarantine.is_symlink() or quarantine.resolve() != self.root / "quarantine" / "packs":
                    raise RepositoryError("Pack quarantine must be an owned directory inside the history.")
                quarantine.mkdir(parents=True, exist_ok=True)
                self.sync_dir(quarantine.parent)
                self.sync_dir(self.root)
                target = quarantine / path.name
                if target.exists():
                    raise RepositoryError("An unindexed pack conflicts with quarantine; inspect both copies before writing.")
                path.rename(target)
                self.sync_dir(quarantine)
                self.sync_dir(self.packs_dir)
        return removed

    @staticmethod
    def _upgrade_hook(db):
        """Test seam: raise here to simulate a failure between the DDL and the config flip."""

    def upgrade_storage(self):
        """v1 -> v2 (additive): backup index.sqlite3 with the SQLite backup API, then add the
        v2 tables and flip format_version in one transaction. Needs the writer lease.
        Old programs refuse to reopen afterwards; an old process holding a handle still
        writes valid v1 checkpoints, which the new program reads."""
        if self.readonly:
            raise RepositoryError("Repository was opened read-only; open it normally to upgrade.")
        if self.format == 2:
            return {"format": 2, "upgraded": False}
        with self._writer():
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            backup = self.root / f"index.sqlite3.v1-backup-{stamp}"
            if backup.exists():
                raise RepositoryError("Backup target already exists; retry in a second.")
            source = sqlite3.connect(self.database, timeout=30)
            try:
                target = sqlite3.connect(backup)
                try:
                    source.backup(target)
                finally:
                    target.close()
            finally:
                source.close()
            self.packs_dir.mkdir(exist_ok=True)
            # One transaction: SQLite DDL is transactional, but sqlite3.executescript()
            # would COMMIT first, so every statement goes through execute().
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                for statement in V2_STATEMENTS:
                    db.execute(statement)
                self._upgrade_hook(db)
                db.execute("UPDATE config SET value='2' WHERE key='format_version'")
                db.commit()
            self.format = 2
        return {"format": 2, "upgraded": True, "backup": str(backup)}

    def release_writer(self):
        lease, self._lease = self._lease, None
        if lease is not None:
            lease.release()

    @property
    def writer_held(self):
        return self._lease is not None

    def _check_writer_target(self):
        # A handle constructed before relocation must not recreate the deleted database.
        if not self.database.is_file():
            raise RepositoryError("History was moved or removed; reopen its current location.")

    def _acquire_transient(self):
        """Per-call writer lease; None when the long-lived lease is already held."""
        if self.readonly:
            raise RepositoryError("Repository was opened read-only; open it normally to write.")
        if self._lease is not None:
            return None
        transient = FileLock(self.root / WRITER_LOCK, {"owner": "transient"})
        deadline = time.monotonic() + TRANSIENT_WAIT_S
        while True:
            try:
                transient.acquire()
                try:
                    self._check_writer_target()
                except BaseException:
                    transient.release()
                    raise
                return transient
            except LockHeld as exc:
                # Other per-call writers are serialized by waiting; a long-lived
                # holder (recorder/service) is reported immediately.
                if exc.holder.get("owner", "transient") != "transient" or time.monotonic() >= deadline:
                    raise RepositoryError(
                        f"Another writer holds this history: {exc}. Stop that recorder/service first; "
                        "reading the history is still allowed.") from exc
                time.sleep(0.01)

    @contextmanager
    def _writer(self):
        """Every write goes through the lease: held long-term, or transiently per call."""
        transient = self._acquire_transient()
        try:
            yield
        finally:
            if transient is not None:
                transient.release()

    @staticmethod
    def _row(row, *, allow_invalid_manifest=False):
        out = dict(row)
        out.pop("ordinal", None)
        for key in ("metadata", "manifest", "payload"):
            if key in out:
                try:
                    out[key] = json.loads(out[key])
                    if not isinstance(out[key], dict):
                        raise ValueError("Expected a metadata object")
                except (ValueError, TypeError) as exc:
                    if key == "manifest" and allow_invalid_manifest:
                        out[key] = None  # Preparation can save current bytes without reusing a corrupt parent.
                        continue
                    raise RepositoryDataError("Persisted history metadata is corrupt; preserve it and run an integrity check.") from exc
        return out

    @staticmethod
    def _open_segment(db, segment_id):
        if segment_id is not None:
            row = db.execute("SELECT status FROM segments WHERE id=?", (segment_id,)).fetchone()
            if row is None or row[0] != "open":
                raise RepositoryError("Segment is missing or closed; start a new segment before recording.")

    def begin_segment(self, title, source="mixed", metadata=None):
        if not isinstance(title, str) or not title.strip():
            raise RepositoryError("Segment title is empty; provide a short description.")
        source, encoded = _source(source), _metadata(metadata)
        segment_id = uuid.uuid4().hex
        with self._writer(), self._connect() as db:
            db.execute("INSERT INTO segments(id,title,source,started_at,status,metadata) VALUES(?,?,?,?,?,?)",
                       (segment_id, title, source, _now(), "open", encoded))
            return self._row(db.execute("SELECT * FROM segments WHERE id=?", (segment_id,)).fetchone())

    def close_segment(self, segment_id, status="closed"):
        if status not in {"closed", "failed", "interrupted"}:
            raise RepositoryError("End status must be closed, failed or interrupted.")
        with self._writer(), self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM segments WHERE id=?", (segment_id,)).fetchone()
            if row is None:
                raise RepositoryError("Segment not found; inspect segments and use its full id.")
            if row["status"] != "open":
                if row["status"] == status:
                    return self._row(row)  # A same-status retry is idempotent.
                raise RepositoryError("Segment already ended; do not rewrite history, start a new segment.")
            db.execute("UPDATE segments SET status=?, ended_at=? WHERE id=?", (status, _now(), segment_id))
            return self._row(db.execute("SELECT * FROM segments WHERE id=?", (segment_id,)).fetchone())

    def append_event(self, kind, payload, source="unknown", segment_id=None):
        if not isinstance(kind, str) or not kind.strip():
            raise RepositoryError("Event kind is empty; provide an explicit event name.")
        source, encoded = _source(source), _metadata(payload)
        with self._writer(), self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._open_segment(db, segment_id)
            cur = db.execute("INSERT INTO events(created_at,kind,source,segment_id,payload) VALUES(?,?,?,?,?)",
                             (_now(), kind, source, segment_id, encoded))
            return self._row(db.execute("SELECT * FROM events WHERE seq=?", (cur.lastrowid,)).fetchone())

    def _object_path(self, digest):
        if not isinstance(digest, str) or not _HASH.fullmatch(digest):
            raise RepositoryError("Invalid chunk hash; restore metadata from a trusted backup.")
        return self.root / "objects" / digest[:2] / digest

    def _read_chunk(self, digest, size):
        if not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= CHUNK_SIZE:
            raise RepositoryError("Invalid chunk size; restore metadata from a trusted backup.")
        try:
            path = self._object_path(digest)
            with path.open("rb") as stream:
                encoded = stream.read(CHUNK_SIZE + 65537)
            if len(encoded) > CHUNK_SIZE + 65536:
                raise ValueError("compressed block exceeds bound")
            decoder = zlib.decompressobj()
            data = decoder.decompress(encoded, size + 1)
            if (len(data) != size or not decoder.eof or decoder.unused_data
                    or hashlib.sha256(data).hexdigest() != digest):
                raise ValueError("chunk integrity mismatch")
            return data
        except (OSError, ValueError, zlib.error) as exc:
            raise RepositoryError(f"Chunk {digest} is missing or corrupt; restore it from backup before exporting.") from exc

    def _put_chunk(self, data):
        digest = hashlib.sha256(data).hexdigest()
        target = self._object_path(digest)
        if target.exists():
            self._read_chunk(digest, len(data))
            return {"hash": digest, "size": len(data)}
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix="chunk-", dir=target.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(zlib.compress(data, level=1))
                stream.flush()
                os.fsync(stream.fileno())
            try:
                # Atomic create-if-absent, also safe for two concurrent writers.
                from .storage.publish import publish_new
                publish_new(temporary, target)
            except FileExistsError:
                self._read_chunk(digest, len(data))
            except OSError as exc:
                raise RepositoryError("Object publication failed; check local filesystem permissions and atomic rename support.") from exc
            _sync_dir(target.parent)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return {"hash": digest, "size": len(data)}

    def checkpoint(self, path, title="", source="manual", segment_id=None, metadata=None, delta=True,
                   progress=None, cancel=None, scan_backend=None, delta_verify=None, compress_workers=None,
                   compress_max_workers=None):
        path = Path(path).expanduser().resolve()
        source, encoded = _source(source), _metadata(metadata)
        if not isinstance(title, str):
            raise RepositoryError("Checkpoint title must be text; correct the title and retry.")
        if self.readonly:
            raise RepositoryError("Repository was opened read-only; open it normally to write.")
        if not path.is_file():
            raise RepositoryError("Source file does not exist; save/export the layout to a file first.")
        if self.format == 2:
            prepared = self.prepare(path, segment_id=segment_id, delta=delta, progress=progress, cancel=cancel,
                                    scan_backend=scan_backend, delta_verify=delta_verify,
                                    compress_workers=compress_workers, compress_max_workers=compress_max_workers)
            return self.commit(prepared, title=title, source=source, segment_id=segment_id, metadata=metadata)
        with self._writer():
            return self._checkpoint_locked(path, title, source, segment_id, encoded)

    # ------------------------------------------------- format 2: prepare/commit --
    def prepare(self, path, *, segment_id=None, analyze=True, delta=True, progress=None, cancel=None,
                scan_backend=None, delta_verify=None, compress_workers=None, compress_max_workers=None,
                _capture_id=None, _history_before=None):
        """Scan + chunk + dedupe + spool; nothing is published. The returned snapshot holds
        the writer lease until commit() or discard() (also on context exit)."""
        if self.format != 2:
            raise RepositoryError("This history is still format 1; run upgrade_storage() first.")
        path = Path(path).expanduser().resolve()
        if not path.is_file():
            raise RepositoryError("Source file does not exist; save/export the layout to a file first.")
        if self._open_prepare is not None and self._open_prepare.state == "open":
            raise RepositoryError("A prepared snapshot is still open on this history; commit or discard it first "
                                  "(prepares are serial so every version's parent and change record agree).")
        transient = self._acquire_transient()
        try:
            # Under the lease nobody else can be mid-prepare, so leftovers of a crashed
            # run are removed here too (CLI-only users never call acquire_writer).
            self.cleanup_orphans(force=True)
            with self._connect() as db:
                db.execute("BEGIN")
                captured = None
                if _capture_id is None:
                    self._open_segment(db, segment_id)
                else:
                    from .capture_pipeline import validate_capture
                    captured = validate_capture(db, self, _capture_id, segment_id, path)
                head = (history_order.predecessor(db, _history_before) if _history_before is not None
                        else history_order.head(db))
                evidence = ((captured["metadata"].get("evidence") if captured else self._evidence(db, segment_id))
                            if analyze else None)
            if _history_before is not None:
                evidence = {"segment_id": None, "events_seq_max_at_prepare": 0, "coverage": "saved_files_only"}
            parent = self._row(head, allow_invalid_manifest=True) if head else None
            prepared = _engine.prepare_snapshot(self, path, parent, analyze=analyze, use_delta=delta,
                                                evidence=evidence, progress=progress, cancel=cancel,
                                                scan_backend_pref=scan_backend, delta_verify=delta_verify,
                                                compress_workers=compress_workers, compress_max_workers=compress_max_workers)
        except _engine.CancelledError as exc:
            if transient is not None:
                transient.release()
            raise SaveCancelled(str(exc)) from exc
        except StorageError as exc:
            if transient is not None:
                transient.release()
            raise RepositoryError(str(exc)) from exc
        except BaseException:
            if transient is not None:
                transient.release()
            raise
        prepared.history_before = _history_before
        prepared.capture_queue_id = _capture_id
        prepared.lease = transient
        prepared.on_close = self._prepare_closed
        self._open_prepare = prepared
        return prepared

    @staticmethod
    def _evidence(db, segment_id):
        """What this save can say about the observed process: the segment and the event seq
        range recorded in it so far (not an atomic binding, see CHANGESET_V1 §4)."""
        seq_max = db.execute("SELECT coalesce(max(seq), 0) FROM events").fetchone()[0]
        out = {"segment_id": segment_id, "events_seq_max_at_prepare": seq_max}
        if segment_id is not None:
            rng = db.execute("SELECT min(seq), max(seq), count(*) FROM events WHERE segment_id=?", (segment_id,)).fetchone()
            out["segment_events"] = {"seq_min": rng[0], "seq_max": rng[1], "count": rng[2]}
        return out

    def _prepare_closed(self, prepared):
        if self._open_prepare is prepared:
            self._open_prepare = None

    def commit(self, prepared, *, title="", source="manual", segment_id=None, metadata=None):
        # Every input check discards the prepared snapshot on failure; otherwise the repository
        # stays stuck on "prepared snapshot still open" after one bad argument.
        try:
            source, encoded = _source(source), _metadata(metadata)
            if getattr(prepared, "recovery", None):
                encoded = _metadata({**json.loads(encoded), "storage_recovery": prepared.recovery})
        except Exception:
            prepared.discard()
            raise
        if not isinstance(title, str):
            prepared.discard()
            raise RepositoryError("Checkpoint title must be text; correct the title and retry.")
        try:
            checkpoint_id = _engine.commit_snapshot(
                self, prepared, title=title, source=source, segment_id=segment_id,
                encoded_metadata=encoded, filename=getattr(prepared, "original_filename", prepared.source.name))
        except StorageError as exc:
            raise RepositoryError(str(exc)) from exc
        record = self.get_checkpoint(checkpoint_id)
        record["timing_ms"] = prepared.stats.get("timing_ms", {})       # not stored; measurement of this call
        record["scan"] = prepared.stats.get("scan", {})                 # backend actually used + FFI counters
        record["delta_verify"] = prepared.stats.get("delta_verify")     # reader that checked fresh patches (P3)
        record["compress"] = prepared.stats.get("compress", {})         # P4: workers, tasks, waits
        return record

    def get_changeset(self, checkpoint_id):
        with self._connect() as db:
            if self.format != 2:
                return None
            row = db.execute("SELECT root FROM changesets WHERE checkpoint_id=?", (checkpoint_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def changes(self, checkpoint_id, *, limit=50, cursor=None, kind=None):
        """Recorded change entries of a version: root summary + one page of logical entries.
        Reads metadata objects only (never layout payload, never KLayout)."""
        from .storage import changes as _changes
        record = self.get_checkpoint(checkpoint_id)
        root = self.get_changeset(checkpoint_id)
        if root is None:
            return {"status": "unavailable", "reason": "no change record for this version", "items": [], "next_cursor": None}
        out = {"status": root["coverage"]["status"], "root": root, "items": [], "next_cursor": None}
        index = _engine.ObjectIndex(self)
        try:
            if root.get("evidence_root"):
                out["evidence"] = _engine.parse(index.get(root["evidence_root"], _engine.METADATA_OBJECT_LIMIT))
            if not root.get("entries_root"):
                return out
            entries_obj = _changes.load_entries_root(index, root)
            out["entry_count"] = entries_obj.get("logical_count", entries_obj.get("count"))
            out["entry_count_stored"] = entries_obj.get("count")
            out["kinds"] = entries_obj.get("kinds", {})
            out.update(_changes.page(index, root, record["manifest"], limit=limit, cursor=cursor, kind=kind))
            out["diagnostics"]["metadata_objects_read"] = index.metadata_reads   # includes the root/evidence reads above
        except _changes.CursorError as exc:
            raise CursorError(str(exc)) from exc
        except (StorageError, _engine.MetadataError, ValueError, KeyError, TypeError, IndexError,
                sqlite3.Error) as exc:
            raise RepositoryError(f"Change record unreadable: {exc}; restore the history from backup.") from exc
        finally:
            index.close()
        return out

    def export_changes(self, checkpoint_id, destination):
        """Portable JSONL (CHANGESET_V1 §5) to a NEW file; temporary file + atomic publish."""
        from .storage import changes as _changes
        record = self.get_checkpoint(checkpoint_id)
        root = self.get_changeset(checkpoint_id)
        if root is None:
            raise RepositoryError("No change record for this version; nothing to export.")
        target = Path(destination).expanduser().absolute()
        if target.exists() or target.is_symlink():
            raise RepositoryError("Export target already exists; choose a new filename. Nothing was overwritten.")
        if target.resolve().is_relative_to(self.root):
            raise RepositoryError("Export outside the repository data directory; choose a separate output path.")
        if not target.parent.is_dir():
            raise RepositoryError("Export parent directory does not exist; create/select a directory first.")
        index = _engine.ObjectIndex(self)
        fd, temporary = tempfile.mkstemp(prefix=".vestigraph-changes-", dir=target.parent)
        try:
            # Same shape as export(): the fd is owned by the `with` immediately, so an error in
            # the metadata reads below never leaks it (or, on Windows, blocks the unlink).
            with os.fdopen(fd, "wb") as stream:
                evidence = None
                if root.get("evidence_root"):
                    evidence = _engine.parse(index.get(root["evidence_root"], _engine.METADATA_OBJECT_LIMIT))
                count = _changes.write_jsonl(stream, index, root, record["manifest"], evidence)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                from .storage.publish import publish_new
                publish_new(temporary, target)
            except FileExistsError as exc:
                raise RepositoryError("Export target appeared during export; choose a new filename.") from exc
            except OSError as exc:
                raise RepositoryError("Could not publish the export atomically; check destination permissions and filesystem support.") from exc
            _sync_dir(target.parent)
        except (StorageError, _engine.MetadataError, ValueError, KeyError, TypeError, IndexError,
                sqlite3.Error) as exc:
            raise RepositoryError(f"Change record unreadable: {exc}; restore the history from backup.") from exc
        finally:
            Path(temporary).unlink(missing_ok=True)
            index.close()
        return {"checkpoint_id": checkpoint_id, "destination": str(target), "entries": count}

    def _checkpoint_locked(self, path, title, source, segment_id, encoded):
        with self._connect() as db:
            self._open_segment(db, segment_id)
        chunks, whole, total = [], hashlib.sha256(), 0
        path_before = _stamp(path.stat())
        with path.open("rb") as stream:
            before = _stamp(os.fstat(stream.fileno()))
            while True:
                data = stream.read(CHUNK_SIZE)
                if not data:
                    break
                whole.update(data)
                total += len(data)
                chunks.append(self._put_chunk(data))
            after = _stamp(os.fstat(stream.fileno()))
        # Compare ctime only within the same API: on Windows CPython, stat and
        # fstat can expose different ctime semantics. Identity/size/mtime must
        # still agree across APIs. Neither check is an atomic FS snapshot.
        if (before != after or path_before != _stamp(path.stat())
                or before[:4] != path_before[:4] or total != before[2]):
            raise RepositoryError("Source changed while being read; pause editing/saving and retry the checkpoint.")
        digest = whole.hexdigest()
        manifest = {"format": 1, "chunk_size": CHUNK_SIZE, "size": total,
                    "sha256": digest, "chunks": chunks}
        checkpoint_id = uuid.uuid4().hex
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._open_segment(db, segment_id)
            parent = db.execute("SELECT id FROM checkpoints ORDER BY ordinal DESC LIMIT 1").fetchone()
            db.execute('''INSERT INTO checkpoints
                (id,parent_id,segment_id,title,source,created_at,filename,size,sha256,manifest,metadata)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                       (checkpoint_id, parent[0] if parent else None, segment_id,
                        title or path.name, source, _now(), path.name, total,
                        digest, _json(manifest), encoded))
        return self.get_checkpoint(checkpoint_id)

    def get_checkpoint(self, checkpoint_id):
        with self._connect() as db:
            row = db.execute("SELECT * FROM checkpoints WHERE id=?", (checkpoint_id,)).fetchone()
        if row is None:
            raise RepositoryError("Checkpoint not found; run history and copy its full id.")
        with self._connect() as db:
            return history_order.decorate(db, self._row(row))

    def import_checkpoint(self, path, **options):
        """Insert confirmed legacy bytes before a version, preserving the current head."""
        return history_order.import_checkpoint(self, path, **options)

    def history_revision(self):
        with self._connect() as db:
            return history_order.revision(db)

    def history(self, limit=50):
        with self._connect() as db:
            if history_order.enabled(db):
                db.execute("BEGIN")
                return history_order.page(self, db, limit, None, None, None)["items"]
            rows = db.execute("SELECT * FROM checkpoints ORDER BY ordinal DESC LIMIT ?", (_limit(limit),)).fetchall()
        return [self._row(row) for row in rows]

    def segments(self, limit=50):
        with self._connect() as db:
            rows = db.execute("SELECT * FROM segments ORDER BY ordinal DESC LIMIT ?", (_limit(limit),)).fetchall()
        return [self._row(row) for row in rows]

    def events(self, segment_id=None, limit=200):
        with self._connect() as db:
            if segment_id is None:
                rows = db.execute("SELECT * FROM events ORDER BY seq DESC LIMIT ?", (_limit(limit),)).fetchall()
            else:
                rows = db.execute("SELECT * FROM events WHERE segment_id=? ORDER BY seq DESC LIMIT ?",
                                  (segment_id, _limit(limit))).fetchall()
        return [self._row(row) for row in rows]

    def export(self, checkpoint_id, destination):
        record = self.get_checkpoint(checkpoint_id)
        target = Path(destination).expanduser().absolute()
        if target.exists() or target.is_symlink():
            raise RepositoryError("Export target already exists; choose a new filename. Nothing was overwritten.")
        if target.resolve().is_relative_to(self.root):
            raise RepositoryError("Export outside the repository data directory; choose a separate output path.")
        if not target.parent.is_dir():
            raise RepositoryError("Export parent directory does not exist; create/select a directory first.")
        manifest = record["manifest"]
        if not isinstance(manifest, dict):
            raise RepositoryError("Manifest metadata is corrupt; preserve the history and run an integrity check.")
        fmt = manifest.get("format")
        if fmt == 1:
            if manifest.get("chunk_size") != CHUNK_SIZE:
                raise RepositoryError("Unsupported manifest; use the matching Vestigraph version.")
        elif fmt != 2:
            raise RepositoryError("Unsupported manifest; use the matching Vestigraph version.")
        fd, temporary = tempfile.mkstemp(prefix=".vestigraph-export-", dir=target.parent)
        try:
            whole, total = hashlib.sha256(), 0
            with os.fdopen(fd, "wb") as stream:
                if fmt == 2:
                    try:
                        total, digest, _ = _engine.export_v2(self, record, stream)
                    except StorageError as exc:
                        raise RepositoryError(str(exc)) from exc
                    if total != record["size"] or digest != record["sha256"]:
                        raise RepositoryError("File integrity mismatch; restore complete metadata and objects from backup.")
                else:
                    for chunk in manifest["chunks"]:
                        data = self._read_chunk(chunk["hash"], chunk["size"])
                        stream.write(data)
                        total += len(data)
                        whole.update(data)
                    if (total != record["size"] or total != manifest["size"] or
                            whole.hexdigest() != record["sha256"] or whole.hexdigest() != manifest["sha256"]):
                        raise RepositoryError("File integrity mismatch; restore complete metadata and objects from backup.")
                stream.flush()
                os.fsync(stream.fileno())
            try:
                from .storage.publish import publish_new
                publish_new(temporary, target)  # Atomic no-replace, including FAT/exFAT.
            except FileExistsError as exc:
                raise RepositoryError("Export target appeared during export; choose a new filename.") from exc
            except OSError as exc:
                raise RepositoryError("Could not publish the export atomically; check destination permissions and filesystem support.") from exc
            _sync_dir(target.parent)
        except (ValueError, TypeError, KeyError, IndexError, sqlite3.DatabaseError) as exc:
            raise RepositoryError("Manifest metadata is corrupt; preserve the history and run an integrity check.") from exc
        finally:
            Path(temporary).unlink(missing_ok=True)
        return target

    # ---------------------------------------------------- paged read queries --
    # Keyset pagination on the storage ordinal/seq (commit order, newest first)
    # with a snapshot upper bound so concurrent appends never shift pages.
    _PAGE_KEY = {"checkpoints": "ordinal", "segments": "ordinal", "events": "seq"}

    def _page(self, table, limit, before, upper, where="", args=()):
        key = self._PAGE_KEY[table]
        limit = _limit(limit)
        for name, value in (("before", before), ("upper", upper)):
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                raise RepositoryError(f"Pagination {name} must be a non-negative integer cursor value.")
        with self._connect() as db:
            if upper is None:
                upper = db.execute(f"SELECT coalesce(max({key}),0) FROM {table}").fetchone()[0]
            clauses, params = [f"{key}<=?"], [upper]
            if before is not None:
                clauses.append(f"{key}<?")
                params.append(before)
            if where:
                clauses.append(where)
                params.extend(args)
            rows = db.execute(
                f"SELECT * FROM {table} WHERE {' AND '.join(clauses)} ORDER BY {key} DESC LIMIT ?",
                (*params, limit + 1)).fetchall()
        more = len(rows) > limit
        rows = rows[:limit]
        items = []
        for row in rows:
            item = self._row(row)
            item[key] = row[key]
            items.append(item)
        return {"items": items, "upper": upper,
                "next_before": rows[-1][key] if more else None}

    def page_checkpoints(self, limit=50, before=None, upper=None, segment_id=None, expected_revision=None):
        with self._connect() as db:
            db.execute("BEGIN")
            return history_order.page(self, db, limit, before, upper, segment_id, expected_revision)

    def page_segments(self, limit=50, before=None, upper=None):
        return self._page("segments", limit, before, upper)

    def page_events(self, limit=50, before=None, upper=None, segment_id=None):
        if segment_id is None:
            return self._page("events", limit, before, upper)
        return self._page("events", limit, before, upper, "segment_id=?", (segment_id,))

    def get_segment(self, segment_id):
        with self._connect() as db:
            row = db.execute("SELECT * FROM segments WHERE id=?", (segment_id,)).fetchone()
        if row is None:
            raise RepositoryError("Segment not found; inspect segments and use its full id.")
        return self._row(row)

    def get_event(self, seq):
        if isinstance(seq, bool) or not isinstance(seq, int):
            raise RepositoryError("Event seq must be an integer.")
        with self._connect() as db:
            row = db.execute("SELECT * FROM events WHERE seq=?", (seq,)).fetchone()
        if row is None:
            raise RepositoryError("Event not found; list events and use its seq.")
        return self._row(row)

    def segment_counts(self, segment_id):
        """Exact per-segment counts (not a recent-window estimate)."""
        with self._connect() as db:
            checkpoints = db.execute("SELECT count(*) FROM checkpoints WHERE segment_id=?", (segment_id,)).fetchone()[0]
            events = db.execute("SELECT count(*) FROM events WHERE segment_id=?", (segment_id,)).fetchone()[0]
        return {"checkpoints": checkpoints, "events": events}

    def counts(self):
        """Row counts only; no object directory scan (see stats for storage bytes)."""
        with self._connect() as db:
            out = {name: db.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
                   for name in ("events", "segments", "checkpoints")}
            out["logical_bytes"] = db.execute("SELECT coalesce(sum(size),0) FROM checkpoints").fetchone()[0]
            if history_order.visible(db) != "1":
                out["stored_checkpoints"] = out["checkpoints"]
                out["checkpoints"] = db.execute("SELECT count(*) FROM checkpoints c WHERE " + history_order.visible(db)).fetchone()[0]
            head = history_order.head(db)
        out["last_checkpoint_id"] = head["id"] if head else None
        out["last_checkpoint_at"] = head["created_at"] if head else None
        return out

    def change_summaries(self, checkpoint_ids):
        """Batch of ChangeSet-root summaries for up to 100 checkpoint ids, one read
        snapshot, no layout/recipe/entry access (docs/SPEC_PERFORMANCE_READ_ACCESS.md §4.4)."""
        from .storage import summaries as _summaries
        try:
            return _summaries.change_summaries(self, checkpoint_ids)
        except (_engine.StorageError, sqlite3.Error) as exc:
            raise RepositoryError(str(exc)) from exc

    def evidence_package(self, checkpoint_ids):
        """Read a bounded, ordered history window; no layout payload reads or writes."""
        from .storage import evidence as _evidence
        try:
            return _evidence.evidence_package(self, checkpoint_ids)
        except (_engine.StorageError, sqlite3.Error) as exc:
            raise RepositoryError(str(exc)) from exc

    def known_checkpoint_ids(self, checkpoint_ids):
        """Which of `checkpoint_ids` exist in this history, in one query -- the batch
        equivalent of calling get_checkpoint() once per id to check scope."""
        ids = list(dict.fromkeys(checkpoint_ids))
        if not ids:
            return set()
        found = set()
        with self._connect() as db:
            for start in range(0, len(ids), _IN_CHUNK):     # bounded IN (...) lists whatever the caller passes
                chunk = ids[start:start + _IN_CHUNK]
                placeholders = ",".join("?" for _ in chunk)
                found.update(row[0] for row in
                             db.execute(f"SELECT id FROM checkpoints WHERE id IN ({placeholders})", chunk))
        return found

    def stats(self):
        with self._connect() as db:
            counts = {name: db.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
                      for name in ("events", "segments", "checkpoints")}
            counts["logical_bytes"] = db.execute("SELECT coalesce(sum(size),0) FROM checkpoints").fetchone()[0]
        objects = list((self.root / "objects").glob("*/*"))
        objects = [path for path in objects if _HASH.fullmatch(path.name) and path.is_file()]
        loose_bytes = sum(path.stat().st_size for path in objects)
        counts.update(objects=len(objects), stored_bytes=loose_bytes, loose_objects=len(objects),
                      loose_bytes=loose_bytes, pack_objects=0, pack_bytes=0, packs=0, format=self.format)
        if self.format == 2:
            with self._connect() as db:
                pack_objects = db.execute("SELECT count(*) FROM objects").fetchone()[0]
                packs = db.execute("SELECT count(*), coalesce(sum(size),0) FROM packs").fetchone()
            counts.update(pack_objects=pack_objects, packs=packs[0], pack_bytes=packs[1],
                          objects=len(objects) + pack_objects, stored_bytes=loose_bytes + packs[1])
        codec = self.services.codecs.delta_codec
        counts["delta_encoder"] = codec.encoder_version() if codec is not None else None      # None = "pip install vestigraph[storage-delta]"
        from vestigraph.vesti_formats.vesti_format_gds import native as _scan_backend
        counts["scan_backend"] = {"preference": _engine.scan_backend_preference(),
                                  "native": _scan_backend.capability()}
        counts["compression"] = _engine.adaptive.status()
        return counts
