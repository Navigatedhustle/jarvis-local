"""Read-only sender-aware mailbox retrieval shared by speech and tool calls.

Identity comes from stored people and Demo's preferences, never quoted mail text.
Conversation previews are not counted twice when a full message is available.
"""

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ZONE = ZoneInfo("America/Denver")
NUMBERS = dict(zip("one two three four five six seven eight nine ten".split(), range(1, 11)))
FILLER = set(
    "email emails mail messages message thread threads latest last recent recently summarize summary find pull show read tell me sent send from to by my manager boss please jarvis what did does has have the a an about regarding of and that on in since this week".split()
)


def words(text):
    return re.findall(r"[a-z0-9]+", text.lower().replace("'s", ""))


def resolve_person(db, text):
    tokens = set(words(text)) - FILLER
    explicit_address = re.search(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9.-]+\.[a-z]{2,}", text, re.I)
    if explicit_address:
        address = explicit_address[0].lower()
        return {"id": address, "name": address, "profile": {}}, []
    people = db.query("SELECT id,name,profile FROM people")
    preferences = db.get_state("preferences.work_priorities", {}) or {}
    aliases = {p.get("person_id"): p for p in preferences.get("people", [])}
    if re.search(r"\bmy (?:direct )?(?:manager|boss)\b", text, re.I):
        managers = [
            p
            for p in people
            if "direct manager"
            in str(
                aliases.get(p["id"], {}).get("relationship", p["profile"].get("demo_relationship", ""))
            ).lower()
        ]
        if len(managers) == 1:
            return managers[0], []
    exact, partial = [], []
    for p in people:
        if p["id"].lower() in text.lower():
            return p, []
        names = [p["name"], aliases.get(p["id"], {}).get("name", "")]
        sets = [set(words(n)) for n in names if n]
        if any(len(n) >= 2 and n <= tokens for n in sets):
            exact.append(p)
        elif any(tokens & n for n in sets):
            partial.append(p)
    candidates = exact or partial
    if len(candidates) == 1:
        return candidates[0], []
    return None, [{"name": p["name"], "address": p["id"]} for p in candidates[:8]]


def mail_intent(text):
    return bool(
        re.search(
            r"\b(e?mails?|messages?|threads?|sent me|emailed|inbox)\b|\bwhat (?:did|has) .{1,80} (?:send|sent)\b",
            text,
            re.I,
        )
    )


def overview_request(text):
    """Recognize general inbox status without treating conversational filler as search terms."""
    text = re.sub(
        r"^(?:(?:all right|alright|and|hey|jarvis|well|okay|ok)[,.!? ]+)+", "", text.strip(), flags=re.I
    )
    return bool(
        re.fullmatch(
            r"(?:what (?:does|do) my (?:emails?|inbox|mail) look like|how(?:'s| is) my (?:inbox|email)|(?:anything|what(?:'s| is)) new (?:in|with) (?:my )?(?:inbox|email|mail)|(?:check|summarize|review|show me) my (?:latest |recent |new )?(?:emails?|inbox|mail))(?:[?.!, ]+(?:anything new|what(?:'s| is) new))?[?.! ]*",
            text,
            re.I,
        )
    )


def inbox_overview(db, conversation, now=None):
    """Bounded incoming-only recent summary; no model, invented action or full-mailbox claim."""
    now = now or datetime.now(ZONE)
    rows = db.query(
        """SELECT id,title,metadata,occurred_at FROM records WHERE kind='email' AND NOT deleted
        AND metadata->>'folder_role'='inbox' AND occurred_at >= %s AND occurred_at <= %s
        ORDER BY occurred_at DESC LIMIT 100""",
        (now - timedelta(hours=24), now),
    )
    seen, unique = set(), []
    for row in rows:
        key = row["metadata"].get("conversationId") or row["id"]
        if key not in seen:
            seen.add(key)
            unique.append(row)

    def rank(row):
        triage = row["metadata"].get("triage") or {}
        return (
            {"urgent": 0, "high": 1, "normal": 2, "low": 3}.get(triage.get("priority"), 2),
            -row["occurred_at"].timestamp(),
        )

    selected = sorted(unique, key=rank)[:3]
    sources = [
        {
            "id": r["id"],
            "kind": "email",
            "title": r["title"],
            "at": r["occurred_at"].isoformat(),
            "sender": r["metadata"].get("from", {}).get("emailAddress", {}).get("name", "Unknown sender"),
            "coverage": r["metadata"].get("coverage"),
        }
        for r in selected
    ]
    if sources:
        count = ("At least " if len(rows) == 100 else "") + str(len(unique))
        answer = count + " incoming threads are saved from the last 24 hours. "
        answer += (
            "Worth reviewing: "
            + "; ".join(s["sender"] + ", " + " ".join(s["title"].split()[:10]) for s in sources)
            + ". I can open any of those."
        )
    else:
        answer = "No incoming mail from the last 24 hours is in the saved index yet."
    stamp = db.get_state("outlook.last_sync")
    try:
        fresh = stamp and (now - datetime.fromisoformat(stamp)).total_seconds() < 900
    except (ValueError, TypeError):
        fresh = False
    answer += (
        " Coverage is partial."
        if fresh and not db.get_state("outlook.error") and db.get_state("setup.enabled.outlook", True)
        else " Outlook needs refreshing; this is saved coverage, not a live all-clear."
    )
    db.set_state("conversation.last_briefing_sources." + conversation, sources)
    return {"reply": answer, "sources": sources}


def requested_count(text, default=5):
    match = re.search(
        r"\b(?:last|latest|recent|summarize|summary of)\s+(\d+|" + "|".join(NUMBERS) + r")\b", text, re.I
    )
    if not match:
        return default
    value = match[1].lower()
    return min(12, max(1, int(value) if value.isdigit() else NUMBERS[value]))


