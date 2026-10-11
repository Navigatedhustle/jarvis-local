"""Nano-only TLS, one-use pairing and WSS acceptance. No email/calendar mutations.

Creates and then logs out a temporary phone session. Run before Demo pairs; issuing
a code deliberately replaces the previous unused code. Credentials never printed.
"""

import asyncio
import json
import subprocess

import httpx
import websockets

from jarvis.config import settings


async def main():
    config = json.loads(subprocess.check_output(["tailscale", "serve", "status", "--json"]))
    if not config.get("Web") or any(config.get("AllowFunnel", {}).values()):
        raise RuntimeError("Private Serve configuration is not ready")
    if not settings.mobile_url:
        raise RuntimeError("Private origin is not configured")
    async with httpx.AsyncClient(timeout=15) as owner, httpx.AsyncClient(timeout=15) as phone:
        result = await owner.post(
            "http://127.0.0.1:8100/auth/phone/pair", headers={"Authorization": "Bearer " + settings.api_token}
        )
        result.raise_for_status()
        grant = result.json()
        headers = {"Origin": settings.mobile_url, "X-Jarvis-Request": "1"}
        base = settings.mobile_url + "/api"
        response = await phone.post(
            base + "/auth/phone/redeem", headers=headers, json={"code": grant["code"]}
        )
        response.raise_for_status()
        cookie = response.headers["set-cookie"]
        assert "Secure" in cookie and "HttpOnly" in cookie and "SameSite=strict" in cookie
        try:
            health = await phone.get(base + "/health")
            health.raise_for_status()
            assert health.json()["database"]
            replay = await phone.post(
                base + "/auth/phone/redeem", headers=headers, json={"code": grant["code"]}
            )
            assert replay.status_code == 409
            async with websockets.connect(
                settings.mobile_url.replace("https:", "wss:") + "/api/voice",
                origin=settings.mobile_url,
                additional_headers={"Cookie": "jarvis_session=" + phone.cookies.get("jarvis_session")},
                open_timeout=15,
            ) as voice:
                event = json.loads(await asyncio.wait_for(voice.recv(), 15))
                assert event["type"] == "hello"
        finally:
            await phone.post(base + "/auth/logout", headers=headers)
        assert (await phone.get(base + "/health")).status_code == 401
    print(
        json.dumps(
            {
                "private_https": True,
                "tls_verified": True,
                "secure_cookie": True,
                "single_use_pairing": True,
                "voice_wss": True,
                "logout_revoked": True,
                "funnel": False,
                "physical_iphone_test": "pending",
            }
        )
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as error:
        print(json.dumps({"phone_access_validation": "not_complete", "error_class": type(error).__name__}))
        raise SystemExit(1) from None
