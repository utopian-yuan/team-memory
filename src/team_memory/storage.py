import hashlib
import json
import os
import secrets
import sqlite3
import tempfile
from contextlib import contextmanager
from pathlib import Path

from .models import Failure, iso, now

SCHEMA = """
CREATE TABLE spaces (id TEXT PRIMARY KEY);
CREATE TABLE grants (actor TEXT, space TEXT REFERENCES spaces, role TEXT NOT NULL, PRIMARY KEY(actor,space));
CREATE TABLE tokens (digest TEXT PRIMARY KEY, actor TEXT NOT NULL, client TEXT NOT NULL, human INTEGER NOT NULL, scopes TEXT, max_role TEXT NOT NULL);
CREATE TABLE bindings (group_id TEXT REFERENCES spaces, project TEXT REFERENCES spaces, PRIMARY KEY(group_id,project));
CREATE TABLE sources (id TEXT PRIMARY KEY, space TEXT REFERENCES spaces, adapter TEXT, external_id TEXT,
 revision TEXT, blob TEXT, author TEXT, occurred TEXT, ingested TEXT, day TEXT, live INTEGER DEFAULT 1,
 trusted INTEGER DEFAULT 0, processed TEXT, UNIQUE(space,adapter,external_id,revision));
CREATE UNIQUE INDEX live_source ON sources(space,adapter,external_id) WHERE live=1;
CREATE TABLE memories (id TEXT, version INTEGER, space TEXT REFERENCES spaces, kind TEXT, claim TEXT,
 subjects TEXT, topic TEXT, state TEXT, reason TEXT, base INTEGER, effective TEXT, recorded TEXT, recheck TEXT,
 PRIMARY KEY(id,version));
CREATE UNIQUE INDEX current_memory ON memories(id) WHERE state IN ('active','disputed');
CREATE UNIQUE INDEX current_topic ON memories(space,topic) WHERE topic IS NOT NULL AND state IN ('active','disputed');
CREATE TABLE evidence (id TEXT, version INTEGER, source TEXT REFERENCES sources, quote TEXT, offset INTEGER,
 PRIMARY KEY(id,version,source,offset), FOREIGN KEY(id,version) REFERENCES memories(id,version));
CREATE TABLE reviews (id TEXT PRIMARY KEY, memory TEXT, version INTEGER, status TEXT, generation INTEGER,
 remind TEXT, opened TEXT, FOREIGN KEY(memory,version) REFERENCES memories(id,version));
CREATE TABLE decisions (review TEXT REFERENCES reviews, generation INTEGER, actor TEXT, action TEXT,
 at TEXT, before_claim TEXT, after_claim TEXT, PRIMARY KEY(review,generation));
CREATE TABLE requests (actor TEXT, client TEXT, request TEXT, digest TEXT, response TEXT,
 PRIMARY KEY(actor,client,request));
CREATE VIRTUAL TABLE search_index USING fts5(id UNINDEXED, version UNINDEXED, terms);
CREATE TABLE transitions (id TEXT, version INTEGER, state TEXT, at TEXT);
PRAGMA user_version=2;
"""


def uid():
    return secrets.token_hex(16)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def packed(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def atomic_file(path, data):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as out:
        tmp = Path(out.name)
        try:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
    try:
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


MIGRATION_3 = (
    """CREATE TABLE legacy_records(space TEXT REFERENCES spaces, legacy_id TEXT, source TEXT REFERENCES sources,
       status TEXT, payload TEXT, PRIMARY KEY(space,legacy_id))""",
    """CREATE TABLE archive_members(space TEXT REFERENCES spaces, archive TEXT, external_id TEXT,
       PRIMARY KEY(space,archive,external_id))""",
)


class Store:
    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.blobs = self.root / "events"
        self.blobs.mkdir(mode=0o700, exist_ok=True)
        self.db = self.root / "state.sqlite"
        with self.tx() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                # Each statement is inside the initialization write lock.
                for statement in SCHEMA.strip().split(";"):
                    if statement.strip():
                        db.execute(statement)
            elif version == 1:
                db.execute("ALTER TABLE tokens ADD COLUMN scopes TEXT")
                db.execute(
                    "ALTER TABLE tokens ADD COLUMN max_role TEXT NOT NULL DEFAULT 'review'"
                )
                db.execute("PRAGMA user_version=2")
            elif version not in (2, 3):
                raise RuntimeError("unsupported database version")
            if version < 3:
                for statement in MIGRATION_3:
                    db.execute(statement)
                db.execute("PRAGMA user_version=3")

    @contextmanager
    def tx(self, write=True):
        db = sqlite3.connect(self.db, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            db.commit()
        finally:
            db.close()

    def blob(self, text):
        key = digest(text)
        path = self.blobs / (key + ".jsonl")
        if not path.exists():
            atomic_file(path, (packed({"text": text}) + "\n").encode())
        return key

    def read_blob(self, key):
        text = json.loads((self.blobs / (key + ".jsonl")).read_text())["text"]
        if digest(text) != key:
            raise OSError("source checksum mismatch")
        return text

    def grant(self, actor, space, role):
        if space.startswith("personal:") and space != f"personal:{actor}":
            raise Failure(403, "personal space belongs to its owner")
        with self.tx() as db:
            db.execute("INSERT OR IGNORE INTO spaces VALUES (?)", (space,))
            db.execute(
                "INSERT OR REPLACE INTO grants VALUES (?,?,?)", (actor, space, role)
            )

    def issue(self, actor, client, human=False, *, spaces=None, max_role="review"):
        token = secrets.token_urlsafe(32)
        with self.tx() as db:
            db.execute(
                "INSERT INTO tokens VALUES (?,?,?,?,?,?)",
                (
                    digest(token),
                    actor,
                    client,
                    human,
                    packed(spaces) if spaces is not None else None,
                    max_role,
                ),
            )
        return token

    def revoke_client(self, actor, client):
        with self.tx() as db:
            return db.execute(
                "DELETE FROM tokens WHERE actor=? AND client=?", (actor, client)
            ).rowcount

    def authenticate(self, token):
        with self.tx(False) as db:
            row = db.execute(
                "SELECT actor,client,human,scopes,max_role FROM tokens WHERE digest=?",
                (digest(token),),
            ).fetchone()
        if not row:
            raise Failure(401, "authentication required")
        principal = dict(row)
        principal["scopes"] = (
            json.loads(row["scopes"]) if row["scopes"] is not None else None
        )
        return principal


def require(db, who, space, role="read"):
    row = db.execute(
        "SELECT role FROM grants WHERE actor=? AND space=?", (who["actor"], space)
    ).fetchone()
    rank = {"read": 0, "write": 1, "review": 2}
    allowed = who.get("scopes")
    if (
        (allowed is not None and space not in allowed)
        or rank[who.get("max_role", "review")] < rank[role]
        or not row
        or rank[row[0]] < rank[role]
    ):
        raise Failure(404, "not found")


def transition(db, memory, version, state):
    db.execute(
        "UPDATE memories SET state=? WHERE id=? AND version=?", (state, memory, version)
    )
    if state in ("rejected", "revoked", "superseded"):
        db.execute(
            "DELETE FROM search_index WHERE id=? AND version=?", (memory, version)
        )
    db.execute(
        "INSERT INTO transitions VALUES (?,?,?,?)", (memory, version, state, iso(now()))
    )
