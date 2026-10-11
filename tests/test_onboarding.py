import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

from jarvis import api
from jarvis.account_vault import AccountVault, apply_accounts
from jarvis.config import Settings
from jarvis.onboarding import AUTH, CONFIG, DEGRADED, OFFLINE, READY, Setup, integration_state
from jarvis.outlook import MicrosoftAuth
from jarvis.phone import Phone
from jarvis.telegram import Telegram


def local_settings(tmp_path, **values):
    return Settings(
        _env_file=None,
        secret_dir=tmp_path / "secrets",
        data_dir=tmp_path / "data",
        api_token="synthetic-local-key-over-32-characters",
        **values,
    )


def healthy():
    return {
        "database": True,
        "models": dict.fromkeys(("primary", "background", "embedding", "reranker"), "ready"),
        "voice": {"stt": "ready", "tts": "ready"},
        "outlook": {"last_sync": None, "stale": True, "error": None},
        "demo_mode": False,
    }


def telegram_values():
    return {"telegram_token": "123456789:" + "X" * 35, "telegram_user_id": "123456789"}


async def test_offline_speech_copy_does_not_claim_ready(db, tmp_path):
    settings = local_settings(tmp_path)
    setup = Setup(settings, db, None, MicrosoftAuth(settings))
    health = healthy()
    health["voice"] = {"stt": "offline", "tts": "offline"}
    snapshot = await setup.snapshot(health)
    for row in snapshot["readiness"]:
        if row["name"] in ("Voice input", "Voice output"):
            assert row["state"] == OFFLINE and "unavailable" in row["detail"]


async def test_browser_expired_auth_is_distinct_from_transient_failure(db, tmp_path):
    settings = local_settings(tmp_path)
    setup = Setup(settings, db, None, MicrosoftAuth(settings))
    db.set_state("outlook.provider", "browser")
    db.set_state("setup.enabled.outlook", True)
    health = healthy()
    for error_class, state in (("AuthRequired", AUTH), ("BrowserUnavailable", DEGRADED)):
        health["outlook"]["error"] = {"error_class": error_class}
        snapshot = await setup.snapshot(health)
        assert snapshot["integrations"]["outlook"]["state"] == state
        assert snapshot["integrations"]["outlook"]["status"] == "Needs Attention"


def test_states_do_not_conflate_saved_configuration_with_verified_access():
    now = datetime.now(timezone.utc)
    assert integration_state(False)["state"] == CONFIG
    assert integration_state(True)["state"] == AUTH
    observation = {
        "state": READY,
        "checked_at": now.isoformat(),
        "last_success": now.isoformat(),
        "detail": "Verified",
    }
    assert integration_state(True, observation)["status"] == "Connected"
    assert integration_state(True, observation, enabled=False)["state"] == AUTH
    assert integration_state(True, observation, now=now + timedelta(days=2))["state"] == DEGRADED
    assert integration_state(False, observation)["status"] == "Not Connected"


def test_account_vault_is_encrypted_reloads_and_can_mask_legacy_credentials(tmp_path):
    settings = local_settings(tmp_path)
    vault = AccountVault(settings.secret_dir)
    value = telegram_values()
    vault.update(value)
    assert value["telegram_token"].encode() not in vault.path.read_bytes()
    assert not (settings.secret_dir / "runtime.env").exists()
    assert vault.load() == value
    vault.update({"telegram_token": ""})
    settings.telegram_token = "legacy-token"
    assert apply_accounts(settings).telegram_token == ""
    vault.path.write_bytes(b"damaged")
    with pytest.raises(Exception):
        vault.load()  # Do not fall back to a previously revoked environment value.


async def test_unconfigured_tests_never_contact_any_provider(db, tmp_path):
    settings = local_settings(tmp_path)
    factory = AsyncMock(side_effect=AssertionError("External network was attempted"))
    graph = SimpleNamespace(request=AsyncMock(side_effect=AssertionError("Graph called")))
    setup = Setup(settings, db, graph, MicrosoftAuth(settings), client_factory=factory)
    for provider in ("outlook", "telegram", "twilio"):
        result = await setup.test(provider)
        assert result["state"] == CONFIG and result["last_success"] is None
        assert "No external connection" in integration_state(False, result)["detail"]
    graph.request.assert_not_awaited()
    factory.assert_not_called()


