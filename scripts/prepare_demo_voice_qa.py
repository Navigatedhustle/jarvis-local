"""Synthetic fixtures for the actual September 21 demo failure sequence."""

import json
from pathlib import Path

import httpx

folder = Path("data/voice_demo_qa")
folder.mkdir(parents=True, exist_ok=True)
phrases = [
    "Hey Jarvis, what do I got going on tomorrow? Let me know.",
    "Dang check out the calendar on Outlook. Why can't you see it?",
    "Hey Jarvis, the week of September 21st.",
    "Because he's only local. All right, well, what does local AI look like for the competitive report? How can I capitalize on it?",
    "Local AI competitiveness against HP.",
    "All right, and what does my email look like? Anything new?",
]
with httpx.Client(timeout=120) as client:
    for index, text in enumerate(phrases):
        result = client.post("http://127.0.0.1:8186/synthesize", json={"text": text})
        result.raise_for_status()
        (folder / f"{index}.wav").write_bytes(result.content)
        print(json.dumps({"fixture": index, "created": True}), flush=True)
