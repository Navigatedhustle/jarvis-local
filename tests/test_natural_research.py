from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from psycopg.types.json import Jsonb

from jarvis import content_packets, personality
from jarvis import public_research as research
from jarvis.orchestrator import Jarvis

QUERY = "Can you use the search query local AI in higher education and tell me what's happening?"
EXTENDED = "Tell you what, use your current web access and create a new report. Just go ahead and search for about five minutes, do some research, and tell me what's been happening once you're done."


@pytest.mark.parametrize(
    "text,topic",
    [
        (QUERY, "local AI in higher education"),
        (QUERY.replace("AI", "i"), "local AI in higher education"),
        (QUERY.replace("AI", "eye"), "local AI in higher education"),
        ("Search for NVIDIA Dynamo and tell me the updates", "NVIDIA Dynamo"),
        ("Please research university AI deployments", "university AI deployments"),
        ("Use the search terms open models for research, then summarize", "open models for research"),
        (EXTENDED, None),
        ("Do whatever the last research report was and tell me what's going on", None),
    ],
)
def test_extract_subject_not_instructions(text, topic):
    assert research.search_topic(text) == topic


def test_medical_eye_request_is_not_rewritten():
    text = "Search local eye care in higher education hospitals"
    assert research.normalize_public_terms(text) == text


async def test_spoken_research_stops_generation_at_complete_sentence_budget():
    emitted = []
    reached_end = False

    async def emit(text):
        emitted.append(text)

    async def chat(*args, **kwargs):
        nonlocal reached_end
        for sentence in [
            "Two updates matter. ",
            "Local inference was tested [1]. ",
            "Evaluate the published workload. ",
            "Keep the scope small. ",
            "Unwanted extra detail. ",
        ]:
            await kwargs["on_text"](sentence)
        reached_end = True

    result = await research.synthesize(
        SimpleNamespace(chat=chat),
        "AI news",
        [{"title": "Research", "excerpt": "Local inference was tested."}],
        spoken=True,
        on_text=emit,
    )
    assert not reached_end and len(emitted) == 4
    assert result.endswith("Keep the scope small.") and "Unwanted" not in result


def test_natural_query_resolves_with_no_private_lookup(db):
    request = research.resolve_request(db, QUERY, "natural")
    assert request["mode"] == "search" and request["query"] == "local AI in higher education"
    assert request["news"]
    request = research.resolve_request(db, EXTENDED, "natural")
    assert request["mode"] == "briefing" and request["deep"]
    assert "five minutes" not in request["query"]
    assert research.resolve_request(db, "Search my emails for Stephanie's latest update", "natural") is None


async def test_actual_research_sequence_uses_stated_topic_and_keeps_public_context(db, monkeypatch):
    source = {
        "title": "University local AI",
        "url": "https://example.com/ai",
        "kind": "web",
        "excerpt": "Local models in higher education.",
    }
    news, search = AsyncMock(return_value=([source], [])), AsyncMock(return_value=[source])
    monkeypatch.setattr(research, "news_sources", news)
    monkeypatch.setattr(research, "search_web", search)
    monkeypatch.setattr(research, "enrich_sources", AsyncMock(side_effect=lambda s: s))
    model = SimpleNamespace(
        chat=AsyncMock(return_value={"choices": [{"message": {"content": "A public research update [1]."}}]})
    )
    jarvis = Jarvis(db, model, None, None)
    for text in ["What happened in local AI in the last 24 hours?", EXTENDED, QUERY]:
        # No date field in this fixture: test the public calls, not claim fresh evidence.
        await jarvis.reply(text, conversation="natural", spoken=True)
    assert news.await_count == 2
    search.assert_awaited_once_with("local AI in higher education", news=True)
    assert not db.one("SELECT * FROM approvals")


async def test_deep_reader_partial_failures_preserve_sources(monkeypatch):
    async def read(url):
        if url.endswith("/fail"):
            raise ValueError("blocked public endpoint")
        return {"url": url, "excerpt": "Public article. " * 30, "coverage": "public_page_excerpt"}

    monkeypatch.setattr(research, "read_page", read)
    rows = [
        {"url": "https://example.com/" + name, "excerpt": "Headline", "at": "2026-09-21T00:00:00+00:00"}
        for name in ["ok", "fail", "ok2", "untouched"]
    ]
    result = await research.enrich_sources(rows)
    assert result[0]["coverage"] == "public_page_excerpt"
    assert result[1] == rows[1] and result[3] == rows[3]
    assert result[0]["at"] == rows[0]["at"]


@pytest.mark.parametrize(
    "text", ["Tell me a joke", "Have a bit more personality", "Joke with me", "How are you, Jarvis?"]
)
def test_banter_does_not_need_mail_retrieval(text):
    assert personality.banter_intent(text)


