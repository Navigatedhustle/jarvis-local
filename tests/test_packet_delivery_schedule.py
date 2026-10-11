from copy import deepcopy
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from jarvis import content_packets, packet_schedule, policy
from jarvis.config import Settings
from jarvis.inference import InferenceDeferred
from jarvis.policy import Approvals, PolicyError
from jarvis.scheduler import Jobs

NOW = datetime(2026, 9, 21, 14, tzinfo=timezone.utc)
GRANT = {
    "policy_id": packet_schedule.POLICY,
    "authorized": True,
    "enabled": True,
    "validation_pending": False,
    "recipient": content_packets.RECIPIENT,
    "scope": "weekday_public_research_packet_only",
}


@pytest.fixture(autouse=True)
def approval_clock_matches_fixed_delivery_day(monkeypatch):
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)

    monkeypatch.setattr(policy, "datetime", FixedDateTime)


def fixture_packet(db, tmp_path):
    settings = Settings(data_dir=tmp_path)
    db.set_state(packet_schedule.STATE, GRANT)
    db.set_state("content_packets.enabled", True)
    db.set_state("setup.enabled.outlook", True)
    content_packets.admit(db, Jobs(db), "2026-09-21", NOW)
    path = content_packets.directory(settings, "2026-09-21")
    path.mkdir(parents=True)
    (path / "review.pdf").write_bytes(b"%PDF-1.4 synthetic fixture")
    content_packets.checkpoint(
        db,
        "2026-09-21",
        "ready",
        {"pdf": {"sha256": content_packets.digest_file(path / "review.pdf")}},
        "ready",
    )
    return settings


def test_grant_requires_exact_scope_and_successful_validation(db):
    for field, value in [
        ("authorized", False),
        ("recipient", "other@example.invalid"),
        ("scope", "any_mail"),
        ("policy_id", "unknown"),
        ("validation_pending", True),
        ("enabled", False),
    ]:
        changed = deepcopy(GRANT)
        changed[field] = value
        db.set_state(packet_schedule.STATE, changed)
        assert not packet_schedule.grant_status(db)["enabled"]


async def test_one_exact_daily_send_and_durable_duplicate_protection(db, tmp_path):
    settings = fixture_packet(db, tmp_path)
    transport = AsyncMock(return_value={"sent": True, "verified": True})
    await packet_schedule.deliver(settings, db, {"edition": "2026-09-21"}, transport, NOW)
    assert transport.await_count == 1
    payload, action = transport.call_args.args
    assert payload["message"]["toRecipients"] == [{"emailAddress": {"address": content_packets.RECIPIENT}}]
    assert Approvals(db).get(action)["status"] == "succeeded"
    assert (await packet_schedule.deliver(settings, db, {"edition": "2026-09-21"}, transport, NOW))[
        "duplicate_refused"
    ]
    assert transport.await_count == 1


async def test_uncertain_send_never_retries_after_restart(db, tmp_path):
    settings = fixture_packet(db, tmp_path)
    transport = AsyncMock(side_effect=TimeoutError())
    with pytest.raises(PolicyError):
        await packet_schedule.deliver(settings, db, {"edition": "2026-09-21"}, transport, NOW)
    Approvals(db).recover()
    result = await packet_schedule.deliver(settings, db, {"edition": "2026-09-21"}, transport, NOW)
    assert result["status"] == "uncertain"
    assert transport.await_count == 1


@pytest.mark.parametrize(
    "at",
    [
        datetime(2026, 9, 21, 10, tzinfo=timezone.utc),
        datetime(2026, 9, 22, 14, tzinfo=timezone.utc),
        datetime(2026, 9, 26, 14, tzinfo=timezone.utc),
    ],
)
async def test_no_early_weekend_or_stale_delivery(db, tmp_path, at):
    settings = fixture_packet(db, tmp_path)
    transport = AsyncMock()
    assert (await packet_schedule.deliver(settings, db, {"edition": "2026-09-21"}, transport, at))["skipped"]
    transport.assert_not_awaited()


