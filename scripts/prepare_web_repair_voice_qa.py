"""Public-only synthetic speech fixtures for the September 21 failed conversation."""

import json
import sys
from pathlib import Path

import httpx

day = "--day" in sys.argv
folder = Path("data/voice_web_day_qa" if day else "data/voice_web_repair_qa")
folder.mkdir(parents=True, exist_ok=True)
phrases = [
    "Well actually just give me an AI update. What's going on in the market? What's trending and how can I capitalize?",
    "No, I need you to use your web access. Do whatever the last research report was and tell me exactly what's been going on.",
    "You literally provided me an update the other day, so I don't know what's going on.",
]
if day:
    phrases = [
        "Alright, well I see you also have access to the internet. Can you do a quick poll? What's happened in the last twenty-four hours in AI, specifically local AI?"
    ]
with httpx.Client(timeout=120) as client:
    for index, text in enumerate(phrases):
        result = client.post("http://127.0.0.1:8186/synthesize", json={"text": text})
        result.raise_for_status()
        (folder / f"{index}.wav").write_bytes(result.content)
        print(json.dumps({"fixture": index, "created": True}), flush=True)
