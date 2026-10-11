"""Bounded neighborhoods over existing PostgreSQL relationships; no inferred tool authority."""

import hashlib
import math
from datetime import datetime, timezone

from .commitment_radar import own_names
from .config import settings
from .source_insights import read as source_notes

FLAG = "feature.HPOS_MEMORY_GRAPH"
ROOT = "actor:demo"
MAX_EXPANSIONS = 9


def node(db, identifier):
    if identifier == ROOT:
        return {"id": ROOT, "type": "person", "name": "Demo User", "status": "owner", "metadata": {}}
    return db.one("SELECT * FROM memory_graph_nodes WHERE id=%s", (identifier,))


def weighted(edge, now):
    stamp = edge.get("activity_at")
    days = max(0, (now - stamp).total_seconds() / 86400) if stamp else None
    recency = round(20 * math.exp(-days / 30), 2) if days is not None else 0
    factors = {"relationship": edge["base_weight"], "recency": recency}
    return {**edge, "weight": round(sum(factors.values()), 2), "weight_factors": factors}


def edge(source, target, relationship, provenance, status, confidence=None, weight=70):
    return {
        "source": source,
        "target": target,
        "relationship": relationship,
        "provenance": provenance,
        "status": status,
        "confidence": confidence,
        "base_weight": weight,
        "activity_at": None,
    }


def neighbors(db, selected, limit):
    identifier = selected["id"]
    rows = db.query(
        """SELECT * FROM memory_graph_edges WHERE source=%s OR target=%s
        ORDER BY base_weight DESC,activity_at DESC NULLS LAST,source,target,relationship LIMIT %s""",
        (identifier, identifier, limit + 1),
    )
    derived = {}
    if identifier.startswith("commitment:"):
        # Names from source text do not imply ownership. Only a unique saved identity may link.
        for field, relation in (("owner", "OWED_BY"), ("beneficiary", "OWED_TO")):
            name = selected["metadata"].get(field, "").strip()
            if name.lower() in own_names():
                target = ROOT
            else:
                people = (
                    db.query(
                        "SELECT id FROM people WHERE lower(id)=%s OR lower(trim(name))=%s LIMIT 2",
                        (name.lower(), name.lower()),
                    )
                    if name
                    else []
                )
                target = "person:" + people[0]["id"] if len(people) == 1 else None
            if target:
                rows.append(
                    edge(
                        identifier,
                        target,
                        relation,
                        selected["metadata"]["source_id"],
                        selected["status"],
                        selected["confidence"],
                    )
                )
    if identifier == ROOT or identifier.startswith("person:"):
        names = list(own_names()) if identifier == ROOT else [identifier.removeprefix("person:")]
        if identifier != ROOT:
            same = db.query(
                "SELECT id FROM people WHERE lower(trim(name))=%s LIMIT 2",
                (selected["name"].strip().lower(),),
            )
            if len(same) == 1:
                names.append(selected["name"].strip().lower())
        for row in db.query(
            """SELECT c.id,c.owner,c.beneficiary,c.source_id,c.status,c.confidence FROM commitments c
            WHERE c.status IN ('open','candidate') AND (lower(trim(c.owner))=ANY(%s) OR lower(trim(c.beneficiary))=ANY(%s))
            ORDER BY CASE WHEN c.status='open' THEN 0 ELSE 1 END,c.due_at NULLS LAST,c.created_at DESC LIMIT 8""",
            (names, names),
        ):
            for field, relation in (("owner", "OWED_BY"), ("beneficiary", "OWED_TO")):
                if row[field].strip().lower() in names:
                    rows.append(
                        edge(
                            "commitment:" + str(row["id"]),
                            identifier,
                            relation,
                            row["source_id"],
                            row["status"],
                            row["confidence"],
                        )
                    )
    if identifier == ROOT or identifier == "person:" + settings.outlook_browser_account.lower():
        rows.append(
            edge(
                ROOT,
                "person:" + settings.outlook_browser_account.lower(),
                "ACCOUNT_IDENTITY",
                "configured account",
                "configured identity",
                1.0,
            )
        )
    if identifier.startswith("record:") and selected["type"] in {"document", "transcript"}:
        notes = source_notes(db, selected["metadata"]["source_id"])
        for decision in notes["decisions"][:4]:
            text = decision["text"]
            key = "decision:" + hashlib.sha256((identifier + "\0" + text).encode()).hexdigest()[:32]
            derived[key] = {
                "id": key,
                "type": "decision",
                "name": text[:160],
                "summary": text[:400],
                "status": "extracted candidate",
                "expandable": False,
                "metadata": {"source_id": selected["metadata"]["source_id"]},
            }
            rows.append(
                edge(
                    key,
                    identifier,
                    "EXTRACTED_FROM",
                    selected["metadata"]["source_id"],
                    "extracted candidate",
                    weight=60,
                )
            )
    return rows, derived


