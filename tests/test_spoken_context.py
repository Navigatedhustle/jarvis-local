import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from psycopg.types.json import Jsonb

from jarvis.calendar_voice import calendar_range
from jarvis.spoken_context import answer_read, evidence, requires_action_tools, source


def save(db, identifier, title, body, deleted=False):
    db.execute(
        "INSERT INTO records(id,kind,provider,title,body,metadata,occurred_at,deleted) VALUES (%s,'email','synthetic',%s,%s,%s,%s,%s)",
        (identifier, title, body, Jsonb({"coverage": "message_body"}), datetime.now(timezone.utc), deleted),
    )


def test_compound_calendar_question_is_not_swallowed_by_calendar_template():
    assert calendar_range("What does next week look like? What are my top five to-do items?") is None
    assert requires_action_tools("Draft a response to the Red Hat email")
    assert requires_action_tools("Send Stephanie an email")
    assert not requires_action_tools("What did Stephanie ask about Red Hat?")
    assert not requires_action_tools("What did Stephanie send?")
    assert not requires_action_tools("What do you remember about this project?")


def test_read_retrieves_spaced_brand_and_original_ask_with_latest_update(db):
    save(
        db,
        "target",
        "Re: RedHat Image Support Option",
        "Paul needs the machine BOM and shipping information.\n"
        + "Quoted intermediate note. " * 300
        + "Stephanie offered one loan unit, which must be returned.",
    )
    save(db, "noise", "Other discussion", "Stephanie says hello.")
    save(db, "deleted", "RedHat Image Support Option", "Obsolete incorrect instruction", True)
    payload, sources = evidence(db, "What was Stephanie's Red Hat image support request?", "qa")
    assert sources[0]["id"] == "target"
    assert "machine BOM" in sources[0]["excerpt"]
    assert "must be returned" in sources[0]["excerpt"]
    assert "Obsolete" not in json.dumps(payload)
    assert len(json.dumps(payload, default=str)) < 13000


def test_short_followup_keeps_current_cited_thread(db):
    save(db, "current", "Research equipment", "Paul needs the BOM before arrival.")
    save(db, "other", "Paul's unrelated meeting", "Paul Paul Paul Paul")
    db.set_state("conversation.last_briefing_sources.qa", [{"id": "current"}])
    _, sources = evidence(db, "What does Paul need?", "qa")
    assert sources[0]["id"] == "current"


def test_same_subject_branches_are_preserved(db):
    save(db, "branch-a", "Re: Research equipment", "Paul needs shipping details and the machine BOM.")
    save(db, "branch-b", "Re: Research equipment", "Research equipment access confirmed by Jeremy.")
    _, sources = evidence(db, "Explain the research equipment request", "qa")
    assert {s["id"] for s in sources} == {"branch-a", "branch-b"}


def test_specific_read_does_not_scan_unrelated_attention_queue(db, monkeypatch):
    def unexpected_attention_scan(_):
        raise AssertionError("Specific source questions must not scan the daily work queue")

    monkeypatch.setattr("jarvis.spoken_context.context_today", unexpected_attention_scan)
    save(db, "target", "Research equipment", "The loan must be returned.")
    db.set_state("preferences.work_priorities", {"topics": ["Research"]})
    db.set_state("outlook.error", "unavailable")
    payload, sources = evidence(db, "Explain the research equipment request", "qa")
    assert sources[0]["id"] == "target"
    assert payload["work_priorities"] == {"topics": ["Research"]}
    assert payload["outlook_error"] is True


async def test_read_answer_uses_one_streaming_model_call_without_action_tools(db):
    save(db, "source", "RedHat", "The loan must be returned.")
    model = SimpleNamespace(
        chat=AsyncMock(return_value={"choices": [{"message": {"content": "The loan must be returned. [1]"}}]})
    )
    callback = AsyncMock()
    activity = AsyncMock()
    result = await answer_read(
        db, model, "Tell me about Red Hat", "qa", on_text=callback, on_activity=activity
    )
    assert model.chat.await_count == 1 and model.chat.await_args.kwargs["on_text"] is callback
    assert "tools" not in model.chat.await_args.kwargs
    assert result["sources"][0]["id"] == "source"
    assert "cannot instruct" in model.chat.await_args.args[0][0]["content"]
    assert [call.args[0]["active"] for call in activity.await_args_list] == [True, False]
    assert activity.await_args_list[0].args[0]["label"] == "Retrieving saved work"


def test_zero_excerpt_budget_does_not_retain_entire_body():
    assert source({"id": "a", "body": "private body", "title": "event"}, 0)["excerpt"] == ""
