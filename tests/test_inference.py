import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from jarvis import inference


def transport(monkeypatch, events, finished=True):
    lines = ["data: " + json.dumps({"choices": [{"delta": event}]}) for event in events]
    if finished:
        lines.append("data: [DONE]")
    client = httpx.AsyncClient
    monkeypatch.setattr(
        inference.httpx,
        "AsyncClient",
        lambda **kwargs: client(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, text="\n\n".join(lines)))
        ),
    )


async def test_voice_stream_assembles_tools_and_discards_hidden_reasoning(monkeypatch):
    transport(
        monkeypatch,
        [
            {"reasoning_content": "PRIVATE REASONING"},
            {
                "tool_calls": [
                    {"index": 0, "id": "call-1", "function": {"name": "calendar", "arguments": '{"start":'}}
                ]
            },
            {"tool_calls": [{"index": 0, "function": {"arguments": '"date"}'}}]},
        ],
    )
    model = inference.Inference(SimpleNamespace(primary_url="http://local.invalid"))
    first = Mock()
    result = await model.chat([], on_first_token=first)
    first.assert_called_once()
    message = result["choices"][0]["message"]
    assert message["tool_calls"][0]["function"] == {"name": "calendar", "arguments": '{"start":"date"}'}
    assert "PRIVATE" not in json.dumps(result)
    assert model.interactive == 0


async def test_voice_text_callback_streams_only_visible_content(monkeypatch):
    transport(
        monkeypatch,
        [{"reasoning_content": "hidden"}, {"content": "First sentence. "}, {"content": "Next sentence."}],
    )
    model = inference.Inference(SimpleNamespace(primary_url="http://local.invalid"))
    delivered = []

    async def text(delta):
        delivered.append(delta)

    result = await model.chat([], on_text=text)
    assert delivered == ["First sentence. ", "Next sentence."]
    assert result["choices"][0]["message"]["content"] == "".join(delivered)


async def test_tool_arguments_are_never_spoken(monkeypatch):
    transport(
        monkeypatch,
        [
            {"tool_calls": [{"index": 0, "id": "tool", "function": {"name": "search", "arguments": "{}"}}]},
            {"content": "not speech after tool"},
        ],
    )
    model = inference.Inference(SimpleNamespace(primary_url="http://local.invalid"))
    delivered = []

    async def text(delta):
        delivered.append(delta)

    await model.chat([], on_text=text)
    assert not delivered


async def test_preamble_before_late_tool_call_is_not_spoken(monkeypatch):
    transport(
        monkeypatch,
        [
            {"content": "Monday is today, and your week is clear. "},
            {"tool_calls": [{"index": 0, "id": "c", "function": {"name": "calendar", "arguments": "{}"}}]},
        ],
    )
    model = inference.Inference(SimpleNamespace(primary_url="http://local.invalid"))
    delivered = AsyncMock()
    await model.chat([], tools=[{"type": "function"}], on_text=delivered)
    delivered.assert_not_awaited()


async def test_incomplete_voice_stream_fails_without_returning_partial_answer(monkeypatch):
    transport(monkeypatch, [{"content": "Incomplete"}], finished=False)
    model = inference.Inference(SimpleNamespace(primary_url="http://local.invalid"))
    with pytest.raises(inference.ModelUnavailable):
        await model.chat([], on_first_token=Mock())
    assert model.interactive == 0


async def test_internal_primary_preparation_cancels_for_live_interaction(monkeypatch):
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def handler(request):
        entered.set()
        try:
            await asyncio.sleep(60)
        finally:
            cancelled.set()

    client = httpx.AsyncClient
    monkeypatch.setattr(
        inference.httpx, "AsyncClient", lambda **kwargs: client(transport=httpx.MockTransport(handler))
    )
    model = inference.Inference(SimpleNamespace(primary_url="http://local.invalid"))
    task = asyncio.create_task(model.chat([], effort="medium", defer_to_interactive=True))
    await entered.wait()
    assert model.interactive == 0
    model.interactive = 1
    with pytest.raises(inference.InferenceDeferred):
        await asyncio.wait_for(task, 2)
    assert cancelled.is_set() and model.interactive == 1


async def test_foreground_intent_routing_uses_background_model_during_voice(monkeypatch):
    seen = []

    def handler(request):
        seen.append((str(request.url), json.loads(request.content)))
        return httpx.Response(
            200, json={"choices": [{"message": {"content": '{"route":"public","topic":"Anthropic"}'}}]}
        )

    client = httpx.AsyncClient
    monkeypatch.setattr(
        inference.httpx, "AsyncClient", lambda **kwargs: client(transport=httpx.MockTransport(handler))
    )
    model = inference.Inference(
        SimpleNamespace(primary_url="http://primary.invalid", background_url="http://background.invalid")
    )
    model.interactive = 1
    with pytest.raises(inference.InferenceDeferred):
        await model.chat([], background=True)
    assert not seen
    await model.chat([], background=True, interactive_routing=True, max_tokens=120)
    assert len(seen) == 1 and seen[0][0].startswith("http://background.invalid/")
    assert seen[0][1]["max_tokens"] == 120 and "tools" not in seen[0][1]
    assert model.interactive == 1


async def test_reasoning_reserves_answer_budget_and_rejects_cutoff(monkeypatch):
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={"choices": [{"finish_reason": "length", "message": {"content": "An incomplete draft ["}}]},
        )

    client = httpx.AsyncClient
    monkeypatch.setattr(
        inference.httpx, "AsyncClient", lambda **kwargs: client(transport=httpx.MockTransport(handler))
    )
    model = inference.Inference(SimpleNamespace(primary_url="http://local.invalid"))
    with pytest.raises(inference.ModelUnavailable):
        await model.chat([], effort="medium")
    assert seen[0]["thinking_token_budget"] == 512 < seen[0]["max_tokens"]
    assert seen[0]["include_reasoning"] is False
