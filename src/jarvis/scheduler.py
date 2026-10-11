import asyncio
import re
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from psycopg.types.json import Jsonb

from .inference import InferenceDeferred
from .workload import PRIORITIES, RETRYABLE_INTERNAL, work_priority


def failure_details(error):
    """Locate failures without retaining exception messages, URLs, source lines or locals."""
    categories = {
        "connection closed": "driver_connection_closed",
        "target page, context or browser has been closed": "target_closed",
        "object has been collected": "object_collected",
        "execution context was destroyed": "navigation_context_destroyed",
        "connection disposed": "connection_disposed",
    }
    return {
        "error_class": type(error).__name__,
        "model_failure_category": getattr(error, "reason", None)
        if getattr(error, "reason", None)
        in {"generation_limit_or_filter", "incomplete_stream", "transport_error", "invalid_response"}
        else None,
        "frames": [
            {"file": Path(frame.filename).name, "function": frame.name, "line": frame.lineno}
            for frame in traceback.extract_tb(error.__traceback__)[-5:]
        ],
        "browser_protocol_error": str(error).startswith(("Cannot find object to ", "Unknown new child:")),
        "browser_failure_category": next(
            (category for pattern, category in categories.items() if pattern in str(error).lower()), None
        ),
    }


def next_morning(now, zone="America/Denver"):
    local = now.astimezone(ZoneInfo(zone))
    candidate = local.replace(hour=7, minute=0, second=0, microsecond=0)
    if candidate <= local:
        candidate += timedelta(days=1)
    while candidate.weekday() == 6:
        candidate += timedelta(days=1)
    return candidate.astimezone(timezone.utc)


def communication_allowed(db, channel, now=None):
    now = now or datetime.now(timezone.utc)
    if db.get_state("demo_mode", False):
        return False
    rule = db.get_state("communication_rule")
    if rule and datetime.fromisoformat(rule["until"]) > now:
        if rule["mode"] == "quiet" or (rule["mode"] == "text_only" and channel == "phone"):
            return False
    return True


def parse_communication_rule(text, now=None):
    now = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo("America/Denver"))
    normalized = text.strip().lower().rstrip(".")
    if normalized in {"text only today", "quiet until tomorrow"}:
        until = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return {"mode": "text_only" if normalized.startswith("text") else "quiet", "until": until.isoformat()}
    match = re.fullmatch(r"no calls until (\d{1,2})(?::(\d{2}))?\s*(am|pm)?", normalized)
    if match:
        hour = int(match.group(1))
        minute = int(match.group(2) or 0)
        if not 1 <= hour <= 12 or minute > 59:
            return None
        period = match.group(3) or "pm"
        hour = hour % 12 + (12 if period == "pm" else 0)
        until = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if until <= now:
            until += timedelta(days=1)
        return {"mode": "text_only", "until": until.isoformat()}
    return None


