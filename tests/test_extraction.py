import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from jarvis.extraction import source_chunks
from jarvis.intelligence import ingest_message
from jarvis.proactive import Proactive


def test_unicode_slices_preserve_tail_and_respect_model_budget():
    text = "研究 " * 2000 + "TAIL: I will send the final research report Friday."
    parts, complete = source_chunks(text)
    assert complete and len(parts) > 1
    assert all(len(part.encode("utf-8")) <= 4000 for part in parts)
    assert "TAIL: I will send" in parts[-1]
    assert "�" not in "".join(parts)
    limited, complete = source_chunks(text, maximum=1)
    assert len(limited) == 1 and not complete


async def test_long_source_schedules_resumable_chunks_and_reaches_last_commitment(db):
    tail = "I will send the final research report Friday."
    ingest_message(
        db,
        {
            "id": "long",
            "subject": "Research source",
            "body": {"content": "Research context. " * 800 + tail},
            "receivedDateTime": datetime.now(timezone.utc).isoformat(),
        },
    )

    async def infer(messages, **kwargs):
        has_tail = tail in messages[-1]["content"]
        value = {
            "summary": "Source excerpt.",
            "decisions": [],
            "commitments": [
                {
                    "owner": "Demo",
                    "beneficiary": "Reviewer",
                    "text": tail,
                    "confidence": 0.9,
                    "due_at": None,
                }
            ]
            if has_tail
            else [],
        }
        return {"choices": [{"message": {"content": json.dumps(value)}}]}

    inference = SimpleNamespace(chat=AsyncMock(side_effect=infer))
    engine = Proactive(None, db, inference, None, None, None)
    result = await engine.extract_source({"source_id": "graph:mail:long"})
    assert result["queued_chunks"] > 1 and not result["extracted"]
    inference.chat.assert_not_awaited()
    jobs = db.query("SELECT payload FROM jobs WHERE kind='extract_chunk' ORDER BY payload->>'index'")
    for job in reversed(jobs):
        await engine.extract_chunk(job["payload"])
    state = db.get_state("extracted.graph:mail:long")
    assert state["extraction_coverage"] == "stored_body"
    assert db.one("SELECT text,status FROM commitments") == {"text": tail, "status": "candidate"}
    await engine.extract_chunk(jobs[-1]["payload"])
    assert db.one("SELECT count(*) AS n FROM commitments")["n"] == 1
    db.execute("UPDATE records SET body='Changed source' WHERE id='graph:mail:long'")
    assert "skipped" in await engine.extract_chunk(jobs[0]["payload"])
