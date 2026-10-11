from datetime import datetime, timedelta, timezone

from test_command_center import activate, commitment, record

from jarvis import priorities
from jarvis.command_center import build, read


def row(identifier, kind="email", priority=3, due=None, **extra):
    return {
        "id": identifier,
        "source_id": identifier,
        "kind": kind,
        "priority": priority,
        "title": identifier,
        "due_at": due.isoformat() if due else None,
        "confidence": "candidate" if kind == "email" else "confirmed",
        "reason": "Source evidence",
        **extra,
    }


def test_rank_deduplicate_and_do_not_fabricate_five():
    now = datetime.now(timezone.utc)
    mail = row("request", priority=2, actionable=True)
    soon = row("meeting", "event", 4, now + timedelta(hours=1))
    categories = {
        "today": [mail, soon, row("routine", priority=6)],
        "waiting_on_me": [mail],
        "at_risk": [row("someone-elses-promise", "commitment", 0)],
        "upcoming": [
            row("past", "event", 4, now - timedelta(hours=1)),
            row("far", "event", 4, now + timedelta(days=4)),
        ],
    }
    selected = priorities.select(categories, now)
    assert [r["id"] for r in selected] == ["meeting", "request"]
    assert selected[1]["action"] == "Review and respond" and selected[1]["confidence"] == "candidate"
    assert len(priorities.select({"today": [row(str(i)) for i in range(10)]}, now)) == 5


def test_calendar_conflicts_do_not_crowd_out_the_entire_brief():
    now = datetime.now(timezone.utc)
    events = [row(str(i), "event", 1, now + timedelta(hours=1)) for i in range(6)]
    mails = [row("mail" + str(i), priority=2, actionable=True) for i in range(4)]
    result = priorities.select({"at_risk": events, "today": [*events, *mails]}, now)
    assert len(result) == 5
    assert len(result[0]["conflict_sources"]) == 6
    assert result[0]["action"] == "Resolve calendar overlaps"
    assert sum(r["kind"] == "email" for r in result) == 4


def test_brief_persists_six_hours_and_refreshes_on_boundary(db):
    activate(db)
    now = datetime.now(timezone.utc)
    record(db, "mail", when=now, meta={"triage": {"priority": "high", "actionable": True}})
    build(db, now)
    first = db.get_state(priorities.KEY)
    assert len(first["items"]) == 1 and not priorities.due(db, now + timedelta(hours=5, minutes=59))
    record(
        db,
        "newer",
        when=now + timedelta(seconds=1),
        meta={"triage": {"priority": "high", "actionable": True}},
    )
    build(db, now + timedelta(hours=1))
    assert db.get_state(priorities.KEY) == first
    assert priorities.due(db, now + timedelta(hours=6))
    build(db, now + timedelta(hours=6))
    assert db.get_state(priorities.KEY)["built_at"] != first["built_at"]
    assert len(db.get_state(priorities.KEY)["items"]) == 2
    assert db.one("SELECT count(*) n FROM approvals")["n"] == 0
    assert db.one("SELECT count(*) n FROM jobs")["n"] == 0


def test_removed_changed_completed_and_past_sources_disappear_immediately(db):
    activate(db)
    now = datetime.now(timezone.utc)
    record(db, "mail", when=now, meta={"triage": {"priority": "high", "actionable": True}})
    record(db, "event", "event", now + timedelta(minutes=10), {"showAs": "busy"})
    mine = commitment(db, due=now + timedelta(hours=1))
    build(db)
    assert len(read(db)["priorities"]["items"]) == 3
    db.execute("UPDATE records SET deleted=true WHERE id='mail'")
    db.execute("UPDATE commitments SET status='done' WHERE id=%s", (mine,))
    assert read(db, now + timedelta(minutes=11))["priorities"]["items"] == []


def test_declined_cancelled_free_and_all_day_are_not_meeting_todos(db):
    activate(db)
    now = datetime.now(timezone.utc)
    for i, meta in enumerate(
        [
            {"isCancelled": True},
            {"showAs": "free"},
            {"isAllDay": True},
            {"responseStatus": {"response": "declined"}},
        ]
    ):
        record(db, str(i), "event", now + timedelta(hours=1), meta)
    build(db)
    assert read(db)["priorities"]["items"] == []


def test_stale_and_disconnected_brief_is_not_labelled_current(db):
    activate(db)
    build(db)
    later = datetime.now(timezone.utc) + timedelta(hours=7)
    result = read(db, later)
    assert result["priorities"]["stale"] and result["outlook_stale"]
    db.set_state("setup.enabled.outlook", False)
    assert not read(db)["outlook_enabled"]


def test_new_sent_reply_removes_email_without_waiting_six_hours(db):
    activate(db)
    now = datetime.now(timezone.utc)
    record(
        db,
        "received",
        when=now,
        meta={"conversationId": "thread", "triage": {"priority": "high", "actionable": True}},
    )
    build(db)
    assert len(read(db)["priorities"]["items"]) == 1
    record(
        db,
        "reply",
        when=now + timedelta(seconds=1),
        meta={"conversationId": "thread", "folder_role": "sentitems"},
    )
    assert read(db)["priorities"]["items"] == []
