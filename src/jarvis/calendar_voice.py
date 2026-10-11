"""Bounded, read-only calendar briefings from saved Outlook evidence."""

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .meetings import meeting_context

ZONE = ZoneInfo("America/Denver")
DAYS = "monday tuesday wednesday thursday friday saturday sunday".split()


def first_meeting_request(text):
    return bool(
        re.search(
            r"\b(?:first (?:meeting|appointment|event)|(?:my|the) day (?:start|begin)s?|start of (?:my|the) day|(?:what time|when) do I start)\b",
            text,
            re.I,
        )
    )


def calendar_read_request(text):
    return bool(
        re.search(r"\b(calendar|schedule|agenda|meetings?|appointments?|prepare|look like)\b", text, re.I)
        or first_meeting_request(text)
        or re.search(
            r"\b(?:do I have anything|what(?:'s| is) on (?:my )?plate|am I (?:busy|free)|what (?:do I|have I|I) (?:have|got)(?: going on)? (?:today|tomorrow|this week|next week|on (?:monday|tuesday|wednesday|thursday|friday|saturday|sunday))|what(?:'s| is) (?:going on|on) (?:for me|tomorrow|today))\b",
            text,
            re.I,
        )
    )


def resolve_calendar_range(db, text, conversation):
    span = calendar_range(text)
    if span or not calendar_read_request(text):
        return span
    # Reuse only a recent explicit calendar window, never assistant claims or mail content.
    if re.search(
        r"\b(send|cancel|cancellation|approve|approved|approval|delete|move|reschedule|create|book|add|arrange|set up|schedule (?:a|an|the)|invite|remember|draft|research|emails?|mail|threads?|news)\b",
        text,
        re.I,
    ):
        return None
    if re.search(r"\b(?:next meeting|week of|\d{4}-\d{2}-\d{2})\b", text, re.I):
        return None
    if re.search(
        r"\b(today|tomorrow|week|month|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
        text,
        re.I,
    ):
        return None
    saved = db.get_state("conversation.calendar_window." + conversation, {})
    try:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(saved["at"])
        if timedelta(0) <= age < timedelta(hours=1):
            return datetime.fromisoformat(saved["start"]), datetime.fromisoformat(saved["end"])
    except (KeyError, ValueError, TypeError):
        pass
    return calendar_range("my schedule for this week")


def calendar_range(text, now=None):
    """Recognize routine reads only; leave writes and complex requests to policy routing."""
    text = text.lower()
    if re.search(r"\b(e?mails?|messages?|threads?)\b", text):
        return None
    if re.search(r"\b(top \w+|to.do|priorities|things to do)\b", text):
        return None  # A calendar plus work-priority question needs the combined evidence path.
    if re.search(
        r"\b(send|cancel|cancellation|approve|approved|approval|delete|move|reschedule|create|book|add|arrange|set up|schedule (?:a|an|the)|invite|remember|draft|research|compare)\b",
        text,
    ):
        return None
    explicit = re.search(
        r"\b(?:week of\s+)?(?:\d{4}-\d{2}-\d{2}|(?:january|february|march|april|may|june|july|august|september|october|november|december) \d{1,2}(?:st|nd|rd|th)?(?:,? \d{4})?)\b",
        text,
    )
    # A spoken date correction is a complete calendar read, not a semantic search.
    if not calendar_read_request(text) and not (explicit and "week of" in text):
        return None
    now = now or datetime.now(ZONE)
    today = now.astimezone(ZONE).date()
    days = 1
    if explicit:
        value = re.sub(r"^week of\s+", "", explicit[0])
        value = re.sub(r"(\d)(?:st|nd|rd|th)\b", r"\1", value).replace(",", "")
        day = None
        for fmt in ("%Y-%m-%d", "%B %d %Y", "%B %d"):
            try:
                parsed = datetime.strptime(value, fmt)
                day = parsed.replace(year=today.year).date() if fmt == "%B %d" else parsed.date()
                break
            except ValueError:
                continue
        if day is None:
            return None
        if "week of" in text:
            day -= timedelta(days=day.weekday())
            days = 7
    elif re.search(r"\b(?:this|next|the|my|coming|upcoming) week\b", text):
        monday = today - timedelta(days=today.weekday())
        following = bool(re.search(r"\b(?:next|coming) week\b", text)) or (
            "this week" not in text and today.weekday() >= 5
        )
        day = monday + timedelta(days=7 if following else 0)
        days = 7
    elif "tomorrow" in text:
        day = today + timedelta(days=1)
    elif "today" in text:
        day = today
    else:
        match = re.search(r"\b(" + "|".join(DAYS) + r")\b", text)
        if not match:
            return None
        offset = (DAYS.index(match.group(1)) - today.weekday()) % 7
        day = today + timedelta(days=offset or (7 if "next " in text else 0))
    start = datetime.combine(day, datetime.min.time(), tzinfo=ZONE)
    end = datetime.combine(day + timedelta(days=days), datetime.min.time(), tzinfo=ZONE)
    return start, end


