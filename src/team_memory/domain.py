import json
import re
from zoneinfo import ZoneInfo

from .models import (
    WORKSPACE_STORAGE_KEY,
    Decision,
    Failure,
    Record,
    Source,
    iso,
    now,
    public_adapter,
)
from .storage import Store, digest, packed, require, transition, uid

CURRENT = "('active','disputed')"


def tokens(text):
    # Index Han characters and overlapping bigrams, retain identifiers as full tokens.
    output = []
    for word in re.findall(r"[\u3400-\u9fff]+|[a-z0-9]+|[^\W_]", text.casefold()):
        if re.fullmatch(r"[\u3400-\u9fff]+", word):
            output.extend(word)
            output.extend(word[i : i + 2] for i in range(len(word) - 1))
        else:
            output.append(word)
    return list(dict.fromkeys(output))


def open_review(db, memory, version):
    review = uid()
    db.execute(
        "INSERT INTO reviews VALUES (?,?,?,'pending',1,NULL,?)",
        (review, memory, version, iso(now())),
    )
    return review


def cancel(db, memory, version):
    transition(db, memory, version, "revoked")
    db.execute(
        "UPDATE reviews SET status='revoked',generation=generation+1 WHERE memory=? AND version=? AND status IN ('pending','deferred')",
        (memory, version),
    )


def retire_source(db, source):
    db.execute("UPDATE sources SET live=0 WHERE id=?", (source,))
    rows = db.execute(
        """SELECT m.* FROM memories m JOIN evidence e ON m.id=e.id AND m.version=e.version
                         WHERE e.source=? AND m.state NOT IN ('revoked','rejected')""",
        (source,),
    ).fetchall()
    for row in rows:
        remaining = db.execute(
            """SELECT s.trusted,s.space,s.adapter,e.quote FROM evidence e JOIN sources s ON s.id=e.source
                                  WHERE e.id=? AND e.version=? AND s.live=1""",
            (row["id"], row["version"]),
        ).fetchall()
        if not remaining:
            cancel(db, row["id"], row["version"])
        elif row["state"] == "active" and not any(
            ref["trusted"]
            and ref["space"] == row["space"]
            and ref["quote"] == row["claim"]
            and ref["adapter"] in ("personal_note", "project_document", "human_review")
            for ref in remaining
        ):
            transition(db, row["id"], row["version"], "disputed")
            open_review(db, row["id"], row["version"])


