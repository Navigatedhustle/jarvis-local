import re
from datetime import datetime, timezone
from uuid import uuid4

from psycopg.types.json import Jsonb

PRIORITY_TOPICS = {
    "ZGX Fury": r"\bzgx[\s-]+fury\b",
    "ZGX Nano": r"\bzgx[\s-]+nano\b",
    "LHA": r"\b(?:lha|lighthouse(?:[\s-]+accounts?)?)\b",
    "Sandbox events": r"\bsandbox\b[\s\S]{0,100}\b(?:events?|workshops?|sessions?|summits?|roadshows?)\b|\b(?:events?|workshops?|sessions?|summits?|roadshows?)\b[\s\S]{0,100}\bsandbox\b",
}


def triage(message, vip=False, priorities=None):
    body = message.get("body", {}).get("content", "")
    text = (message.get("subject", "") + "\n" + body).lower()
    factors = []
    if vip:
        factors.append("Sender is a confirmed VIP")
    matched_topics = [
        topic
        for topic in (priorities or {}).get("topics", [])
        if topic in PRIORITY_TOPICS and re.search(PRIORITY_TOPICS[topic], text)
    ]
    factors.extend("Demo's priority topic: " + topic for topic in matched_topics)
    if message.get("importance") == "high":
        factors.append("Sender marked high importance")
    if re.search(r"\b(zgx|higher education|research|lighthouse|fury)\b", text):
        factors.append("Relevant to HP AI, higher education or research")
    request = bool(
        re.search(
            r"\b(can you|could you|please (send|review|confirm|provide)|need your|your approval)\b", text
        )
    )
    if request:
        factors.append("Contains an explicit request")
    if re.search(r"\b(due|deadline|by tomorrow|by friday|by eod|by end of day)\b", text):
        factors.append("Contains deadline language; date needs confirmation")
    if message.get("isDraft"):
        return {"priority": "normal", "actionable": False, "factors": ["Draft message"]}
    return {
        "priority": "high" if vip or matched_topics or len(factors) >= 2 else "normal",
        "actionable": request,
        "factors": factors,
        "method": "deterministic_features",
    }


def addressed_to_someone_else(body):
    opening = re.split(r"(?im)^\s*(?:From:|On .+wrote:)", body, maxsplit=1)[0][:1200]
    greeting = re.search(r"(?im)^\s*(?:hi|hello|dear|hey)\s+([a-z][a-z .'-]{0,60})[,!:\n]", opening)
    if not greeting or re.search(r"\bdemo\b", opening, re.I):
        return False
    return greeting.group(1).strip().lower() not in {"team", "all", "everyone", "folks", "colleagues"}


def message_triage(db, message, folder_role="received", priorities=None):
    address = message.get("from", {}).get("emailAddress", {}).get("address", "").lower()
    person = db.one("SELECT vip FROM people WHERE id=%s", (address,)) if address else None
    if folder_role in {"sentitems", "drafts", "junkemail", "deleteditems"}:
        return {
            "priority": "normal",
            "actionable": False,
            "factors": ["Excluded from incoming response alerts: " + folder_role],
        }
    return triage(
        message,
        person["vip"] if person else False,
        db.get_state("preferences.work_priorities", {}) if priorities is None else priorities,
    )


def refresh_mail_priorities(db):
    """Reclassify saved evidence without reimporting or queuing outbound notifications."""
    priorities = db.get_state("preferences.work_priorities", {})
    changed = 0
    for record in db.query("SELECT id FROM records WHERE kind='email' AND NOT deleted"):
        with db.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM records WHERE id=%s AND NOT deleted FOR UPDATE", (record["id"],)
            ).fetchone()
            if not row:
                continue
            metadata = row["metadata"]
            result = message_triage(
                db,
                {**metadata, "subject": row["title"], "body": {"content": row["body"]}},
                metadata.get("folder_role", "received"),
                priorities,
            )
            if result != metadata.get("triage"):
                conn.execute(
                    "UPDATE records SET metadata=jsonb_set(metadata,'{triage}',%s),updated_at=now() WHERE id=%s",
                    (Jsonb(result), row["id"]),
                )
                changed += 1
    db.audit("mail.priorities_refreshed", details={"changed": changed}, actor="demo")
    return changed


def ingest_message(db, message, folder_name="", folder_role="received", provider="graph"):
    identifier = provider + ":mail:" + message["id"]
    if "@removed" in message:
        db.execute("UPDATE records SET deleted=true,updated_at=now() WHERE id=%s", (identifier,))
        return
    sender = message.get("from", {}).get("emailAddress", {})
    address = sender.get("address", "").lower()
    importance = message_triage(db, message, folder_role)
    metadata = {**message, "triage": importance, "folder": folder_name, "folder_role": folder_role}
    metadata.pop("body", None)
    occurred = message.get("receivedDateTime") or message.get("sentDateTime")
    with db.transaction() as conn:
        conn.execute(
            """INSERT INTO records(id,kind,provider,title,body,metadata,occurred_at)
                        VALUES (%s,'email',%s,%s,%s,%s,%s)
                        ON CONFLICT(id) DO UPDATE SET title=excluded.title,body=excluded.body,
                        metadata=excluded.metadata,occurred_at=excluded.occurred_at,deleted=false,updated_at=now()""",
            (
                identifier,
                provider,
                message.get("subject", "(No subject)"),
                message.get("body", {}).get("content", ""),
                Jsonb(metadata),
                occurred,
            ),
        )
        if address:
            conn.execute(
                """INSERT INTO people(id,name,last_interaction,profile) VALUES (%s,%s,%s,%s)
                            ON CONFLICT(id) DO UPDATE SET name=excluded.name,
                            last_interaction=greatest(people.last_interaction,excluded.last_interaction)""",
                (address, sender.get("name") or address, occurred, Jsonb({"source_id": identifier})),
            )


