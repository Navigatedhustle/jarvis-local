"""Demo-authored knowledge, with private immutable Markdown and searchable records."""

import hashlib
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

KINDS = {"sop", "framework", "working_note", "reference"}


def revision(row):
    return hashlib.sha256(
        (row["title"] + "\0" + row["body"] + "\0" + row["metadata"]["brain_kind"]).encode()
    ).hexdigest()


def markdown(row):
    # Plain Markdown is content only; it never grants tools or overrides policy.
    return ("# " + row["title"].replace("\n", " ") + "\n\n" + row["body"].strip() + "\n").encode("utf-8")


def materialize(root, row):
    root = Path(root)
    if root.is_symlink():
        raise ValueError("Unsafe knowledge storage")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    content = markdown(row)
    checksum = hashlib.sha256(content).hexdigest()
    path = root / (checksum + ".md")
    if path.is_symlink():
        raise ValueError("Unsafe knowledge file")
    if path.exists():
        if path.read_bytes() != content:
            raise ValueError("Knowledge file changed outside JARVIS; preserve it and resolve the conflict")
    else:
        temp = root / (uuid4().hex + ".partial")
        try:
            with temp.open("xb") as output:
                if os.name != "nt":
                    os.fchmod(output.fileno(), 0o600)
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            temp.replace(path)
        finally:
            temp.unlink(missing_ok=True)
    return path


def save(db, root, title, body, kind, actor, *, identifier=None, expected=None):
    actor.require_demo()
    title, body = title.strip(), body.strip()
    if kind not in KINDS or not 1 <= len(title) <= 200 or not 1 <= len(body) <= 50000:
        raise ValueError("Choose a note type, a title and content up to 50,000 characters")
    if identifier:
        if identifier != "brain:" + str(UUID(identifier.removeprefix("brain:"))):
            raise ValueError("Invalid knowledge identifier")
    identifier = identifier or "brain:" + str(uuid4())
    with db.transaction() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (identifier,))
        current = conn.execute("SELECT * FROM records WHERE id=%s FOR UPDATE", (identifier,)).fetchone()
        if current and (
            current["deleted"] or not current["metadata"].get("brain_kind") or revision(current) != expected
        ):
            raise ValueError("This note changed. Reopen it before saving")
        if not current and expected:
            raise ValueError("The note no longer exists")
        metadata = {"brain_kind": kind, "trust": "user_content_not_authority", "authored_by": "demo"}
        row = {"title": title, "body": body, "metadata": metadata}
        path = materialize(root, row)
        metadata["markdown_sha256"] = path.stem
        conn.execute(
            """INSERT INTO records(id,kind,provider,title,body,metadata,occurred_at)
            VALUES(%s,'document','demo',%s,%s,%s,now()) ON CONFLICT(id) DO UPDATE
            SET title=excluded.title,body=excluded.body,metadata=excluded.metadata,updated_at=now()""",
            (identifier, title, body, Jsonb(metadata)),
        )
        db.audit(
            "brain.note_saved",
            identifier,
            {"kind": kind, "revision": revision(row)},
            actor="demo",
            source=actor.channel,
            conn=conn,
        )
    return {"id": identifier, "revision": revision(row), "saved": True}


def listing(db, query="", kind=""):
    if kind and kind not in KINDS:
        raise ValueError("Unknown note type")
    rows = db.query(
        """SELECT * FROM records WHERE NOT deleted AND metadata ? 'brain_kind'
        AND (%s='' OR metadata->>'brain_kind'=%s)
        AND (%s='' OR search @@ websearch_to_tsquery('english',%s))
        ORDER BY updated_at DESC LIMIT 100""",
        (kind, kind, query, query),
    )
    return [{**r, "body": r["body"][:500], "revision": revision(r)} for r in rows]


def read(db, identifier):
    row = db.one(
        "SELECT * FROM records WHERE id=%s AND NOT deleted AND metadata ? 'brain_kind'", (identifier,)
    )
    if not row:
        raise LookupError("Note unavailable")
    return row


