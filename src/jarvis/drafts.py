"""Revision-bound internal draft review; send approvals remain immutable and separate."""

import hashlib
import json

from psycopg.types.json import Jsonb


def revision(row):
    return hashlib.sha256(
        json.dumps(
            {key: row[key] for key in ("id", "title", "body", "metadata")}, sort_keys=True, default=str
        ).encode()
    ).hexdigest()


def edit(db, identifier, expected, title, body, actor):
    with db.transaction() as conn:
        row = conn.execute(
            "SELECT * FROM records WHERE id=%s AND kind='draft' AND NOT deleted FOR UPDATE", (identifier,)
        ).fetchone()
        if not row:
            raise LookupError("Draft not found")
        if row["metadata"].get("writing_request") and row["metadata"].get("writing_state") != "ready":
            raise ValueError("This writing request has no completed draft to edit yet")
        if revision(row) != expected:
            raise ValueError("This draft changed. Reopen it before saving your edit.")
        metadata = {
            **row["metadata"],
            "review_required": False,
            "reviewed_by": "demo",
            "status": "internal",
        }
        metadata.pop("review_reason", None)
        conn.execute(
            "UPDATE records SET title=%s,body=%s,metadata=%s,updated_at=now() WHERE id=%s",
            (title.strip(), body.strip(), Jsonb(metadata), identifier),
        )
        db.audit(
            "draft.reviewed",
            identifier,
            {"source_id": metadata.get("source_id")},
            actor="demo",
            source=actor.channel,
            conn=conn,
        )
    return {"saved": True, "sent": False}
