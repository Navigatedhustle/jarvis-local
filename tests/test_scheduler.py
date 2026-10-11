from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from jarvis.inference import InferenceDeferred
from jarvis.scheduler import Jobs, communication_allowed, next_morning


def test_failure_diagnostics_never_include_message_source_or_local_secrets():
    import json

    from jarvis.scheduler import failure_details

    private = "synthetic-mail-and-credential-must-not-leak"
    try:
        raise Exception("Cannot find object to method: https://private.invalid/" + private)
    except Exception as error:
        detail = failure_details(error)
    assert private not in json.dumps(detail) and "https://" not in json.dumps(detail)
    assert detail["browser_protocol_error"] and detail["error_class"] == "Exception"
    assert detail["frames"][-1]["file"] == "test_scheduler.py"
    assert set(detail["frames"][-1]) == {"file", "function", "line"}


def test_no_sunday_morning_and_dst():
    saturday_after_call = datetime(2026, 9, 19, 14, tzinfo=timezone.utc)
    result = next_morning(saturday_after_call).astimezone(ZoneInfo("America/Denver"))
    assert result.weekday() == 0 and result.hour == 7
    dst = next_morning(datetime(2026, 10, 31, 15, tzinfo=timezone.utc))
    assert dst.astimezone(ZoneInfo("America/Denver")).hour == 7


def test_model_failure_diagnostics_allow_only_fixed_categories():
    from jarvis.inference import ModelUnavailable
    from jarvis.scheduler import failure_details

    assert (
        failure_details(ModelUnavailable(reason="transport_error"))["model_failure_category"]
        == "transport_error"
    )
    assert failure_details(ModelUnavailable(reason="private credential"))["model_failure_category"] is None


def test_quiet_rules_expire_and_demo_mode_wins(db):
    now = datetime.now(timezone.utc)
    db.set_state("communication_rule", {"mode": "text_only", "until": (now + timedelta(hours=1)).isoformat()})
    assert not communication_allowed(db, "phone", now)
    assert communication_allowed(db, "telegram", now)
    assert communication_allowed(db, "phone", now + timedelta(hours=2))
    db.set_state("demo_mode", True)
    assert not communication_allowed(db, "telegram", now)


def test_jobs_dedupe_and_recovery(db):
    jobs = Jobs(db)
    for _ in range(2):
        jobs.enqueue("morning_call", "synthetic-call", datetime.now(timezone.utc))
    assert jobs.claim()["kind"] == "morning_call"
    assert jobs.claim() is None
    jobs.recover()
    assert db.one("SELECT status FROM jobs")["status"] == "uncertain"


def test_fresh_mail_does_not_starve_behind_historical_enrichment(db):
    jobs = Jobs(db)
    now = datetime.now(timezone.utc)
    jobs.enqueue("extract_chunk", "old-source", now - timedelta(hours=1), priority=8)
    jobs.enqueue("outlook_sync", "sync", now, priority=9)
    jobs.enqueue("outlook_sync", "sync", now, priority=7)
    assert jobs.claim()["kind"] == "outlook_sync"


def test_read_worker_cannot_claim_outbound_or_reasoning_jobs(db):
    jobs = Jobs(db)
    now = datetime.now(timezone.utc)
    jobs.enqueue("important_email", "draft", now, priority=6)
    jobs.enqueue("morning_call", "call", now, priority=4)
    jobs.enqueue("outlook_sync", "sync", now, priority=7)
    assert jobs.claim(only="outlook_sync")["kind"] == "outlook_sync"
    assert jobs.claim(only="outlook_sync") is None
    assert jobs.claim(exclude="outlook_sync")["kind"] == "morning_call"
    assert jobs.claim(exclude="outlook_sync")["kind"] == "important_email"


def test_meeting_notification_does_not_replay_after_crash(db):
    jobs = Jobs(db)
    jobs.enqueue("meeting_prepare", "synthetic-meeting", datetime.now(timezone.utc))
    jobs.claim()
    jobs.recover()
    assert db.one("SELECT status FROM jobs")["status"] == "uncertain"
    assert jobs.claim() is None


async def test_live_interaction_defers_internal_extraction_without_exhausting_retries(db):
    jobs = Jobs(db)
    jobs.enqueue("extract_source", "deferred-source", datetime.now(timezone.utc))

    async def defer(_):
        raise InferenceDeferred()

    await jobs.run_one({"extract_source": defer})
    row = db.one("SELECT status,attempts,error FROM jobs")
    assert row == {"status": "pending", "attempts": 0, "error": None}


def test_bounded_memory_maintenance_does_not_starve_behind_old_extraction(db):
    db.set_state("feature.HPOS_WORKLOAD_GUARD", True)
    jobs = Jobs(db)
    now = datetime.now(timezone.utc)
    jobs.enqueue("extract_source", "old-extraction", now - timedelta(days=1), priority=6)
    jobs.enqueue("memory_consolidate", "bounded-consolidation", now, priority=6)
    jobs.enqueue("memory_views", "bounded-view", now, priority=6)
    jobs.enqueue("important_email", "important", now, priority=4)
    assert [jobs.claim()["kind"] for _ in range(4)] == [
        "important_email",
        "memory_views",
        "memory_consolidate",
        "extract_source",
    ]


async def test_shutdown_transport_error_does_not_mark_outlook_unhealthy(db):
    import asyncio

    import pytest

    jobs = Jobs(db)
    jobs.enqueue("outlook_sync", "shutdown-read", datetime.now(timezone.utc))

    async def transport(_):
        asyncio.current_task().cancel()
        try:
            await asyncio.sleep(0)
        except asyncio.CancelledError:
            raise Exception("Connection closed while reading from the driver") from None

    pending = asyncio.create_task(jobs.run_one({"outlook_sync": transport}))
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert db.get_state("outlook.error") is None
    assert db.one("SELECT status FROM jobs")["status"] == "running"
    assert not db.one("SELECT * FROM audit WHERE action='job.pending'")
    jobs.recover()
    assert db.one("SELECT status FROM jobs")["status"] == "pending"
