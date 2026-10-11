from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from jarvis.calendar_voice import ZONE, calendar_brief, calendar_range
from jarvis.intelligence import ingest_event


@pytest.mark.parametrize(
    "text,day,days",
    [
        ("Can you get me an update on my schedule for the week? Anything I should prepare for?", 21, 7),
        ("I need to know my schedule for the week. What does it look like", 21, 7),
        ("Look at my calendar this week", 14, 7),
        ("What does my schedule look like on Monday? Anything I should prepare for?", 21, 1),
        ("What's tomorrow look like?", 20, 1),
    ],
)
def test_calendar_understands_real_requests_with_denver_dates(text, day, days):
    start, end = calendar_range(text, datetime(2026, 9, 19, 20, tzinfo=ZONE))
    assert start.day == day and (end.date() - start.date()).days == days


@pytest.mark.parametrize(
    "text",
    [
        "Cancel my meeting on Monday",
        "Move my calendar meeting next week",
        "Send Stephanie my schedule for the week",
        "Remember this week is important",
        "Research the latest AI models",
        "Prepare me for my next meeting",
    ],
)
def test_calendar_read_does_not_capture_writes_or_unrelated_requests(text):
    assert calendar_range(text) is None


def test_week_brief_prioritizes_real_topics_and_excludes_cancelled_free(db):
    start = datetime(2026, 9, 21, 9, tzinfo=ZONE)
    for key, title, extra in [
        ("routine", "Routine sync", {}),
        ("priority", "ZGX Fury customer sandbox", {}),
        ("cancelled", "Cancelled event", {"isCancelled": True}),
        ("free", "Travel information", {"showAs": "free"}),
    ]:
        ingest_event(db, {"id": key, "subject": title, "start": {"dateTime": start.isoformat()}, **extra})
    db.set_state("preferences.work_priorities", {"topics": ["ZGX Fury"]})
    db.set_state("outlook.last_sync", datetime.now(timezone.utc).isoformat())
    brief = calendar_brief(db, "qa", (start.replace(hour=0), start + timedelta(days=7)), prepare=True)
    assert "2 timed meetings" in brief["reply"]
    assert brief["reply"].index("ZGX Fury") < brief["reply"].index("Routine")
    assert len(brief["sources"]) == 2
    assert "today" not in brief["reply"]
    assert "Coverage may be incomplete" in brief["reply"]


async def test_week_request_does_not_wait_for_model_or_grant_external_authority(db):
    from types import SimpleNamespace

    from jarvis.orchestrator import Jarvis

    model = SimpleNamespace(chat=AsyncMock(side_effect=AssertionError("No inference required")))
    jarvis = Jarvis(db, model, None, None)
    result = await jarvis.reply(
        "Look at my calendar for the week and tell me what I should prepare for", spoken=True
    )
    assert "saved" in result["reply"] and "refreshing" in result["reply"]
    model.chat.assert_not_called()


@pytest.mark.parametrize(
    "text",
    [
        "There we go, what time does my day start tomorrow?",
        "When does my day begin tomorrow?",
        "What time do I start tomorrow?",
        "Do I have anything tomorrow?",
        "What's on my agenda tomorrow?",
    ],
)
def test_day_start_and_agenda_are_calendar_reads(text):
    start, end = calendar_range(text, datetime(2026, 9, 20, 20, tzinfo=ZONE))
    assert start.day == 21 and (end - start).days == 1


@pytest.mark.parametrize("spoken", [True, False])
async def test_actual_failed_phrase_reads_first_meeting_not_unrelated_text(db, spoken):
    from types import SimpleNamespace

    from jarvis.calendar_voice import resolve_calendar_range
    from jarvis.orchestrator import Jarvis

    day = (datetime.now(ZONE) + timedelta(days=1)).replace(hour=7, minute=0, second=0, microsecond=0)
    for key, extra in [
        ("cancelled", {"isCancelled": True}),
        ("all-day", {"isAllDay": True}),
        ("declined", {"responseStatus": {"response": "declined"}}),
        ("first", {}),
    ]:
        ingest_event(db, {"id": key, "subject": key, "start": {"dateTime": day.isoformat()}, **extra})
    db.execute(
        "INSERT INTO messages(conversation,role,content) VALUES('actual','assistant','No calendar or internet access')"
    )
    model = SimpleNamespace(chat=AsyncMock(side_effect=AssertionError("Routing must not depend on a model")))
    jarvis = Jarvis(db, model, None, None)
    result = await jarvis.reply(
        "There we go, what time does my day start tomorrow?", conversation="actual", spoken=spoken
    )
    assert "7:00 AM" in result["reply"] and "first saved meeting" in result["reply"]
    assert [s["title"] for s in result["sources"]] == ["first"]
    followup = await jarvis.reply(
        "My new schedule, how come you can't see it?", conversation="actual", spoken=spoken
    )
    assert "1 timed meeting" in followup["reply"]
    assert resolve_calendar_range(db, "Schedule a meeting with Stephanie", "actual") is None
    assert resolve_calendar_range(db, "Add a meeting tomorrow", "actual") is None
    model.chat.assert_not_called()