class Jobs:
    def __init__(self, db):
        self.db = db

    def enqueue(self, kind, key, due, payload=None, priority=8):
        self.db.execute(
            """INSERT INTO jobs(id,kind,dedupe_key,due_at,payload,priority)
                          VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT(dedupe_key) DO UPDATE
                          SET priority=least(jobs.priority,excluded.priority) WHERE jobs.status='pending'""",
            (uuid4(), kind, key, due, Jsonb(payload or {}), priority),
        )

    def claim(self, only=None, exclude=None):
        if self.db.get_state("demo_mode", False):
            return None
        order = "priority"
        if self.db.get_state("feature.HPOS_WORKLOAD_GUARD", False):
            order = (
                "CASE kind "
                + " ".join(f"WHEN '{kind}' THEN {priority}" for kind, priority in PRIORITIES.items())
                + " ELSE 7 END"
            )
        with self.db.transaction() as conn:
            row = conn.execute(
                f"""SELECT * FROM jobs WHERE status='pending' AND due_at<=now()
                AND (%s::text IS NULL OR kind=%s) AND (%s::text IS NULL OR kind<>%s)
                ORDER BY {order},CASE kind WHEN 'memory_views' THEN 0 WHEN 'memory_consolidate' THEN 1 ELSE 2 END,due_at FOR UPDATE SKIP LOCKED LIMIT 1""",
                (only, only, exclude, exclude),
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE jobs SET status='running',locked_at=now(),attempts=attempts+1 WHERE id=%s",
                    (row["id"],),
                )
            return row

    def recover(self):
        # Read/internal jobs can retry. Outbound calls/notifications might have happened already.
        self.db.execute(
            "UPDATE jobs SET status=CASE WHEN kind IN ('morning_call','notify','meeting_prepare','health_notice','packet_deliver') THEN 'uncertain' ELSE 'pending' END WHERE status='running'"
        )

    async def run_one(self, handlers, only=None, exclude=None, inference=None):
        job = self.claim(only=only, exclude=exclude)
        if not job:
            return False
        try:
            handler = handlers[job["kind"]]
            priority = PRIORITIES.get(job["kind"], 7)
            guard = getattr(inference, "workload", None)
            if guard and guard.enabled and guard.reason(priority):
                raise InferenceDeferred("Higher priority work or resource pressure")
            with work_priority(priority):
                result = await handler(job["payload"])
            self.db.execute(
                "UPDATE jobs SET status='done',result=%s,error=NULL WHERE id=%s", (Jsonb(result), job["id"])
            )
        except Exception as e:
            # A child transport can replace CancelledError with a disconnect
            # exception while shutting down. Preserve the application's pending
            # cancellation; startup recovery owns the interrupted durable job.
            task = asyncio.current_task()
            if task and task.cancelling():
                raise asyncio.CancelledError() from None
            if isinstance(e, InferenceDeferred) and job["kind"] in RETRYABLE_INTERNAL:
                self.db.execute(
                    "UPDATE jobs SET status='pending',attempts=greatest(0,attempts-1),error=NULL,due_at=now()+interval '15 seconds' WHERE id=%s",
                    (job["id"],),
                )
                return True
            outbound = job["kind"] in (
                "morning_call",
                "notify",
                "meeting_prepare",
                "health_notice",
                "packet_deliver",
            )
            status = "uncertain" if outbound else ("failed" if job["attempts"] >= 4 else "pending")
            self.db.execute(
                "UPDATE jobs SET status=%s,error=%s,due_at=now()+interval '60 seconds' WHERE id=%s",
                (status, type(e).__name__, job["id"]),
            )
            self.db.audit("job." + status, job["id"], {"kind": job["kind"], **failure_details(e)})
            if job["kind"] == "outlook_sync":
                self.db.set_state(
                    "outlook.error",
                    {
                        "error_class": type(e).__name__,
                        "stage": getattr(e, "stage", None),
                        "at": datetime.now(timezone.utc).isoformat(),
                    },
                )
        return True


