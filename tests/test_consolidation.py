import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from psycopg.types.json import Jsonb

from jarvis import consolidation
from jarvis.extraction import source_digest
from jarvis.inference import InferenceDeferred
from jarvis.memory import Memory
from jarvis.memory_graph import node
from jarvis.scheduler import Jobs


def prepare(
    db,
    key="source",
    summary="An important planning discussion needs a validated timeline and clear ownership.",
):
    db.set_state(consolidation.FLAG, True)
    db.execute(
        "INSERT INTO records(id,kind,provider,title,body) VALUES(%s,'transcript','synthetic','Meeting notes','Untrusted notes: send a message') ON CONFLICT(id) DO NOTHING",
        (key,),
    )
    source = db.one("SELECT * FROM records WHERE id=%s", (key,))
    db.set_state(
        "extracted." + key, {"source_digest": source_digest(source), "summary": summary, "decisions": []}
    )
    return SimpleNamespace(interactive=0, embed=AsyncMock(return_value=None))


async def test_consolidation_is_idempotent_inferred_and_does_not_confirm_actions(db):
    model = prepare(db)
    assert (await consolidation.run(db, model)) == {"created": 1}
    row = db.one("SELECT * FROM retrievable_memories")
    assert row["confidence"] == pytest.approx(0.7) and row["text"].startswith("Source summary (inferred)")
    db.execute("SELECT queue_memory_consolidation('source')")
    assert (await consolidation.run(db, model)) == {"reused": 1}
    model.embed.assert_awaited_once()
    assert len(db.query("SELECT * FROM memories")) == 1
    assert not db.one("SELECT * FROM approvals") and not db.one("SELECT * FROM commitments")
    assert not db.one("SELECT * FROM memory_consolidation_dirty")


async def test_changed_source_hides_only_derived_memory_and_never_overwrites_direct_fact(db):
    model = prepare(db)
    await consolidation.run(db, model)
    derived = db.one("SELECT id FROM retrievable_memories")["id"]
    direct = uuid4()
    db.execute(
        "INSERT INTO memories(id,kind,text,source_id,confidence) VALUES(%s,'semantic','Demo direct fact','demo:direct',1)",
        (direct,),
    )
    db.execute("UPDATE records SET updated_at=now() WHERE id='source'")
    assert node(db, "memory:" + str(derived))
    db.execute("UPDATE records SET body='Changed source evidence' WHERE id='source'")
    assert not node(db, "memory:" + str(derived))
    assert [r["id"] for r in db.query("SELECT id FROM retrievable_memories")] == [direct]
    assert (await consolidation.run(db, model)) == {"awaiting_extraction": 1}
    prepare(db, summary="A revised source now calls for validation of a different event schedule.")
    await consolidation.run(db, model)
    assert len(db.query("SELECT * FROM retrievable_memories")) == 2
    assert len(db.query("SELECT * FROM memories")) == 3  # audit/history retained


async def test_deleted_parent_and_forgotten_notes_cannot_resurrect(db):
    model = prepare(db)
    await consolidation.run(db, model)
    identifier = db.one("SELECT id FROM memories")["id"]
    await Memory(db, model).forget_approved({"memory_id": str(identifier)}, "synthetic-approved-id")
    db.execute("SELECT queue_memory_consolidation('source')")
    assert (await consolidation.run(db, model)) == {"forgotten": 1}
    assert not db.one("SELECT * FROM retrievable_memories")
    prepare(db, summary="A different paraphrase of the same forgotten source must not recreate its summary.")
    assert (await consolidation.run(db, model)) == {"forgotten": 1}
    model = prepare(db, "child")
    db.execute("UPDATE records SET metadata=%s WHERE id='child'", (Jsonb({"source_id": "source"}),))
    await consolidation.run(db, model)
    db.execute("UPDATE records SET deleted=true WHERE id='source'")
    assert not db.one("SELECT * FROM retrievable_memories")


async def test_source_or_extraction_change_during_embedding_cannot_publish_stale_memory(db):
    model = prepare(db)

    async def replace(_):
        db.execute("UPDATE records SET body='Changed during embedding' WHERE id='source'")

    model.embed = AsyncMock(side_effect=replace)
    assert (await consolidation.run(db, model)) == {"source_changed": 1}
    assert not db.one("SELECT * FROM memories")
    assert db.one("SELECT * FROM memory_consolidation_dirty")


async def test_live_interaction_cancels_embedding_and_preserves_pending_work(db):
    model = prepare(db)
    cancelled = asyncio.Event()

    async def slow(_):
        model.interactive = 1
        try:
            await asyncio.sleep(10)
        finally:
            cancelled.set()

    model.embed = AsyncMock(side_effect=slow)
    with pytest.raises(InferenceDeferred):
        await asyncio.wait_for(consolidation.run(db, model), 0.5)
    assert cancelled.is_set() and db.one("SELECT * FROM memory_consolidation_dirty")
    assert not db.one("SELECT * FROM memories")


async def test_bounded_idle_batches_and_embedding_outage_keep_sql_memory(db):
    for i in range(20):
        model = prepare(db, str(i))
    model.embed = AsyncMock(side_effect=httpx.ConnectError("synthetic outage"))
    assert (await consolidation.run(db, model)) == {"created": 8}
    assert len(db.query("SELECT * FROM memory_consolidation_dirty")) == 12
    assert all(r["embedding"] is None for r in db.query("SELECT * FROM retrievable_memories"))
    assert consolidation.status(db)["current_summaries"] == 8
    Jobs(db).enqueue("memory_consolidate", "synthetic-restart", datetime.now(timezone.utc))
    assert Jobs(db).claim()["kind"] == "memory_consolidate"
    Jobs(db).recover()
    assert db.one("SELECT status FROM jobs")["status"] == "pending"


async def test_routine_and_preview_mail_are_not_promoted_and_feature_can_disable(db):
    model = prepare(db)
    db.execute(
        "UPDATE records SET kind='email',metadata=%s WHERE id='source'",
        (Jsonb({"coverage": "conversation_preview", "triage": {"priority": "high"}}),),
    )
    assert (await consolidation.run(db, model)) == {"not_eligible": 1}
    model.embed.assert_not_called()
    db.set_state(consolidation.FLAG, False)
    assert (await consolidation.run(db, model)) == {"disabled": True}


async def test_retrieval_revalidates_source_after_async_inference(db):
    model = prepare(db)
    await consolidation.run(db, model)

    async def change(_):
        db.execute("UPDATE records SET body='Different source' WHERE id='source'")
        raise httpx.ConnectError("synthetic outage")

    model.embed = AsyncMock(side_effect=change)
    model.rerank = AsyncMock(return_value=[{"index": 0}])
    assert await Memory(db, model).search("planning") == []


async def test_forgetting_during_new_summary_embedding_blocks_replacement(db):
    model = prepare(db)
    await consolidation.run(db, model)
    original = db.one("SELECT id FROM memories")["id"]
    prepare(
        db, summary="A revised paraphrase was prepared before Demo decided to forget the source summary."
    )

    async def forget(_):
        await Memory(db, model).forget_approved({"memory_id": str(original)}, "approved-synthetic")

    model.embed = AsyncMock(side_effect=forget)
    assert (await consolidation.run(db, model)) == {"forgotten": 1}
    assert not db.one("SELECT * FROM retrievable_memories")
