"""Synthetic voice fixtures; source records and model choices are untouched."""

import json
from pathlib import Path

import httpx

folder = Path("data/voice_relationship_qa")
folder.mkdir(parents=True, exist_ok=True)
with httpx.Client(timeout=120) as client:
    for index, text in enumerate(
        [
            "What do I owe my manager?",
            "When did I last interact with my manager?",
            "What did my manager ask me about?",
        ]
    ):
        result = client.post("http://127.0.0.1:8186/synthesize", json={"text": text})
        result.raise_for_status()
        (folder / f"{index}.wav").write_bytes(result.content)
        print(json.dumps({"fixture": index, "created": True}), flush=True)
