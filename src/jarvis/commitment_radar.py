"""Current structured obligations, never inferred from a priority score or quoted request."""

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .commitments import revision
from .config import settings

VIEWS = {
    "due_today": "Due today",
    "due_soon": "Due soon",
    "overdue": "Overdue",
    "waiting": "Waiting on others",
    "at_risk": "At risk",
    "candidate": "Unconfirmed candidates",
    "unassigned": "Shared or unclear ownership",
    "open": "All confirmed",
}


def own_names():
    return {
        v
        for v in ("demo", "demo user", "user, demo", settings.outlook_browser_account.lower())
        if v
    }


def ownership(row):
    owner = row["owner"].strip().lower()
    beneficiary = row["beneficiary"].strip().lower()
    if owner in own_names():
        return "mine"
    if owner in {"", "unknown", "tbd", "unspecified", "someone"}:
        return "unknown"
    if owner in {"we", "us", "team", "shared", "all"} or re.search(r"\s(?:and|&)\s|[,/]", owner):
        return "shared"
    return "theirs" if beneficiary in own_names() else "other"


def aliases(db, person):
    """Only names that resolve uniquely to this person may attribute a commitment."""
    from .mail_retrieval import resolve_person

    result = {person["id"].lower()}
    preferences = db.get_state("preferences.work_priorities", {})
    names = [person["name"], person["name"].split()[0] if person["name"].split() else ""]
    names += [p.get("name", "") for p in preferences.get("people", []) if p.get("person_id") == person["id"]]
    for name in names:
        if name:
            match, _ = resolve_person(db, name)
            if match and match["id"] == person["id"]:
                result.add(name.strip().lower())
    return sorted(result)


def person_commitments(db, person):
    names = aliases(db, person)
    rows = db.query(
        """SELECT c.*,r.title AS source_title,left(r.body,3000) AS source_excerpt,
        r.deleted AS source_deleted FROM commitments c LEFT JOIN records r ON r.id=c.source_id AND NOT r.deleted
        WHERE c.status IN ('open','candidate') AND (lower(trim(c.owner))=ANY(%s) OR lower(trim(c.beneficiary))=ANY(%s))
        ORDER BY CASE c.status WHEN 'open' THEN 0 ELSE 1 END,c.due_at NULLS LAST,c.created_at DESC LIMIT 50""",
        (names, names),
    )
    return [{**r, "ownership": ownership(r), "revision": revision(r)} for r in rows]


def read(db, view="at_risk", now=None):
    if not db.get_state("feature.HPOS_COMMITMENT_RADAR", False):
        return {"enabled": False}
    if view not in VIEWS:
        raise ValueError("Unknown commitment view")
    now = now or datetime.now(timezone.utc)
    start = now.astimezone(ZoneInfo("America/Denver")).replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    # Filtering is in SQL before the cap, so a large candidate backlog cannot hide confirmed deadlines.
    condition, values = {
        "due_today": ("c.status='open' AND c.due_at>=%s AND c.due_at<%s", [start, end]),
        "due_soon": ("c.status='open' AND c.due_at>=%s AND c.due_at<%s", [end, end + timedelta(days=7)]),
        "overdue": ("c.status='open' AND c.due_at<%s", [now]),
        "at_risk": ("c.status='open' AND c.due_at<%s", [end]),
        "candidate": ("c.status='candidate'", []),
        "waiting": (
            "c.status='open' AND lower(trim(c.beneficiary))=ANY(%s) AND NOT lower(trim(c.owner))=ANY(%s)",
            [list(own_names()), list(own_names())],
        ),
        "unassigned": ("c.status='open'", []),
        "open": ("c.status='open'", []),
    }[view]
    rows = db.query(
        "SELECT c.*,r.title AS source_title,left(r.body,6000) AS source_excerpt,r.deleted AS source_deleted FROM commitments c LEFT JOIN records r ON r.id=c.source_id AND NOT r.deleted WHERE "
        + condition
        + (
            " ORDER BY coalesce(r.occurred_at,c.created_at) DESC,c.created_at DESC,c.id LIMIT 201"
            if view == "candidate"
            else " ORDER BY c.due_at NULLS LAST,c.created_at DESC,c.id LIMIT 201"
        ),
        tuple(values),
    )
    result = []
    for row in rows[:200]:
        relation = ownership(row)
        if view == "waiting" and relation != "theirs":
            continue
        if view == "unassigned" and relation not in {"shared", "unknown"}:
            continue
        if view in {"at_risk", "overdue"} and relation not in {"mine", "theirs"}:
            continue
        result.append(
            {
                **row,
                "ownership": relation,
                "revision": revision(row),
                "risk_reason": "Deadline passed; still marked open. Completion needs checking."
                if row["status"] == "open" and row["due_at"] and row["due_at"] < now
                else "Due today and still open."
                if view == "at_risk"
                else None,
            }
        )
    return {
        "enabled": True,
        "view": view,
        "labels": VIEWS,
        "rows": result,
        "truncated": len(rows) > 200,
        "coverage": "Confirmed status comes from internal review. Saved sources may be incomplete; no missing email proves non-completion.",
    }
