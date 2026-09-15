"""User-confirmed history order, independent of immutable storage parents.

Created lazily for imports. Existing checkpoint IDs, parents and packs never change.
Every insertion and its idempotency marker publish in the checkpoint transaction.
"""
from contextlib import contextmanager

TABLE = "history_timeline"


def enabled(db):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone() is not None


def ensure(db):
    db.execute("CREATE TABLE IF NOT EXISTS history_timeline (checkpoint_id TEXT PRIMARY KEY REFERENCES checkpoints(id), position INTEGER NOT NULL)")
    db.execute("CREATE INDEX IF NOT EXISTS history_position ON history_timeline(position)")
    db.execute("INSERT OR IGNORE INTO history_timeline SELECT id,ordinal FROM checkpoints")
    db.execute("CREATE TABLE IF NOT EXISTS history_import_items (batch_id TEXT NOT NULL,item_id TEXT NOT NULL,sha256 TEXT NOT NULL,checkpoint_id TEXT NOT NULL REFERENCES checkpoints(id),PRIMARY KEY(batch_id,item_id))")
    db.execute("INSERT OR IGNORE INTO config VALUES('history_revision','0')")


def revision(db):
    row = db.execute("SELECT value FROM config WHERE key='history_revision'").fetchone()
    return int(row[0]) if row else 0


def visible(db):
    exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='history_collapsed'").fetchone()
    return "c.id NOT IN (SELECT checkpoint_id FROM history_collapsed)" if exists else "1"


def head(db):
    if enabled(db):
        return db.execute("SELECT c.* FROM checkpoints c JOIN history_timeline h ON h.checkpoint_id=c.id WHERE " + visible(db) + " ORDER BY h.position DESC LIMIT 1").fetchone()
    return db.execute("SELECT * FROM checkpoints ORDER BY ordinal DESC LIMIT 1").fetchone()


def predecessor(db, anchor):
    from .store import RepositoryError
    if not enabled(db):
        raise RepositoryError("History import has not been initialized.")
    rank = db.execute("SELECT position FROM history_timeline WHERE checkpoint_id=?", (anchor,)).fetchone()
    if rank is None:
        raise RepositoryError("The version to insert before no longer exists.")
    return db.execute("SELECT c.* FROM checkpoints c JOIN history_timeline h ON h.checkpoint_id=c.id WHERE h.position<? AND " + visible(db) + " ORDER BY h.position DESC LIMIT 1", (rank[0],)).fetchone()


def publish(db, checkpoint_id, before_id=None, import_key=None, sha256=None):
    if not enabled(db):
        return
    if before_id is None:
        pos = db.execute("SELECT coalesce(max(position),0)+1 FROM history_timeline").fetchone()[0]
    else:
        pos = db.execute("SELECT position FROM history_timeline WHERE checkpoint_id=?", (before_id,)).fetchone()[0]
        db.execute("UPDATE history_timeline SET position=position+1 WHERE position>=?", (pos,))
        db.execute("UPDATE config SET value=CAST(value AS INTEGER)+1 WHERE key='history_revision'")
    db.execute("INSERT INTO history_timeline VALUES(?,?)", (checkpoint_id, pos))
    if import_key is not None:
        db.execute("INSERT INTO history_import_items VALUES(?,?,?,?)", (*import_key, sha256, checkpoint_id))


def decorate(db, item):
    if enabled(db):
        row = db.execute("SELECT position FROM history_timeline WHERE checkpoint_id=?", (item["id"],)).fetchone()
        previous = predecessor(db, item["id"]) if row else None
        item["history_parent_id"] = previous["id"] if previous else None
        item["history_position"] = row[0] if row else None
    return item


