import asyncio
import json
import time
from pathlib import Path

import httpx


async def main():
    start = time.monotonic()
    async with httpx.AsyncClient(timeout=180) as client:
        audio = await client.post(
            "http://127.0.0.1:8186/synthesize",
            json={
                "text": "Good morning, Demo. Three things deserve your attention. Your local assistant is ready."
            },
        )
        audio.raise_for_status()
        tts = time.monotonic() - start
        Path("data/voice-sample.wav").write_bytes(audio.content)
        transcript = await client.post(
            "http://127.0.0.1:8185/transcribe", files={"file": ("sample.wav", audio.content, "audio/wav")}
        )
        transcript.raise_for_status()
    report = {
        "tts_seconds": round(tts, 3),
        "audio_bytes": len(audio.content),
        "roundtrip_seconds": round(time.monotonic() - start, 3),
        **transcript.json(),
    }
    Path("data/voice-validation.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
