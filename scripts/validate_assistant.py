"""Live product smoke check; writes a private report without credentials."""

import json
import time
from pathlib import Path

import httpx

from jarvis.config import settings

with httpx.Client(
    base_url="http://127.0.0.1:8100", headers={"Authorization": "Bearer " + settings.api_token}, timeout=180
) as client:
    health = client.get("/health")
    health.raise_for_status()
    assert all(value == "ready" for value in health.json()["models"].values())
    assert health.json()["voice"]["stt"] == health.json()["voice"]["tts"] == "ready"
    started = time.monotonic()
    answer = client.post("/chat", json={"text": "Jarvis, what actually needs my attention today?"})
    answer.raise_for_status()
    report = {
        "health": health.json(),
        "attention_response": answer.json(),
        "answer_seconds": round(time.monotonic() - started, 3),
    }
    Path("data/assistant-validation.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
