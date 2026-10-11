"""Incremental person snapshots over address-bound evidence; no model in profile maintenance."""

import asyncio
import json
import math
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from psycopg.types.json import Jsonb

from .commitment_radar import person_commitments
from .intelligence import PRIORITY_TOPICS
from .mail_retrieval import resolve_person, search_mail
from .spoken_context import requires_action_tools, source

FLAG = "feature.HPOS_RELATIONSHIP_GRAPH"


def brief(text, words=24):
    parts = text.split()
    return " ".join(parts[:words]) + ("…" if len(parts) > words else "")


def intent(text):
    if requires_action_tools(text):
        return None
    patterns = {
        "owe": r"\bwhat (?:do i|does .+?) owe\b|\bwhat am i waiting (?:on|for) from\b",
        "last": r"\bwhen did i last (?:talk|speak|interact)|\blast interaction with\b",
        "history": r"\b(?:history|relationship) with\b|\bwhat did .+? ask me\b",
        "followup": r"\bwho haven['’]?t i followed up with\b|\bwho needs (?:a )?follow.up\b",
    }
    return next((kind for kind, pattern in patterns.items() if re.search(pattern, text, re.I)), None)


def person_text(text):
    for pattern in (
        r"what do i owe (.+)",
        r"what does (.+?) owe (?:me|demo)",
        r"waiting (?:on|for) from (.+)",
        r"(?:history|relationship|last interaction) with (.+)",
        r"when did i last (?:talk|speak|interact) (?:to|with) (.+)",
        r"what did (.+?) ask me.*",
    ):
        match = re.search(pattern, text, re.I)
        if match:
            return match[1].strip(" .?!")
    return text


def weight(snapshot, now=None):
    now = now or datetime.now(timezone.utc)
    last = snapshot.get("last_interaction")
    age = max(0, (now - datetime.fromisoformat(last)).total_seconds() / 86400) if last else 365
    factors = {**snapshot.get("weight_factors", {}), "recency": round(20 * math.exp(-age / 30), 1)}
    return {
        "score": round(min(100, sum(factors.values())), 1),
        "factors": factors,
        "meaning": "Attention weighting from saved evidence, not a judgment about personal trust.",
    }