def neighborhood(db, identifier=ROOT, *, limit=45, depth=2):
    if not db.get_state(FLAG, False):
        return {"enabled": False, "nodes": [], "edges": []}
    if not 10 <= limit <= 75 or depth not in (1, 2) or len(identifier) > 4096:
        raise ValueError("Neighborhood requires 10–75 nodes and depth 1–2")
    start = node(db, identifier)
    if not start:
        raise LookupError("This memory entity is no longer available")
    now = datetime.now(timezone.utc)
    nodes, edges, frontier = {identifier: start}, {}, [identifier]
    expanded = set()
    truncated = False
    for level in range(depth):
        next_frontier = []
        for current in frontier:
            if len(expanded) == MAX_EXPANSIONS:
                truncated = True
                break
            expanded.add(current)
            rows, derived = neighbors(db, nodes[current], min(24, limit))
            rows = sorted(
                (weighted(e, now) for e in rows), key=lambda e: (-e["weight"], e["source"], e["target"])
            )
            for e in rows:
                other = e["target"] if e["source"] == current else e["source"]
                if other not in nodes:
                    if len(nodes) >= limit:
                        truncated = True
                        continue
                    found = derived.get(other) or node(db, other)
                    if not found:
                        continue
                    nodes[other] = found
                    if not other.startswith("decision:"):
                        next_frontier.append(other)
                key = hashlib.sha256(
                    (e["source"] + "\0" + e["relationship"] + "\0" + e["target"]).encode()
                ).hexdigest()[:32]
                edges[key] = {"id": key, **e}
            if len(rows) >= min(24, limit) + 1:
                truncated = True
        frontier = next_frontier
    return {
        "enabled": True,
        "visualization_enabled": bool(db.get_state("feature.HPOS_GRAPH_VISUALIZATION", False)),
        "root": identifier,
        "nodes": list(nodes.values()),
        "edges": list(edges.values()),
        "depth": depth,
        "limit": limit,
        "bounded": True,
        "truncated": truncated,
        "expanded_nodes": len(expanded),
        "as_of": now.isoformat(),
        "meaning": "External memory and source relationships, not model weights or hidden reasoning. Candidates remain unconfirmed; invitations do not prove attendance.",
        "coverage": "Partial saved Outlook and memory evidence; an absent edge does not establish an absent relationship.",
    }


async def lookup(db, memory, query, limit=10):
    """Reuse hybrid SQL/pgvector/reranking; never load a graph during normal conversation."""
    if not db.get_state(FLAG, False):
        return {"enabled": False, "nodes": []}
    if not query.strip() or len(query) > 500 or not 1 <= limit <= 20:
        raise ValueError("Enter a query of 1–500 characters")
    found = []
    literal = "%" + query.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    named = db.query(
        "SELECT 'person:'||id id FROM people WHERE name ILIKE %s ORDER BY vip DESC,name LIMIT 6",
        (literal,),
    ) + db.query(
        "SELECT 'project:'||id id FROM project_entities WHERE status<>'archived' AND name ILIKE %s ORDER BY name LIMIT 6",
        (literal,),
    )
    for item in named:
        row = node(db, item["id"])
        if row:
            found.append(row)
    for item in await memory.search(query, limit=limit):
        row = node(db, "memory:" + str(item["id"]))
        if row:
            found.append(row)
    return {
        "enabled": True,
        "nodes": found[:limit],
        "method": "structured names and existing hybrid memory retrieval",
        "coverage": "Saved memories with valid provenance only",
    }