class MemoryService:
    def __init__(self, store: Store, timezone="Asia/Shanghai"):
        self.store = store
        self.zone = ZoneInfo(timezone)

    def ingest(self, s: Source, who=None, *, trusted=False, dry_run=False):
        if s.adapter == "feishu_group" and not s.space.startswith("group:"):
            raise Failure(422, "Feishu archives require a group space")
        fingerprint = digest(s.text)
        revision = s.revision or fingerprint
        with self.store.tx(not dry_run) as db:
            if who:
                require(db, who, s.space, "write")
            elif not db.execute(
                "SELECT id FROM spaces WHERE id=?", (s.space,)
            ).fetchone():
                raise Failure(404, "space must be provisioned first")
            external_id = (
                digest(packed([who["actor"], who["client"], s.external_id]))
                if who
                else s.external_id
            )
            adapter_key = (
                WORKSPACE_STORAGE_KEY
                if s.adapter == "assistant_workspace"
                else s.adapter
            )
            key = (s.space, adapter_key, external_id)
            row = db.execute(
                "SELECT * FROM sources WHERE space=? AND adapter=? AND external_id=? AND revision=?",
                (*key, revision),
            ).fetchone()
            if row:
                if (
                    row["blob"] != fingerprint
                    or row["author"] != s.author
                    or (
                        s.revision is not None and row["occurred"] != iso(s.occurred_at)
                    )
                ):
                    raise Failure(
                        409, "same source revision has different content or metadata"
                    )
                return {"status": "existing", "source_id": row["id"]}
            if dry_run:
                return {"status": "new"}
            old = db.execute(
                "SELECT id FROM sources WHERE space=? AND adapter=? AND external_id=? AND live=1",
                key,
            ).fetchone()
            if old:
                retire_source(db, old["id"])
            source_id = uid()
            db.execute(
                """INSERT INTO sources(id,space,adapter,external_id,revision,blob,author,occurred,ingested,day,trusted)
                          VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    source_id,
                    *key,
                    revision,
                    self.store.blob(s.text),
                    s.author,
                    iso(s.occurred_at),
                    iso(now()),
                    str(s.occurred_at.astimezone(self.zone).date()),
                    trusted and who is None,
                ),
            )
            return {"status": "added", "source_id": source_id}

    def withdraw(self, source, who=None):
        with self.store.tx() as db:
            row = db.execute(
                "SELECT space FROM sources WHERE id=?", (source,)
            ).fetchone()
            if not row:
                raise Failure(404, "not found")
            if who:
                require(db, who, row["space"], "review")
                if not who["human"]:
                    raise Failure(403, "human confirmation required")
            retire_source(db, source)

    def bind(self, group, project, remove=False):
        if not group.startswith("group:") or not project.startswith("project:"):
            raise Failure(422, "expected group and project")
        with self.store.tx() as db:
            if not remove:
                db.execute(
                    "INSERT OR IGNORE INTO bindings VALUES (?,?)", (group, project)
                )
                return
            db.execute(
                "DELETE FROM bindings WHERE group_id=? AND project=?", (group, project)
            )
            rows = db.execute(
                """SELECT DISTINCT m.id,m.version FROM memories m JOIN evidence e
                               ON e.id=m.id AND e.version=m.version JOIN sources s ON s.id=e.source
                               WHERE m.space=? AND s.space=? AND m.state!='revoked'""",
                (project, group),
            ).fetchall()
            for row in rows:
                cancel(db, *row)

    def record(self, who, data: Record):
        with self.store.tx() as db:
            require(db, who, data.space, "write")
            return self.add_record(db, who, data)

    def add_record(self, db, who, data, *, direct=False, reasons=None):
        key = (who["actor"], who["client"], data.request_id)
        signature = digest(data.model_dump_json())
        replay = db.execute(
            "SELECT * FROM requests WHERE actor=? AND client=? AND request=?", key
        ).fetchone()
        if replay:
            if replay["digest"] != signature:
                raise Failure(409, "request ID already used")
            return json.loads(replay["response"])
        evidence = {}
        for ref in data.evidence:
            src = db.execute(
                "SELECT * FROM sources WHERE id=? AND live=1", (ref.source_id,)
            ).fetchone()
            if not src:
                raise Failure(404, "not found")
            if src["space"] != data.space:
                require(db, who, src["space"], "review")
                bound = db.execute(
                    "SELECT 1 FROM bindings WHERE group_id=? AND project=?",
                    (src["space"], data.space),
                ).fetchone()
                if not bound or not who["human"]:
                    raise Failure(403, "authorized group publication required")
                require(db, who, data.space, "review")
                direct = False
            offset = self.store.read_blob(src["blob"]).find(ref.quote)
            if offset < 0:
                raise Failure(422, "quote does not match source")
            evidence[(ref.source_id, offset)] = ref.quote
        memory = data.memory_id
        base = data.base_version
        topic = data.topic
        if memory:
            old = db.execute(
                f"SELECT version,topic FROM memories WHERE id=? AND space=? AND state IN {CURRENT}",
                (memory, data.space),
            ).fetchone()
            if not old:
                raise Failure(404, "not found")
            if old["version"] != base:
                raise Failure(409, "stale base version")
            if topic is not None and topic != old["topic"]:
                raise Failure(409, "topic changed")
            topic = old["topic"]
        elif topic:
            old = db.execute(
                f"SELECT id,version,claim,state FROM memories WHERE space=? AND topic=? AND state IN {CURRENT}",
                (data.space, topic),
            ).fetchone()
            if old:
                memory, base = old["id"], old["version"]
                if (
                    direct
                    and not reasons
                    and old["state"] == "active"
                    and old["claim"] == data.claim
                    and evidence
                ):
                    for (source, offset), quote in evidence.items():
                        db.execute(
                            "INSERT OR IGNORE INTO evidence VALUES (?,?,?,?,?)",
                            (memory, base, source, quote, offset),
                        )
                    response = {
                        "memory_id": memory,
                        "version": base,
                        "state": "active",
                        "reasons": [],
                    }
                    db.execute(
                        "INSERT INTO requests VALUES (?,?,?,?,?)",
                        (*key, signature, packed(response)),
                    )
                    return response
        memory = memory or uid()
        version = db.execute(
            "SELECT coalesce(max(version),0)+1 FROM memories WHERE id=?", (memory,)
        ).fetchone()[0]
        current_time = iso(now())
        reasons = list(reasons or [])
        if base is not None:
            reasons.append("changes_current_decision")
        if not evidence:
            reasons.append("missing_evidence")
        if not direct and not reasons:
            reasons.append("requires_confirmation")
        state = "candidate" if reasons else "active"
        db.execute(
            "INSERT INTO memories VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                memory,
                version,
                data.space,
                data.kind,
                data.claim,
                packed(data.subjects),
                topic,
                state,
                packed(reasons),
                base,
                iso(data.effective_at) if data.effective_at else current_time,
                current_time,
                iso(data.recheck_at) if data.recheck_at else None,
            ),
        )
        for (source, offset), quote in evidence.items():
            db.execute(
                "INSERT INTO evidence VALUES (?,?,?,?,?)",
                (memory, version, source, quote, offset),
            )
        db.execute(
            "INSERT INTO search_index VALUES (?,?,?)",
            (memory, version, " ".join(tokens(data.claim))),
        )
        transition(db, memory, version, state)
        response = {
            "memory_id": memory,
            "version": version,
            "state": state,
            "reasons": reasons,
        }
        if state == "candidate":
            response["review_id"] = open_review(db, memory, version)
        db.execute(
            "INSERT INTO requests VALUES (?,?,?,?,?)",
            (*key, signature, packed(response)),
        )
        return response

    def detail_row(self, db, who, row):
        require(db, who, row["space"])
        result = dict(row)
        result["subjects"] = json.loads(result["subjects"])
        result["reason"] = json.loads(result["reason"])
        result["evidence"] = [
            {**e, "adapter": public_adapter(e["adapter"])}
            for e in db.execute(
                """SELECT e.source AS source_id,e.quote,e.offset,s.adapter,s.revision,s.space
                FROM evidence e JOIN sources s ON s.id=e.source JOIN grants g ON g.space=s.space
                WHERE g.actor=? AND e.id=? AND e.version=? AND s.live=1""",
                (who["actor"], row["id"], row["version"]),
            )
            if who.get("scopes") is None or e["space"] in who["scopes"]
        ]
        return result

    def revoke_memory(self, who, memory, version):
        with self.store.tx() as db:
            row = db.execute(
                "SELECT space FROM memories WHERE id=? AND version=?", (memory, version)
            ).fetchone()
            if not row:
                raise Failure(404, "not found")
            require(db, who, row["space"], "review")
            if not who["human"]:
                raise Failure(403, "human confirmation required")
            cancel(db, memory, version)
            # A pending correction of a withdrawn base cannot become a current fact.
            for child in db.execute(
                "SELECT version FROM memories WHERE id=? AND base=? AND state='candidate'",
                (memory, version),
            ).fetchall():
                cancel(db, memory, child["version"])

    def get(self, who, memory, version=None, as_of=None, known_at=None):
        with self.store.tx(False) as db:
            if as_of or known_at:
                if version is not None:
                    raise Failure(422, "specify a version or a historical time")
                effective = iso(as_of or known_at)
                cutoff = iso(known_at or now())
                row = db.execute(
                    """SELECT m.* FROM memories m WHERE id=? AND effective<=?
                    AND state!='revoked' AND (SELECT state FROM transitions t WHERE t.id=m.id
                    AND t.version=m.version AND at<=? ORDER BY at DESC,rowid DESC LIMIT 1)
                    IN ('active','superseded') ORDER BY effective DESC, version DESC LIMIT 1""",
                    (memory, effective, cutoff),
                ).fetchone()
            elif version is None:
                row = db.execute(
                    f"SELECT * FROM memories WHERE id=? AND state IN {CURRENT}",
                    (memory,),
                ).fetchone()
            else:
                row = db.execute(
                    "SELECT * FROM memories WHERE id=? AND version=? AND state!='revoked'",
                    (memory, version),
                ).fetchone()
            if not row:
                raise Failure(404, "not found")
            result = self.detail_row(db, who, row)
            cutoff = iso(known_at or now())
            result["history"] = [
                dict(t)
                for t in db.execute(
                    "SELECT state,at FROM transitions WHERE id=? AND version=? AND at<=? ORDER BY at,rowid",
                    (memory, row["version"], cutoff),
                )
            ]
            if known_at and result["history"]:
                result["state"] = result["history"][-1]["state"]
            return result

    def search(self, who, q):
        with self.store.tx(False) as db:
            require(db, who, q.space)
            terms = tokens(q.query)
            if not terms:
                return []
            match = " OR ".join('"' + t + '"' for t in terms)
            rows = db.execute(
                """SELECT m.* FROM search_index f JOIN memories m ON f.id=m.id AND f.version=m.version
                WHERE search_index MATCH ? AND m.space=? AND m.state='active' ORDER BY bm25(search_index),m.recorded DESC LIMIT ?""",
                (match, q.space, q.limit),
            )
            return [self.detail_row(db, who, r) for r in rows]

    def context(self, who, q):
        result = {"current": [], "pending": []}
        with self.store.tx(False) as db:
            require(db, who, q.space)
            states = (
                ("active", "candidate", "disputed")
                if q.include_pending
                else ("active",)
            )
            marks = ",".join("?" for _ in states)
            rows = db.execute(
                f"""SELECT * FROM memories WHERE space=? AND state IN ({marks})
                ORDER BY (state='active') DESC, effective DESC, recorded DESC""",
                (q.space, *states),
            )
            used = len(packed(result))
            for row in rows:
                item = {
                    k: row[k]
                    for k in ("id", "version", "kind", "claim", "effective", "state")
                }
                item["references"] = [
                    e["source_id"] for e in self.detail_row(db, who, row)["evidence"]
                ]
                cost = len(packed(item)) + 1
                if used + cost <= q.budget:
                    result["current" if row["state"] == "active" else "pending"].append(
                        item
                    )
                    used += cost
        return result

    def reviews(self, who, space, review_id=None):
        with self.store.tx(False) as db:
            require(db, who, space, "review")
            sql = """SELECT r.* FROM reviews r JOIN memories m ON m.id=r.memory AND m.version=r.version
                     WHERE m.space=? AND (r.status='pending' OR (r.status='deferred' AND r.remind<=?))"""
            args = [space, iso(now())]
            if review_id:
                sql += " AND r.id=?"
                args.append(review_id)
            result = []
            for review in db.execute(sql + " ORDER BY r.opened", args):
                row = db.execute(
                    "SELECT * FROM memories WHERE id=? AND version=?",
                    (review["memory"], review["version"]),
                ).fetchone()
                prior = db.execute(
                    "SELECT claim,version,state FROM memories WHERE id=? AND version=? AND state!='revoked'",
                    (row["id"], row["base"]),
                ).fetchone()
                result.append(
                    {
                        **dict(review),
                        "candidate": self.detail_row(db, who, row),
                        "prior": dict(prior) if prior else None,
                    }
                )
            if review_id and not result:
                raise Failure(404, "not found")
            return result

    def decide(self, who, review_id, d: Decision):
        if not who["human"]:
            raise Failure(403, "human confirmation required")
        with self.store.tx() as db:
            r = db.execute("SELECT * FROM reviews WHERE id=?", (review_id,)).fetchone()
            if not r:
                raise Failure(404, "not found")
            row = db.execute(
                "SELECT * FROM memories WHERE id=? AND version=?",
                (r["memory"], r["version"]),
            ).fetchone()
            require(db, who, row["space"], "review")
            if row["state"] not in ("candidate", "disputed"):
                raise Failure(409, "candidate is no longer available")
            if r["generation"] != d.expected_version or r["status"] not in (
                "pending",
                "deferred",
            ):
                raise Failure(409, "review changed; refresh")
            memory, version = row["id"], row["version"]
            if d.action in ("confirm", "edit"):
                old = db.execute(
                    f"SELECT id,version FROM memories WHERE (id=? OR (space=? AND topic=?)) AND state IN {CURRENT}",
                    (memory, row["space"], row["topic"]),
                ).fetchone()
                if old and tuple(old) != (memory, version):
                    if tuple(old) != (memory, row["base"]):
                        raise Failure(
                            409, "current decision changed; rebase before confirming"
                        )
                    transition(db, memory, old["version"], "superseded")
                elif row["base"] and not old:
                    raise Failure(409, "base withdrawn")
                if d.action == "edit" and row["state"] == "disputed":
                    previous = version
                    version = db.execute(
                        "SELECT max(version)+1 FROM memories WHERE id=?", (memory,)
                    ).fetchone()[0]
                    transition(db, memory, previous, "superseded")
                    db.execute(
                        """INSERT INTO memories SELECT id,?,space,kind,claim,subjects,topic,
                                  'candidate',reason,?,effective,?,NULL FROM memories WHERE id=? AND version=?""",
                        (version, previous, iso(now()), memory, previous),
                    )
                    db.execute(
                        "INSERT INTO evidence SELECT id,?,source,quote,offset FROM evidence WHERE id=? AND version=?",
                        (version, memory, previous),
                    )
                    db.execute(
                        "INSERT INTO search_index VALUES (?,?,?)", (memory, version, "")
                    )
                if d.action == "edit":
                    db.execute(
                        "UPDATE memories SET claim=? WHERE id=? AND version=?",
                        (d.claim, memory, version),
                    )
                    db.execute(
                        "UPDATE search_index SET terms=? WHERE id=? AND version=?",
                        (" ".join(tokens(d.claim)), memory, version),
                    )
                has_evidence = db.execute(
                    "SELECT 1 FROM evidence WHERE id=? AND version=?", (memory, version)
                ).fetchone()
                if not has_evidence or d.action == "edit":
                    note = d.claim or row["claim"]
                    sid, instant = uid(), now()
                    db.execute(
                        """INSERT INTO sources(id,space,adapter,external_id,revision,blob,author,occurred,ingested,day,trusted,processed)
                               VALUES (?,?,'human_review',?,'1',?,?,?,?,?,1,'manual')""",
                        (
                            sid,
                            row["space"],
                            review_id,
                            self.store.blob(note),
                            who["actor"],
                            iso(instant),
                            iso(instant),
                            str(instant.astimezone(self.zone).date()),
                        ),
                    )
                    db.execute(
                        "INSERT INTO evidence VALUES (?,?,?,?,0)",
                        (memory, version, sid, note),
                    )
                transition(db, memory, version, "active")
                db.execute(
                    "UPDATE memories SET recheck=NULL WHERE id=? AND version=? AND recheck<=?",
                    (memory, version, iso(now())),
                )
            elif d.action == "reject":
                transition(db, memory, version, "rejected")
            db.execute(
                "UPDATE reviews SET status=?,generation=generation+1,remind=? WHERE id=?",
                (
                    "deferred" if d.action == "defer" else d.action,
                    iso(d.remind_at) if d.remind_at else None,
                    review_id,
                ),
            )
            db.execute(
                "INSERT INTO decisions VALUES (?,?,?,?,?,?,?)",
                (
                    review_id,
                    r["generation"],
                    who["actor"],
                    d.action,
                    iso(now()),
                    row["claim"],
                    d.claim or row["claim"],
                ),
            )
            return {
                "review_id": review_id,
                "action": d.action,
                "memory_id": memory,
                "version": version,
            }
