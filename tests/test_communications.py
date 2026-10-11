import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
from twilio.request_validator import RequestValidator

from jarvis.phone import Phone, valid_signature
from jarvis.telegram import Telegram


def config(**kwargs):
    return SimpleNamespace(
        telegram_token="synthetic",
        telegram_user_id=123,
        twilio_account_sid="ACsynthetic",
        twilio_auth_token="synthetic-signature-key",
        twilio_from="+15005550006",
        demo_phone="+15005550001",
        public_webhook_url="https://example.invalid",
        **kwargs,
    )


def test_telegram_rejects_other_users_and_groups():
    bot = Telegram(config(), None, None, None)
    assert bot.authorized({"from": {"id": 123}, "chat": {"id": 123, "type": "private"}})
    assert not bot.authorized({"from": {"id": 999}, "chat": {"id": 123, "type": "private"}})
    assert not bot.authorized({"from": {"id": 123}, "chat": {"id": -123, "type": "group"}})


async def test_duplicate_telegram_update_has_no_second_effect(db):
    jarvis = SimpleNamespace(reply=AsyncMock(return_value={"reply": "Synthetic response"}))
    bot = Telegram(config(), db, jarvis, None)
    bot.send = AsyncMock()
    update = {
        "update_id": 1,
        "message": {"text": "Hello", "from": {"id": 123}, "chat": {"id": 123, "type": "private"}},
    }
    await bot.process(update)
    result = await bot.process(update)
    assert result["duplicate"]
    jarvis.reply.assert_awaited_once()


def test_twilio_signature_binds_url_and_payload():
    token = "synthetic-signature-key"
    url = "https://example.invalid/twilio/status/nonce"
    body = {"CallSid": "CAsynthetic", "CallStatus": "no-answer"}
    signature = RequestValidator(token).compute_signature(url, body)
    assert valid_signature(token, url, body, signature)
    assert not valid_signature(token, url, {**body, "CallStatus": "completed"}, signature)
    assert not valid_signature(token, url + "/other", body, signature)
    assert not valid_signature(token, url, body, "")


async def test_phone_only_calls_configured_demo_and_quiet_blocks(db):
    calls = []

    def transport(request):
        calls.append(request)
        return httpx.Response(201, json={"sid": "CAsynthetic"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        phone = Phone(config(), db, client=client)
        await phone.call_demo("synthetic:brief")
        assert b"To=%2B15005550001" in calls[0].content
        assert b"MachineDetection=Enable" in calls[0].content
        db.set_state(
            "communication_rule",
            {"mode": "quiet", "until": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()},
        )
        assert (await phone.call_demo("synthetic:brief"))["suppressed"]
        assert len(calls) == 1


async def test_voicemail_unknown_and_duplicate_status_use_single_written_fallback(db):
    db.set_state(
        "call.nonce",
        {"expires": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(), "briefing_id": "brief"},
    )
    db.set_state("call.sid.CAsynthetic", {"nonce": "nonce"})
    db.set_state(
        "brief",
        {"text": "Private synthetic briefing", "audio_url": "https://example.invalid/twilio/audio/private"},
    )
    telegram = SimpleNamespace(send=AsyncMock())
    phone = Phone(config(), db, telegram)
    assert "private" not in phone.answer("nonce", "machine_start")
    assert "<Hangup" in phone.answer("nonce", "unknown")
    results = await asyncio.gather(
        *[phone.status("nonce", {"CallSid": "CAsynthetic", "CallStatus": "completed"}) for _ in range(2)]
    )
    assert any(result.get("duplicate") for result in results)
    telegram.send.assert_awaited_once_with("Private synthetic briefing", proactive=True)
