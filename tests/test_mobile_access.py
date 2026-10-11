import hashlib
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from jarvis import api
from jarvis.config import Settings
from jarvis.mobile_access import issue, redeem
from jarvis.policy import PolicyError

URL = "https://demo-device.example.ts.net"


def test_longer_phone_access_requires_explicit_opt_in():
    class Inputs(HTMLParser):
        fields = {}

        def handle_starttag(self, tag, attrs):
            if tag == "input":
                attrs = dict(attrs)
                self.fields[attrs.get("id")] = attrs

    form = Inputs()
    form.feed(Path("frontend/index.html").read_text(encoding="utf-8"))
    assert "checked" not in form.fields["phone-remember"]
    assert api.PairCode(code="synthetic").remember is False


def test_remote_revocation_rechecks_database_after_bounded_cache(db, monkeypatch):
    from jarvis import mobile_access

    now = 1800000000.0
    monkeypatch.setattr(mobile_access.time, "time", lambda: now)
    monkeypatch.setattr(api, "db", db)
    monkeypatch.setattr(api, "settings", config())
    value = redeem(db, api.settings, issue(db, api.settings)["code"], remember=True)
    digest = hashlib.sha256(value.encode()).hexdigest()
    assert api.session_expiry(digest) > now
    device = db.get_state("dashboard.session." + digest)["device_id"]
    mobile_access.revoke(db, device)
    now += 2.1
    assert api.session_expiry(digest) == 0


def config():
    return Settings(_env_file=None, api_token="synthetic-" + "x" * 40, mobile_url=URL)


@pytest.mark.parametrize(
    "url",
    [
        "http://demo-device.example.ts.net",
        "https://evil.example",
        "https://demo-device.example.ts.net/path",
        "https://user@demo-device.example.ts.net",
        "https://demo-device.example.ts.net:123",
        "https://demo-device.example.ts.net?x=1",
    ],
)
def test_mobile_origin_rejects_broad_or_insecure_hosts(url):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, mobile_url=url)


def test_pairing_is_hashed_bound_single_use_and_durable(db):
    settings = config()
    grant = issue(db, settings)
    state = db.get_state("dashboard.phone_pairing")
    assert grant["code"] not in str(state)
    session = redeem(db, settings, grant["code"])
    assert db.get_state("dashboard.session." + hashlib.sha256(session.encode()).hexdigest())
    with pytest.raises(PolicyError):
        redeem(db, settings, grant["code"])
    assert grant["code"] not in str(db.query("SELECT details FROM audit"))


def test_pairing_expiry_replacement_key_rotation_and_rate_limit(db):
    settings = config()
    old = issue(db, settings)
    grant = issue(db, settings)
    with pytest.raises(PolicyError):
        redeem(db, settings, old["code"])
    state = db.get_state("dashboard.phone_pairing")
    state["expires"] = 0
    db.set_state("dashboard.phone_pairing", state)
    with pytest.raises(PolicyError):
        redeem(db, settings, grant["code"])
    grant = issue(db, settings)
    rotated = settings.model_copy(update={"api_token": "rotated-key"})
    with pytest.raises(PolicyError):
        redeem(db, rotated, grant["code"])
    for _ in range(5):
        with pytest.raises(PolicyError):
            redeem(db, settings, "incorrect")
    with pytest.raises(PolicyError, match="Wait a minute"):
        redeem(db, settings, grant["code"])


def test_concurrent_pairing_has_only_one_winner(db):
    settings = config()
    grant = issue(db, settings)

    def attempt():
        try:
            return bool(redeem(db, settings, grant["code"]))
        except PolicyError:
            return False

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sum(executor.map(lambda _: attempt(), range(2))) == 1


