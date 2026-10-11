"""Normalize reviewable Outlook proposals; this module performs no external writes."""

import re
from datetime import datetime, timedelta, timezone


def addresses(values):
    if not isinstance(values, list) or len(values) > 20 or not all(isinstance(x, str) for x in values):
        raise ValueError("Choose at most 20 explicit email addresses")
    result = list(dict.fromkeys(x.strip().lower() for x in values))
    if any(
        not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+", x)
        for x in result
    ):
        raise ValueError("Use complete email addresses; JARVIS will not guess recipients")
    return result


def meeting_payload(subject, start, end, attendees, body=""):
    start = datetime.fromisoformat(start.replace("Z", "+00:00")) if isinstance(start, str) else start
    end = datetime.fromisoformat(end.replace("Z", "+00:00")) if isinstance(end, str) else end
    if (
        not start.tzinfo
        or not end.tzinfo
        or start <= datetime.now(timezone.utc)
        or not timedelta(0) < end - start <= timedelta(days=1)
    ):
        raise ValueError("Choose timezone-aware future meeting times, at most 24 hours apart")
    if (
        not isinstance(subject, str)
        or not 1 <= len(subject.strip()) <= 200
        or not isinstance(body, str)
        or len(body) > 10000
    ):
        raise ValueError("Meeting title and agenda exceed supported bounds")
    return {
        "subject": subject.strip(),
        "body": {"contentType": "Text", "content": body},
        "start": {"dateTime": start.astimezone(timezone.utc).isoformat(), "timeZone": "UTC"},
        "end": {"dateTime": end.astimezone(timezone.utc).isoformat(), "timeZone": "UTC"},
        "attendees": [{"emailAddress": {"address": a}, "type": "required"} for a in addresses(attendees)],
    }


def email_payload(subject, body, recipients):
    if not isinstance(subject, str) or not 1 <= len(subject.strip()) <= 500:
        raise ValueError("A subject of at most 500 characters is required")
    if not isinstance(body, str) or not 1 <= len(body.strip()) <= 20000:
        raise ValueError("A message of at most 20,000 characters is required")
    recipients = addresses(recipients)
    if not recipients:
        raise ValueError("At least one explicit recipient is required")
    return {
        "message": {
            "subject": subject.strip(),
            "body": {"contentType": "Text", "content": body.strip()},
            "toRecipients": [{"emailAddress": {"address": value}} for value in recipients],
        }
    }
