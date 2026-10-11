"""Synthetic public research questions using the existing original voice."""

import json
from pathlib import Path

import httpx

folder = Path("data/voice_research_qa")
folder.mkdir(parents=True, exist_ok=True)
phrases = [
    "Jarvis, what is the weather today in Rio Rancho New Mexico?",
    "Give me my AI news briefing.",
    "What is the weather in San Jose California tomorrow?",
]
with httpx.Client(timeout=120) as client:
    for index, text in enumerate(phrases):
        result = client.post("http://127.0.0.1:8186/synthesize", json={"text": text})
        result.raise_for_status()
        (folder / f"{index}.wav").write_bytes(result.content)
        print(json.dumps({"fixture": index, "created": True}), flush=True)
