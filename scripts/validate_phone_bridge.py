"""Synthetic Twilio transport into actual local voice services; no telephone call is placed."""

import asyncio
import audioop
import base64
import json
import time
import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from twilio.request_validator import RequestValidator

from jarvis.config import settings
from jarvis.db import Database
from jarvis.phone_stream import bridge


async def main():
    db = Database(settings.database_url)

    class SimulatedAccount:
        def get_state(self, key, default=None):
            return True if key == "setup.enabled.twilio" else db.get_state(key, default)

        def set_state(self, key, value):
            if key == "setup.phone_verified":
                raise AssertionError("A simulated transport cannot mark real telephone readiness")
            return db.set_state(key, value)

        def __getattr__(self, name):
            return getattr(db, name)

    nonce = "synthetic-" + uuid4().hex
    call_sid, stream_sid = "CA" + uuid4().hex, "MS" + uuid4().hex
    config = settings.model_copy(
        update={
            "twilio_account_sid": "ACsynthetic",
            "twilio_auth_token": "synthetic-protocol-validation",
            "public_webhook_url": "https://example.invalid",
        }
    )
    db.set_state(
        "call." + nonce, {"expires": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()}
    )
    db.set_state("call.sid." + call_sid, {"nonce": nonce})
    with wave.open("data/stt-workday-sample.wav", "rb") as audio:
        assert audio.getsampwidth() == 2 and audio.getnchannels() == 1
        pcm, _ = audioop.ratecv(audio.readframes(audio.getnframes()), 2, 1, audio.getframerate(), 8000, None)
    mulaw = audioop.lin2ulaw(pcm + bytes(16000), 2)
    frames = [
        {
            "event": "start",
            "streamSid": stream_sid,
            "start": {
                "accountSid": config.twilio_account_sid,
                "streamSid": stream_sid,
                "callSid": call_sid,
                "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1},
            },
        }
    ]
    frames.extend(
        {
            "event": "media",
            "streamSid": stream_sid,
            "media": {"payload": base64.b64encode(mulaw[offset : offset + 160]).decode()},
        }
        for offset in range(0, len(mulaw), 160)
    )
    output = []
    complete = asyncio.Event()
    started = time.monotonic()
    path = "/twilio/media/" + nonce

    class Socket:
        url = SimpleNamespace(path=path)
        headers = {
            "x-twilio-signature": RequestValidator(config.twilio_auth_token).compute_signature(
                config.public_webhook_url + path, {}
            )
        }

        async def accept(self):
            pass

        async def close(self, **kwargs):
            pass

        async def receive_text(self):
            if frames:
                return json.dumps(frames.pop(0))
            await complete.wait()
            return json.dumps({"event": "stop", "streamSid": stream_sid})

        async def send_json(self, value):
            output.append({"event": value["event"], "seconds": round(time.monotonic() - started, 3)})
            if value["event"] == "media":
                data = base64.b64decode(value["media"]["payload"], validate=True)
                assert len(data) > 100 and not data.startswith(b"RIFF")
            if value["event"] == "mark":
                complete.set()

    try:
        async with asyncio.timeout(180):
            await bridge(Socket(), nonce, config, SimulatedAccount())
        assert any(item["event"] == "media" for item in output), output
        report = {
            "transport": "synthetic Twilio",
            "voice_services": "real Nano services",
            "events": output,
            "telephone_call_placed": False,
        }
        Path("data/phone-bridge-validation.json").write_text(json.dumps(report, indent=2))
        print(json.dumps(report, indent=2))
    finally:
        for key in ("call." + nonce, "call.sid." + call_sid, "call.stream.used." + nonce):
            db.execute("DELETE FROM state WHERE key=%s", (key,))
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