def build_person(db, identifier, now=None):
    now = now or datetime.now(timezone.utc)
    pending = db.one("SELECT revision FROM relationship_dirty WHERE person_id=%s", (identifier,))
    person = db.one("SELECT * FROM people WHERE id=%s", (identifier,))
    if not person:
        db.execute("DELETE FROM relationship_dirty WHERE person_id=%s", (identifier,))
        return False
    rows = db.query(
        """SELECT r.id,r.kind,r.title,left(r.body,1800) AS body,r.metadata,r.occurred_at
        FROM records r WHERE NOT r.deleted AND r.kind IN ('email','event','document','transcript')
        AND (r.occurred_at IS NULL OR (r.occurred_at>=%s AND r.occurred_at<%s))
        AND EXISTS (SELECT 1 FROM record_people rp JOIN records origin ON origin.id=rp.record_id AND NOT origin.deleted WHERE rp.person_id=%s AND (rp.record_id=r.id OR (r.kind IN ('document','transcript') AND rp.record_id=r.metadata->>'source_id')))
        ORDER BY r.occurred_at DESC NULLS LAST LIMIT 120""",
        (now - timedelta(days=90), now + timedelta(days=30), identifier),
    )
    full = {
        r["metadata"].get("conversationId")
        for r in rows
        if r["kind"] == "email" and r["metadata"].get("coverage") != "conversation_preview"
    }
    rows = [
        r
        for r in rows
        if r["metadata"].get("coverage") != "conversation_preview"
        or not r["metadata"].get("conversationId")
        or r["metadata"].get("conversationId") not in full
    ]
    mail = [r for r in rows if r["kind"] == "email"]
    events = [r for r in rows if r["kind"] == "event" and not r["metadata"].get("isCancelled")]
    incoming = [
        r
        for r in mail
        if r["metadata"].get("from", {}).get("emailAddress", {}).get("address", "").lower() == identifier
    ]
    outgoing = [r for r in mail if r["metadata"].get("folder_role") == "sentitems"]
    distinct = len({r["metadata"].get("conversationId") or r["id"] for r in mail})
    commitments = person_commitments(db, person)
    confirmed = [r for r in commitments if r["status"] == "open"]
    automation = bool(
        re.search(r"(?:no.?reply|notification|newsletter|mailer|smartsheet|donotreply)", identifier, re.I)
    )
    bulk = bool(incoming) and all("unsubscribe" in r["body"].lower() for r in incoming[:5])
    eligible = bool(
        person["vip"]
        or confirmed
        or (not automation and not bulk and ((distinct >= 3 and incoming) or outgoing or len(events) >= 2))
    )
    last_rows = [
        r for r in rows if r["kind"] in {"email", "event"} and r["occurred_at"] and r["occurred_at"] <= now
    ]
    last = max((r["occurred_at"] for r in last_rows), default=None)
    preferences = db.get_state("preferences.work_priorities", {})
    topics = [
        t
        for t in preferences.get("topics", [])
        if t in PRIORITY_TOPICS and any(re.search(PRIORITY_TOPICS[t], r["title"], re.I) for r in rows)
    ]
    snapshot = {
        "person_id": identifier,
        "name": person["name"],
        "organization": person["organization"],
        "relationship": person["profile"].get("demo_relationship"),
        "title": person["profile"].get("title"),
        "vip": person["vip"],
        "eligible": eligible,
        "built_at": now.isoformat(),
        "last_interaction": last.isoformat() if last else None,
        "last_interaction_kind": max(last_rows, key=lambda r: r["occurred_at"])["kind"]
        if last_rows
        else None,
        "topics": topics,
        "distinct_threads": distinct,
        "sent_context_count": len(outgoing),
        "sources": [
            source(r, 650)
            for r in (
                last_rows[:12]
                + [r for r in events if r["occurred_at"] > now][:6]
                + [r for r in rows if r["kind"] in {"document", "transcript"}][:2]
            )
        ],
        "weight_factors": {
            "explicit_vip": 30 if person["vip"] else 0,
            "frequency": round(min(20, 5 * math.log2(1 + distinct)), 1),
            "open_commitments": min(20, len(confirmed) * 5),
            "upcoming_meeting": 12
            if any(now < r["occurred_at"] < now + timedelta(days=7) for r in events)
            else 0,
            "priority_topics": min(8, len(topics) * 2),
        },
        "coverage": "Bounded saved correspondence and calendar invitations. An invitation does not prove a meeting occurred. Quoted names never establish participation.",
    }
    if not eligible:
        # Keep only the eligibility decision for incidental/bulk senders, not a rich profile.
        snapshot = {
            "person_id": identifier,
            "name": person["name"],
            "eligible": False,
            "built_at": now.isoformat(),
            "last_interaction": None,
            "sources": [],
            "topics": [],
            "weight_factors": {},
            "coverage": "No rich profile prepared: insufficient relevant interaction. Saved mail remains searchable.",
        }
    rev = pending["revision"] if pending else 0
    with db.transaction() as conn:
        conn.execute(
            """INSERT INTO relationship_profiles(person_id,snapshot,source_revision,eligible) VALUES (%s,%s,%s,%s)
            ON CONFLICT(person_id) DO UPDATE SET snapshot=excluded.snapshot,source_revision=excluded.source_revision,eligible=excluded.eligible,updated_at=now()""",
            (identifier, Jsonb(snapshot), rev, eligible),
        )
        conn.execute("DELETE FROM relationship_dirty WHERE person_id=%s AND revision=%s", (identifier, rev))
    return eligible


async def refresh(db, inference, limit=8):
    if not db.get_state(FLAG, False):
        return {"disabled": True}
    changed = 0
    for row in db.query(
        "SELECT d.person_id FROM relationship_dirty d LEFT JOIN people p ON p.id=d.person_id ORDER BY p.vip DESC NULLS LAST,d.changed_at LIMIT %s",
        (min(limit, 20),),
    ):
        guard = getattr(inference, "workload", None)
        if inference.interactive or (guard and guard.reason(5)):
            break
        await asyncio.to_thread(build_person, db, row["person_id"])
        changed += 1
        await asyncio.sleep(0)
    if changed:
        db.audit("relationships.refreshed", details={"profiles_checked": changed})
    return {"profiles_checked": changed}


