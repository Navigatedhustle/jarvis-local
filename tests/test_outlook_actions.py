from datetime import datetime, timedelta, timezone

import pytest

from jarvis.orchestrator import Jarvis
from jarvis.outlook_actions import meeting_payload
from jarvis.policy import Approvals


def test_meeting_payload_requires_explicit_times_and_valid_addresses():
    start = datetime.now(timezone.utc) + timedelta(days=2)
    value = meeting_payload("Review", start, start + timedelta(hours=1), ["person@example.test"], "Agenda")
    assert value["start"]["timeZone"] == "UTC"
    assert value["attendees"][0]["emailAddress"]["address"] == "person@example.test"
    assert (
        len(
            meeting_payload("Review", start, start + timedelta(hours=1), ["A@b.test", "a@b.test"])[
                "attendees"
            ]
        )
        == 1
    )
    for attendees in [["Stephanie"], ["a@b.test\r\nBcc: bad@b.test"], [{}], [None]]:
        with pytest.raises(ValueError):
            meeting_payload("Review", start, start + timedelta(hours=1), attendees)
    with pytest.raises(ValueError):
        meeting_payload("Review", start.replace(tzinfo=None), start + timedelta(hours=1), [])
    with pytest.raises(ValueError):
        meeting_payload("Review", start, start - timedelta(minutes=1), [])


async def test_model_can_only_propose_with_known_attendees_never_execute(db):
    assistant = Jarvis(db, None, None, Approvals(db))
    start = datetime.now(timezone.utc) + timedelta(days=1)
    args = {
        "subject": "Review",
        "start": start.isoformat(),
        "end": (start + timedelta(hours=1)).isoformat(),
        "attendees": ["person@example.test"],
        "body": "Agenda",
    }
    with pytest.raises(ValueError, match="verified"):
        await assistant.call_tool("prepare_meeting_proposal", args)
    assert not db.one("SELECT * FROM approvals")
    db.execute("INSERT INTO people(id,name) VALUES('person@example.test','Example person')")
    result = await assistant.call_tool("prepare_meeting_proposal", args)
    assert result["id"] and db.one("SELECT status FROM approvals")["status"] == "pending"