def date_bounds(text, now=None):
    now = (now or datetime.now(ZONE)).astimezone(ZONE)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    lower = text.lower()
    if "this week" in lower or "last week" in lower:
        start = today - timedelta(days=today.weekday() + (7 if "last week" in lower else 0))
        return start, start + timedelta(days=7)
    if "yesterday" in lower:
        return today - timedelta(days=1), today
    if "today" in lower:
        return today, today + timedelta(days=1)
    match = re.search(r"\b(?:last|past) (\d+) days\b", lower)
    if match:
        return now - timedelta(days=min(int(match[1]), 3660)), now
    match = re.search(r"\bsince (\d{4}-\d{2}-\d{2}|[a-z]+ \d{1,2}(?:st|nd|rd|th)?(?:,? \d{4})?)", lower)
    if match:
        value = re.sub(r"(\d)(st|nd|rd|th)", r"\1", match[1]).replace(",", "")
        for fmt in ("%Y-%m-%d", "%B %d %Y", "%B %d"):
            try:
                parsed = datetime.strptime(value, fmt).replace(tzinfo=ZONE)
                if fmt == "%B %d":
                    parsed = parsed.replace(year=now.year)
                    if parsed > now:
                        parsed = parsed.replace(year=now.year - 1)
                return parsed, None
            except ValueError:
                continue
    return None, None


def search_mail(db, text, *, limit=None):
    person, ambiguous = resolve_person(db, text)
    limit = limit or requested_count(text, 12)
    filters = ["kind='email'", "NOT deleted"]
    params = []
    direction = "sender"
    if person:
        direction = "recipient" if re.search(r"\b(?:sent|emails?|messages?) to\b", text, re.I) else "sender"
        if direction == "sender":
            filters.append("lower(metadata->'from'->'emailAddress'->>'address')=%s")
        else:
            filters.append(
                "EXISTS (SELECT 1 FROM jsonb_array_elements(coalesce(metadata->'toRecipients','[]'::jsonb)) AS r WHERE lower(r->'emailAddress'->>'address')=%s)"
            )
        params.append(person["id"].lower())
    elif ambiguous:
        return {
            "records": [],
            "ambiguous_people": ambiguous,
            "coverage": "Choose the intended person before searching.",
        }
    elif re.search(r"\b(?:from|to)\s+\S+", text, re.I):
        return {
            "records": [],
            "unresolved_person": True,
            "coverage": "The requested person could not be identified. Ask for their full name or email address; do not substitute messages mentioning the name.",
        }
    start, end = date_bounds(text)
    if start:
        filters.append("occurred_at >= %s")
        params.append(start)
    if end:
        filters.append("occurred_at < %s")
        params.append(end)
    topic_match = re.search(r"\b(?:about|regarding|subject:)\s+(.+)", text, re.I)
    if not topic_match:
        topic_match = re.search(r"['’]s\s+(.+?)\s+(?:e?mails?|thread)\b", text, re.I)
    topic = topic_match[1] if topic_match else ("" if person else text)
    topic_words = [w for w in words(topic) if w not in FILLER and not w.isdigit()]
    if person:
        identity = set(words(person["name"])) | set(words(person["id"]))
        topic_words = [w for w in topic_words if w not in identity]
    if topic_words:
        # Join adjacent spoken product words too (Red Hat / RedHat).
        query = " OR ".join(
            dict.fromkeys(topic_words + [a + b for a, b in zip(topic_words, topic_words[1:])])
        )
        filters.append("search @@ websearch_to_tsquery('english',%s)")
        params.append(query)
    elif not person:
        return {"records": [], "coverage": "No resolvable person or searchable topic."}
    rows = db.query(
        "SELECT id,kind,provider,title,left(body,16000) AS body,metadata,occurred_at FROM records WHERE "
        + " AND ".join(filters)
        + " ORDER BY occurred_at DESC NULLS LAST,id LIMIT 100",
        tuple(params),
    )
    # A preview represents a conversation, not a second email. Keep actual branches.
    full_threads = {
        r["metadata"].get("conversationId")
        for r in rows
        if r["metadata"].get("coverage") != "conversation_preview"
    }
    rows = [
        r
        for r in rows
        if r["metadata"].get("coverage") != "conversation_preview"
        or not r["metadata"].get("conversationId")
        or r["metadata"].get("conversationId") not in full_threads
    ]
    return {
        "records": rows[:limit],
        "person": {"name": person["name"], "address": person["id"]} if person else None,
        "direction": direction,
        "requested_count": limit,
        "range_start": start,
        "range_end": end,
        "coverage": "Saved mailbox only; missing matches do not prove no email exists. Preview snippets are not full messages.",
    }


def thread_records(db, row, limit=8):
    thread = row.get("metadata", {}).get("conversationId")
    if not thread:
        return [row]
    rows = db.query(
        "SELECT id,kind,provider,title,left(body,16000) AS body,metadata,occurred_at FROM records WHERE NOT deleted AND kind='email' AND provider=%s AND metadata->>'conversationId'=%s ORDER BY occurred_at DESC NULLS LAST,id LIMIT %s",
        (row["provider"], thread, limit),
    )
    oldest = db.one(
        "SELECT id,kind,provider,title,left(body,16000) AS body,metadata,occurred_at FROM records WHERE NOT deleted AND kind='email' AND provider=%s AND metadata->>'conversationId'=%s ORDER BY occurred_at ASC NULLS LAST,id LIMIT 1",
        (row["provider"], thread),
    )
    if oldest and oldest["id"] not in {r["id"] for r in rows}:
        rows = rows[: limit - 1] + [oldest]
    return rows
