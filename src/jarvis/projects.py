"""Bounded project snapshots on the existing database and cooperative background worker."""

import asyncio
import hashlib
import json
import re
from datetime import datetime, timezone

from psycopg.types.json import Jsonb

from .commitment_radar import ownership
from .intelligence import PRIORITY_TOPICS
from .spoken_context import requires_action_tools, source

FLAG = "feature.HPOS_PROJECT_BRAINS"
COVERAGE = "Prepared from bounded saved sources. A subject match suggests relevance, not ownership or confirmed project status. Outlook coverage is partial."


def key(name):
    return hashlib.sha256(name.strip().casefold().encode()).hexdigest()[:24]


def matches(project, title):
    for name in [project["name"], *project["aliases"]]:
        pattern = PRIORITY_TOPICS.get(name) or (r"(?<!\w)" + re.escape(name) + r"(?!\w)")
        if re.search(pattern, title, re.I):
            return True
    return False


def suggestions(db, title):
    # Explicit Demo preferences and labelled project subjects, never instructions in message bodies.
    preferences = db.get_state("preferences.work_priorities", {})
    result = [
        (name, "Demo priority topic")
        for name in preferences.get("topics", [])
        if name in PRIORITY_TOPICS and re.search(PRIORITY_TOPICS[name], title, re.I)
    ]
    for match in re.finditer(
        r"\b(?:project|initiative)[: ]+([A-Za-z0-9][A-Za-z0-9 &'/-]{2,55})(?=[:|\[\]]|$)", title, re.I
    ):
        name = match[1].strip(" -/")
        if 1 <= len(name.split()) <= 6 and not re.search(
            r"\b(?:update|status|review|newsletter|unsubscribe|ignore|approve|send|password)\b", name, re.I
        ):
            result.append((name, "Repeated project subject suggestion"))
    return result[:4]


def index_source(db, pending):
    identifier = pending["record_id"]
    row = db.one("SELECT id,kind,title,metadata,deleted,occurred_at FROM records WHERE id=%s", (identifier,))
    active = row and not row["deleted"] and row["kind"] in {"email", "event", "document", "transcript"}
    if active and row["occurred_at"]:
        active = (datetime.now(timezone.utc) - row["occurred_at"]).days <= 90
    old = {
        r["project_id"]
        for r in db.query("SELECT project_id FROM project_sources WHERE record_id=%s", (identifier,))
    }
    if active:
        for name, origin in suggestions(db, row["title"]):
            # Candidate discovery cannot grow an unbounded collection from hostile source subjects.
            db.execute(
                "INSERT INTO project_entities(id,name,origin) SELECT %s,%s,%s WHERE (SELECT count(*) FROM project_entities)<100 ON CONFLICT(id) DO NOTHING",
                (key(name), name, origin),
            )
    projects = db.query(
        "SELECT id,name,aliases FROM project_entities WHERE status<>'archived' ORDER BY created_at LIMIT 100"
    )
    linked = {
        p["id"]: "Subject matches project label" for p in projects if active and matches(p, row["title"])
    }
    if active and row["kind"] in {"document", "transcript"}:
        for link in db.query(
            "SELECT ps.project_id FROM project_sources ps JOIN records r ON r.id=ps.record_id AND NOT r.deleted WHERE ps.record_id=%s",
            (row["metadata"].get("source_id"),),
        ):
            linked[link["project_id"]] = "Attachment linked through its source"
    with db.transaction() as conn:
        conn.execute("DELETE FROM project_sources WHERE record_id=%s", (identifier,))
        for project_id, reason in linked.items():
            conn.execute(
                "INSERT INTO project_sources VALUES(%s,%s,%s) ON CONFLICT DO NOTHING",
                (project_id, identifier, reason),
            )
        affected = list(old | linked.keys())
        conn.execute(
            "UPDATE project_entities SET dirty=true,revision=revision+1 WHERE id=ANY(%s)", (affected,)
        )
        # Requeue explicit children when their parent's attribution changes.
        if old != linked.keys():
            conn.execute(
                "INSERT INTO project_source_dirty(record_id) SELECT id FROM records WHERE metadata->>'source_id'=%s AND kind IN ('document','transcript') AND NOT deleted AND id<>%s LIMIT 50 ON CONFLICT(record_id) DO UPDATE SET revision=project_source_dirty.revision+1,changed_at=now()",
                (identifier, identifier),
            )
        conn.execute(
            "DELETE FROM project_source_dirty WHERE record_id=%s AND revision=%s",
            (identifier, pending["revision"]),
        )