def page(repo, db, limit, before, upper, segment_id, expected_revision=None):
    from .store import RepositoryError, _limit
    limit = _limit(limit)
    rev = revision(db)
    if expected_revision is not None and expected_revision != rev:
        raise RepositoryError("History order changed; reload the version list.")
    for value in (before, upper):
        if value is not None and (type(value) is not int or value < 0):
            raise RepositoryError("Pagination must use non-negative integer cursor values.")
    has_order = enabled(db)
    position = "h.position" if has_order else "c.ordinal"
    source = "checkpoints c JOIN history_timeline h ON h.checkpoint_id=c.id" if has_order else "checkpoints c"
    if upper is None:
        upper = db.execute(f"SELECT coalesce(max({position}),0) FROM {source}").fetchone()[0]
    clauses, args = [f"{position}<=?", visible(db)], [upper]
    if before is not None:
        clauses.append(f"{position}<?")
        args.append(before)
    if segment_id is not None:
        clauses.append("c.segment_id=?")
        args.append(segment_id)
    columns = "c.*,h.position AS history_position" if has_order else "c.*"
    rows = db.execute(f"SELECT {columns} FROM {source} WHERE " + " AND ".join(clauses) + f" ORDER BY {position} DESC LIMIT ?", (*args, limit+1)).fetchall()
    items = []
    for row in rows[:limit]:
        item = repo._row(row)
        item["ordinal"] = row["ordinal"]
        items.append(decorate(db, item))
    return {"items": items, "upper": upper, "next_before": rows[limit-1]["history_position" if has_order else "ordinal"] if len(rows)>limit else None, "history_revision": rev}


@contextmanager
def writer(repo):
    own = not repo.writer_held
    if own:
        repo.acquire_writer("legacy history import")
    try:
        yield
    finally:
        if own:
            repo.release_writer()


def import_checkpoint(repo, path, *, before_id, batch_id, item_id, expected_sha256,
                      title="", metadata=None, filename=None, cancel=None):
    from .store import RepositoryError, _HASH
    if repo.format != 2:
        raise RepositoryError("Upgrade this history to storage format 2 before importing older versions.")
    if not all(isinstance(v, str) and 1 <= len(v) <= 128 for v in (before_id, batch_id, item_id)):
        raise RepositoryError("Import requires a target version, batch ID and item ID.")
    if not isinstance(expected_sha256, str) or not _HASH.fullmatch(expected_sha256):
        raise RepositoryError("Import requires the confirmed input SHA-256.")
    if metadata is not None and not isinstance(metadata, dict):
        raise RepositoryError("Import metadata must be a JSON object.")
    with writer(repo):
        with repo._connect() as db:
            ensure(db)
            existing = db.execute("SELECT * FROM history_import_items WHERE batch_id=? AND item_id=?", (batch_id, item_id)).fetchone()
            if existing:
                if existing["sha256"] != expected_sha256:
                    raise RepositoryError("This import item was already used for different content.")
                checkpoint = repo.get_checkpoint(existing["checkpoint_id"])
                if checkpoint["metadata"]["legacy_import"]["before_checkpoint_id"] != before_id:
                    raise RepositoryError("This import item was already used for a different insertion point.")
                return checkpoint
            predecessor(db, before_id)
        prepared = repo.prepare(path, cancel=cancel, _history_before=before_id)
        try:
            if prepared.raw_sha256 != expected_sha256:
                raise RepositoryError("The import file changed after confirmation; select it again.")
            prepared.history_import_key = (batch_id, item_id)
            if filename is not None:
                from pathlib import PureWindowsPath, PurePosixPath
                if (not isinstance(filename, str) or not filename or len(filename)>255
                        or PureWindowsPath(filename).name != filename or PurePosixPath(filename).name != filename):
                    raise RepositoryError("Import filename must be a basename.")
                prepared.original_filename = filename
            info = dict(metadata or {})
            info.update(origin="legacy_file_import", coverage="saved_files_only")
            info["legacy_import"] = dict(info.get("legacy_import") or {}, batch_id=batch_id,
                                         item_id=item_id, before_checkpoint_id=before_id)
            return repo.commit(prepared, title=title, source="manual", metadata=info)
        except BaseException:
            prepared.discard()
            raise
