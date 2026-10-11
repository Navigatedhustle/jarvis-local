"""A bounded, source-backed work queue. No LLM or full-mailbox scan in the request path."""

import json
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import priorities
from .intelligence import PRIORITY_TOPICS, addressed_to_someone_else, calendar_conflicts, context_today
from .mail_retrieval import mail_intent

ZONE = ZoneInfo("America/Denver")
CATEGORIES = {
    "today": "Today",
    "waiting_on_me": "Waiting on me",
    "waiting_on_them": "Waiting on them",
    "at_risk": "At risk",
    "upcoming": "Upcoming",
    "prepared": "Prepared by JARVIS",
}


def enabled(db):
    return db.get_state("feature.HPOS_COMMAND_CENTER", False) is True


def intent(text):
    value = text.lower().strip(" .?!")
    if mail_intent(text):
        return None
    if re.search(r"\b(?:send|draft|approve|deny|forget|remember|cancel|delete|create)\b", value):
        return None
    patterns = [
        ("waiting_on_them", r"what (?:am i|are we) waiting on|waiting on (?:them|others)"),
        ("waiting_on_me", r"waiting on me|what do i owe$"),
        ("at_risk", r"at risk|promises.*(?:breaking|danger)|what am i missing"),
        ("prepared", r"what (?:have you|has jarvis) prepared"),
        ("upcoming", r"prepare tonight|prepare for tomorrow"),
        ("can_wait", r"what can wait"),
        (
            "today",
            r"what (?:actually )?needs (?:my )?attention|what matters today|what needs me|give me my briefing",
        ),
    ]
    for category, pattern in patterns:
        if re.search(pattern, value):
            return category
    return None


def revision(db):
    return db.one("SELECT revision FROM hpos_revisions WHERE name='command'")["revision"]


