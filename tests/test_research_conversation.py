from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from jarvis import public_research as research
from jarvis.orchestrator import Jarvis

PHRASES = [
    "Well actually just give me a go ahead and give me an AI update. What's going on in the market? What's trending and how can I capitalize",
    "No, I need you to use your web access. Do whatever the last research report was and tell me exactly what's been going on.",
    "You literally provided me an update the other day, so I don't know what's going on",
]
SOURCE = {
    "id": "web:test",
    "kind": "web",
    "title": "Local inference research update",
    "url": "https://example.com/research",
    "excerpt": "A new inference research article.",
    "trust": "untrusted",
}
LAST_DAY = "Alright, well I see you also have access to the internet. Can you do a quick poll? What's happened in the last 24 hours in AI, specifically local AI?"


@pytest.mark.parametrize(
    "phrase",
    PHRASES[:2]
    + [
        "Give me an A. I. update",
        "What is trending in AI?",
        "Give me an eye update. What's trending in the market?",
    ],
)
def test_actual_spoken_update_requests(phrase):
    assert research.intent(phrase) == "briefing"


@pytest.mark.parametrize("spoken", [True, False])
async def test_failed_real_conversation_stays_on_public_evidence(db, monkeypatch, spoken):
    news = AsyncMock(return_value=([SOURCE], []))
    monkeypatch.setattr(research, "news_sources", news)
    monkeypatch.setattr(research, "enrich_sources", AsyncMock(side_effect=lambda sources: sources))
    model = SimpleNamespace(
        chat=AsyncMock(
            return_value={
                "choices": [{"message": {"content": "A public inference update is available [1]."}}]
            }
        )
    )
    jarvis = Jarvis(db, model, None, None)
    jarvis.call_tool = AsyncMock(side_effect=AssertionError("No private retrieval or external actions"))
    for phrase in PHRASES:
        answer = await jarvis.reply(phrase, conversation="web-repair", spoken=spoken)
        assert answer["sources"] == [SOURCE]
        assert not research.false_web_denial(answer["reply"])
    assert news.await_count == 3
    jarvis.call_tool.assert_not_called()
    assert not db.one("SELECT id FROM approvals")


def test_public_context_is_recent_conversation_scoped_and_does_not_leak_work(db):
    request = "Search the web for university local inference"
    db.set_state(
        "research.context.test",
        {
            "mode": "search",
            "query": "university local inference",
            "request": request,
            "at": research.now_utc().isoformat(),
        },
    )
    db.execute("INSERT INTO messages(conversation,role,content) VALUES ('test','user',%s)", (request,))
    assert research.resolve_request(db, "What about open models?", "test")["query"].endswith("; open models")
    assert research.resolve_request(db, "What about my emails from Stephanie?", "test") is None
    assert research.resolve_request(db, "What about our project?", "test") is None
    assert research.resolve_request(db, "Use your web access", "another")["mode"] == "clarify"
    db.execute(
        "INSERT INTO messages(conversation,role,content) VALUES ('test','user','What do I owe Stephanie?')"
    )
    assert research.resolve_request(db, "Look that up", "test")["mode"] == "clarify"
    db.execute("INSERT INTO messages(conversation,role,content) VALUES ('test','user',%s)", (request,))
    state = db.get_state("research.context.test")
    state["at"] = (research.now_utc() - timedelta(hours=1)).isoformat()
    db.set_state("research.context.test", state)
    assert research.resolve_request(db, "Use your web access", "test")["mode"] == "clarify"


@pytest.mark.parametrize(
    "denial",
    [
        "I cannot browse the web or access prior reports; I only process excerpts.",
        "I don't have internet access.",
        "I'm just text-based.",
    ],
)
async def test_false_denial_cannot_reach_text_or_streamed_speech(denial):
    chunks = []

    async def emit(value):
        chunks.append(value)

    async def chat(*args, **kwargs):
        for char in denial:
            await kwargs["on_text"](char)
        return {"choices": [{"message": {"content": denial}}]}

    answer = await research.synthesize(
        SimpleNamespace(chat=chat), "AI update", [SOURCE], spoken=True, on_text=emit
    )
    assert not research.false_web_denial(answer)
    assert not research.false_web_denial("".join(chunks))
    assert "source headlines" in answer and "[1]" in answer


async def test_unrelated_fallback_search_results_are_not_evidence():
    payload = b"<rss><channel><item><title>Voting guide</title><link>https://example.com/vote</link><description>Election locations</description></item></channel></rss>"
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=payload))
    ) as client:
        assert await research.search_web("university local inference", client) == []