def listing(db):
    if not db.get_state(FLAG, False):
        return {"enabled": False}
    rows = db.query("""SELECT p.id,p.name,p.organization,p.vip,r.snapshot,r.updated_at,
        (d.person_id IS NOT NULL) AS stale FROM relationship_profiles r JOIN people p ON p.id=r.person_id
        LEFT JOIN relationship_dirty d ON d.person_id=p.id WHERE r.eligible
        ORDER BY p.vip DESC,r.snapshot->>'last_interaction' DESC NULLS LAST LIMIT 100""")
    profiles = [
        {
            **{k: v for k, v in r.items() if k != "snapshot"},
            "topics": r["snapshot"]["topics"],
            "last_interaction": r["snapshot"]["last_interaction"],
            "weight": weight(r["snapshot"]),
        }
        for r in rows
    ]
    return {
        "enabled": True,
        "profiles": sorted(profiles, key=lambda r: r["weight"]["score"], reverse=True),
        "pending": db.one("SELECT count(*) AS n FROM relationship_dirty")["n"],
        "coverage": "Profiles require explicit importance or repeated relevant interaction. Other saved senders remain searchable.",
    }


def detail(db, identifier):
    if not db.get_state(FLAG, False):
        return {"enabled": False}
    row = db.one(
        "SELECT p.*,r.snapshot,(d.person_id IS NOT NULL) AS stale FROM people p LEFT JOIN relationship_profiles r ON r.person_id=p.id LEFT JOIN relationship_dirty d ON d.person_id=p.id WHERE p.id=%s",
        (identifier,),
    )
    if not row:
        raise LookupError("Person not found")
    snap = row.get("snapshot")
    if not snap:
        return {
            "enabled": True,
            "ready": False,
            "name": row["name"],
            "message": "This person's context is waiting for background preparation.",
        }
    ids = [s["id"] for s in snap["sources"]]
    current = {
        r["id"]: source(r, 650)
        for r in db.query(
            "SELECT r.id,r.kind,r.title,left(r.body,1800) AS body,r.metadata,r.occurred_at FROM records r WHERE r.id=ANY(%s) AND NOT r.deleted AND EXISTS(SELECT 1 FROM record_people rp JOIN records origin ON origin.id=rp.record_id AND NOT origin.deleted WHERE rp.person_id=%s AND (rp.record_id=r.id OR (r.kind IN ('document','transcript') AND rp.record_id=r.metadata->>'source_id')))",
            (ids, identifier),
        )
    }
    snap["sources"] = [s for s in snap["sources"] if current.get(s["id"]) == s]
    return {
        **snap,
        "name": row["name"],
        "vip": row["vip"],
        "enabled": True,
        "ready": True,
        "stale": row["stale"],
        "weight": weight(snap),
        "commitments": person_commitments(db, row),
    }


