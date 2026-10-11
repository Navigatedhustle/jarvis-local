"""Live smoke test: only synthetic prompts; metrics contain no credentials or HP data."""

import asyncio
import json
import time
from pathlib import Path

import httpx

from jarvis.config import settings
from jarvis.inference import Inference


async def main():
    model = Inference(settings)
    report = {}
    vector = json.loads(await model.embed("JARVIS synthetic retrieval check"))
    report["embedding_dimensions"] = len(vector)
    report["rerank"] = await model.rerank(
        "meeting preparation", ["Prepare for a customer meeting", "Banana bread"]
    )
    async with httpx.AsyncClient(timeout=240) as client:
        for role, url in [("primary", settings.primary_url), ("background", settings.background_url)]:
            start = time.monotonic()
            payload = {
                "model": "jarvis-" + role,
                "messages": [{"role": "user", "content": "Reply with exactly JARVIS_READY."}],
                "max_tokens": 64,
                "stream": True,
                "chat_template_kwargs": {"enable_thinking": False},
            }
            first = None
            text = ""
            async with client.stream("POST", url + "/v1/chat/completions", json=payload) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if line.startswith("data: ") and line != "data: [DONE]":
                        part = json.loads(line[6:])
                        choices = part.get("choices", [])
                        if choices and choices[0].get("delta", {}).get("content"):
                            first = first or time.monotonic()
                            text += choices[0]["delta"]["content"]
            report[role] = {
                "ttft_seconds": round((first - start), 3) if first else None,
                "total_seconds": round(time.monotonic() - start, 3),
                "reply": text,
            }
    Path("data/runtime-validation.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
