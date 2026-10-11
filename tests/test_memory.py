import httpx

from jarvis.memory import Memory
from jarvis.policy import Actor, Approvals


class OfflineInference:
    async def embed(self, text):
        raise httpx.ConnectError("Unavailable")

    async def rerank(self, query, texts):
        raise httpx.ConnectError("Unavailable")


async def test_memory_persists_with_provenance_when_models_down(db):
    memory = Memory(db, OfflineInference())
    record = await memory.remember("Simon needs 25 Nanos for the event", source_id="synthetic:transcript")
    assert not record["indexed"]
    found = await Memory(db, OfflineInference()).search("Simon")
    assert found[0]["source_id"] == "synthetic:transcript"
    assert str(found[0]["id"]) == record["id"]


async def test_forget_removes_text_and_vectors_from_retrieval(db):
    memory = Memory(db, OfflineInference())
    record = await memory.remember("Synthetic secret project Alpha")
    engine = Approvals(db)
    action = engine.propose("memory.forget", {"memory_id": record["id"]})
    engine.decide(action["id"], "YES", action["digest"], Actor("demo", "dashboard"))
    await engine.execute(action["id"], {"memory.forget": memory.forget_approved})
    assert await memory.search("Alpha") == []
    assert db.one("SELECT text FROM memories WHERE id=%s", (record["id"],))["text"] == "[forgotten]"
