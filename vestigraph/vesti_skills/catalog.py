"""Append-only skill revisions in the service catalog, outside raw history storage."""
import json
import uuid
from ..service.catalog import fingerprint
from ..service.errors import bad_request, conflict, not_found
from ..service.state import now_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS skill_assets (
 ordinal INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
 project_id TEXT NOT NULL REFERENCES projects(id),
 document_id TEXT NOT NULL REFERENCES documents(id),
 title TEXT NOT NULL, state TEXT NOT NULL, revision INTEGER NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 request_key TEXT, request_digest TEXT NOT NULL,
 UNIQUE(project_id,request_key));
CREATE INDEX IF NOT EXISTS skill_assets_project ON skill_assets(project_id,ordinal);
CREATE TABLE IF NOT EXISTS skill_revisions (
 skill_id TEXT NOT NULL REFERENCES skill_assets(id), revision INTEGER NOT NULL,
 content TEXT NOT NULL, content_digest TEXT NOT NULL,
 PRIMARY KEY(skill_id,revision));
"""


def encoded(value):
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
    except (ValueError, TypeError) as exc:
        raise bad_request("Skill data must be finite JSON.") from exc
    if len(text.encode("utf-8")) > 800 * 1024:
        raise bad_request("Skill asset exceeds 800 KiB.")
    return text


class SkillCatalog:
    def __init__(self, catalog):
        self.catalog = catalog
        with catalog._db() as db:
            db.executescript(SCHEMA)

    def get(self, sid, revision=None):
        if revision is not None and (type(revision) is not int or not 1 <= revision <= 2**63-1):
            raise bad_request("Invalid skill revision.")
        with self.catalog._db() as db:
            if revision is None:
                row = db.execute("SELECT r.content FROM skill_assets a JOIN skill_revisions r ON r.skill_id=a.id AND r.revision=a.revision WHERE a.id=?", (sid,)).fetchone()
            else:
                row = db.execute("SELECT content FROM skill_revisions WHERE skill_id=? AND revision=?", (sid,revision)).fetchone()
        if row is None:
            raise not_found("Skill")
        return json.loads(row[0])

    def list(self, project_id, before=None, state=None, limit=30):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise bad_request("Skill list limit must be between 1 and 100.")
        if before is not None and (type(before) is not int or not 1 <= before <= 2**63-1):
            raise bad_request("Invalid skill list cursor.")
        if state is not None and state not in ("awaiting_agent", "draft", "published"):
            raise bad_request("Unknown skill state.")
        clauses, args = ["project_id=?"], [project_id]
        if before is not None:
            clauses.append("ordinal<?"); args.append(before)
        if state is not None:
            clauses.append("state=?"); args.append(state)
        with self.catalog._db() as db:
            rows = db.execute("SELECT * FROM skill_assets WHERE " + " AND ".join(clauses) +
                              " ORDER BY ordinal DESC LIMIT ?", (*args,limit+1)).fetchall()
        items = [{k:r[k] for k in ("id","project_id","document_id","title","state","revision","created_at","updated_at")} for r in rows[:limit]]
        return {"items":items,"next_before":rows[limit-1]["ordinal"] if len(rows)>limit else None}

    def create(self, project_id, document_id, payload, request_key=None):
        digest = fingerprint(payload)
        with self.catalog._db() as db:
            db.execute("BEGIN IMMEDIATE")
            if request_key:
                old = db.execute("SELECT id,request_digest FROM skill_assets WHERE project_id=? AND request_key=?", (project_id,request_key)).fetchone()
                if old:
                    if old["request_digest"] != digest:
                        raise conflict("SKILL_REQUEST_CONFLICT", "This request ID was already used for another skill.")
                    return self.get(old["id"])
            now = now_iso()
            item = dict(payload, schema="vestigraph.skill", version=1, id=uuid.uuid4().hex,
                        project_id=project_id, document_id=document_id, revision=1,
                        created_at=now, updated_at=now)
            text = encoded(item)
            db.execute("INSERT INTO skill_assets(id,project_id,document_id,title,state,revision,created_at,updated_at,request_key,request_digest) VALUES(?,?,?,?,?,1,?,?,?,?)",
                       (item["id"],project_id,document_id,item["intent"]["title"],item["state"],now,now,request_key,digest))
            db.execute("INSERT INTO skill_revisions VALUES(?,?,?,?)", (item["id"],1,text,fingerprint(item)))
        return item

    def update(self, sid, expected_revision, transform):
        if type(expected_revision) is not int or expected_revision < 1:
            raise bad_request("A positive expected_revision is required.")
        with self.catalog._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT r.content FROM skill_assets a JOIN skill_revisions r ON r.skill_id=a.id AND r.revision=a.revision WHERE a.id=?", (sid,)).fetchone()
            if row is None:
                raise not_found("Skill")
            item = json.loads(row[0])
            if item["revision"] != expected_revision:
                raise conflict("SKILL_REVISION_CONFLICT", "This skill changed in another window; reload before saving.")
            transform(item)
            item["revision"] += 1
            item["updated_at"] = now_iso()
            text = encoded(item)
            db.execute("INSERT INTO skill_revisions VALUES(?,?,?,?)", (sid,item["revision"],text,fingerprint(item)))
            db.execute("UPDATE skill_assets SET title=?,state=?,revision=?,updated_at=? WHERE id=?",
                       (item["intent"]["title"],item["state"],item["revision"],item["updated_at"],sid))
        return item
