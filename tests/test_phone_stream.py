import asyncio
import base64
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from twilio.request_validator import RequestValidator

from jarvis import phone_stream
from jarvis.phone import Phone
from jarvis.policy import Actor, PolicyError
from jarvis.voice import wav_bytes


def seeded_call(db):
    settings = SimpleNamespace(
        twilio_account_sid="ACsynthetic",
        twilio_auth_token="synthetic-token",
        public_webhook_url="https://example.invalid",
        api_token="synthetic-local-token",
    )
    db.set_state(
        "call.nonce",
        {"expires": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(), "briefing_id": "brief"},
    )
    db.set_state("call.sid.CAsynthetic", {"nonce": "nonce"})
    db.set_state("brief", {"audio_url": "https://example.invalid/twilio/audio/example"})
    start = {
        "event": "start",
        "streamSid": "MSsynthetic",
        "start": {
            "streamSid": "MSsynthetic",
            "callSid": "CAsynthetic",
            "accountSid": "ACsynthetic",
            "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1},
        },
    }
    return settings, start


def test_cleared_audio_acknowledgement_does_not_verify_a_phone_conversation():
    pending, cleared = {"played", "interrupted"}, {"interrupted"}
    assert not phone_stream.acknowledge_playback("unknown", pending, cleared)
    assert not phone_stream.acknowledge_playback("interrupted", pending, cleared)
    assert phone_stream.acknowledge_playback("played", pending, cleared)
    assert not phone_stream.acknowledge_playback("played", pending, cleared)


def test_phone_codec_and_outbound_authority(db):
    assert phone_stream.wav_to_mulaw(wav_bytes(bytes(3200))) == bytes([255]) * 800
    settings, start = seeded_call(db)
    assert phone_stream.start_is_authorized(db, settings, "nonce", start)
    assert not phone_stream.start_is_authorized(db, settings, "wrong", start)
    start["start"]["accountSid"] = "ACother"
    assert not phone_stream.start_is_authorized(db, settings, "nonce", start)
    assert '<Connect><Stream url="wss://example.invalid/twilio/media/nonce"' in Phone(settings, db).answer(
        "nonce", "human"
    )
    with pytest.raises(PolicyError):
        Actor("demo", "phone").require_demo()


async def test_phone_stream_rejects_unsigned_socket_before_accepting():
    websocket = SimpleNamespace(
        url=SimpleNamespace(path="/twilio/media/nonce"), headers={}, close=AsyncMock(), accept=AsyncMock()
    )
    settings = SimpleNamespace(public_webhook_url="https://example.invalid", twilio_auth_token="synthetic")
    await phone_stream.bridge(websocket, "nonce", settings, None)
    websocket.close.assert_awaited_once_with(code=1008)
    websocket.accept.assert_not_awaited()


async def test_signed_media_bridge_sends_mulaw_clear_and_consumes_nonce(db, monkeypatch):
    settings, start = seeded_call(db)
    url = settings.public_webhook_url + "/twilio/media/nonce"
    signature = RequestValidator(settings.twilio_auth_token).compute_signature(url, {})
    sent = []

    class Socket:
        url = SimpleNamespace(path="/twilio/media/nonce")
        headers = {"x-twilio-signature": signature}
        accept = AsyncMock()
        close = AsyncMock()
        first = True

        async def receive_text(self):
            if self.first:
                self.first = False
                return json.dumps(start)
            await asyncio.Event().wait()

        async def send_json(self, value):
            sent.append(value)

    class Local:
        async def __aiter__(self):
            yield json.dumps({"type": "cancel"})
            yield json.dumps(
                {
                    "type": "audio",
                    "data": base64.b64encode(wav_bytes(bytes(3200))).decode(),
                    "turn_id": "synthetic-turn",
                }
            )

    @asynccontextmanager
    async def connect(*args, **kwargs):
        assert args[0].endswith("channel=phone")
        yield Local()

    def handler(request):
        return httpx.Response(
            200, json={"authenticated": True}, headers={"set-cookie": "jarvis_session=synthetic; Path=/"}
        )

    factory = httpx.AsyncClient
    monkeypatch.setattr(phone_stream.websockets, "connect", connect)
    monkeypatch.setattr(
        phone_stream.httpx,
        "AsyncClient",
        lambda **kwargs: factory(transport=httpx.MockTransport(handler), **kwargs),
    )
    await phone_stream.bridge(Socket(), "nonce", settings, db)
    assert [event["event"] for event in sent] == ["clear", "media", "mark"]
    assert base64.b64decode(sent[1]["media"]["payload"]) == bytes([255]) * 800
    sent.clear()
    await phone_stream.bridge(Socket(), "nonce", settings, db)
    assert not sent  # The signed call's stream authority cannot be replayed.
