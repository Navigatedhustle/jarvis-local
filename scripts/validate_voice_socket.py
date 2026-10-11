"""Live authenticated voice socket + immediate interruption check using synthetic audio."""

import asyncio
import json
import time
import wave
from pathlib import Path

import httpx
import websockets

from jarvis.config import settings


async def main():
    async with httpx.AsyncClient() as client:
        response = await client.post(
            "http://127.0.0.1:3100/api/auth/login", json={"token": settings.api_token}
        )
        response.raise_for_status()
        cookie = "jarvis_session=" + response.cookies["jarvis_session"]
    with wave.open("data/stt-sample.wav", "rb") as audio:
        assert audio.getframerate() == 16000 and audio.getsampwidth() == 2
        pcm = audio.readframes(audio.getnframes()) + bytes(16000 * 2)
    start = time.monotonic()
    events = []
    async with websockets.connect(
        "ws://127.0.0.1:3100/api/voice",
        origin="http://127.0.0.1:3100",
        additional_headers={"Cookie": cookie},
        max_size=8_000_000,
    ) as ws:
        for offset in range(0, len(pcm), 640):
            await ws.send(pcm[offset : offset + 640])
        async with asyncio.timeout(180):
            async for raw in ws:
                event = json.loads(raw)
                events.append({"type": event["type"], "seconds": round(time.monotonic() - start, 3)})
                if event["type"] == "error":
                    raise AssertionError(event["text"])
                if event["type"] == "audio":
                    interrupted = time.monotonic()
                    await ws.send("cancel")
                    while True:
                        result = json.loads(await ws.recv())
                        if result["type"] == "cancel":
                            latency = (time.monotonic() - interrupted) * 1000
                            assert latency < 1000
                            break
                    break
    assert any(e["type"] == "audio" for e in events)
    report = {"events": events, "cancellation_ms": round(latency, 2)}
    Path("data/voice-socket-validation.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
