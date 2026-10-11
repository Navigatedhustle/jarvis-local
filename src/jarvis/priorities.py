"""Six-hour priority brief from the existing source-backed command snapshot."""

from datetime import datetime, timedelta, timezone

KEY = "hpos.priorities.snapshot"
INTERVAL = timedelta(hours=6)


def due(db, now=None):
    now = now or datetime.now(timezone.utc)
    value = db.get_state(KEY, {})
    return not value or value.get("version") != 2 or now >= datetime.fromisoformat(value["next_refresh"])


def select(categories, now):
    owned = {r["id"] for r in categories.get("waiting_on_me", []) if r["kind"] == "commitment"}
    candidates = {}
    for group in ("at_risk", "today", "waiting_on_me", "upcoming"):
        for row in categories.get(group, []):
            kind = row["kind"]
            when = datetime.fromisoformat(row["due_at"]) if row.get("due_at") else None
            if kind not in {"email", "event", "commitment"}:
                continue
            if kind == "commitment" and row["id"] not in owned:
                continue
            if kind == "event" and (not when or not now <= when < now + timedelta(hours=48)):
                continue
            if kind == "email" and row["priority"] >= 6:
                continue
            priority = row["priority"]
            if kind == "event" and when < now + timedelta(hours=3):
                priority = min(priority, 1)
            action = (
                "Prepare for"
                if kind == "event"
                else "Review and respond"
                if kind == "email"
                else "Follow through"
            )
            if kind == "email" and not row.get("actionable"):
                action = "Review"
            item = {**row, "action": action, "rank_priority": priority}
            previous = candidates.get(row["id"])
            if not previous or priority < previous["rank_priority"]:
                candidates[row["id"]] = item

    def order(row):
        stamp = datetime.fromisoformat(row["due_at"]).timestamp() if row.get("due_at") else float("inf")
        return row["rank_priority"], -stamp if row["kind"] == "email" else stamp, row["id"]

    conflicts = [
        candidates[r["id"]]
        for r in categories.get("at_risk", [])
        if r["kind"] == "event" and r["id"] in candidates
    ]
    if len(conflicts) > 1:
        conflicts.sort(key=order)
        for item in conflicts:
            candidates.pop(item["id"], None)
        first = conflicts[0]
        candidates[first["id"]] = {
            **first,
            "action": "Resolve calendar overlaps",
            "display_title": f"Check {len(conflicts)} overlapping meetings",
            "reason": "Review the conflicting meetings before deciding which to attend. Calendar changes still need your approval.",
            "conflict_sources": [
                {k: r.get(k) for k in ("id", "title", "source_id", "due_at")} for r in conflicts
            ],
        }

    selected, seen = [], set()
    for row in sorted(candidates.values(), key=order):
        source = row.get("source_id") or row["id"]
        if source not in seen:
            selected.append(row)
            seen.add(source)
        if len(selected) == 5:
            break
    return selected


def prepare(db, command, now):
    if not due(db, now):
        return
    value = {
        "version": 2,
        "built_at": now.isoformat(),
        "next_refresh": (now + INTERVAL).isoformat(),
        "items": select(command["categories"], now),
        "outlook_last_sync": command["outlook_last_sync"],
    }
    db.set_state(KEY, value)
    db.audit(
        "hpos.priorities.prepared",
        details={"count": len(value["items"]), "next_refresh": value["next_refresh"]},
    )


def read(db, command, now):
    saved = db.get_state(KEY, {})
    if not saved:
        return {"ready": False, "items": [], "stale": True}
    # Never keep deleted, changed, completed, reassigned or past-calendar work
    # merely to fill five slots. Replacements are selected on the next refresh.
    current = {r["id"]: r for rows in command["categories"].values() for r in rows}
    owned = {r["id"] for r in command["categories"].get("waiting_on_me", [])}
    answered = {
        r["id"]
        for r in db.query(
            """SELECT r.id FROM records r WHERE r.id=ANY(%s) AND EXISTS (
        SELECT 1 FROM records sent WHERE sent.kind='email' AND NOT sent.deleted
        AND sent.metadata->>'folder_role'='sentitems'
        AND sent.metadata->>'conversationId'=r.metadata->>'conversationId'
        AND sent.occurred_at>r.occurred_at)""",
            ([r["id"] for r in saved["items"] if r["kind"] == "email"],),
        )
    }
    items = []
    for item in saved["items"]:
        if item["id"] in answered:
            continue
        row = current.get(item["id"])
        if not row or any(
            row.get(k) != item.get(k) for k in ("title", "due_at", "source_id", "owner", "beneficiary")
        ):
            continue
        if item["kind"] == "commitment" and item["id"] not in owned:
            continue
        if item["kind"] == "event" and datetime.fromisoformat(item["due_at"]) < now:
            continue
        if any(
            not current.get(s["id"])
            or any(current[s["id"]].get(k) != s.get(k) for k in ("title", "due_at", "source_id"))
            for s in item.get("conflict_sources", [])
        ):
            continue
        items.append(item)
    return {
        **saved,
        "items": items,
        "ready": True,
        "stale": due(db, now),
        "removed": len(saved["items"]) - len(items),
    }
