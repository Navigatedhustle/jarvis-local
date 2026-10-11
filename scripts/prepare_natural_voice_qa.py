"""Synthetic local speech for the actual unresolved research requests."""

import json
from pathlib import Path

import httpx

phrases = [
    "Can you use the search query local AI in higher education and tell me what's happening?",
    "Use your current web access and create a new report. Search for about five minutes, do some research, and tell me what's been happening.",
    "You sent the PDF to my own email. Find the last sent message to myself and use that PDF to give me a summary.",
    "Jarvis, tell me a joke. You can joke with me, you know.",
]
folder = Path("data/voice_natural_qa")
folder.mkdir(parents=True, exist_ok=True)
with httpx.Client(timeout=120) as client:
    for i, text in enumerate(phrases):
        result = client.post("http://127.0.0.1:8186/synthesize", json={"text": text})
        result.raise_for_status()
        (folder / f"{i}.wav").write_bytes(result.content)
        print(json.dumps({"fixture": i, "ready": True}), flush=True)
