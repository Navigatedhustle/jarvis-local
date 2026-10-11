"""Synthetic inputs only; outputs stay in ignored runtime data."""

import json
from pathlib import Path

import httpx

folder = Path("data/voice_mail_qa")
folder.mkdir(parents=True, exist_ok=True)
phrases = [
    "Summarize the last five emails that Stephanie Belace, my manager, sent me.",
    "Summarize the second email thread. What should I know?",
    "Show me emails from Mike Manager about Sandbox.",
]
with httpx.Client(timeout=120) as client:
    for index, text in enumerate(phrases):
        result = client.post("http://127.0.0.1:8186/synthesize", json={"text": text})
        result.raise_for_status()
        (folder / f"{index}.wav").write_bytes(result.content)
        print(json.dumps({"fixture": index, "created": True}), flush=True)
