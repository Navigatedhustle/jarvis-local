"""Create synthetic acceptance utterances using the local speech service."""

import json
from pathlib import Path

import httpx

folder = Path("data/voice_acceptance")
folder.mkdir(parents=True, exist_ok=True)
phrases = [
    "Jarvis, what can you do for me?",
    "Keep it brief. What's tomorrow look like?",
    "Stop. Just give me the first meeting.",
]
with httpx.Client(timeout=150) as client:
    for index, text in enumerate(phrases):
        result = client.post("http://127.0.0.1:8186/synthesize", json={"text": text})
        result.raise_for_status()
        (folder / f"utterance-{index}.wav").write_bytes(result.content)
        check = client.post(
            "http://127.0.0.1:8185/transcribe", files={"file": ("sample.wav", result.content, "audio/wav")}
        )
        check.raise_for_status()
        print(json.dumps({"utterance": index, **check.json()}), flush=True)
