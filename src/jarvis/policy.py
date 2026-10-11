"""Policy is application code. Model output never constructs a trusted actor."""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

AUTOMATIC = frozenset(
    {
        "mail.search",
        "mail.read",
        "calendar.read",
        "memory.search",
        "memory.write",
        "email.draft",
        "task.create",
        "research.search",
    }
)
CONSEQUENTIAL = frozenset(
    {"email.send", "email.save_draft", "calendar.create", "calendar.move", "calendar.cancel", "memory.forget"}
)


class PolicyError(ValueError):
    pass


@dataclass(frozen=True)
class Actor:
    identity: str
    channel: str

    def require_demo(self):
        if self.identity != "demo" or self.channel not in ("dashboard", "telegram", "local_cli"):
            raise PolicyError("Only authenticated Demo can authorize this action")


def digest(kind, payload):
    return hashlib.sha256(
        json.dumps(
            {"kind": kind, "payload": payload}, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()


class Approvals:
    def __init__(self, db):
        self.db = db

    def propose(self, kind, payload, *, ttl_minutes=30, actor="jarvis"):
        if kind not in CONSEQUENTIAL:
            raise PolicyError("Unknown or unsupported consequential action")
        identifier = uuid4()
        expires = datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)
        with self.db.transaction() as conn:
            conn.execute(
                """INSERT INTO approvals(id,kind,payload,digest,status,expires_at,actor)
                            VALUES (%s,%s,%s,%s,'pending',%s,%s)""",
                (identifier, kind, Jsonb(payload), digest(kind, payload), expires, actor),
            )
            self.db.audit("approval.proposed", identifier, {"kind": kind}, actor=actor, conn=conn)
        return self.get(identifier)

    def get(self, identifier):
        try:
            identifier = UUID(str(identifier))
        except ValueError as e:
            raise PolicyError("A complete action ID is required") from e
        row = self.db.one("SELECT * FROM approvals WHERE id=%s", (identifier,))
        if not row:
            raise PolicyError("Unknown action ID")
        return row

    def decide(self, identifier, command, expected_digest, actor):
        actor.require_demo()
        command = command.upper().strip()
        if command not in {"YES", "APPROVE", "NO", "DENY"}:
            raise PolicyError("Use APPROVE or DENY for the displayed action ID; EDIT creates a new proposal")
        expired = False
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM approvals WHERE id=%s FOR UPDATE", (identifier,)).fetchone()
            revoking = command in {"NO", "DENY"} and row and row["status"] == "approved"
            if not row or (row["status"] != "pending" and not revoking):
                raise PolicyError("Action is not pending; duplicate/replayed decisions are refused")
            if row["digest"] != expected_digest or digest(row["kind"], row["payload"]) != row["digest"]:
                raise PolicyError("Action content does not match the reviewed proposal")
            expired = row["expires_at"] <= datetime.now(timezone.utc)
            status = "expired" if expired else ("approved" if command in {"YES", "APPROVE"} else "denied")
            conn.execute("UPDATE approvals SET status=%s,decided_at=now() WHERE id=%s", (status, identifier))
            self.db.audit(
                "approval." + status, identifier, actor=actor.identity, source=actor.channel, conn=conn
            )
        if expired:
            raise PolicyError("Approval expired; request a fresh proposal")
        return self.get(identifier)

    def edit(self, identifier, payload, actor):
        actor.require_demo()
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM approvals WHERE id=%s FOR UPDATE", (identifier,)).fetchone()
            if not row or row["status"] not in {"pending", "approved"}:
                raise PolicyError("Action cannot be edited")
            conn.execute("UPDATE approvals SET status='superseded' WHERE id=%s", (identifier,))
            new_id = uuid4()
            conn.execute(
                """INSERT INTO approvals(id,kind,payload,digest,status,expires_at,actor)
                            VALUES (%s,%s,%s,%s,'pending',now()+interval '30 minutes',%s)""",
                (new_id, row["kind"], Jsonb(payload), digest(row["kind"], payload), actor.identity),
            )
            self.db.audit(
                "approval.edited",
                identifier,
                {"replacement_id": str(new_id)},
                actor=actor.identity,
                source=actor.channel,
                conn=conn,
            )
        return self.get(new_id)

    async def execute(self, identifier, handlers):
        # Handler selection happens in trusted code, never from user URLs or model output.
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM approvals WHERE id=%s FOR UPDATE", (identifier,)).fetchone()
            if not row or row["status"] != "approved":
                raise PolicyError("Action does not hold an unused approval")
            if row["expires_at"] <= datetime.now(timezone.utc):
                raise PolicyError("Approval expired before execution")
            if row["kind"] not in handlers:
                raise PolicyError("This integration is not ready to execute the action")
            if digest(row["kind"], row["payload"]) != row["digest"]:
                raise PolicyError("Payload changed after approval")
            conn.execute("UPDATE approvals SET status='executing' WHERE id=%s", (identifier,))
            self.db.audit("action.executing", identifier, {"kind": row["kind"]}, conn=conn)
        try:
            result = await handlers[row["kind"]](row["payload"], str(identifier))
        except Exception as e:
            # Transport failure can mean an external action already happened. Never retry automatically.
            self.db.execute(
                "UPDATE approvals SET status='uncertain',error=%s WHERE id=%s", (type(e).__name__, identifier)
            )
            self.db.audit("action.uncertain", identifier, {"error_class": type(e).__name__})
            raise PolicyError("Result uncertain; reconciliation required before another attempt") from e
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE approvals SET status='succeeded',result=%s WHERE id=%s", (Jsonb(result), identifier)
            )
            self.db.audit("action.succeeded", identifier, conn=conn)
        return result

    def recover(self):
        self.db.execute(
            "UPDATE approvals SET status='uncertain',error='Process restarted during execution' WHERE status='executing'"
        )
        self.db.execute(
            "UPDATE approvals SET status='expired' WHERE status IN ('pending','approved') AND expires_at<=now()"
        )
