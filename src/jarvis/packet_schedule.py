"""Demo's narrow weekday self-delivery grant; never a general mail permission."""

from datetime import datetime, time, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

from psycopg.types.json import Jsonb

from .content_packets import RECIPIENT, due_day
from .inference import InferenceDeferred
from .packet_delivery import proposal
from .policy import Approvals, digest

POLICY = "JARVIS-LINKEDIN-SELF-DELIVERY-20260921-V1"
STATE = "content_packets.delivery_grant"


def grant_status(db):
    value = db.get_state(STATE, {})
    authorized = (
        value.get("policy_id") == POLICY
        and value.get("authorized") is True
        and value.get("recipient") == RECIPIENT
        and value.get("scope") == "weekday_public_research_packet_only"
    )
    return {
        "authorized": authorized,
        "validated": authorized and value.get("validation_pending") is False,
        "enabled": authorized and value.get("enabled") is True and value.get("validation_pending") is False,
        "policy_id": POLICY,
    }


def eligible(db, edition, now):
    return (
        grant_status(db)["enabled"]
        and db.get_state("content_packets.enabled", False)
        and db.get_state("setup.enabled.outlook", False)
        and not db.get_state("demo_mode", False)
        and str(edition) == due_day(now)
    )


def schedule(db, jobs, now):
    edition = due_day(now)
    if not edition or not eligible(db, edition, now):
        return
    row = db.one("SELECT status FROM content_packets WHERE edition=%s", (edition,))
    if (
        row
        and row["status"] == "ready"
        and not db.one("SELECT edition FROM packet_deliveries WHERE edition=%s", (edition,))
    ):
        jobs.enqueue("packet_deliver", "packet-deliver:" + edition, now, {"edition": edition}, priority=6)


async def deliver(settings, db, payload, transport, now=None):
    clock = (lambda: now) if now else (lambda: datetime.now(timezone.utc))
    current = clock()
    edition = payload.get("edition")
    if str(edition) != due_day(current):
        return {"skipped": True, "reason": "outside_authorized_day"}
    if not eligible(db, edition, current):
        raise InferenceDeferred("Delivery is paused")
    row = db.one("SELECT * FROM content_packets WHERE edition=%s", (edition,))
    if not row or row["status"] != "ready":
        raise ValueError("Only a completed public research packet can be delivered")
    mail = proposal(settings, row)
    action = uuid4()
    expires = datetime.combine(
        current.astimezone(ZoneInfo("America/Denver")).date(), time(23, 59, 59), ZoneInfo("America/Denver")
    )
    with db.transaction() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("packet-delivery:" + edition,))
        previous = conn.execute("SELECT * FROM packet_deliveries WHERE edition=%s", (edition,)).fetchone()
        if previous:
            return {"status": previous["status"], "duplicate_refused": True}
        # Never create a second automatic action if an exact proposal already exists.
        existing = conn.execute(
            "SELECT id,status FROM approvals WHERE kind='email.send' AND payload->'packet'->>'edition'=%s LIMIT 1",
            (edition,),
        ).fetchone()
        if existing:
            return {"status": existing["status"], "existing_action": str(existing["id"])}
        conn.execute(
            "INSERT INTO approvals(id,kind,payload,digest,status,expires_at,actor,decided_at) VALUES(%s,'email.send',%s,%s,'approved',%s,%s,now())",
            (action, Jsonb(mail), digest("email.send", mail), expires, POLICY),
        )
        conn.execute(
            "INSERT INTO packet_deliveries(edition,action_id,status,policy_id) VALUES(%s,%s,'reserved',%s)",
            (edition, action, POLICY),
        )
        db.audit(
            "packet_delivery.authorized",
            action,
            {"policy_id": POLICY, "edition": edition, "sha256": mail["packet"]["sha256"]},
            actor="demo",
            source="standing_grant",
            conn=conn,
        )
    try:
        # Recheck after durable reservation, before any external operation.
        if not eligible(db, edition, clock()):
            raise ValueError("Delivery permission was paused")
        result = await Approvals(db).execute(action, {"email.send": transport})
    except BaseException:
        db.execute(
            "UPDATE packet_deliveries SET status='uncertain',updated_at=now() WHERE edition=%s", (edition,)
        )
        raise
    db.execute(
        "UPDATE packet_deliveries SET status='succeeded',updated_at=now() WHERE edition=%s", (edition,)
    )
    return result


def recover(db):
    db.execute(
        "UPDATE packet_deliveries d SET status=CASE WHEN a.status='succeeded' THEN 'succeeded' ELSE 'uncertain' END,updated_at=now() FROM approvals a WHERE d.action_id=a.id AND d.status='reserved'"
    )
