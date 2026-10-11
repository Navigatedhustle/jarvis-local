from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jarvis.intelligence import addressed_to_someone_else, ingest_message
from jarvis.proactive import Proactive, draft_evidence, extraction_evidence


async def test_requested_reply_is_review_required_and_rejects_source_change(db):
    db.execute(
        "INSERT INTO records(id,kind,provider,title,body) VALUES('reply','email','synthetic','Question','Please review')"
    )
    assistant = SimpleNamespace(
        call_tool=AsyncMock(
            return_value={"id": "reply", "title": "Question", "body": "Please review", "metadata": {}}
        )
    )
    engine = Proactive(SimpleNamespace(), db, None, assistant, None, None)

    async def changed(*args):
        db.execute("UPDATE records SET body='Changed evidence' WHERE id='reply'")
        return "Draft based on outdated evidence."

    engine.summarize = changed
    with pytest.raises(ValueError, match="Source changed"):
        await engine.important_email({"source_id": "reply", "requested": True})
    assert not db.one("SELECT id FROM records WHERE kind='draft'")
    engine.summarize = AsyncMock(return_value="Please review this proposed reply.")
    await engine.important_email({"source_id": "reply", "requested": True})
    assert db.one("SELECT metadata FROM records WHERE kind='draft'")["metadata"]["review_required"]
    assert not db.one("SELECT id FROM jobs WHERE kind='notify'")


def test_extraction_budget_preserves_unicode_and_labels_partial_evidence():
    source = {
        "title": "Project update",
        "body": "Research context\n" + "研究" * 10000,
        "occurred_at": "2026-09-01T12:00:00+00:00",
        "metadata": {"coverage": "message_body", "unrelated": "x" * 100000},
    }
    evidence = extraction_evidence(source)
    assert len(evidence["body"].encode("utf-8")) <= 4000
    assert evidence["extraction_coverage"] == "excerpt"
    assert evidence["occurred_at"] == source["occurred_at"]
    assert "unrelated" not in evidence
    assert len(source["body"]) > 10000
    source["body"] = "I will send the research update Friday."
    assert extraction_evidence(source)["extraction_coverage"] == "stored_body"


def test_draft_context_preserves_current_source_and_labels_truncation():
    import json

    source = {
        "id": "current",
        "title": "Research review",
        "body": "证据 " * 18000,
        "search": "INTERNAL SEARCH INDEX " * 20000,
        "metadata": {"coverage": "message_body"},
        "thread": [
            {"id": "current", "body": "duplicate"},
            *[{"id": str(i), "body": "x" * 15000} for i in range(20)],
        ],
    }
    evidence = draft_evidence(source)
    assert len(evidence["message"]["body"].encode()) <= 12000
    assert evidence["message"]["body_complete"] is False
    assert len(evidence["related_messages"]) == 4
    assert "INTERNAL SEARCH INDEX" not in json.dumps(evidence)
    assert "duplicate" not in json.dumps(evidence)


def test_another_named_recipient_is_not_mistaken_for_a_request_to_demo():
    assert addressed_to_someone_else(
        "CAUTION: External Email\nHi Chris,\nCan you confirm?\nFrom: Demo User\nQuoted reply"
    )
    assert not addressed_to_someone_else("Hi Demo,\nCan you review the brief?")
    assert not addressed_to_someone_else("Hi Chris,\nDemo, could you review the research brief?")


async def test_historical_ingestion_does_not_spam_and_fresh_notices_coalesce(db):
    telegram = SimpleNamespace(send=AsyncMock(return_value={"delivered": True}))
    engine = Proactive(SimpleNamespace(telegram_token="synthetic"), db, None, None, telegram, None)
    now = datetime.now(timezone.utc)
    for identifier, date in [("old", now - timedelta(days=60)), ("fresh", now), ("fresh2", now)]:
        ingest_message(
            db,
            {
                "id": identifier,
                "subject": "Please review ZGX",
                "isRead": False,
                "body": {"content": "Can you review?"},
                "receivedDateTime": date.isoformat(),
            },
        )
    assert "skipped" in await engine.notify({"source_id": "graph:mail:old"})
    await engine.notify({"source_id": "graph:mail:fresh"})
    assert "coalesced" in await engine.notify({"source_id": "graph:mail:fresh2"})
    telegram.send.assert_awaited_once()


async def test_recovered_morning_job_does_not_compete_with_current_work(db):
    inference = SimpleNamespace(chat=AsyncMock())
    engine = Proactive(SimpleNamespace(), db, inference, None, None, None)
    result = await engine.morning_prepare(
        {"scheduled_at": (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()}
    )
    assert "skipped" in result
    inference.chat.assert_not_awaited()
