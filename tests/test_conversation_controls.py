import httpx

from jarvis.memory import Memory
from jarvis.orchestrator import Jarvis, compact_context
from jarvis.policy import Actor, Approvals


class Offline:
    async def embed(self, text):
        raise httpx.ConnectError("Offline")


def test_prompt_context_keeps_provenance_without_redundant_source_payload():
    context = {
        "emails": [
            {
                "id": "source",
                "title": "Research",
                "excerpt": "x" * 2000,
                "metadata": {
                    "from": {"emailAddress": {"name": "Reviewer"}},
                    "coverage": "message_body",
                    "unused_raw": "x" * 40000,
                },
            }
        ],
        "events": [],
    }
    result = compact_context(context)
    assert result["emails"][0]["id"] == "source"
    assert result["emails"][0]["metadata"]["coverage"] == "message_body"
    assert "unused_raw" not in result["emails"][0]["metadata"]
    assert len(result["emails"][0]["excerpt"]) == 700


async def test_simple_voice_greeting_does_not_wait_on_models_or_mail(db):
    jarvis = Jarvis(db, None, None, Approvals(db))
    result = await jarvis.reply("Can you hear me, Jarvis?", actor=Actor("demo", "dashboard"), spoken=True)
    assert "I'm here" in result["reply"]


async def test_remember_forget_approve_bound_and_redacted(db):
    actor = Actor("demo", "dashboard")
    inference = Offline()
    memory = Memory(db, inference)
    jarvis = Jarvis(db, inference, memory, Approvals(db))
    await jarvis.reply("Remember this permanently: Synthetic secret Alpha", actor=actor)
    proposed = await jarvis.reply("Forget that", actor=actor)
    assert "Synthetic secret Alpha" in proposed["reply"]
    assert db.one("SELECT forgotten_at FROM memories")["forgotten_at"] is None
    result = await jarvis.reply("Approve it", actor=actor)
    assert "Executed only action" in result["reply"]
    assert db.one("SELECT text FROM memories")["text"] == "[forgotten]"
    assert (
        db.one("SELECT count(*) AS n FROM messages WHERE content LIKE %s", ("%Synthetic secret Alpha%",))["n"]
        == 0
    )
    duplicate = await jarvis.reply("Approve it", actor=actor)
    assert "not pending" in duplicate["reply"]