def current_sources(db, identifier, limit=40):
    return db.query(
        """SELECT r.id,r.kind,r.title,left(r.body,1800) AS body,r.metadata,r.occurred_at,ps.reason
        FROM project_sources ps JOIN records r ON r.id=ps.record_id AND NOT r.deleted
        WHERE ps.project_id=%s AND NOT EXISTS(SELECT 1 FROM project_source_dirty d WHERE d.record_id=r.id OR d.record_id=r.metadata->>'source_id')
        AND (r.kind NOT IN ('document','transcript') OR NOT (r.metadata ? 'source_id') OR EXISTS(SELECT 1 FROM records parent WHERE parent.id=r.metadata->>'source_id' AND NOT parent.deleted))
        ORDER BY r.occurred_at DESC NULLS LAST LIMIT %s""",
        (identifier, limit),
    )


def obligations(db, identifier):
    rows = db.query(
        """SELECT c.id,c.owner,c.beneficiary,left(c.text,600) AS text,c.due_at,c.status,c.confidence,c.source_id
        FROM commitments c JOIN project_sources ps ON ps.record_id=c.source_id JOIN records r ON r.id=c.source_id AND NOT r.deleted
        WHERE ps.project_id=%s AND c.status IN ('open','candidate')
        AND NOT EXISTS(SELECT 1 FROM project_source_dirty d WHERE d.record_id=r.id OR d.record_id=r.metadata->>'source_id')
        ORDER BY CASE c.status WHEN 'open' THEN 0 ELSE 1 END,c.due_at NULLS LAST,c.created_at DESC LIMIT 20""",
        (identifier,),
    )
    return [{**r, "ownership": ownership(r)} for r in rows]


def build(db, project):
    rows = current_sources(db, project["id"], 60)
    full = {
        r["metadata"].get("conversationId")
        for r in rows
        if r["metadata"].get("coverage") != "conversation_preview"
    }
    rows = [
        r
        for r in rows
        if r["metadata"].get("coverage") != "conversation_preview"
        or not r["metadata"].get("conversationId")
        or r["metadata"].get("conversationId") not in full
    ]
    sources = [source(r, 650) for r in rows[:16]]
    people = db.query(
        """SELECT p.id,p.name,p.organization,p.vip,count(DISTINCT rp.record_id) interactions
        FROM people p JOIN record_people rp ON rp.person_id=p.id JOIN project_sources ps ON ps.record_id=rp.record_id
        JOIN records r ON r.id=ps.record_id AND NOT r.deleted WHERE ps.project_id=%s
        GROUP BY p.id ORDER BY p.vip DESC,interactions DESC LIMIT 12""",
        (project["id"],),
    )
    threads = len({r["metadata"].get("conversationId") or r["id"] for r in rows})
    eligible = (
        project["status"] == "active"
        or (project["origin"] == "Demo priority topic" and bool(rows))
        or (threads >= 3 and len(people) >= 2)
    )
    snapshot = {
        "eligible": eligible,
        "built_at": datetime.now(timezone.utc).isoformat(),
        "sources": sources if eligible else [],
        "people": people if eligible else [],
        "distinct_sources": threads,
        "coverage": COVERAGE,
        "status_summary": f"{threads} distinct saved sources; {len(people)} linked people. "
        + (
            "Active project confirmed by Demo."
            if project["status"] == "active"
            else "Project status needs confirmation."
        ),
        "recent_changes": [{"id": s["id"], "title": s["title"], "at": s.get("at")} for s in sources[:3]]
        if eligible
        else [],
    }
    db.execute(
        "UPDATE project_entities SET snapshot=%s,dirty=false,updated_at=now() WHERE id=%s AND revision=%s",
        (Jsonb(snapshot), project["id"], project["revision"]),
    )


