"""Reproduce Demo's routing failures with synthetic local speech, not a microphone acceptance claim."""

import json
from pathlib import Path

import httpx

folder = Path("data/voice_access_qa")
folder.mkdir(parents=True, exist_ok=True)
phrases = [
    "There we go, what time does my day start tomorrow?",
    "What can you do now, Jarvis? Do you have internet access?",
    "What's the weather in Rio Rancho New Mexico tomorrow?",
]
with httpx.Client(timeout=120) as client:
    for index, text in enumerate(phrases):
        result = client.post("http://127.0.0.1:8186/synthesize", json={"text": text})
        result.raise_for_status()
        (folder / f"{index}.wav").write_bytes(result.content)
        print(json.dumps({"fixture": index, "created": True}), flush=True)
