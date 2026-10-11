from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_command_center import record

from jarvis import public_research
from jarvis.calendar_voice import ZONE, calendar_range, resolve_calendar_range
from jarvis.intelligence import ingest_event
from jarvis.mail_retrieval import inbox_overview, overview_request
from jarvis.orchestrator import Jarvis


def test_explicit_competitive_query_keeps_the_requested_vendor_and_scope():
    assert (
        public_research.search_topic("Research local AI competitive offerings from Apple")
        == "local AI competitive offerings from Apple"
    )


async def test_competitive_suggestion_cannot_presuppose_an_hp_advantage_in_speech():
    emitted = []
    text = "Dell published a local agent workflow [1]. I'd highlight HP ZGX Nano's edge for universities."

    async def emit(value):
        emitted.append(value)

    async def chat(*args, **kwargs):
        await kwargs["on_text"](text + " ")
        return {"choices": [{"message": {"content": text}}]}

    result = await public_research.synthesize(
        SimpleNamespace(chat=chat),
        "local AI competitiveness",
        [{"title": "Dell local workflow", "excerpt": "Dell published an agent workflow."}],
        spoken=True,
        on_text=emit,
    )
    assert "Nano's edge" not in result and "Nano's edge" not in "".join(emitted)
    assert "controlled university pilot" in result and "controlled university pilot" in "".join(emitted)


@pytest.mark.parametrize(
    "text",
    [
        "Hey Jarvis, what do I got going on tomorrow? Let me know",
        "What have I got tomorrow?",
        "What do I have going on tomorrow?",
        "What's going on for me tomorrow?",
        "What is on tomorrow?",
    ],
)
def test_natural_agenda_requests(text):
    start, end = calendar_range(text, datetime(2026, 9, 21, 18, tzinfo=ZONE))
    assert start.day == 22 and (end - start).days == 1


@pytest.mark.parametrize(
    "text,day,days",
    [
        ("Hey Jarv, the week of September 21st", 21, 7),
        ("Look at my calendar for the week of October 12", 12, 7),
        ("My schedule on 2026-09-23", 23, 1),
        ("My schedule for the upcoming week", 21, 7),
        ("My schedule for next week", 28, 7),
    ],
)
def test_explicit_date_corrections_and_week_boundaries(text, day, days):
    start, end = calendar_range(text, datetime(2026, 9, 21, 18, tzinfo=ZONE))
    assert start.day == day and (end - start).days == days


@pytest.mark.parametrize(
    "text",
    [
        "Send my calendar for the week of September 21",
        "Cancel the meeting September 21",
        "Remember the week of September 21",
        "Search emails for the week of September 21",
        "My schedule on February 31",
    ],
)
def test_date_parser_preserves_action_and_data_boundaries(text):
    assert calendar_range(text) is None


@pytest.mark.parametrize("spoken", [True, False])
async def test_demo_calendar_then_correction_without_model(db, spoken):
    tomorrow = (datetime.now(ZONE) + timedelta(days=1)).replace(hour=9, minute=0)
    ingest_event(
        db,
        {"id": "demo", "subject": "Synthetic priority meeting", "start": {"dateTime": tomorrow.isoformat()}},
    )
    db.set_state("outlook.last_sync", datetime.now(timezone.utc).isoformat())
    model = SimpleNamespace(chat=AsyncMock(side_effect=AssertionError("No model for agenda reads")))
    jarvis = Jarvis(db, model, None, None)
    first = await jarvis.reply(
        "Hey Jarvis, what do I got going on tomorrow? Let me know", "demo", spoken=spoken
    )
    correction = await jarvis.reply(
        "Dang check out the calendar on Outlook. Why can't you see it", "demo", spoken=spoken
    )
    assert (
        first["sources"] == correction["sources"]
        and first["sources"][0]["title"] == "Synthetic priority meeting"
    )
    week = await jarvis.reply("Hey Jarv, the week of September 21st", "demo", spoken=spoken)
    assert "week of September 21" in week["reply"]
    assert resolve_calendar_range(db, "Send my schedule", "demo") is None
    model.chat.assert_not_called()


@pytest.mark.parametrize(
    "text",
    [
        "Because he's only local. All right, well, what does local AI look like for the competitive report? How can I capitalize on it?",
        "Local AI competitiveness against HP.",
        "Local eye competitiveness against HP.",
        "What does local eye look like for the competitive report? How can I capitalize on it?",
        "What is the local AI competitive landscape?",
        "What are our competitors doing with local AI?",
    ],
)
async def test_competitive_questions_use_public_sources_not_saved_mail(db, monkeypatch, text):
    request = public_research.resolve_request(db, text, "demo")
    assert request["mode"] == "search" and request["news"]
    assert request["query"].startswith("local AI") or request["query"].startswith("Local AI")
    reader = AsyncMock(
        return_value={
            "reply": "A sourced market update.",
            "sources": [{"kind": "web", "title": "Official announcement"}],
        }
    )
    monkeypatch.setattr(public_research, "answer", reader)
    jarvis = Jarvis(
        db, SimpleNamespace(chat=AsyncMock(side_effect=AssertionError("Wrong local retrieval"))), None, None
    )
    result = await jarvis.reply(text, "demo", spoken=True)
    assert result["sources"][0]["kind"] == "web"
    reader.assert_awaited_once()


@pytest.mark.parametrize(
    "text",
    [
        "Search my emails for local AI competitive reports",
        "Send Stephanie a local AI competitive report",
        "Remember local AI competitive positioning",
    ],
)
def test_competitive_route_does_not_export_private_work(db, text):
    assert public_research.resolve_request(db, text, "demo") is None


@pytest.mark.parametrize(
    "text",
    [
        "All right, and what does my email look like? Anything new?",
        "Anything new in my inbox?",
        "Check my recent emails",
        "How is my inbox?",
    ],
)
def test_inbox_conversation_is_not_a_keyword_search(text):
    assert overview_request(text)


@pytest.mark.parametrize(
    "text",
    [
        "Show me emails from Stephanie",
        "Send my email",
        "Delete my inbox",
        "Summarize my email about local AI",
    ],
)
def test_specific_mail_and_actions_remain_separate(text):
    assert not overview_request(text)


async def test_inbox_summary_is_recent_incoming_only_and_fast(db):
    now = datetime.now(timezone.utc)
    for key, hours, folder in [("incoming", 1, "inbox"), ("old", 49, "inbox"), ("sent", 1, "sentitems")]:
        record(
            db,
            key,
            when=now - timedelta(hours=hours),
            meta={"folder_role": folder, "from": {"emailAddress": {"name": "Synthetic Sender"}}},
        )
    result = inbox_overview(db, "demo", now)
    assert [s["id"] for s in result["sources"]] == ["incoming"]
    assert "needs refreshing" in result["reply"]
    model = SimpleNamespace(chat=AsyncMock(side_effect=AssertionError("No model for inbox status")))
    result = await Jarvis(db, model, None, None).reply(
        "All right, and what does my email look like? Anything new?", "demo", spoken=True
    )
    assert [s["id"] for s in result["sources"]] == ["incoming"]
    model.chat.assert_not_called()
