"""Synthetic next-meeting prompts through the existing voice model."""

import json
from pathlib import Path

import httpx

folder = Path("data/voice_meeting_qa")
folder.mkdir(parents=True, exist_ok=True)
with httpx.Client(timeout=120) as client:
    for index, text in enumerate(
        [
            "Prepare me for my next meeting.",
            "Brief me on my next meeting.",
            "What is the state of lighthouse accounts?",
        ]
    ):
        response = client.post("http://127.0.0.1:8186/synthesize", json={"text": text})
        response.raise_for_status()
        (folder / f"{index}.wav").write_bytes(response.content)
        print(json.dumps({"fixture": index, "created": True}), flush=True)