async def test_save_disconnect_preserve_data_and_never_echo_credentials(db, tmp_path):
    settings = local_settings(tmp_path)
    setup = Setup(settings, db, None, MicrosoftAuth(settings))
    secret = telegram_values()["telegram_token"]
    await setup.save("telegram", telegram_values())
    assert not db.get_state("setup.enabled.telegram")
    snapshot = await setup.snapshot(healthy())
    assert snapshot["integrations"]["telegram"]["state"] == AUTH
    assert secret not in json.dumps(snapshot, default=str)
    assert secret not in json.dumps(db.query("SELECT * FROM audit"), default=str)
    db.set_state("synthetic.work", {"preserve": True})
    await setup.disconnect("telegram")
    assert settings.telegram_token == "" and AccountVault(settings.secret_dir).load()["telegram_token"] == ""
    assert db.get_state("synthetic.work")["preserve"]


@pytest.mark.parametrize("failure,expected", [(401, AUTH), (503, DEGRADED), (None, OFFLINE)])
async def test_failed_checks_keep_last_success_without_claiming_connected(db, tmp_path, failure, expected):
    settings = local_settings(tmp_path, **telegram_values())
    last = datetime.now(timezone.utc).isoformat()
    db.set_state("setup.check.telegram", {"last_success": last})

    def handler(request):
        if failure is None:
            raise httpx.ConnectError("A secret URL must not be exposed", request=request)
        return httpx.Response(failure, text="SECRET PROVIDER RESPONSE")

    setup = Setup(
        settings,
        db,
        None,
        MicrosoftAuth(settings),
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    result = await setup.test("telegram")
    assert result["state"] == expected and result["last_success"] == last
    assert "SECRET" not in str(result) and settings.telegram_token not in str(result)


async def test_telegram_identity_and_webhook_conflicts_are_verified_without_sending(db, tmp_path):
    settings = local_settings(tmp_path, **telegram_values())
    requests = []

    def handler(request):
        requests.append(request.url.path.rsplit("/", 1)[-1])
        values = {
            "getMe": {"is_bot": True},
            "getChat": {"type": "private", "id": settings.telegram_user_id},
            "getWebhookInfo": {"url": "https://existing.example/webhook"},
        }
        return httpx.Response(200, json={"ok": True, "result": values[requests[-1]]})

    setup = Setup(
        settings,
        db,
        None,
        MicrosoftAuth(settings),
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    result = await setup.test("telegram")
    assert result["state"] == DEGRADED
    assert requests == ["getMe", "getChat", "getWebhookInfo"]


async def test_twilio_account_success_does_not_fabricate_phone_readiness(db, tmp_path, monkeypatch):
    settings = local_settings(
        tmp_path,
        twilio_account_sid="AC" + "0" * 32,
        twilio_auth_token="1" * 32,
        twilio_from="+15005550006",
        demo_phone="+15005550001",
        public_webhook_url="https://example.invalid",
    )
    methods = []

    def handler(request):
        methods.append(request.method)
        payload = (
            {"status": "active"}
            if "IncomingPhoneNumbers" not in request.url.path
            else {
                "incoming_phone_numbers": [
                    {"phone_number": settings.twilio_from, "capabilities": {"voice": True}}
                ]
            }
        )
        return httpx.Response(200, json=payload)

    setup = Setup(
        settings,
        db,
        None,
        MicrosoftAuth(settings),
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    assert (await setup.test("twilio"))["state"] == READY
    snapshot = await setup.snapshot(healthy())
    assert next(row for row in snapshot["readiness"] if row["name"] == "Telephone")["state"] == DEGRADED
    assert snapshot["phone_verified"] is None and methods == ["GET", "GET"]
    db.set_state("setup.phone_verified", datetime.now(timezone.utc).isoformat())
    monkeypatch.setattr(setup, "gateway_available", AsyncMock(return_value=False))
    snapshot = await setup.snapshot(healthy())
    assert next(row for row in snapshot["readiness"] if row["name"] == "Telephone")["state"] == DEGRADED
    monkeypatch.setattr(setup, "gateway_available", AsyncMock(return_value=True))
    snapshot = await setup.snapshot(healthy())
    assert next(row for row in snapshot["readiness"] if row["name"] == "Telephone")["state"] == READY


async def test_saved_disabled_connections_cannot_contact_demo(db, tmp_path):
    settings = local_settings(tmp_path, **telegram_values())
    db.set_state("setup.enabled.telegram", False)
    db.set_state("setup.enabled.twilio", False)
    assert (await Telegram(settings, db, None, None).send("Synthetic", proactive=True))["suppressed"]
    assert "blocked" in await Phone(settings, db).call_demo("synthetic")


def test_setup_api_auth_csrf_confirmation_and_secret_safe_errors(db, tmp_path, monkeypatch):
    settings = local_settings(tmp_path)
    monkeypatch.setattr(api, "settings", settings)
    monkeypatch.setattr(api, "db", db)
    monkeypatch.setitem(api.runtime, "setup", Setup(settings, db, None, MicrosoftAuth(settings)))
    api.sessions.clear()
    api.login_attempts.clear()
    monkeypatch.setattr(api, "health", AsyncMock(return_value=healthy()))
    client = TestClient(api.app)  # Injected isolated state; no service-worker lifespan.
    assert client.get("/setup").status_code == 401
    rejected = client.post("/auth/login", json={"token": "SECRET" * 100})
    assert rejected.status_code == 422 and "SECRET" not in rejected.text
    assert client.post("/auth/login", json={"token": settings.api_token}).status_code == 200
    body = {"confirm": True, "values": telegram_values()}
    assert client.post("/setup/telegram/configure", json=body).status_code == 403
    headers = {"X-Jarvis-Request": "1"}
    assert (
        client.post("/setup/telegram/configure", json={"confirm": False}, headers=headers).status_code == 422
    )
    bad = client.post(
        "/setup/telegram/configure",
        json={"confirm": True, "values": {"telegram_token": "SECRET_INVALID_INPUT"}},
        headers=headers,
    )
    assert bad.status_code == 422 and "SECRET_INVALID_INPUT" not in bad.text
    assert client.post("/setup/telegram/configure", json=body, headers=headers).status_code == 200
    status = client.get("/setup")
    assert status.status_code == 200 and telegram_values()["telegram_token"] not in status.text
    assert (
        client.post("/setup/telegram/disconnect", json={"confirm": True}, headers=headers).status_code == 200
    )
    assert settings.telegram_token == ""


async def test_microsoft_pending_sign_in_is_nonblocking_and_disconnect_clears_cache(
    db, tmp_path, monkeypatch
):
    settings = local_settings(
        tmp_path,
        ms_client_id="00000000-0000-0000-0000-000000000001",
        ms_tenant_id="00000000-0000-0000-0000-000000000002",
    )
    auth = MicrosoftAuth(settings)
    flow = {"device_code": "synthetic-private-device-code"}
    auth.flow = flow

    def pending(value, **kwargs):
        assert value is flow and kwargs["exit_condition"](flow)
        return {"error": "authorization_pending"}

    monkeypatch.setattr(auth, "app", lambda: SimpleNamespace(acquire_token_by_device_flow=pending))
    assert not (await auth.finish_device())["connected"]
    assert auth.flow is flow
    auth.path.parent.mkdir(parents=True, exist_ok=True)
    auth.path.write_text("synthetic-cache")
    await Setup(settings, db, None, auth).disconnect("outlook")
    assert auth.flow is None and not auth.path.exists() and settings.ms_client_id == ""
