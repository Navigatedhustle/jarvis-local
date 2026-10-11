"""Human review of internal commitment candidates, with immutable source provenance."""

import hashlib
import json


class StaleCommitment(ValueError):
    pass


def revision(row):
    fields = {key: row.get(key) for key in ("id", "owner", "beneficiary", "text", "due_at", "status")}
    return hashlib.sha256(json.dumps(fields, sort_keys=True, default=str).encode()).hexdigest()


def review(db, identifier, body, actor):
    actor.require_demo()
    with db.transaction() as conn:
        current = conn.execute("SELECT * FROM commitments WHERE id=%s FOR UPDATE", (identifier,)).fetchone()
        if not current:
            raise LookupError("Commitment not found")
        if revision(current) != body.revision:
            raise StaleCommitment("This commitment changed. Refresh it before reviewing again.")
        if current["status"] == "candidate" and body.status == "done":
            raise ValueError("Confirm or dismiss a candidate before marking it complete.")
        conn.execute(
            "UPDATE commitments SET owner=%s,beneficiary=%s,text=%s,due_at=%s,status=%s WHERE id=%s",
            (
                body.owner.strip(),
                body.beneficiary.strip(),
                body.text.strip(),
                body.due_at,
                body.status,
                identifier,
            ),
        )
        db.audit(
            "commitment.reviewed",
            identifier,
            {
                "previous_status": current["status"],
                "status": body.status,
                "source_id": current["source_id"],
                "previous_revision": body.revision,
                "deadline_changed": current["due_at"] != body.due_at,
            },
            actor="demo",
            source=actor.channel,
            conn=conn,
        )
    return {"saved": True, "status": body.status, "outlook_changed": False}