@pytest.mark.parametrize(
    "key,value", [("demo_mode", True), ("content_packets.enabled", False), ("setup.enabled.outlook", False)]
)
async def test_pause_blocks_delivery_before_approval_creation(db, tmp_path, key, value):
    settings = fixture_packet(db, tmp_path)
    db.set_state(key, value)
    with pytest.raises(InferenceDeferred):
        await packet_schedule.deliver(settings, db, {"edition": "2026-09-21"}, AsyncMock(), NOW)
    assert not db.query("SELECT * FROM approvals")


async def test_changed_pdf_is_rejected_before_action(db, tmp_path):
    settings = fixture_packet(db, tmp_path)
    (content_packets.directory(settings, "2026-09-21") / "review.pdf").write_bytes(b"changed")
    with pytest.raises(ValueError):
        await packet_schedule.deliver(settings, db, {"edition": "2026-09-21"}, AsyncMock(), NOW)
    assert not db.query("SELECT * FROM approvals")


async def test_existing_denial_is_not_overridden_by_recurring_grant(db, tmp_path):
    from jarvis.packet_delivery import proposal
    from jarvis.policy import Actor

    settings = fixture_packet(db, tmp_path)
    row = db.one("SELECT * FROM content_packets WHERE edition='2026-09-21'")
    approvals = Approvals(db)
    action = approvals.propose("email.send", proposal(settings, row))
    approvals.decide(action["id"], "DENY", action["digest"], Actor("demo", "local_cli"))
    transport = AsyncMock()
    result = await packet_schedule.deliver(settings, db, {"edition": "2026-09-21"}, transport, NOW)
    assert result["status"] == "denied"
    transport.assert_not_awaited()


def test_restart_reserved_delivery_requires_reconciliation(db, tmp_path):
    from jarvis.packet_delivery import proposal
    from jarvis.policy import Actor

    settings = fixture_packet(db, tmp_path)
    row = db.one("SELECT * FROM content_packets WHERE edition='2026-09-21'")
    approvals = Approvals(db)
    action = approvals.propose("email.send", proposal(settings, row))
    approvals.decide(action["id"], "APPROVE", action["digest"], Actor("demo", "local_cli"))
    db.execute("UPDATE approvals SET status='executing' WHERE id=%s", (action["id"],))
    db.execute(
        "INSERT INTO packet_deliveries(edition,action_id,status,policy_id) VALUES('2026-09-21',%s,'reserved',%s)",
        (action["id"], packet_schedule.POLICY),
    )
    approvals.recover()
    packet_schedule.recover(db)
    assert db.one("SELECT status FROM packet_deliveries")["status"] == "uncertain"
    assert approvals.get(action["id"])["status"] == "uncertain"


def test_delivery_controls_cannot_create_a_grant(db, monkeypatch):
    from fastapi.testclient import TestClient

    from jarvis import api

    monkeypatch.setattr(api, "db", db)
    monkeypatch.setattr(api, "settings", Settings(api_token="p" * 40))
    client = TestClient(api.app)
    route = "/content-packets/delivery-preferences"
    assert client.post(route, json={"enabled": True}).status_code == 401
    headers = {"Authorization": "Bearer " + "p" * 40}
    assert client.post(route, headers=headers, json={"enabled": True}).status_code == 409
    db.set_state(packet_schedule.STATE, GRANT)
    assert client.post(route, headers=headers, json={"enabled": False}).json()["enabled"] is False
    assert client.post(route, headers=headers, json={"enabled": True}).json()["enabled"] is True


def test_schedule_deduplicates_and_requires_ready_packet(db, tmp_path):
    fixture_packet(db, tmp_path)
    jobs = Jobs(db)
    packet_schedule.schedule(db, jobs, NOW)
    packet_schedule.schedule(db, jobs, NOW)
    assert len(db.query("SELECT * FROM jobs WHERE kind='packet_deliver'")) == 1
    db.set_state("demo_mode", True)
    packet_schedule.schedule(db, jobs, NOW)
    assert len(db.query("SELECT * FROM jobs WHERE kind='packet_deliver'")) == 1