async def test_live_update_does_not_reuse_morning_brief(db, monkeypatch):
    db.set_state(
        "research.brief",
        {
            "built_at": (research.now_utc() - timedelta(hours=2)).isoformat(),
            "sources": [SOURCE],
            "reply": "Earlier cached brief",
        },
    )
    fresh = AsyncMock(return_value=([SOURCE], []))
    monkeypatch.setattr(research, "news_sources", fresh)
    model = SimpleNamespace(
        chat=AsyncMock(return_value={"choices": [{"message": {"content": "Fresh public evidence [1]."}}]})
    )
    answer = await research.answer(db, model, "AI update")
    assert "Fresh public" in answer["reply"]
    fresh.assert_awaited_once()


async def test_spoken_feedback_question_knows_actual_voice_capability(db):
    model = SimpleNamespace(chat=AsyncMock())
    answer = await Jarvis(db, model, None, None).reply("Are you hearing yourself?", spoken=True)
    assert "Speaker protection" in answer["reply"]
    model.chat.assert_not_called()


async def test_first_verified_sentence_reaches_speech_without_waiting_for_second():
    chunks = []

    async def emit(value):
        chunks.append(value)

    async def chat(*args, **kwargs):
        await kwargs["on_text"]("Two public developments are worth examining.")
        assert chunks == ["Two public developments are worth examining. "]
        await kwargs["on_text"](" An inference update is available [1].")
        return {
            "choices": [
                {
                    "message": {
                        "content": "Two public developments are worth examining. An inference update is available [1]."
                    }
                }
            ]
        }

    await research.synthesize(SimpleNamespace(chat=chat), "AI update", [SOURCE], spoken=True, on_text=emit)


async def test_search_fallback_survives_provider_failure():
    def handler(request):
        if request.url.host == "www.bing.com":
            return httpx.Response(503)
        return httpx.Response(
            200,
            content=b"<rss><channel><item><title>University inference research</title><link>https://example.com/ai</link></item></channel></rss>",
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert len(await research.search_web("university inference", client)) == 1


async def test_real_last_day_request_beats_capability_and_enforces_publication_dates(db, monkeypatch):
    now = research.now_utc()
    rows = [
        {**SOURCE, "at": (now - timedelta(hours=2)).isoformat()},
        {**SOURCE, "at": (now - timedelta(days=5)).isoformat(), "id": "old"},
        {**SOURCE, "at": None, "id": "undated"},
        {**SOURCE, "at": (now + timedelta(hours=2)).isoformat(), "id": "future"},
    ]
    fetched = AsyncMock(return_value=(rows, []))
    monkeypatch.setattr(research, "news_sources", fetched)
    capability = AsyncMock(side_effect=AssertionError("This is research, not a connectivity question"))
    monkeypatch.setattr(research, "capability_answer", capability)
    model = SimpleNamespace(
        chat=AsyncMock(
            return_value={"choices": [{"message": {"content": "One verified public development [1]."}}]}
        )
    )
    result = await Jarvis(db, model, None, None).reply(LAST_DAY, conversation="day-window", spoken=True)
    assert result["sources"] == rows[:1]
    assert "since" in fetched.call_args.kwargs
    assert "Only publications since" in model.chat.call_args.args[0][1]["content"]
    capability.assert_not_called()


async def test_requested_window_without_evidence_is_honest_and_does_not_use_model(db, monkeypatch):
    monkeypatch.setattr(research, "news_sources", AsyncMock(return_value=([], [])))
    model = SimpleNamespace(chat=AsyncMock())
    answer = await research.answer(db, model, "AI news in the past 24 hours")
    assert "not proof that nothing happened" in answer["reply"]
    model.chat.assert_not_called()


def test_publication_instructions_stay_out_of_external_query(db):
    request = research.resolve_request(db, "Search the web for NVIDIA news in the last 24 hours", "query")
    assert request["query"] == "NVIDIA news in the last 24 hours"
    assert request["since"]


def test_real_speech_homophone_with_news_window_still_routes_public_ai():
    assert (
        research.intent(
            "Alright, well I see you also have access to the internet. Can you do a quick poll? What's happened in the last 24 hours in I, specifically local eye?"
        )
        == "briefing"
    )
    assert research.intent("What is the address of my local eye clinic?") is None


def test_atom_citations_use_article_not_later_comment_feed_link():
    payload = b"""<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Local inference</title>
    <link rel="alternate" type="text/html" href="https://example.com/article/"/>
    <published>2026-09-21T10:00:00Z</published>
    <link rel="replies" type="text/html" href="https://example.com/article/#comments"/>
    <link rel="replies" type="application/atom+xml" href="https://example.com/article/feed/"/>
    </entry></feed>"""
    assert research.parse_feed(payload, "Test")[0]["url"] == "https://example.com/article/"
    assert research.parse_feed(payload.replace(b'rel="alternate"', b'rel="self"'), "Test") == []