async def refresh(db, inference, limit=24):
    if not db.get_state(FLAG, False):
        return {"disabled": True}

    def blocked():
        guard = getattr(inference, "workload", None)
        return inference.interactive or (guard and guard.reason(5))

    count = 0
    for row in db.query(
        "SELECT d.* FROM project_source_dirty d LEFT JOIN records r ON r.id=d.record_id ORDER BY r.occurred_at DESC NULLS LAST,d.changed_at,d.record_id LIMIT %s",
        (min(limit, 50),),
    ):
        if blocked():
            break
        await asyncio.to_thread(index_source, db, row)
        count += 1
        await asyncio.sleep(0)
    rebuilt = 0
    for project in db.query(
        "SELECT * FROM project_entities WHERE dirty AND status<>'archived' ORDER BY updated_at LIMIT 3"
    ):
        if blocked():
            break
        await asyncio.to_thread(build, db, project)
        rebuilt += 1
    if count or rebuilt:
        db.audit("projects.refreshed", details={"sources_checked": count, "snapshots": rebuilt})
    return {"sources_checked": count, "snapshots": rebuilt}


def listing(db):
    if not db.get_state(FLAG, False):
        return {"enabled": False}
    rows = db.query(
        "SELECT id,name,status,origin,revision,dirty AS stale,updated_at,snapshot FROM project_entities WHERE status<>'archived' AND (status='active' OR snapshot->>'eligible'='true') ORDER BY status,name LIMIT 50"
    )
    return {
        "enabled": True,
        "projects": [
            {
                **{k: v for k, v in r.items() if k != "snapshot"},
                "source_count": r["snapshot"].get("distinct_sources", 0),
                "people_count": len(r["snapshot"].get("people", [])),
            }
            for r in rows
        ],
        "pending_sources": db.one("SELECT count(*) n FROM project_source_dirty")["n"],
        "coverage": COVERAGE,
    }


def detail(db, identifier):
    if not db.get_state(FLAG, False):
        return {"enabled": False}
    project = db.one("SELECT * FROM project_entities WHERE id=%s AND status<>'archived'", (identifier,))
    if not project:
        raise LookupError("Project not found")
    current = {r["id"]: source(r, 650) for r in current_sources(db, identifier, 60)}
    snap = project["snapshot"]
    sources = [s for s in snap.get("sources", []) if current.get(s["id"]) == s]
    decisions = []
    if db.get_state("feature.HPOS_MEETING_INTELLIGENCE", False):
        from .source_insights import read as read_insights

        for s in [s for s in sources if s["kind"] in {"document", "transcript"}][:3]:
            try:
                decisions.extend(read_insights(db, s["id"])["decisions"][:3])
            except LookupError:
                continue
    rows = obligations(db, identifier)
    people = db.query(
        "SELECT DISTINCT p.id,p.name,p.organization,p.vip FROM people p JOIN record_people rp ON rp.person_id=p.id WHERE rp.record_id=ANY(%s) ORDER BY p.vip DESC,p.name LIMIT 12",
        (list(current),),
    )
    now = datetime.now(timezone.utc)
    risks = [
        {
            "text": r["text"],
            "source_id": r["source_id"],
            "reason": "Confirmed obligation is overdue; check whether completed.",
        }
        for r in rows
        if r["status"] == "open"
        and r["ownership"] in {"mine", "theirs"}
        and r["due_at"]
        and r["due_at"] < now
    ]
    return {
        "enabled": True,
        "id": identifier,
        "name": project["name"],
        "status": project["status"],
        "revision": project["revision"],
        "stale": project["dirty"],
        "built_at": snap.get("built_at"),
        "sources": sources,
        "people": people,
        "commitments": rows,
        "risks": risks,
        "coverage": COVERAGE,
        "status_summary": snap.get("status_summary", "Context is being prepared."),
        "next_actions": (
            ["Confirm whether this is an active project."] if project["status"] == "candidate" else []
        )
        + (["Review overdue confirmed obligations."] if risks else [])
        + ["Review recent evidence before making an external commitment."],
        "open_questions": ["What is the current desired outcome and who owns it?"],
        "decisions": decisions[:6],
        "recent_changes": [{"id": s["id"], "title": s["title"], "at": s.get("at")} for s in sources[:3]],
    }


