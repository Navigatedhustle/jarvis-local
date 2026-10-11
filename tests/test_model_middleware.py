import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from jarvis.model_middleware import SpeechPriorityMiddleware


async def test_speech_lease_preserves_work_and_wrong_release_cannot_resume():
    app = FastAPI()
    engine = SimpleNamespace(pause_generation=AsyncMock(), resume_generation=AsyncMock())
    app.state.engine_client = engine
    app.add_middleware(SpeechPriorityMiddleware)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://model") as client:
        assert (await client.get("/_jarvis/speech/acquire")).status_code == 405
        lease = (await client.post("/_jarvis/speech/acquire")).json()["lease"]
        engine.pause_generation.assert_awaited_once_with(mode="keep", clear_cache=False)
        assert (await client.post("/_jarvis/speech/acquire")).status_code == 409
        await client.post("/_jarvis/speech/release", json={"lease": "stale"})
        engine.resume_generation.assert_not_awaited()
        await client.post("/_jarvis/speech/release", json={"lease": lease})
        engine.resume_generation.assert_awaited_once()


@pytest.mark.parametrize("pause_fails", [False, True])
async def test_abandoned_or_uncertain_pause_automatically_resumes(monkeypatch, pause_fails):
    monkeypatch.setattr(SpeechPriorityMiddleware, "lease_seconds", 0.01)
    resumed = asyncio.Event()
    app = FastAPI()
    app.state.engine_client = SimpleNamespace(
        pause_generation=AsyncMock(side_effect=RuntimeError() if pause_fails else None),
        resume_generation=AsyncMock(side_effect=lambda: resumed.set()),
    )
    app.add_middleware(SpeechPriorityMiddleware)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://model") as client:
        response = await client.post("/_jarvis/speech/acquire")
        assert response.status_code == (503 if pause_fails else 200)
        await asyncio.wait_for(resumed.wait(), 1)