def response_evidence(db, message_id):
    """Read only the source set saved with this assistant response; never expand neighbors."""
    if not db.get_state("feature.HPOS_RESPONSE_EVIDENCE", False):
        return {"enabled": False, "nodes": [], "edges": []}
    message = db.one(
        "SELECT id,metadata,created_at FROM messages WHERE id=%s AND role='assistant' AND conversation='demo'",
        (message_id,),
    )
    if not message:
        raise LookupError("Saved assistant response not found")
    root = "response:" + str(message_id)
    nodes = {
        root: {
            "id": root,
            "type": "response",
            "name": "This answer",
            "status": "saved response",
            "summary": "Links identify sources selected for this answer, not hidden reasoning or proof that every statement is correct.",
            "metadata": {},
            "expandable": False,
            "activity_at": message["created_at"],
        }
    }
    sources = message["metadata"].get("sources", [])
    if not isinstance(sources, list):
        sources = []
    missing = 0
    edges = []
    for index, source in enumerate(sources[:24]):
        if not isinstance(source, dict) or not isinstance(source.get("id"), str):
            missing += 1
            continue
        kind = source.get("kind")
        prefix = (
            "commitment:"
            if kind == "commitment"
            else "memory:"
            if kind in {"memory", "semantic", "episodic", "preference", "procedural"}
            else "record:"
        )
        identifier = prefix + source["id"]
        current = node(db, identifier)
        if not current:
            missing += 1
            continue
        # Revalidate existence/provenance before displaying even an old saved excerpt.
        # Excerpts are historical response context; relationships below are current.
        nodes[identifier] = {
            **current,
            "name": source.get("title") or current["name"],
            "summary": (source.get("excerpt") or "")[:700],
            "expandable": False,
            "metadata": {
                **current.get("metadata", {}),
                "selected_for_response": True,
                "response_source_number": index + 1,
            },
        }
        edges.append(
            {
                **edge(root, identifier, "SELECTED_FOR_RESPONSE", "", "saved retrieval", weight=100),
                "id": f"{root}:{index}",
                "weight": 100,
            }
        )
    identifiers = [identifier for identifier in nodes if identifier != root]
    # Only actual saved-source nodes can enter this view. No people/projects inferred from a title.
    if identifiers:
        linked = db.query(
            "SELECT * FROM memory_graph_edges WHERE source=ANY(%s) AND target=ANY(%s) ORDER BY source,target,relationship LIMIT 100",
            (identifiers, identifiers),
        )
        for item in linked:
            edges.append(
                {
                    **weighted(item, datetime.now(timezone.utc)),
                    "id": hashlib.sha256(
                        (item["source"] + item["relationship"] + item["target"]).encode()
                    ).hexdigest()[:24],
                }
            )
    return {
        "enabled": True,
        "evidence_only": True,
        "message_id": message_id,
        "root": root,
        "nodes": list(nodes.values()),
        "edges": edges,
        "limit": 25,
        "bounded": True,
        "truncated": len(sources) > 24,
        "unavailable_sources": missing,
        "saved_source_count": len(sources),
        "as_of": message["created_at"],
        "meaning": "Sources selected for this saved answer. This is evidence provenance, not hidden chain of thought, model weights or authorization.",
        "coverage": f"{len(nodes) - 1} available saved sources; {missing} unavailable or no longer valid. Excerpts reflect response time; source-to-source links reflect current saved relationships. Other uncited context may not appear. Deleted or forgotten sources are excluded.",
    }
