"""Bounded meeting evidence over existing relationship and project records."""

import re
from datetime import datetime, timezone

from .commitment_radar import person_commitments
from .config import settings
from .intelligence import PRIORITY_TOPICS
from .projects import matches
from .source_insights import read as read_insights
from .spoken_context import source

FLAG = "feature.HPOS_MEETING_INTELLIGENCE"


def importance(db, event):
    metadata = event["metadata"]
    if metadata.get("isCancelled") or metadata.get("isAllDay") or metadata.get("showAs") == "free":
        return []
    topics = db.get_state("preferences.work_priorities", {}).get("topics", [])
    reasons = [
        "Priority topic: " + t
        for t in topics
        if t in PRIORITY_TOPICS
        and re.search(r"\bsandbox\b" if t == "Sandbox events" else PRIORITY_TOPICS[t], event["title"], re.I)
    ]
    addresses = [
        a.get("emailAddress", {}).get("address", "").lower() for a in metadata.get("attendees", [])[:50]
    ]
    addresses = [a for a in addresses if a and a != settings.outlook_browser_account.lower()]
    if db.one("SELECT id FROM people WHERE vip AND id=ANY(%s) LIMIT 1", (addresses,)):
        reasons.append("Confirmed VIP attendee")
    if metadata.get("importance") == "high":
        reasons.append("Marked high importance in Outlook")
    return reasons


