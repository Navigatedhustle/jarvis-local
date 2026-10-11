"""Private phone pairing. Codes are short-lived, single-use and stored only as hashes."""

import hashlib
import secrets
import time
from uuid import uuid4

from .policy import PolicyError


def key_id(settings):
    return hashlib.sha256(settings.api_token.encode()).hexdigest()


def issue(db, settings):
    if not settings.mobile_url:
        raise PolicyError("Private phone access has not been configured yet")
    code = secrets.token_hex(6).upper()
    with db.transaction() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(71008109)")
        db.set_state(
            "dashboard.phone_pairing",
            {
                "digest": hashlib.sha256(code.encode()).hexdigest(),
                "expires": time.time() + 120,
                "key_id": key_id(settings),
            },
            conn=conn,
        )
        db.audit("dashboard.phone_pairing_created", actor="demo", source="dashboard", conn=conn)
    return {"code": code, "expires_in": 120, "url": settings.mobile_url}


def redeem(db, settings, code, *, remember=False):
    code = code.replace("-", "").replace(" ", "").upper()
    digest = hashlib.sha256(code.encode()).hexdigest()
    now = time.time()
    with db.transaction() as conn:
        # Rate limit is global, durable across restarts and serializes concurrent redemptions.
        conn.execute("SELECT pg_advisory_xact_lock(71008109)")
        row = conn.execute("SELECT value FROM state WHERE key='dashboard.phone_attempts'").fetchone()
        attempts = [t for t in (row["value"] if row else []) if now - t < 60]
        if len(attempts) >= 8:
            result = "Wait a minute before trying another pairing code"
        else:
            attempts.append(now)
            db.set_state("dashboard.phone_attempts", attempts, conn=conn)
            row = conn.execute("SELECT value FROM state WHERE key='dashboard.phone_pairing'").fetchone()
            pending = row["value"] if row else {}
            if (
                pending.get("expires", 0) > now
                and pending.get("key_id") == key_id(settings)
                and secrets.compare_digest(pending.get("digest", ""), digest)
            ):
                conn.execute("DELETE FROM state WHERE key='dashboard.phone_pairing'")
                value = secrets.token_urlsafe(48)
                session_digest = hashlib.sha256(value.encode()).hexdigest()
                db.set_state(
                    "dashboard.session." + session_digest,
                    {
                        "expires": now + (30 * 86400 if remember else 8 * 3600),
                        "key_id": key_id(settings),
                        "kind": "phone",
                        "device_id": str(uuid4()),
                        "created_at": now,
                        "remember": remember,
                    },
                    conn=conn,
                )
                db.audit("dashboard.phone_paired", actor="demo", source="dashboard", conn=conn)
                return value
            result = "Pairing code expired or incorrect. Create a fresh code in JARVIS on Windows."
    # Raise after commit so unsuccessful attempts survive and cannot bypass the limiter.
    raise PolicyError(result)


def devices(db, settings):
    return [
        {k: r["value"][k] for k in ("device_id", "created_at", "expires", "remember")}
        for r in db.query(
            "SELECT value FROM state WHERE key LIKE 'dashboard.session.%%' AND value->>'kind'='phone'"
        )
        if r["value"].get("key_id") == key_id(settings) and r["value"].get("expires", 0) > time.time()
    ]


def revoke(db, device_id):
    with db.transaction() as conn:
        rows = conn.execute(
            "DELETE FROM state WHERE key LIKE 'dashboard.session.%%' AND value->>'kind'='phone' AND value->>'device_id'=%s RETURNING key",
            (str(device_id),),
        ).fetchall()
        db.audit("dashboard.phone_revoked", str(device_id), actor="demo", source="dashboard", conn=conn)
    return [r["key"].removeprefix("dashboard.session.") for r in rows]