def ingest_event(db, event, provider="graph"):
    identifier = provider + ":event:" + event["id"]
    if "@removed" in event:
        db.execute("UPDATE records SET deleted=true,updated_at=now() WHERE id=%s", (identifier,))
        return
    start = event.get("start", {}).get("dateTime")
    if start and datetime.fromisoformat(start).tzinfo is None:
        start += "Z"  # Provider requests UTC; Graph returns zone separately.
    db.execute(
        """INSERT INTO records(id,kind,provider,title,body,metadata,occurred_at)
                  VALUES (%s,'event',%s,%s,%s,%s,%s)
                  ON CONFLICT(id) DO UPDATE SET title=excluded.title,body=excluded.body,
                  metadata=excluded.metadata,occurred_at=excluded.occurred_at,deleted=false,updated_at=now()""",
        (
            identifier,
            provider,
            event.get("subject", "(No subject)"),
            event.get("body", {}).get("content", ""),
            Jsonb(event),
            start,
        ),
    )


def context_today(db):
    emails = db.query("""SELECT * FROM (SELECT DISTINCT ON (r.provider,coalesce(nullif(r.metadata->>'conversationId',''),r.id))
                          r.id,r.title,left(r.body,1800) AS excerpt,r.metadata,r.occurred_at FROM records r WHERE r.kind='email' AND NOT r.deleted
                          AND r.metadata->'triage'->>'priority'='high'
                          AND (coalesce(r.metadata->>'isRead','false')='false' OR r.metadata->'triage'->>'actionable'='true')
                          AND NOT EXISTS (SELECT 1 FROM records sent WHERE sent.kind='email' AND NOT sent.deleted
                            AND sent.metadata->>'folder_role'='sentitems'
                            AND sent.metadata->>'conversationId'=r.metadata->>'conversationId'
                            AND sent.occurred_at>r.occurred_at)
                          ORDER BY r.provider,coalesce(nullif(r.metadata->>'conversationId',''),r.id),r.occurred_at DESC
                          ) threads ORDER BY occurred_at DESC LIMIT 15""")
    events = db.query("""SELECT id,title,metadata,occurred_at FROM records WHERE kind='event' AND NOT deleted
                          AND coalesce(metadata->>'isCancelled','false')<>'true'
                          AND occurred_at>=now()-interval '2 hours' AND occurred_at<now()+interval '48 hours'
                          ORDER BY occurred_at LIMIT 30""")
    commitments = db.query(
        "SELECT * FROM commitments WHERE status IN ('open','candidate') ORDER BY CASE status WHEN 'open' THEN 0 ELSE 1 END,due_at NULLS LAST LIMIT 20"
    )
    approvals = db.query(
        "SELECT id,kind,status,expires_at FROM approvals WHERE status IN ('pending','approved','uncertain') ORDER BY created_at DESC LIMIT 20"
    )
    return {
        "work_priorities": db.get_state("preferences.work_priorities", {}),
        "emails": emails,
        "events": events,
        "calendar_conflicts": calendar_conflicts(events),
        "commitments": commitments,
        "approvals": approvals,
        "outlook_sync": db.get_state("outlook.last_sync"),
        "outlook_error": db.get_state("outlook.error"),
        "outlook_enabled": db.get_state("setup.enabled.outlook", True),
        "outlook_ingestion": db.get_state("outlook.ingestion"),
    }


def calendar_conflicts(events):
    intervals = []
    for event in events:
        metadata = event["metadata"]
        if metadata.get("isCancelled") or metadata.get("showAs") == "free" or metadata.get("isAllDay"):
            continue
        value = metadata.get("end", {}).get("dateTime")
        if not value or not event["occurred_at"]:
            continue
        try:
            end = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if end.tzinfo is None:
                end = end.replace(tzinfo=timezone.utc)  # Graph is requested with UTC preference.
        except ValueError:
            continue
        intervals.append((event["occurred_at"], end, event["id"]))
    intervals.sort()
    return [
        {
            "event_ids": [first[2], second[2]],
            "overlap_start": max(first[0], second[0]),
            "overlap_end": min(first[1], second[1]),
        }
        for i, first in enumerate(intervals)
        for second in intervals[i + 1 :]
        if first[0] < second[1] and second[0] < first[1]
    ]


EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "commitments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "owner": {"type": "string"},
                    "beneficiary": {"type": "string"},
                    "text": {"type": "string"},
                    "due_at": {"type": ["string", "null"]},
                    "confidence": {"type": "number"},
                },
                "required": ["owner", "beneficiary", "text", "due_at", "confidence"],
                "additionalProperties": False,
            },
        },
        "decisions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "commitments", "decisions"],
    "additionalProperties": False,
}


def save_extraction(db, source_id, extracted):
    for item in extracted["commitments"]:
        if not 0.8 <= item["confidence"] <= 1:
            continue
        due = item.get("due_at")
        if due:
            parsed = datetime.fromisoformat(due.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                due = None  # Never silently guess timezone for a commitment.
        db.execute(
            """INSERT INTO commitments(id,owner,beneficiary,text,due_at,confidence,source_id)
                      VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(source_id,text) DO NOTHING""",
            (uuid4(), item["owner"], item["beneficiary"], item["text"], due, item["confidence"], source_id),
        )
    db.audit("source.extracted", source_id, {"candidate_commitments": len(extracted["commitments"])})