def build(db, now=None):
    """Called by the background queue only; confirmed promises remain distinct from candidates."""
    now = now or datetime.now(timezone.utc)
    rev = revision(db)
    work = context_today(db)
    start = now.astimezone(ZONE).replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow = start + timedelta(days=1)
    items = {key: [] for key in CATEGORIES}

    def add(category, row, kind, reason, priority=5, confidence="confirmed", **extra):
        date = row.get("due_at") or row.get("occurred_at")
        item = {
            "id": str(row["id"]),
            "kind": kind,
            "title": row.get("text") or row.get("title") or row.get("kind", "Work item"),
            "source_id": row.get("source_id") or str(row["id"]),
            "due_at": date.isoformat() if date else None,
            "owner": row.get("owner"),
            "beneficiary": row.get("beneficiary"),
            "status": row.get("status"),
            "reason": reason,
            "priority": priority,
            "confidence": confidence,
            **extra,
        }
        items[category].append(item)

    commitments = db.query(
        "SELECT * FROM commitments WHERE status='open' ORDER BY due_at NULLS LAST,created_at LIMIT 201"
    )
    own = {"demo", "demo user"}
    from .config import settings

    own.add(settings.outlook_browser_account.lower())
    for row in commitments[:200]:
        owner = row["owner"].strip().lower()
        beneficiary = row["beneficiary"].strip().lower()
        due = row["due_at"]
        mine = owner in own
        theirs = beneficiary in own and not mine and owner not in {"", "unknown", "shared", "team", "tbd"}
        if mine:
            add("waiting_on_me", row, "commitment", "Confirmed commitment, still marked open.", 2)
        elif theirs:
            add("waiting_on_them", row, "commitment", "Confirmed commitment to Demo, still marked open.", 3)
        if due and due < tomorrow and (mine or theirs):
            overdue = due < now
            add(
                "at_risk",
                row,
                "commitment",
                "Overdue and still marked open; completion needs checking."
                if overdue
                else "Due today and still marked open.",
                0 if overdue else 1,
            )
            if overdue:
                add("today", row, "commitment", "Overdue and still marked open; verify completion.", 0)
        if due and start <= due < tomorrow:
            add("today", row, "commitment", "Confirmed deadline today.", 1)
        elif due and tomorrow <= due < start + timedelta(days=8):
            add("upcoming", row, "commitment", "Confirmed deadline in the coming week.", 3)
    for row in work["emails"]:
        meta = row["metadata"]
        if not row["occurred_at"] or row["occurred_at"] < now - timedelta(days=14):
            continue
        factors = meta.get("triage", {}).get("factors", [])[:4]
        current_topic = any(
            re.search(pattern, row["title"], re.I)
            for topic, pattern in PRIORITY_TOPICS.items()
            if topic in work["work_priorities"].get("topics", [])
        )
        vip = any("confirmed VIP" in factor for factor in factors)
        routine_report = bool(
            re.search(
                r"\b(unbooked backlog|sales report|sales forecast report|pipeline report)\b",
                row["title"],
                re.I,
            )
        )
        if routine_report and not current_topic and not vip:
            continue
        actionable = meta.get("triage", {}).get("actionable") is True and not addressed_to_someone_else(
            row.get("excerpt", "")
        )
        add(
            "today",
            row,
            "email",
            "Review candidate: "
            + ("; ".join(factors) or "Relevant saved thread")
            + ". This is not a confirmed obligation.",
            2 if vip and actionable else 3 if current_topic else 5 if actionable else 6,
            "candidate",
            person=meta.get("from", {}).get("emailAddress", {}),
            coverage=meta.get("coverage"),
            actionable=actionable,
        )
        if actionable:
            add(
                "waiting_on_me",
                row,
                "email",
                "Contains a request; verify Demo owns the response.",
                5,
                "candidate",
                person=meta.get("from", {}).get("emailAddress", {}),
            )
    events = db.query(
        "SELECT id,title,metadata,occurred_at FROM records WHERE kind='event' AND NOT deleted AND occurred_at>=%s AND occurred_at<%s AND coalesce(metadata->>'isCancelled','false')<>'true' AND coalesce(metadata->>'showAs','')<>'free' AND coalesce(metadata->'responseStatus'->>'response','')<>'declined' AND coalesce(metadata->>'isAllDay','false')<>'true' ORDER BY occurred_at LIMIT 100",
        (now, start + timedelta(days=8)),
    )
    for row in events:
        category = "today" if row["occurred_at"] < tomorrow else "upcoming"
        add(
            category,
            row,
            "event",
            "Saved Outlook calendar item.",
            4,
            coverage=row["metadata"].get("coverage"),
        )
    by_id = {r["id"]: r for r in events}
    for conflict in calendar_conflicts(events):
        # Existing conflict contract retains both source IDs.
        for identifier in conflict.get("event_ids", []):
            if identifier in by_id and by_id[identifier]["occurred_at"] < now + timedelta(hours=48):
                add("at_risk", by_id[identifier], "event", "Overlaps another saved calendar event.", 1)
    for row in db.query(
        "SELECT id,title,metadata,occurred_at FROM records WHERE kind='draft' AND NOT deleted AND coalesce(metadata->>'status','internal') NOT IN ('sent','discarded') ORDER BY updated_at DESC LIMIT 20"
    ):
        add(
            "prepared",
            {**row, "source_id": row["metadata"].get("source_id")},
            "draft",
            "Internal draft ready for review; not sent.",
            3,
        )
    for row in work["approvals"]:
        if row["expires_at"] > now:
            for category in ["waiting_on_me", "today"]:
                add(
                    category,
                    {**row, "title": "Review " + row["kind"], "due_at": row["expires_at"]},
                    "approval",
                    "Your explicit decision is required.",
                    1,
                )
    for key, rows in items.items():
        unique = {r["id"]: r for r in rows}

        def order(row):
            date = datetime.fromisoformat(row["due_at"]).timestamp() if row["due_at"] else float("inf")
            return row["priority"], -date if row["kind"] == "email" else date, row["id"]

        items[key] = sorted(unique.values(), key=order)[:30]
    result = {
        "schema_version": 4,
        "built_at": now.isoformat(),
        "source_revision": rev,
        "categories": items,
        "counts": {k: len(v) for k, v in items.items()},
        "labels": CATEGORIES,
        "category_order": list(CATEGORIES),
        "coverage": "Saved Outlook coverage is partial. Review candidates are not confirmed promises.",
        "commitments_truncated": len(commitments) > 200,
        "candidate_commitments": db.one("SELECT count(*) AS n FROM commitments WHERE status='candidate'")[
            "n"
        ],
        "outlook_last_sync": work["outlook_sync"],
        "outlook_error": bool(work["outlook_error"]),
        "outlook_enabled": work["outlook_enabled"],
    }
    result = json.loads(json.dumps(result, default=str))
    db.set_state("hpos.command.snapshot", result)
    priorities.prepare(db, result, now)
    db.audit("hpos.command.prepared", details={"counts": result["counts"], "source_revision": rev})
    return result