async def serve(db, inference, sync, handlers):
    jobs = Jobs(db)
    jobs.recover()
    handlers = {"outlook_sync": lambda payload: sync.tick(), **handlers}
    while True:
        try:
            now = datetime.now(timezone.utc)
            if db.get_state("feature.HPOS_MEMORY_VIEWS", False) and db.one(
                "SELECT entity_id FROM memory_view_queue LIMIT 1"
            ):
                jobs.enqueue(
                    "memory_views", "memory-views:" + str(int(now.timestamp()) // 60), now, priority=6
                )
            if db.get_state("feature.HPOS_MEMORY_CONSOLIDATION", False) and db.one(
                "SELECT source_id FROM memory_consolidation_dirty LIMIT 1"
            ):
                jobs.enqueue(
                    "memory_consolidate", "consolidate:" + str(int(now.timestamp()) // 60), now, priority=6
                )
            if db.get_state("feature.HPOS_PROJECT_BRAINS", False) and (
                db.one("SELECT record_id FROM project_source_dirty LIMIT 1")
                or db.one("SELECT id FROM project_entities WHERE dirty AND status<>'archived' LIMIT 1")
            ):
                jobs.enqueue("project_refresh", "project:" + str(int(now.timestamp()) // 15), now, priority=5)
            if db.get_state("feature.HPOS_RELATIONSHIP_GRAPH", False) and db.one(
                "SELECT person_id FROM relationship_dirty LIMIT 1"
            ):
                jobs.enqueue(
                    "relationship_refresh", "relationship:" + str(int(now.timestamp()) // 15), now, priority=5
                )
            if db.get_state("feature.HPOS_COMMAND_CENTER", False):
                from .command_center import read
                from .priorities import due as priorities_due

                cached = read(db, now)
                if cached.get("stale") or priorities_due(db, now):
                    jobs.enqueue(
                        "command_refresh", "command:" + str(int(now.timestamp()) // 30), now, priority=4
                    )
            last_sync = db.get_state("outlook.last_sync")
            if last_sync and db.get_state("setup.enabled.outlook", True):
                jobs.enqueue(
                    "attention_prepare",
                    "attention:" + str(int(now.timestamp()) // 300) + ":" + last_sync,
                    now,
                    priority=6,
                )
            if last_sync and (now - datetime.fromisoformat(last_sync)).total_seconds() > 900:
                jobs.enqueue(
                    "health_notice", "outlook-stale:" + str(int(now.timestamp()) // 21600), now, priority=4
                )
            from .content_packets import schedule
            from .public_research import daily_due

            schedule(db, jobs, now)
            from .packet_schedule import schedule as schedule_packet_delivery

            schedule_packet_delivery(db, jobs, now)

            research_day = daily_due(now)
            if research_day and db.get_state("research.daily_enabled", True):
                jobs.enqueue(
                    "research_brief", "research_daily:" + research_day, now, {"daily": True}, priority=5
                )
            morning = next_morning(now)
            payload = {
                "date": morning.astimezone(ZoneInfo("America/Denver")).date().isoformat(),
                "scheduled_at": morning.isoformat(),
            }
            jobs.enqueue(
                "morning_prepare",
                "morning_prepare:" + payload["date"],
                morning - timedelta(minutes=5),
                payload,
                priority=5,
            )
            jobs.enqueue("morning_call", "morning_call:" + payload["date"], morning, payload, priority=4)
            events = db.query(
                "SELECT id,title,occurred_at,metadata FROM records WHERE kind='event' AND NOT deleted AND occurred_at>now() AND occurred_at<now()+interval '15 minutes'"
            )
            for event in events:
                if db.get_state("feature.HPOS_MEETING_INTELLIGENCE", False):
                    from .meeting_intelligence import importance

                    if not importance(db, event):
                        continue
                if (
                    db.get_state("feature.HPOS_MEETING_INTELLIGENCE", False)
                    or len(event["metadata"].get("attendees", [])) > 1
                ) and not event["metadata"].get("isCancelled"):
                    jobs.enqueue(
                        "meeting_prepare",
                        "meeting:" + event["id"] + ":" + event["occurred_at"].isoformat(),
                        now,
                        {"source_id": event["id"]},
                        priority=5,
                    )
            if not inference.interactive:
                await jobs.run_one(handlers, exclude="outlook_sync", inference=inference)
        except Exception as e:
            db.audit("scheduler.error", details={"error_class": type(e).__name__})
        await asyncio.sleep(3)


async def serve_outlook(db, inference, sync):
    """One independent read worker keeps mailbox polling fresh during long internal reasoning."""
    jobs = Jobs(db)
    while True:
        try:
            now = datetime.now(timezone.utc)
            browser_mode = db.get_state("outlook.provider") == "browser"
            if (sync.provider.auth.settings.ms_client_id or browser_mode) and db.get_state(
                "setup.enabled.outlook", True
            ):
                interval = 300 if browser_mode else 120
                jobs.enqueue(
                    "outlook_sync", "outlook:" + str(int(now.timestamp()) // interval), now, priority=7
                )
                if not inference.interactive:
                    await jobs.run_one({"outlook_sync": lambda payload: sync.tick()}, only="outlook_sync")
        except Exception as error:
            db.audit("outlook.worker_error", details={"error_class": type(error).__name__})
        await asyncio.sleep(3)
