"""Optional presentation data, stored with the history, never in layout packs.

Readers never create a database. Labels are append-only, compare-and-swap
revisions; thumbnails are immutable by capture id and bounded PNG blobs.
"""
import hashlib
import json
import sqlite3
import struct
import zlib
import threading
from contextlib import contextmanager
from pathlib import Path
from datetime import datetime, timezone

MAX_PNG = 2 * 1024 * 1024
CAPTURE_IMAGE_LOCK = threading.RLock()
SCHEMA = """
CREATE TABLE IF NOT EXISTS labels (
 checkpoint_id TEXT NOT NULL, revision INTEGER NOT NULL, title TEXT NOT NULL,
 actor TEXT NOT NULL, created_at TEXT NOT NULL, request_key TEXT UNIQUE,
 fingerprint TEXT NOT NULL, PRIMARY KEY(checkpoint_id, revision));
CREATE TABLE IF NOT EXISTS thumbnails (
 capture_id TEXT PRIMARY KEY, png BLOB NOT NULL, metadata TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS thumbnail_status (
 capture_id TEXT PRIMARY KEY, status TEXT NOT NULL, reason_code TEXT NOT NULL,
 updated_at TEXT NOT NULL);
"""


class PresentationError(RuntimeError):
    pass


class LabelConflict(PresentationError):
    pass


def validate_png(data):
    if not isinstance(data, bytes) or not 45 <= len(data) <= MAX_PNG or data[:8] != b"\x89PNG\r\n\x1a\n":
        raise PresentationError("Invalid or oversized PNG.")
    pos, width, height, has_data = 8, None, None, False
    while pos < len(data):
        if pos + 12 > len(data):
            raise PresentationError("Truncated PNG.")
        size = struct.unpack_from(">I", data, pos)[0]
        end = pos + 12 + size
        if end > len(data):
            raise PresentationError("Truncated PNG chunk.")
        kind = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + size]
        if zlib.crc32(kind + body) & 0xffffffff != struct.unpack_from(">I", data, end - 4)[0]:
            raise PresentationError("PNG checksum mismatch.")
        if width is None:
            if kind != b"IHDR" or size != 13:
                raise PresentationError("Missing PNG header.")
            width, height = struct.unpack_from(">II", body)
            if not 1 <= width <= 1920 or not 1 <= height <= 1080:
                raise PresentationError("PNG dimensions exceed the thumbnail limit.")
        elif kind == b"IHDR":
            raise PresentationError("Duplicate PNG header.")
        if kind == b"IDAT":
            has_data = True
        if kind == b"IEND":
            if size or end != len(data) or not has_data:
                raise PresentationError("Invalid PNG end.")
            return width, height
        pos = end
    raise PresentationError("Missing PNG end.")