def read(db, now=None):
    if not enabled(db):
        return {"enabled": False}
    now = now or datetime.now(timezone.utc)
    saved = db.get_state("hpos.command.snapshot")
    if not saved:
        return {
            "enabled": True,
            "ready": False,
            "stale": True,
            "message": "Your work summary is being prepared.",
        }
    stale = (
        saved.get("schema_version") != 4
        or revision(db) != saved["source_revision"]
        or (now - datetime.fromisoformat(saved["built_at"])).total_seconds() > 180
    )
    # Deleted sources and resolved work must not survive in a stale response.
    all_items = [r for rows in saved["categories"].values() for r in rows]
    record_ids = [r["id"] for r in all_items if r["kind"] in {"email", "event", "draft"}]
    active_records = {
        r["id"]
        for r in db.query(
            "SELECT id FROM records WHERE id=ANY(%s) AND NOT deleted AND updated_at<=%s AND (kind<>'event' OR coalesce(metadata->>'isCancelled','false')<>'true')",
            (record_ids, saved["built_at"]),
        )
    }
    commitment_ids = [r["id"] for r in all_items if r["kind"] == "commitment"]
    current_commitments = {
        str(r["id"]): r
        for r in db.query(
            "SELECT id,owner,beneficiary,text,due_at FROM commitments WHERE id=ANY(%s::uuid[]) AND status='open'",
            (commitment_ids,),
        )
    }
    active_commitments = set()
    for item in all_items:
        row = current_commitments.get(item["id"])
        if (
            row
            and row["owner"] == item["owner"]
            and row["beneficiary"] == item["beneficiary"]
            and row["text"] == item["title"]
            and (row["due_at"].isoformat() if row["due_at"] else None) == item["due_at"]
        ):
            active_commitments.add(item["id"])
    approval_ids = [r["id"] for r in all_items if r["kind"] == "approval"]
    active_approvals = {
        str(r["id"])
        for r in db.query(
            "SELECT id FROM approvals WHERE id=ANY(%s::uuid[]) AND status IN ('pending','approved','uncertain') AND expires_at>%s",
            (approval_ids, now),
        )
    }
    allowed = active_records | active_commitments | active_approvals
    saved["categories"] = {
        k: [r for r in rows if r["id"] in allowed] for k, rows in saved["categories"].items()
    }
    saved["counts"] = {k: len(rows) for k, rows in saved["categories"].items()}
    # Connection health is live even when the work snapshot has not changed.
    last_sync = db.get_state("outlook.last_sync")
    return {
        **saved,
        "enabled": True,
        "ready": True,
        "stale": stale,
        "outlook_last_sync": last_sync,
        "outlook_error": bool(db.get_state("outlook.error")),
        "outlook_stale": not last_sync or (now - datetime.fromisoformat(last_sync)).total_seconds() > 900,
        "outlook_enabled": db.get_state("setup.enabled.outlook", True),
        "priorities": priorities.read(db, saved, now),
    }


def answer(db, text, conversation):
    key = intent(text)
    data = read(db)
    if not data.get("ready"):
        return {
            "reply": "Your work summary is being prepared. Saved email and calendar searches are still available.",
            "sources": [],
        }
    if key == "can_wait":
        rows = [
            r for r in data["categories"]["today"] if r["confidence"] == "candidate" and r["priority"] >= 6
        ]
        opening = "These are lower-priority review candidates."
    else:
        rows = data["categories"].get(key, [])
        if key == "at_risk" and re.search(r"\b(promises?|commitments?)\b", text, re.I):
            rows = [r for r in rows if r["kind"] == "commitment"]
        opening = {
            "today": "Here's what deserves your attention.",
            "waiting_on_me": "Here's what's waiting on you.",
            "waiting_on_them": "Here's what you're waiting on.",
            "at_risk": "These items need a risk check.",
            "upcoming": "Here's what to prepare for.",
            "prepared": "Here's what I've prepared.",
        }.get(key, "Your saved work summary.")
    selected = rows[:3]
    lines = []
    sources = []
    for index, row in enumerate(selected):
        words = row["title"].split()
        title = " ".join(words[:10]) + ("…" if len(words) > 10 else "")
        label = "Review candidate" if row["confidence"] == "candidate" else "Confirmed"
        reason = "" if row["confidence"] == "candidate" else row["reason"]
        lines.append(f"{label}: {title}. {reason} [{index + 1}]")
        sources.append(
            {
                "id": row["source_id"],
                "kind": row["kind"],
                "title": row["title"],
                "excerpt": row["reason"],
                "at": row["due_at"],
                "confidence": row["confidence"],
            }
        )
    if not selected:
        lines = [
            "No matching confirmed work is identified in the current saved summary. This does not prove there is nothing outstanding."
        ]
    caveat = "Saved coverage is partial."
    if any(row["confidence"] == "candidate" for row in selected):
        caveat += " Review candidates need an ownership check before becoming your tasks."
    if data["stale"]:
        caveat += " This summary awaits refresh; check sources before acting."
    if data["outlook_error"] or data["outlook_stale"] or not data["outlook_enabled"]:
        caveat += " Outlook needs attention."
    result = {"reply": " ".join([opening, *lines, caveat]), "sources": sources}
    db.set_state("conversation.last_briefing_sources." + conversation, sources)
    return result