def query(db, text, conversation):
    """Return minimal pre-retrieved context or an honest deterministic clarification."""
    kind = intent(text)
    if not kind or not db.get_state(FLAG, False):
        return None
    if kind == "followup":
        from .commitment_radar import read

        data = read(db, "overdue")
        if not data.get("enabled"):
            return {
                "reply": "Commitment radar is not enabled. You can still review saved commitments in Tasks.",
                "sources": [],
            }
        rows = [r for r in data.get("rows", []) if r["ownership"] in {"mine", "theirs"}][:5]
        sources = [
            {
                "id": r["source_id"],
                "kind": "commitment",
                "title": r["text"],
                "excerpt": r["risk_reason"],
                "owner": r["owner"],
                "at": r["due_at"].isoformat() if r["due_at"] else None,
            }
            for r in rows
        ]
        reply = (
            "These confirmed commitments need follow-up: "
            + "; ".join(r["owner"] + " → " + r["beneficiary"] + ": " + brief(r["text"]) for r in rows)
            if rows
            else "I haven't identified an overdue confirmed commitment in the saved records. Unreviewed candidates may still need attention."
        )
        return {"reply": reply + " Saved coverage is partial; completion needs checking.", "sources": sources}
    person, ambiguous = resolve_person(db, person_text(text))
    if not person:
        return {
            "reply": "Which person do you mean? "
            + (
                "; ".join(p["name"] for p in ambiguous)
                if ambiguous
                else "Use their full name or email address."
            ),
            "sources": [],
        }
    if kind == "owe":
        rows = person_commitments(db, person)
        confirmed = [r for r in rows if r["status"] == "open" and r["ownership"] in {"mine", "theirs"}]
        if re.search(r"\bi owe|do i owe\b", text, re.I):
            confirmed = [r for r in confirmed if r["ownership"] == "mine"]
        else:
            confirmed = [r for r in confirmed if r["ownership"] == "theirs"]
        chosen = confirmed[:3]
        reply = "; ".join(
            brief(r["text"])
            + (
                " (due " + r["due_at"].astimezone(ZoneInfo("America/Denver")).strftime("%b %d, %Y") + ")"
                if r["due_at"]
                else " (no confirmed deadline)"
            )
            for r in chosen
        )
        if not chosen:
            reply = "No confirmed matching obligation is recorded for " + person["name"] + "."
        candidates = sum(r["status"] == "candidate" for r in rows)
        if candidates:
            reply += f" This view includes {candidates} unconfirmed candidates to review."
        result = {
            "reply": reply + " Saved coverage is partial; open status isn't proof the work is unfinished.",
            "sources": [
                {
                    "id": r["source_id"],
                    "kind": "commitment",
                    "title": r["text"],
                    "excerpt": r["text"],
                    "at": r["due_at"].isoformat() if r["due_at"] else None,
                }
                for r in chosen
            ],
        }
    else:
        data = detail(db, person["id"])
        if not data.get("ready"):
            return {"reply": data["message"], "sources": []}
        if not data.get("eligible"):
            # An explicit question can read a few address-filtered messages without maintaining a rich profile.
            data["sources"] = [
                source(r, 650) for r in search_mail(db, "emails from " + person["id"], limit=6)["records"]
            ]
        sources = data["sources"][:6]
        if re.search(r"ask me", text, re.I):
            sources = [
                s
                for s in data["sources"]
                if s["kind"] == "email" and (s.get("sender_address") or "").lower() == person["id"]
            ][:6]
        if kind == "last":
            dates = [
                s
                for s in sources
                if s.get("at") and datetime.fromisoformat(s["at"]) <= datetime.now(timezone.utc)
            ]
            last = max(dates, key=lambda s: s["at"]) if dates else None
            reply = (
                (
                    "The latest saved "
                    + last["kind"]
                    + " with "
                    + person["name"]
                    + " is dated "
                    + datetime.fromisoformat(last["at"]).strftime("%B %d")
                    + ". An email or calendar invitation does not prove a conversation took place."
                )
                if last
                else "No recent interaction is available in this bounded saved profile."
            )
            result = {"reply": reply + " Saved coverage is partial.", "sources": [last] if last else []}
        else:
            # One existing read-only primary call can summarize this bounded evidence.
            payload = {
                "person": {
                    k: data.get(k)
                    for k in (
                        "person_id",
                        "name",
                        "organization",
                        "relationship",
                        "topics",
                        "built_at",
                        "stale",
                    )
                },
                "evidence": [
                    {"source": i + 1, **{k: v for k, v in row.items() if k != "id"}}
                    for i, row in enumerate(sources)
                ],
                "commitments": [
                    {
                        **{k: r.get(k) for k in ("owner", "beneficiary", "status", "due_at")},
                        "text": r["text"][:500],
                    }
                    for r in data["commitments"][:5]
                ],
                "coverage": data["coverage"],
                "now": datetime.now(timezone.utc).isoformat(),
            }
            return {"context": json.loads(json.dumps(payload, default=str)), "sources": sources}
    db.set_state("conversation.last_briefing_sources." + conversation, result["sources"])
    return result