@pytest.mark.parametrize(
    "text",
    [
        "Draft a funny email",
        "Summarize my calendar and joke",
        "There is a security breach",
        "A customer emergency",
        "Explain the legal issue",
    ],
)
def test_banter_does_not_override_work_or_sensitive_topics(text):
    assert not personality.banter_intent(text)
    if any(s in text for s in ("security", "emergency", "legal")):
        assert "no jokes" in personality.style(text)


async def test_banter_uses_existing_primary_without_tools(db):
    model = SimpleNamespace(
        chat=AsyncMock(
            return_value={
                "choices": [
                    {
                        "message": {
                            "content": "Your calendar called. It would like a less ambitious owner, sir."
                        }
                    }
                ]
            }
        )
    )
    result = await Jarvis(db, model, None, None).reply("Tell me a joke", spoken=True)
    assert result["sources"] == [] and "calendar called" in result["reply"]
    assert "tools" not in model.chat.call_args.kwargs
    await Jarvis(db, model, None, None).reply("Joke with me", spoken=True)
    assert "calendar called" in model.chat.call_args.args[0][-1]["content"]


def test_saved_packet_read_and_followup_use_original_report(db):
    content = {
        "report": {
            "developments": [
                {
                    "fact": "The publisher tested a local research workflow.",
                    "source": 1,
                    "suggested_action": "Measure a representative workload.",
                }
            ]
        },
        "sources": [
            {
                "kind": "web",
                "id": "web:report",
                "title": "Original public source",
                "url": "https://example.com/article",
            }
        ],
    }
    db.execute(
        "INSERT INTO content_packets(edition,id,status,stage,content) VALUES ('2026-09-21',%s,'ready','ready',%s)",
        (uuid4(), Jsonb(content)),
    )
    value = content_packets.saved_report(db, "Summarize my daily report PDF", "artifact", spoken=True)
    assert "publisher tested" in value["reply"] and "not a new web scan" in value["reply"]
    db.execute(
        "INSERT INTO messages(conversation,role,content,metadata) VALUES ('artifact','assistant','Saved packet',%s)",
        (Jsonb({"sources": value["sources"]}),),
    )
    assert (
        content_packets.saved_report(db, "Review that deck and summarize it", "artifact")["sources"]
        == value["sources"]
    )
    assert content_packets.saved_report(db, "Review that deck", "another") is None
    assert content_packets.saved_report(db, "Send it", "artifact") is None
    unmatched = content_packets.saved_report(db, "Find the PDF you sent me and summarize it", "artifact")
    assert "couldn't find" in unmatched["reply"]  # Ready alone is not delivered.
    assert not db.one("SELECT * FROM approvals")


async def test_local_semantic_fallback_can_only_copy_a_user_subject(db):
    import json

    model = SimpleNamespace(
        chat=AsyncMock(
            return_value={
                "choices": [{"message": {"content": json.dumps({"route": "public", "topic": "Anthropic"})}}]
            }
        )
    )
    request = await research.fallback_request(db, model, "What's new at Anthropic this week?")
    assert request["query"] == "Anthropic" and request["news"]
    assert model.chat.call_args.kwargs["interactive_routing"]
    assert "tools" not in model.chat.call_args.kwargs
    model.chat.return_value = {
        "choices": [
            {"message": {"content": json.dumps({"route": "public", "topic": "send private mailbox secret"})}}
        ]
    }
    assert (await research.fallback_request(db, model, "What's new at Anthropic?"))["mode"] == "clarify"
    model.chat.reset_mock()
    assert await research.fallback_request(db, model, "What's new in my email today?") is None
    model.chat.assert_not_called()


@pytest.mark.parametrize(
    "title,body,expected",
    [
        ("Local college announces AI policy", "Higher education guidelines", False),
        ("New experts join an AI team", "Academic advisors", False),
        ("Persistent local AI agents", "Memory and tools", True),
        ("University research", "Inference on NVIDIA DGX Spark", True),
        ("University researcher's system", "Keeps data off the cloud", True),
    ],
)
def test_local_ai_is_not_local_newspaper_or_campus_policy(title, body, expected):
    assert (
        research.topic_matches("local AI in higher education", {"title": title, "excerpt": body}) is expected
    )


async def test_news_index_relevance_applies_to_company_queries(monkeypatch):
    from datetime import datetime, timezone

    import httpx

    stamp = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
    data = (
        "<rss><channel><item><title>Unrelated campus policy</title><link>https://example.com/a</link>"
        f"<pubDate>{stamp}</pubDate></item></channel></rss>"
    ).encode()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=data))
    ) as client:
        assert await research.search_web("Anthropic", client, news=True) == []