def calendar_brief(db, conversation, span, *, prepare=False, first_only=False):
    start, end = span
    db.set_state(
        "conversation.calendar_window." + conversation,
        {"start": start.isoformat(), "end": end.isoformat(), "at": datetime.now(timezone.utc).isoformat()},
    )
    events = db.query(
        """SELECT id,title,metadata,occurred_at FROM records WHERE kind='event' AND NOT deleted
        AND occurred_at >= %s AND occurred_at < %s
        AND coalesce(metadata->>'isCancelled','false') <> 'true'
        AND coalesce(metadata->>'showAs','') <> 'free'
        AND coalesce(metadata->'responseStatus'->>'response','') <> 'declined' ORDER BY occurred_at LIMIT 201""",
        (start, end),
    )
    db.set_state("conversation.calendar_day." + conversation, start.date().isoformat())
    priorities = db.get_state("preferences.work_priorities", {}) or {}
    topics = [str(t).lower() for t in priorities.get("topics", []) if isinstance(t, str)]
    vip = {p["id"].lower() for p in db.query("SELECT id FROM people WHERE vip")}

    def factors(event):
        result = [t for t in topics if t in event["title"].lower()]
        if any(
            a.get("emailAddress", {}).get("address", "").lower() in vip
            for a in event["metadata"].get("attendees", [])
        ):
            result.append("priority attendee")
        return result

    timed = [e for e in events if not e["metadata"].get("isAllDay")]
    selected = (
        timed[:1] if first_only else sorted(timed, key=lambda e: (-len(factors(e)), e["occurred_at"]))[:2]
    )
    label = (
        "the week of " + start.strftime("%B %d").replace(" 0", " ")
        if (end.date() - start.date()).days > 1
        else start.strftime("%A, %B %d").replace(" 0", " ")
    )
    sources = [
        {
            "id": e["id"],
            "kind": "meeting",
            "title": e["title"],
            "at": e["occurred_at"].isoformat(),
            "importance_factors": factors(e),
        }
        for e in (selected if first_only else events)
    ]
    if first_only:
        if selected:
            event = selected[0]
            when = event["occurred_at"].astimezone(ZONE).strftime("%I:%M %p").lstrip("0")
            answer = f"Your first saved meeting for {label} starts at {when} Mountain time: {event['title']}."
        else:
            answer = f"I have no timed meetings saved for {label}."
    elif not events:
        answer = f"I have no meetings saved for {label}."
    else:
        count = f"at least {len(timed)}" if len(events) == 201 else str(len(timed))
        answer = f"For {label}, I have {count} timed {'meeting' if len(timed) == 1 else 'meetings'} saved."
        if selected:
            parts = [
                " ".join(e["title"].split()[:10])
                + " on "
                + e["occurred_at"].astimezone(ZONE).strftime("%A at %I:%M %p").replace(" 0", " ")
                for e in selected
            ]
            lead = "Start your preparation with " if prepare else "Start with "
            answer += " " + lead + "; then ".join(parts) + "."
        if prepare and selected:
            context = meeting_context(db, selected[0]["id"]) or {}
            commitments = [
                c
                for c in context.get("commitments", [])
                if c.get("confidence", 0) >= 0.9 and "demo" in c["owner"].lower()
            ]
            emails = context.get("emails", [])[:3]
            if commitments:
                commitment = commitments[0]
                answer += " One open commitment to review: " + " ".join(commitment["text"].split()[:18]) + "."
                sources.append(
                    {
                        "id": commitment["source_id"],
                        "kind": "commitment",
                        "title": commitment["text"],
                        "excerpt": commitment["text"],
                        "at": commitment["due_at"].isoformat() if commitment["due_at"] else None,
                    }
                )
            elif emails:
                answer += (
                    " I've pulled related threads for the first meeting; review those before the agenda."
                )
            else:
                answer += " I'd review their agendas first; the full schedule is on screen."
            sources.extend(
                {
                    "id": e["id"],
                    "kind": "email",
                    "title": e["title"],
                    "excerpt": e.get("excerpt") or e.get("body", ""),
                    "coverage": e["metadata"].get("coverage"),
                    "at": e["occurred_at"].isoformat() if e["occurred_at"] else None,
                }
                for e in emails
            )
    sync = db.get_state("outlook.last_sync")
    try:
        fresh = sync and (datetime.now(timezone.utc) - datetime.fromisoformat(sync)).total_seconds() < 900
    except (ValueError, TypeError):
        fresh = False
    if not fresh or db.get_state("outlook.error") or db.get_state("setup.enabled.outlook", True) is False:
        answer += " Outlook needs refreshing, so this is the saved calendar."
    else:
        answer += " Coverage may be incomplete."
    return {"reply": answer, "sources": sources}
