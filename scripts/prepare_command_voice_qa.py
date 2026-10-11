"""Create synthetic inputs for the live Command Center voice acceptance."""

import json
from pathlib import Path

import httpx

folder = Path("data/voice_command_qa")
folder.mkdir(parents=True, exist_ok=True)
phrases = [
    "What actually needs my attention today?",
    "What am I waiting on?",
    "What promises am I at risk of breaking?",
]
with httpx.Client(timeout=120) as client:
    for index, text in enumerate(phrases):
        result = client.post("http://127.0.0.1:8186/synthesize", json={"text": text})
        result.raise_for_status()
        (folder / f"{index}.wav").write_bytes(result.content)
        print(json.dumps({"fixture": index, "created": True}), flush=True)