def review(db, identifier, expected_revision, status, actor):
    if not db.get_state(FLAG, False):
        raise ValueError("Project brains are disabled")
    if status not in {"active", "archived"}:
        raise ValueError("Unknown project status")
    with db.transaction() as conn:
        row = conn.execute(
            "UPDATE project_entities SET status=%s,dirty=true,revision=revision+1 WHERE id=%s AND revision=%s RETURNING id",
            (status, identifier, expected_revision),
        ).fetchone()
        if not row:
            raise ValueError("Project changed. Refresh before reviewing.")
        db.audit(
            "project.reviewed", identifier, {"status": status}, actor=actor, source="dashboard", conn=conn
        )
    return {"status": status}


def normalized(text):
    for pattern, replacement in (
        (r"\bl\s+h\s+a\b", "LHA"),
        (r"\bel\s+ha(?:y)?\b", "LHA"),
        (r"\bz\s+g\s+x\b", "ZGX"),
    ):
        text = re.sub(pattern, replacement, text, flags=re.I)
    return text


def intent(text, db=None):
    relevant = not requires_action_tools(text) and bool(
        re.search(
            r"\b(?:state of|status of|project update|project brief|what(?:'s| is) happening (?:with|on)|brief me on)\b",
            text,
            re.I,
        )
    )
    if not relevant or db is None:
        return relevant
    if re.search(r"\bproject\b", text, re.I):
        return True
    return any(
        matches(p, normalized(text))
        for p in db.query("SELECT name,aliases FROM project_entities WHERE status<>'archived' LIMIT 100")
    )


def query(db, text):
    text = normalized(text)
    rows = db.query(
        "SELECT id,name,aliases FROM project_entities WHERE status<>'archived' ORDER BY length(name) DESC LIMIT 100"
    )
    found = [p for p in rows if matches(p, text)]
    if len(found) != 1:
        options = ", ".join(p["name"] for p in (found or rows)[:3])
        return {
            "reply": "Which project do you mean? "
            + (
                options + ", or something else?" if options else "I haven't prepared any project context yet."
            ),
            "sources": [],
        }
    data = detail(db, found[0]["id"])
    sources = data["sources"][:6]
    if not sources:
        return {
            "reply": "That project context is being prepared or its sources changed. Saved mail remains searchable.",
            "sources": [],
        }
    payload = {
        k: data[k]
        for k in (
            "name",
            "status",
            "stale",
            "built_at",
            "status_summary",
            "next_actions",
            "open_questions",
            "coverage",
        )
    }
    payload["people"] = data["people"][:6]
    payload["commitments"] = data["commitments"][:5]
    payload["extracted_decision_candidates"] = [
        d for d in data["decisions"] if d["source_id"] in {s["id"] for s in sources}
    ]
    payload["evidence"] = [
        {"source": i + 1, **{k: v for k, v in s.items() if k != "id"}} for i, s in enumerate(sources)
    ]
    return {"context": json.loads(json.dumps(payload, default=str)), "sources": sources}