def test_phone_api_requires_owner_and_https_and_preserves_csrf(db, monkeypatch):
    settings = config()
    monkeypatch.setattr(api, "db", db)
    monkeypatch.setattr(api, "settings", settings)
    monkeypatch.setattr(api, "ALLOWED_ORIGINS", {*api.ALLOWED_ORIGINS, URL})
    client = TestClient(api.app)
    assert client.post("/auth/phone/pair").status_code == 401
    grant = client.post("/auth/phone/pair", headers={"Authorization": "Bearer " + settings.api_token}).json()
    assert client.post("/auth/phone/redeem", json={"code": grant["code"]}).status_code == 403
    headers = {"Origin": URL, "X-Jarvis-Request": "1"}
    result = client.post("/auth/phone/redeem", json={"code": grant["code"]}, headers=headers)
    assert result.status_code == 200
    cookie = result.headers["set-cookie"]
    assert "Secure" in cookie and "HttpOnly" in cookie and "SameSite=strict" in cookie
    # Restart loads persisted sessions; cookie is sent explicitly because TestClient uses HTTP.
    api.sessions.clear()
    value = result.cookies.get("jarvis_session")
    client.cookies.clear()
    assert client.get("/auth/phone", headers={"Cookie": f"jarvis_session={value}"}).status_code == 200
    assert client.post("/auth/phone/pair", headers={"Cookie": f"jarvis_session={value}"}).status_code == 403
    assert client.post("/auth/phone/redeem", json={"code": grant["code"]}, headers=headers).status_code == 409
    client.post("/auth/logout", headers={**headers, "Cookie": f"jarvis_session={value}"})
    assert client.get("/auth/phone", headers={"Cookie": f"jarvis_session={value}"}).status_code == 401


def test_remembered_phone_persists_next_day_and_restart_but_expires(db, monkeypatch):
    from jarvis import mobile_access

    now = 1800000000.0
    monkeypatch.setattr(mobile_access.time, "time", lambda: now)
    settings = config()
    monkeypatch.setattr(api, "db", db)
    monkeypatch.setattr(api, "settings", settings)
    value = redeem(db, settings, issue(db, settings)["code"], remember=True)
    digest = hashlib.sha256(value.encode()).hexdigest()
    state = db.get_state("dashboard.session." + digest)
    assert state["expires"] == now + 30 * 86400
    assert value not in str(state) and digest not in str(mobile_access.devices(db, settings))
    now += 86400
    api.sessions.clear()
    api.session_checks.clear()
    assert api.session_expiry(digest) > now
    now += 30 * 86400
    assert api.session_expiry(digest) == 0


def test_phone_remote_revoke_invalidates_warm_cache_without_affecting_other_device(db, monkeypatch):
    settings = config()
    monkeypatch.setattr(api, "db", db)
    monkeypatch.setattr(api, "settings", settings)
    first = redeem(db, settings, issue(db, settings)["code"], remember=True)
    second = redeem(db, settings, issue(db, settings)["code"], remember=True)
    first_hash = hashlib.sha256(first.encode()).hexdigest()
    second_hash = hashlib.sha256(second.encode()).hexdigest()
    assert api.session_expiry(first_hash) and api.session_expiry(second_hash)
    device = db.get_state("dashboard.session." + first_hash)["device_id"]
    client = TestClient(api.app)
    assert client.post("/auth/phone/revoke", json={"device_id": device}).status_code == 401
    assert (
        client.post(
            "/auth/phone/revoke", json={"device_id": device}, headers={"Cookie": "jarvis_session=" + second}
        ).status_code
        == 403
    )
    result = client.post(
        "/auth/phone/revoke",
        json={"device_id": device},
        headers={"Authorization": "Bearer " + settings.api_token},
    )
    assert result.status_code == 200
    assert api.session_expiry(first_hash) == 0 and api.session_expiry(second_hash) > 0
    monkeypatch.setattr(api, "settings", settings.model_copy(update={"api_token": "rotated"}))
    assert api.session_expiry(second_hash) == 0


@pytest.mark.parametrize("remember,seconds", [(False, 8 * 3600), (True, 30 * 86400)])
def test_phone_cookie_duration_matches_server_and_logout(db, monkeypatch, remember, seconds):
    settings = config()
    monkeypatch.setattr(api, "db", db)
    monkeypatch.setattr(api, "settings", settings)
    monkeypatch.setattr(api, "ALLOWED_ORIGINS", {*api.ALLOWED_ORIGINS, URL})
    client = TestClient(api.app)
    grant = issue(db, settings)
    headers = {"Origin": URL, "X-Jarvis-Request": "1"}
    result = client.post(
        "/auth/phone/redeem", json={"code": grant["code"], "remember": remember}, headers=headers
    )
    assert result.status_code == 200
    assert f"Max-Age={seconds}" in result.headers["set-cookie"]
    assert all(flag in result.headers["set-cookie"] for flag in ["Secure", "HttpOnly", "SameSite=strict"])
    value = result.cookies.get("jarvis_session")
    digest = hashlib.sha256(value.encode()).hexdigest()
    client.cookies.clear()
    assert api.session_expiry(digest) > 0
    assert (
        client.post("/auth/logout", headers={**headers, "Cookie": "jarvis_session=" + value}).status_code
        == 200
    )
    assert api.session_expiry(digest) == 0
