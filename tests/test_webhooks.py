from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from twilio.request_validator import RequestValidator

from jarvis import webhooks
from jarvis.phone import Phone


@pytest.fixture
def gateway(db, monkeypatch):
    monkeypatch.setattr(webhooks, "db", db)
    monkeypatch.setattr(webhooks.settings, "twilio_auth_token", "synthetic")
    monkeypatch.setattr(webhooks.settings, "twilio_account_sid", "ACsynthetic")
    monkeypatch.setattr(webhooks.settings, "public_webhook_url", "https://example.invalid")
    monkeypatch.setattr(webhooks, "phone", Phone(webhooks.settings, db))
    db.set_state(
        "call.nonce",
        {"expires": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(), "briefing_id": "brief"},
    )
    db.set_state("call.sid.CAsynthetic", {"nonce": "nonce"})
    db.set_state("brief", {"audio_url": "https://example.invalid/twilio/audio/private"})
    return TestClient(webhooks.app)


def signed(client, path, form):
    signature = RequestValidator("synthetic").compute_signature("https://example.invalid" + path, form)
    return client.post(path, data=form, headers={"X-Twilio-Signature": signature})


def test_gateway_binds_real_outbound_call_and_never_serves_admin(gateway):
    for path in ("/health", "/today", "/setup", "/docs", "/api/chat", "/v1/models"):
        assert gateway.get(path).status_code == 404
    path = "/twilio/answer/nonce"
    form = {"CallSid": "CAsynthetic", "AccountSid": "ACsynthetic", "AnsweredBy": "human"}
    assert gateway.post(path, data=form).status_code == 403
    assert signed(gateway, path, form).status_code == 200
    assert "<Play>" in signed(gateway, path, form).text
    assert "<Hangup" in signed(gateway, path, {**form, "AnsweredBy": "machine_start"}).text
    assert signed(gateway, path, {**form, "CallSid": "wrong"}).status_code == 404


def test_gateway_rejects_oversized_and_ambiguous_forms(gateway):
    assert gateway.post("/twilio/status/nonce", content=b"x" * 64001).status_code == 413
    assert (
        gateway.post(
            "/twilio/status/nonce",
            content="CallSid=a&CallSid=b",
            headers={"content-type": "application/x-www-form-urlencoded"},
        ).status_code
        == 400
    )
