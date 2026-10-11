import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from psycopg.types.json import Jsonb

from jarvis.extraction import source_digest
from jarvis.meeting_intelligence import FLAG, context, evidence, importance
from jarvis.meetings import meeting_context, prepare_meeting
from jarvis.proactive import Proactive
from jarvis.source_insights import read


def event(db, attendees=None, title="LHA review"):
    db.set_state(FLAG, True)
    db.set_state("preferences.work_priorities", {"topics": ["LHA"]})
    db.execute(
        "INSERT INTO records(id,kind,provider,title,body,metadata,occurred_at) VALUES('event','event','synthetic',%s,'Review current work',%s,%s)",
        (
            title,
            Jsonb({"attendees": attendees or [], "attendees_complete": True}),
            datetime.now(timezone.utc) + timedelta(hours=2),
        ),
    )
    return db.one("SELECT * FROM records WHERE id='event'")


def model():
    return SimpleNamespace(
        chat=AsyncMock(
            return_value={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "brief": "Review current evidence. Confirm the next milestone.",
                                    "talking_points": ["Which outcome matters?"],
                                }
                            )
                        }
                    }
                ]
            }
        )
    )


def test_importance_does_not_treat_every_invitation_or_demo_as_vip_signal(db):
    e = event(
        db, title="Routine check-in", attendees=[{"emailAddress": {"address": "demo@example.invalid"}}]
    )
    db.execute("INSERT INTO people(id,name,vip) VALUES('demo@example.invalid','Demo',true)")
    assert not importance(db, e)
    e["title"] = "LHA review"
    assert importance(db, e) == ["Priority topic: LHA"]
    e["metadata"]["isCancelled"] = True
    assert not importance(db, e)


def test_meeting_commitments_use_unique_attendees_and_ignore_candidates(db):
    e = event(
        db,
        attendees=[
            {"emailAddress": {"address": "matt.one@example.invalid", "name": "Matt One"}},
            {"emailAddress": {"address": "demo@example.invalid"}},
        ],
    )
    for identifier, name in [
        ("matt.one@example.invalid", "Matt One"),
        ("matt.two@example.invalid", "Matt Two"),
        ("demo@example.invalid", "Demo User"),
    ]:
        db.execute("INSERT INTO people(id,name) VALUES(%s,%s)", (identifier, name))
    for owner, beneficiary, status in [
        ("Demo", "Matt One", "open"),
        ("Demo", "Matt", "open"),
        ("Demo", "Matt Two", "open"),
        ("Demo", "Matt One", "candidate"),
    ]:
        db.execute(
            "INSERT INTO commitments(id,owner,beneficiary,text,status,confidence,source_id) VALUES(%s,%s,%s,%s,%s,0.9,'unrelated')",
            (uuid4(), owner, beneficiary, beneficiary + status, status),
        )
    data = context(db, e)
    assert len(data["commitments"]) == 1 and data["commitments"][0]["beneficiary"] == "Matt One"
    assert all(p["id"] != "demo@example.invalid" for p in data["people"])


async def test_cache_survives_unchanged_sync_but_invalidates_changed_source(db):
    event(db)
    db.execute(
        "INSERT INTO records(id,kind,provider,title,body,occurred_at) VALUES('mail','email','synthetic','LHA milestone','Current request',now())"
    )
    inference = model()
    assert (await prepare_meeting(db, inference))["status"] == "prepared"
    db.execute("UPDATE records SET updated_at=now() WHERE id='event'")
    assert (await prepare_meeting(db, inference))["cached"]
    inference.chat.assert_awaited_once()
    db.execute("UPDATE records SET body='Changed request' WHERE id='mail'")
    await prepare_meeting(db, inference)
    assert inference.chat.await_count == 2


def test_linked_transcript_decisions_retain_digest_and_removal_provenance(db):
    e = event(db)
    db.execute(
        "INSERT INTO records(id,kind,provider,title,body,metadata) VALUES('transcript','transcript','synthetic','Copilot notes','Decision: validate the source.',%s)",
        (Jsonb({"source_id": "event"}),),
    )
    row = db.one("SELECT * FROM records WHERE id='transcript'")
    db.set_state(
        "extracted.transcript",
        {
            "source_digest": source_digest(row),
            "summary": "Review notes",
            "decisions": ["Validate the source"],
            "extraction_coverage": "stored_body",
        },
    )
    data = context(db, e)
    assert data["decisions"][0]["source_id"] == "transcript"
    assert data["decisions"][0]["status"] == "extracted candidate"
    assert evidence(data)[0]["extracted_decision_candidates"]
    db.execute("UPDATE records SET body='Replaced evidence' WHERE id='transcript'")
    assert read(db, "transcript")["status"] == "pending" and not context(db, e)["decisions"]
    db.execute("UPDATE records SET deleted=true WHERE id='transcript'")
    with pytest.raises(LookupError):
        read(db, "transcript")
    assert not db.one("SELECT * FROM approvals")


async def test_short_extraction_cannot_persist_after_source_changes(db):
    db.execute(
        "INSERT INTO records(id,kind,provider,title,body) VALUES('source','transcript','synthetic','Notes','Original')"
    )

    async def summarize(*args, **kwargs):
        db.execute("UPDATE records SET body='Changed' WHERE id='source'")
        return json.dumps({"summary": "Old", "decisions": ["Old decision"], "commitments": []})

    worker = Proactive(SimpleNamespace(), db, None, None, None, None)
    worker.summarize = summarize
    result = await worker.extract_source({"source_id": "source"})
    assert result["skipped"] == "Source changed during extraction"
    assert db.get_state("extracted.source") is None


def test_bounded_meeting_evidence_and_disabled_fallback(db):
    e = event(db)
    for i in range(30):
        db.execute(
            "INSERT INTO records(id,kind,provider,title,body,occurred_at) VALUES(%s,'email','synthetic','LHA evidence',%s,now())",
            (str(i), "Evidence " * 1000),
        )
    payload, sources = evidence(context(db, e))
    assert len(sources) == 7 and sum(len(s["excerpt"]) for s in sources) < 6000
    db.set_state(FLAG, False)
    assert "projects" not in meeting_context(db)
    assert payload["coverage"] and not db.one("SELECT * FROM jobs")


def test_meeting_excerpts_preserve_latest_update_and_original_request(db):
    e = event(db)
    db.execute(
        "INSERT INTO records(id,kind,provider,title,body,occurred_at) VALUES('thread','email','synthetic','LHA timeline',%s,now())",
        ("Latest update. " + ("middle " * 1000) + " Original request: validate the deadline.",),
    )
    _, sources = evidence(context(db, e))
    assert "Latest update" in sources[1]["excerpt"]
    assert "validate the deadline" in sources[1]["excerpt"]


@pytest.mark.parametrize("enabled", [False, True])
def test_brief_calendar_followup_keeps_related_mail_with_either_context_shape(db, enabled):
    from datetime import timedelta

    from jarvis.calendar_voice import ZONE, calendar_brief

    e = event(db, title="LHA timeline")
    db.set_state(FLAG, enabled)
    db.execute(
        "INSERT INTO records(id,kind,provider,title,body,occurred_at) VALUES('thread','email','synthetic','LHA timeline','Original request: validate the deadline.',now())"
    )
    start = e["occurred_at"].astimezone(ZONE).replace(hour=0, minute=0, second=0, microsecond=0)
    result = calendar_brief(db, "qa-followup", (start, start + timedelta(days=1)), prepare=True)
    mail = next(s for s in result["sources"] if s["id"] == "thread")
    assert "validate the deadline" in mail["excerpt"]
    assert "related threads" in result["reply"]