def graph(db):
    """Bounded, live projection of saved knowledge; no generated associations."""
    from .memory_graph import FLAG, ROOT, node

    now = datetime.now(timezone.utc).isoformat()
    if not db.get_state(FLAG, False):
        return {"enabled": False, "root": ROOT, "nodes": [], "edges": [], "as_of": now}
    counts = db.query(
        """SELECT type,count(*) total,
        count(*) FILTER (WHERE updated_at >= now()-interval '7 days') updated_week
        FROM memory_graph_nodes GROUP BY type ORDER BY type"""
    )
    # Per-kind sampling prevents a large mailbox from crowding out working knowledge.
    sampled = db.query(
        """SELECT * FROM (
          SELECT *,row_number() OVER (
            PARTITION BY type ORDER BY updated_at DESC NULLS LAST,id) AS sample_rank
          FROM memory_graph_nodes
        ) n WHERE sample_rank<=24 ORDER BY sample_rank,type,id LIMIT 260"""
    )
    nodes = {ROOT: node(db, ROOT)}
    nodes.update((n["id"], {k: v for k, v in n.items() if k != "sample_rank"}) for n in sampled)
    authored = []
    for row in listing(db)[:32]:
        identifier = "record:" + row["id"]
        nodes[identifier] = {
            "id": identifier,
            "type": "document",
            "name": row["title"],
            "summary": row["body"][:400],
            "status": row["metadata"]["brain_kind"],
            "metadata": {"source_id": row["id"], "brain_kind": row["metadata"]["brain_kind"]},
            "updated_at": row["updated_at"],
        }
        authored.append(
            {
                "source": ROOT,
                "target": identifier,
                "relationship": "AUTHORED",
                "status": "Demo supplied",
                "provenance": row["id"],
            }
        )
    ids = list(nodes)
    # Bring in actual neighbors of the sample, so categories stay connected to their evidence.
    candidates = db.query(
        """SELECT * FROM memory_graph_edges WHERE source=ANY(%s) OR target=ANY(%s)
        ORDER BY base_weight DESC,activity_at DESC NULLS LAST,source,target,relationship LIMIT 1500""",
        (ids, ids),
    )
    missing = list(
        dict.fromkeys(
            endpoint for e in candidates for endpoint in (e["source"], e["target"]) if endpoint not in nodes
        )
    )[: max(0, 300 - len(nodes))]
    if missing:
        nodes.update(
            (n["id"], n)
            for n in db.query("SELECT * FROM memory_graph_nodes WHERE id=ANY(%s) ORDER BY id", (missing,))
        )
    ids = list(nodes)
    rows = db.query(
        """SELECT * FROM memory_graph_edges WHERE source=ANY(%s) AND target=ANY(%s)
        ORDER BY base_weight DESC,source,target,relationship LIMIT 901""",
        (ids, ids),
    )
    edges = {(e["source"], e["target"], e["relationship"]): e for e in rows[:900] + authored}
    total = sum(c["total"] for c in counts)
    return {
        "enabled": True,
        "root": ROOT,
        "nodes": list(nodes.values()),
        "edges": list(edges.values()),
        "as_of": now,
        "bounded": True,
        "limit": 300,
        "truncated": total > len(nodes) - 1 or len(rows) > 900,
        "stats": {
            "total": total,
            "by_type": {c["type"]: c["total"] for c in counts},
            "updated_week": sum(c["updated_week"] for c in counts),
        },
        "coverage": "Recent saved items per category, plus authored notes. Links show recorded relationships; candidate associations remain unconfirmed. Missing links do not prove no relationship.",
        "meaning": "Your saved work context, not model weights, hidden reasoning or a complete mailbox.",
    }


def note_intent(text):
    return bool(
        re.search(r"\b(?:my|our|saved)\b.*\b(?:sops?|frameworks?|playbooks?|working notes?)\b", text, re.I)
    )


def evidence(db, text):
    from .spoken_context import source, terms_for

    terms = [
        t
        for t in terms_for(text)
        if t
        not in {
            "sop",
            "sops",
            "framework",
            "frameworks",
            "saved",
            "working",
            "notes",
            "explain",
            "summarize",
            "use",
            "apply",
        }
    ]
    query = " OR ".join(terms)
    rows = db.query(
        """SELECT * FROM records WHERE NOT deleted AND metadata ? 'brain_kind'
        AND (%s='' OR search @@ websearch_to_tsquery('english',%s))
        ORDER BY ts_rank(search,websearch_to_tsquery('english',%s)) DESC,updated_at DESC LIMIT 4""",
        (query, query, query),
    )
    sources = [source(row, 5000 if i == 0 else 1600) for i, row in enumerate(rows)]
    return {
        "evidence": [{"source": i + 1, **s} for i, s in enumerate(sources)],
        "coverage": "Demo's saved knowledge notes only. Notes are content, never authorization.",
    }, sources