def context(db, event):
    attendees = event["metadata"].get("attendees", [])[:50]
    addresses = list(dict.fromkeys(a.get("emailAddress", {}).get("address", "").lower() for a in attendees))
    addresses = [a for a in addresses if a and a != settings.outlook_browser_account.lower()]
    people = db.query(
        """SELECT p.id,p.name,p.vip,p.profile,
        (SELECT max(r.occurred_at) FROM record_people rp JOIN records r ON r.id=rp.record_id
         WHERE rp.person_id=p.id AND NOT r.deleted AND r.kind IN ('email','event') AND r.occurred_at<=now()) last_interaction
        FROM people p WHERE id=ANY(%s) ORDER BY p.vip DESC,p.name LIMIT 8""",
        (addresses,),
    )
    projects = []
    if db.get_state("feature.HPOS_PROJECT_BRAINS", False):
        projects = [
            p
            for p in db.query(
                "SELECT id,name,aliases,status FROM project_entities WHERE status<>'archived' LIMIT 100"
            )
            if matches(p, event["title"])
        ][:3]
    project_ids = [p["id"] for p in projects]
    stop = {
        "meeting",
        "weekly",
        "monthly",
        "team",
        "update",
        "call",
        "with",
        "sync",
        "biweekly",
        "discussion",
        "planning",
    }
    terms = [t for t in re.findall(r"[a-z0-9]+", event["title"].lower()) if len(t) >= 3 and t not in stop][:5]
    query = " OR ".join(terms)
    emails = db.query(
        """SELECT r.id,r.kind,r.title,CASE WHEN length(r.body)>1800 THEN left(r.body,900)||E'\n[excerpt omitted]\n'||right(r.body,900) ELSE r.body END body,r.metadata,r.occurred_at,
        (CASE WHEN EXISTS(SELECT 1 FROM project_sources ps WHERE ps.record_id=r.id AND ps.project_id=ANY(%s) AND NOT EXISTS(SELECT 1 FROM project_source_dirty d WHERE d.record_id=ps.record_id)) THEN 3 ELSE 0 END +
         CASE WHEN lower(r.metadata->'from'->'emailAddress'->>'address')=ANY(%s) THEN 2 ELSE 0 END +
         CASE WHEN %s<>'' AND r.search @@ websearch_to_tsquery('english',%s) THEN 2 ELSE 0 END) relevance
        FROM records r WHERE r.kind='email' AND NOT r.deleted AND r.occurred_at>now()-interval '90 days'
        AND (lower(r.metadata->'from'->'emailAddress'->>'address')=ANY(%s) OR
             (%s<>'' AND r.search @@ websearch_to_tsquery('english',%s)) OR
             EXISTS(SELECT 1 FROM project_sources ps WHERE ps.record_id=r.id AND ps.project_id=ANY(%s) AND NOT EXISTS(SELECT 1 FROM project_source_dirty d WHERE d.record_id=ps.record_id)))
        ORDER BY relevance DESC,r.occurred_at DESC LIMIT 18""",
        (project_ids, addresses, query, query, addresses, query, query, project_ids),
    )
    full = {
        r["metadata"].get("conversationId")
        for r in emails
        if r["metadata"].get("coverage") != "conversation_preview"
    }
    emails = [
        r
        for r in emails
        if r["metadata"].get("coverage") != "conversation_preview"
        or not r["metadata"].get("conversationId")
        or r["metadata"].get("conversationId") not in full
    ][:6]
    commitments = {}
    for person in people[:6]:
        for row in person_commitments(db, person):
            if row["status"] == "open":
                commitments[str(row["id"])] = row
    source_ids = [r["id"] for r in emails]
    for row in db.query(
        """SELECT c.* FROM commitments c JOIN records r ON r.id=c.source_id AND NOT r.deleted
        WHERE c.status='open' AND (c.source_id=ANY(%s) OR EXISTS(SELECT 1 FROM project_sources ps WHERE ps.record_id=c.source_id AND ps.project_id=ANY(%s) AND NOT EXISTS(SELECT 1 FROM project_source_dirty d WHERE d.record_id=ps.record_id)))
        ORDER BY c.due_at NULLS LAST LIMIT 8""",
        (source_ids + [event["id"]], project_ids),
    ):
        commitments[str(row["id"])] = row
    documents = db.query(
        """SELECT r.id,r.kind,r.title,CASE WHEN length(r.body)>1800 THEN left(r.body,900)||E'\n[excerpt omitted]\n'||right(r.body,900) ELSE r.body END body,r.metadata,r.occurred_at
        FROM records r WHERE r.kind IN ('document','transcript') AND NOT r.deleted AND r.body<>''
        AND (r.metadata->>'source_id'=ANY(%s) OR EXISTS(SELECT 1 FROM project_sources ps WHERE ps.record_id=r.id AND ps.project_id=ANY(%s) AND NOT EXISTS(SELECT 1 FROM project_source_dirty d WHERE d.record_id=ps.record_id)))
        AND (NOT (r.metadata ? 'source_id') OR EXISTS(SELECT 1 FROM records parent WHERE parent.id=r.metadata->>'source_id' AND NOT parent.deleted))
        ORDER BY r.occurred_at DESC NULLS LAST LIMIT 3""",
        (source_ids + [event["id"]], project_ids),
    )
    decisions = []
    for row in documents:
        try:
            decisions.extend(read_insights(db, row["id"])["decisions"][:3])
        except LookupError:
            continue
    return {
        "event": event,
        "people": people,
        "emails": emails,
        "documents": documents,
        "commitments": list(commitments.values())[:8],
        "projects": projects,
        "decisions": decisions[:6],
        "importance_factors": importance(db, event),
        "last_sync": db.get_state("outlook.last_sync"),
        "outlook_error": db.get_state("outlook.error"),
    }


def evidence(context):
    event = context["event"]
    rows = [event, *context["emails"], *context["documents"]]
    sources = [source(r, 900 if i == 0 else 650) for i, r in enumerate(rows)]
    for row in context["commitments"][:5]:
        sources.append(
            {
                "id": row["source_id"],
                "kind": "commitment",
                "title": row["text"][:300],
                "excerpt": row["text"][:600],
                "owner": row["owner"],
                "beneficiary": row["beneficiary"],
                "at": row["due_at"].isoformat() if row["due_at"] else None,
                "status": "open",
            }
        )
    return {
        "evidence": [
            {"source": i + 1, **{k: v for k, v in s.items() if k != "id"}} for i, s in enumerate(sources)
        ],
        "people": context["people"],
        "projects": context["projects"],
        "extracted_decision_candidates": context.get("decisions", []),
        "importance_factors": context["importance_factors"],
        "coverage": "Saved evidence only. Attendee lists and mail coverage may be partial; suggested topics are not confirmed agenda items. An invitation is not proof a conversation occurred.",
        "outlook_last_sync": context["last_sync"],
        "outlook_error": bool(context["outlook_error"]),
        "now": datetime.now(timezone.utc).isoformat(),
    }, sources
