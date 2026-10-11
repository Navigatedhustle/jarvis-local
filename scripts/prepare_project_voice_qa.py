"""Generate synthetic prompts through existing TTS; never record Demo's microphone."""

import json
from pathlib import Path

import httpx

folder = Path("data/voice_project_qa")
folder.mkdir(parents=True, exist_ok=True)
with httpx.Client(timeout=120) as client:
    for index, text in enumerate(
        [
            "What is the state of lighthouse accounts?",
            "Give me a project brief on ZGX Nano.",
            "What is the status of L H A?",
        ]
    ):
        response = client.post("http://127.0.0.1:8186/synthesize", json={"text": text})
        response.raise_for_status()
        (folder / f"{index}.wav").write_bytes(response.content)
        print(json.dumps({"fixture": index, "created": True}), flush=True)
