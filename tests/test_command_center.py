from datetime import datetime, timedelta, timezone
from uuid import uuid4

from psycopg.types.json import Jsonb

from jarvis.command_center import answer, build, intent, read, revision


def commitment(db, owner="Demo", beneficiary="Manager", status="open", due=None, confidence=1):
    identifier = uuid4()
    db.execute(
        "INSERT INTO commitments(id,owner,beneficiary,text,due_at,confidence,status,source_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
        (
            identifier,
            owner,
            beneficiary,
            "Provide the confirmed project schedule",
            due,
            confidence,
            status,
            "synthetic:" + str(identifier),
        ),
    )
    return str(identifier)


def record(db, identifier, kind="email", when=None, meta=None):
    db.execute(
        "INSERT INTO records(id,kind,provider,title,body,metadata,occurred_at) VALUES (%s,%s,'synthetic','Equipment planning','Please review the proposal.',%s,%s)",
        (identifier, kind, Jsonb(meta or {}), when or datetime.now(timezone.utc)),
    )


def activate(db):
    db.set_state("feature.HPOS_COMMAND_CENTER", True)
    db.set_state("outlook.last_sync", datetime.now(timezone.utc).isoformat())


def test_confirmed_ownership_risk_and_candidates_stay_separate(db):
    activate(db)
    now = datetime.now(timezone.utc)
    mine = commitment(db, due=now - timedelta(hours=1))
    theirs = commitment(db, owner="Matt", beneficiary="Demo", due=now - timedelta(hours=2))
    candidate = commitment(db, status="candidate", due=now - timedelta(days=2), confidence=0.99)
    unknown = commitment(db, owner="unknown", beneficiary="Demo", due=now - timedelta(days=1))
    data = build(db)
    assert mine in {r["id"] for r in data["categories"]["waiting_on_me"]}
    assert theirs in {r["id"] for r in data["categories"]["waiting_on_them"]}
    assert {r["id"] for r in data["categories"]["at_risk"]} == {mine, theirs}
    assert {mine, theirs} <= {r["id"] for r in data["categories"]["today"]}
    assert not {candidate, unknown} & {r["id"] for r in data["categories"]["waiting_on_me"]}
    assert data["candidate_commitments"] == 1


def test_keyword_priority_never_becomes_a_confirmed_promise(db):
    activate(db)
    record(
        db,
        "vip-mail",
        meta={
            "triage": {"priority": "high", "actionable": True, "factors": ["Sender is a confirmed VIP"]},
            "isRead": False,
        },
    )
    data = build(db)
    row = data["categories"]["waiting_on_me"][0]
    assert row["confidence"] == "candidate" and not data["categories"]["at_risk"]
    assert "not a confirmed obligation" in data["categories"]["today"][0]["reason"]


def test_cache_invalidation_and_completed_work_cannot_survive_stale_view(db):
    activate(db)
    identifier = commitment(db)
    record(db, "mail", meta={"triage": {"priority": "high", "actionable": True}})
    data = build(db)
    assert not read(db)["stale"]
    db.execute("UPDATE commitments SET status='done' WHERE id=%s", (identifier,))
    db.execute("UPDATE records SET deleted=true WHERE id='mail'")
    assert revision(db) > data["source_revision"]
    current = read(db)
    assert current["stale"]
    assert not current["categories"]["waiting_on_me"]


def test_owner_correction_is_not_repeated_from_cache(db):
    activate(db)
    identifier = commitment(db)
    build(db)
    db.execute("UPDATE commitments SET owner='Someone else' WHERE id=%s", (identifier,))
    assert not read(db)["categories"]["waiting_on_me"]


def test_conflicts_exclude_cancelled_and_free_events(db):
    activate(db)
    when = datetime.now(timezone.utc) + timedelta(hours=1)
    meta = {"end": {"dateTime": (when + timedelta(hours=2)).isoformat()}, "showAs": "busy"}
    record(db, "one", "event", when, meta)
    record(db, "two", "event", when + timedelta(minutes=15), meta)
    record(db, "cancelled", "event", when, {**meta, "isCancelled": True})
    record(db, "free", "event", when, {**meta, "showAs": "free"})
    data = build(db)
    assert {r["id"] for r in data["categories"]["at_risk"]} == {"one", "two"}
    assert not answer(db, "What promises am I at risk of breaking?", "qa")["sources"]


def test_feature_off_stale_disclosure_and_specific_mail_routing(db):
    assert read(db) == {"enabled": False}
    assert intent("What actually needs my attention today?") == "today"
    assert intent("What am I waiting on?") == "waiting_on_them"
    assert intent("What do I owe Matt?") is None
    assert intent("Summarize emails from Stephanie this week") is None
    assert intent("Send the at risk summary to my manager") is None
    activate(db)
    assert not read(db)["ready"]
    commitment(db)
    build(db)
    db.set_state("outlook.error", {"error_class": "SyntheticOffline"})
    result = answer(db, "What is waiting on me?", "qa")
    assert result["sources"] and "Outlook needs attention" in result["reply"]
    assert db.get_state("conversation.last_briefing_sources.qa") == result["sources"]


def test_drafts_are_prepared_not_sent_and_no_external_jobs_created(db):
    activate(db)
    record(db, "draft:one", "draft", meta={"source_id": "source", "status": "internal"})
    data = build(db)
    assert data["categories"]["prepared"][0]["reason"] == "Internal draft ready for review; not sent."
    assert db.one("SELECT count(*) AS n FROM jobs")["n"] == 0
    assert db.one("SELECT count(*) AS n FROM approvals")["n"] == 0


def test_current_work_filters_legacy_reports_and_orders_recent_mail_first(db):
    activate(db)
    now = datetime.now(timezone.utc)
    for identifier, title, age, vip in [
        ("old", "Equipment planning", 3, False),
        ("new", "Equipment planning", 1, False),
        ("report", "Weekly unbooked backlog", 0, False),
        ("vip-report", "Sales report for review", 2, True),
    ]:
        record(
            db,
            identifier,
            when=now - timedelta(days=age),
            meta={
                "triage": {
                    "priority": "high",
                    "actionable": True,
                    "factors": ["Sender is a confirmed VIP"] if vip else ["Explicit request"],
                }
            },
        )
        db.execute("UPDATE records SET title=%s WHERE id=%s", (title, identifier))
    data = build(db)
    assert [r["id"] for r in data["categories"]["today"]] == ["vip-report", "new", "old"]
    assert "report" not in {r["id"] for r in data["categories"]["waiting_on_me"]}
    assert data["category_order"] == [
        "today",
        "waiting_on_me",
        "waiting_on_them",
        "at_risk",
        "upcoming",
        "prepared",
    ]
