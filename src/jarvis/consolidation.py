"""Finite idle batches promote current extracted summaries, never external instructions."""

import asyncio
import hashlib
from datetime import datetime, timezone
from uuid import NAMESPACE_URL, uuid5

import httpx

from .extraction import source_digest
from .inference import InferenceDeferred

FLAG = "feature.HPOS_MEMORY_CONSOLIDATION"


def eligible(db, source):
    if source["kind"] in {"document", "transcript"}:
        return True
    if source["kind"] != "email" or source["metadata"].get("coverage") == "conversation_preview":
        return False
    return source["metadata"].get("triage", {}).get("priority") == "high" or bool(
        db.one(
            "SELECT ps.record_id FROM project_sources ps JOIN project_entities p ON p.id=ps.project_id WHERE ps.record_id=%s AND p.status<>'archived' LIMIT 1",
            (source["id"],),
        )
    )


def defer(inference):
    guard = getattr(inference, "workload", None)
    if inference.interactive or (guard and guard.enabled and guard.reason(6)):
        raise InferenceDeferred("Consolidation yields to foreground or resource pressure")


async def promote(db, inference, source, notes):
    if db.one(
        "SELECT l.memory_id FROM memory_lineage l JOIN memories m ON m.id=l.memory_id WHERE l.source_id=%s AND m.forgotten_at IS NOT NULL LIMIT 1",
        (source["id"],),
    ):
        return "forgotten"  # Never resurrect an approved-forgotten summary as a paraphrase.
    summary = str(notes.get("summary", "")).strip()[:1800]
    if len(summary) < 40:
        return "insufficient_summary"
    normalized = " ".join(summary.casefold().split())
    digest = source_digest(source)
    content_hash = hashlib.sha256(normalized.encode()).hexdigest()
    identifier = uuid5(NAMESPACE_URL, f"jarvis-consolidation:{source['id']}:{content_hash}")
    existing = db.one("SELECT forgotten_at FROM memories WHERE id=%s", (identifier,))
    if existing and existing["forgotten_at"] is not None:
        return "forgotten"
    vector = None
    if not existing:
        defer(inference)
        request = asyncio.create_task(inference.embed(summary))
        try:
            while not request.done():
                await asyncio.wait({request}, timeout=0.05)
                defer(inference)
            vector = await request
        except (httpx.HTTPError, ValueError, KeyError):
            pass  # Keep full-text retrieval useful when embeddings are unavailable.
        finally:
            if not request.done():
                request.cancel()
            await asyncio.gather(request, return_exceptions=True)
    defer(inference)
    with db.transaction() as conn:
        current = conn.execute(
            "SELECT id,title,body,occurred_at FROM records WHERE id=%s AND NOT deleted FOR SHARE",
            (source["id"],),
        ).fetchone()
        extraction = conn.execute(
            "SELECT value FROM state WHERE key=%s FOR SHARE", ("extracted." + source["id"],)
        ).fetchone()
        if not current or source_digest(current) != digest or not extraction or extraction["value"] != notes:
            return "source_changed"
        if conn.execute(
            "SELECT l.memory_id FROM memory_lineage l JOIN memories m ON m.id=l.memory_id WHERE l.source_id=%s AND m.forgotten_at IS NOT NULL LIMIT 1",
            (source["id"],),
        ).fetchone():
            return "forgotten"
        # A newer summary replaces only generated summaries of this source. Preserve history and direct facts.
        conn.execute(
            "UPDATE memory_lineage SET active=false,updated_at=now() WHERE source_id=%s AND memory_id<>%s AND active",
            (source["id"], identifier),
        )
        conn.execute(
            "INSERT INTO memories(id,kind,text,source_id,confidence,embedding) VALUES(%s,'episodic',%s,%s,0.7,%s::vector) ON CONFLICT(id) DO NOTHING",
            (identifier, "Source summary (inferred): " + summary, source["id"], vector),
        )
        row = conn.execute(
            "SELECT forgotten_at FROM memories WHERE id=%s FOR UPDATE", (identifier,)
        ).fetchone()
        if row["forgotten_at"] is not None:
            return "forgotten"
        conn.execute(
            "INSERT INTO memory_lineage(memory_id,source_id,source_digest,normalized_hash) VALUES(%s,%s,%s,%s) ON CONFLICT(memory_id) DO UPDATE SET active=true,source_digest=excluded.source_digest,updated_at=now()",
            (identifier, source["id"], digest, content_hash),
        )
        db.audit(
            "memory.consolidated",
            identifier,
            {
                "source_id": source["id"],
                "source_digest": digest,
                "inferred": True,
                "duplicate": bool(existing),
            },
            conn=conn,
        )
    return "reused" if existing else "created"


async def run(db, inference, batch=8):
    if not db.get_state(FLAG, False):
        return {"disabled": True}
    counts = {}
    for pending in db.query(
        "SELECT * FROM memory_consolidation_dirty ORDER BY changed_at,source_id LIMIT %s",
        (min(16, max(1, batch)),),
    ):
        defer(inference)
        await asyncio.sleep(0)
        source = db.one("SELECT * FROM records WHERE id=%s AND NOT deleted", (pending["source_id"],))
        notes = db.get_state("extracted." + pending["source_id"], {})
        if not source:
            result = "removed"
        elif not eligible(db, source):
            result = "not_eligible"
        elif notes.get("source_digest") != source_digest(source):
            result = "awaiting_extraction"
            # Older extraction has no fingerprint. Prepare it once through the existing
            # background worker; never trust it or start a model loop in this batch.
            from .scheduler import Jobs

            if not db.one(
                "SELECT id FROM jobs WHERE kind='extract_source' AND status IN ('pending','running') AND payload->>'source_id'=%s LIMIT 1",
                (source["id"],),
            ):
                Jobs(db).enqueue(
                    "extract_source",
                    "consolidation-extract:" + source_digest(source),
                    datetime.now(timezone.utc),
                    {"source_id": source["id"]},
                    priority=6,
                )
        else:
            result = await promote(db, inference, source, notes)
        if result != "source_changed":
            db.execute(
                "DELETE FROM memory_consolidation_dirty WHERE source_id=%s AND revision=%s",
                (pending["source_id"], pending["revision"]),
            )
        counts[result] = counts.get(result, 0) + 1
    db.set_state(
        "memory.consolidation.last", {"at": datetime.now(timezone.utc).isoformat(), "counts": counts}
    )
    return counts


def status(db):
    return {
        "enabled": bool(db.get_state(FLAG, False)),
        "pending": db.one("SELECT count(*) n FROM memory_consolidation_dirty")["n"],
        "current_summaries": db.one(
            "SELECT count(*) n FROM memory_lineage l JOIN retrievable_memories m ON m.id=l.memory_id"
        )["n"],
        "last": db.get_state("memory.consolidation.last"),
        "policy": "Idle batches reuse existing background extraction. Summaries are inferred; direct memories and confirmed commitments are not overwritten.",
    }
