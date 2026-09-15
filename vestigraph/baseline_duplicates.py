"""Hide proven redundant automatic baselines from the logical timeline.

The original checkpoints, bytes and storage parents remain available by ID.
Manual checkpoints, imports and changed content are never collapsed.
"""
from . import history_order
from .capture_noop import same_document


def _digest(repo, record):
    manifest = record["manifest"]
    if manifest.get("format") == 2:
        return (manifest.get("normalized_algorithm"), manifest.get("normalized_sha256"))
    handler = repo.services.formats.storage_for_content(
        repo._read_chunk(manifest["chunks"][0]["hash"], manifest["chunks"][0]["size"])[:repo.services.formats.probe_size])
    compare = getattr(handler, "normalized_legacy", None)
    return compare(repo, record) if compare else None


def reconcile(repo):
    if repo.format != 2:
        return 0
    count = 0
    with history_order.writer(repo):
        with repo._connect() as db:
            rows = db.execute("SELECT * FROM checkpoints ORDER BY ordinal LIMIT 8").fetchall()
            hidden = {row[0] for row in db.execute("SELECT checkpoint_id FROM history_collapsed")} if history_order.visible(db) != "1" else set()
        for before_row, after_row in zip(rows, rows[1:]):
            if before_row["id"] in hidden:
                continue
            before, after = repo._row(before_row), repo._row(after_row)
            if any(r["source"] != "system" or r["title"] != "Capture baseline"
                   or r["metadata"].get("capture") not in ("klink", "editor")
                   or r["metadata"].get("origin") == "legacy_file_import" for r in (before, after)):
                continue
            if before["size"] != after["size"] or not same_document(before, {"checkpoint_metadata": after["metadata"]}):
                continue
            left, right = _digest(repo, before), _digest(repo, after)
            if left is None or left != right or not left[1]:
                continue
            with repo._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                history_order.ensure(db)
                ranks = dict(db.execute("SELECT checkpoint_id,position FROM history_timeline WHERE checkpoint_id IN (?,?)", (before["id"], after["id"])))
                if ranks[before["id"]] >= ranks[after["id"]]:
                    continue
                # Imported versions may sit between these originally adjacent baselines.
                # Preserve those nodes and their order; only the proven duplicate is hidden.
                db.execute("CREATE TABLE IF NOT EXISTS history_collapsed (checkpoint_id TEXT PRIMARY KEY REFERENCES checkpoints(id), equivalent_id TEXT NOT NULL REFERENCES checkpoints(id), reason TEXT NOT NULL, algorithm TEXT NOT NULL, digest TEXT NOT NULL)")
                db.execute("INSERT OR IGNORE INTO history_collapsed VALUES(?,?,?,?,?)",
                           (before["id"], after["id"], "redundant_automatic_baseline", *left))
                db.execute("UPDATE config SET value=CAST(value AS INTEGER)+1 WHERE key='history_revision'")
                count += 1
    return count