class Presentation:
    def __init__(self, history_root):
        self.path = Path(history_root) / "presentation.sqlite3"

    @contextmanager
    def _db(self, write=False):
        if not write and not self.path.is_file():
            yield None
            return
        db = sqlite3.connect(str(self.path) if write else self.path.resolve().as_uri() + "?mode=ro",
                             uri=not write, timeout=3)
        db.row_factory = sqlite3.Row
        try:
            if write:
                # The existing relocation copies the main DB, excluding WAL files.
                # A closed writer must leave all presentation bytes in this file.
                db.execute("PRAGMA journal_mode=DELETE")
                db.execute("PRAGMA synchronous=FULL")
                db.executescript(SCHEMA)
            with db:
                yield db
        finally:
            db.close()

    def decorate(self, records):
        for item in records:
            item["original_title"] = item["title"]
            item["title_revision"] = 0
        try:
            self._decorate(records)
        except (sqlite3.Error, OSError, PresentationError):
            # A broken optional image/label DB must not hide restorable versions.
            for item in records:
                item["title"] = item["original_title"]
                item["title_revision"] = 0
                item["presentation_warning"] = "display_metadata_unreadable"
        return records

    def _decorate(self, records):
        with self._db() as db:
            for item in records:
                row = db.execute("SELECT * FROM labels WHERE checkpoint_id=? ORDER BY revision DESC LIMIT 1",
                                 (item["id"],)).fetchone() if db else None
                item["title_revision"] = row["revision"] if row else 0
                if row:
                    if not isinstance(row["title"], str) or len(row["title"]) > 200:
                        raise PresentationError("Invalid saved display name.")
                    item["title"] = row["title"]

    def rename(self, checkpoint_id, title, expected_revision, *, actor="user", request_key=None):
        if not isinstance(title, str) or not 1 <= len(title.strip()) <= 200 or any(ord(c) < 32 for c in title):
            raise PresentationError("Name must be 1-200 characters without control characters.")
        if type(expected_revision) is not int or expected_revision < 0 or actor not in ("user", "agent"):
            raise PresentationError("Invalid title revision or actor.")
        title = title.strip()
        fingerprint = hashlib.sha256(json.dumps([checkpoint_id, title, expected_revision, actor],
                                                ensure_ascii=False).encode()).hexdigest()
        with self._db(write=True) as db:
            db.execute("BEGIN IMMEDIATE")
            if request_key:
                prior = db.execute("SELECT * FROM labels WHERE request_key=?", (request_key,)).fetchone()
                if prior:
                    if prior["fingerprint"] != fingerprint:
                        raise LabelConflict("This request id was already used for a different rename.")
                    return dict(prior)
            current = db.execute("SELECT coalesce(max(revision),0) FROM labels WHERE checkpoint_id=?",
                                 (checkpoint_id,)).fetchone()[0]
            if current != expected_revision:
                raise LabelConflict("The name changed elsewhere; refresh before renaming.")
            created = datetime.now(timezone.utc).isoformat()
            db.execute("INSERT INTO labels VALUES(?,?,?,?,?,?,?)",
                       (checkpoint_id, current + 1, title, actor, created, request_key, fingerprint))
            return dict(db.execute("SELECT * FROM labels WHERE checkpoint_id=? AND revision=?",
                                   (checkpoint_id, current + 1)).fetchone())

    def put_thumbnail(self, capture_id, png, metadata):
        width, height = validate_png(png)
        info = dict(metadata, width=width, height=height, sha256=hashlib.sha256(png).hexdigest())
        with self._db(write=True) as db:
            db.execute("INSERT OR IGNORE INTO thumbnails VALUES(?,?,?)",
                       (capture_id, png, json.dumps(info, ensure_ascii=False)))
            db.execute("DELETE FROM thumbnail_status WHERE capture_id=?", (capture_id,))

    def record_thumbnail_failure(self, capture_id, reason):
        from .thumbnail_diagnostics import REASONS
        if reason not in REASONS:
            raise PresentationError("Invalid screenshot reason.")
        status = "unavailable" if reason == "unsupported" else "cancelled" if reason == "cancelled" else "failed"
        with self._db(write=True) as db:
            # A late failure cannot replace an already saved image.
            db.execute("INSERT OR REPLACE INTO thumbnail_status SELECT ?,?,?,? "
                       "WHERE NOT EXISTS(SELECT 1 FROM thumbnails WHERE capture_id=?)",
                       (capture_id, status, reason, datetime.now(timezone.utc).isoformat(), capture_id))

    def thumbnail_status(self, capture_id):
        from .thumbnail_diagnostics import REASONS, STATES
        missing = {"status": "not_generated", "reason_code": None, "updated_at": None}
        with self._db() as db:
            if db is None or not capture_id:
                return missing
            if db.execute("SELECT 1 FROM thumbnails WHERE capture_id=?", (capture_id,)).fetchone():
                # Presence only: PNG route performs integrity and export-binding checks.
                return {"status": "stored", "reason_code": None, "updated_at": None}
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='thumbnail_status'").fetchone():
                return missing
            sizes = db.execute("SELECT length(status),length(reason_code),length(updated_at) "
                               "FROM thumbnail_status WHERE capture_id=?", (capture_id,)).fetchone()
            if sizes and any(type(n) is not int or not 0 < n <= 64 for n in sizes):
                raise PresentationError("Oversized screenshot status.")
            row = db.execute("SELECT status,reason_code,updated_at FROM thumbnail_status WHERE capture_id=?",
                             (capture_id,)).fetchone()
            if row is None:
                return missing
            result = dict(row)
            if (result["status"] not in STATES or result["reason_code"] not in REASONS
                    or not isinstance(result["updated_at"], str) or len(result["updated_at"]) > 64):
                raise PresentationError("Invalid screenshot status.")
            return result

    def discard_thumbnail(self, capture_id):
        with CAPTURE_IMAGE_LOCK:
            if not self.path.exists():
                return
            with self._db(write=True) as db:
                db.execute("DELETE FROM thumbnails WHERE capture_id=?", (capture_id,))
                db.execute("DELETE FROM thumbnail_status WHERE capture_id=?", (capture_id,))

    def thumbnail(self, capture_id):
        with self._db() as db:
            sizes = db.execute("SELECT length(png),length(metadata) FROM thumbnails WHERE capture_id=?",
                               (capture_id,)).fetchone() if db and capture_id else None
            if sizes and (sizes[0] > MAX_PNG or sizes[1] > 65536):
                raise PresentationError("Oversized thumbnail record.")
            row = db.execute("SELECT png,metadata FROM thumbnails WHERE capture_id=?",
                             (capture_id,)).fetchone() if db and capture_id else None
        if row is None:
            return None
        png, info = bytes(row["png"]), json.loads(row["metadata"])
        if not isinstance(info, dict):
            raise PresentationError("Invalid thumbnail metadata.")
        validate_png(png)
        if hashlib.sha256(png).hexdigest() != info["sha256"]:
            raise PresentationError("Thumbnail checksum mismatch.")
        return png, info
