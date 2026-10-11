"""User-facing readiness, explicitly requested connection checks and private setup."""

import asyncio
import json
import re
from datetime import datetime, timezone
from urllib.parse import urlparse
from uuid import UUID

import httpx

from .account_vault import FIELDS, AccountVault, apply_accounts
from .outlook import AuthRequired

READY, CONFIG, AUTH, DEGRADED, OFFLINE = (
    "Ready",
    "Configuration required",
    "Authorization required",
    "Degraded",
    "Offline",
)


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def integration_state(configured, observation=None, enabled=True, now=None):
    observation = observation or {}
    state = CONFIG if not configured else AUTH
    detail = (
        "Add your account details to begin."
        if not configured
        else "Saved securely. Test the connection before enabling it."
    )
    if not configured and observation.get("state") == CONFIG:
        detail = observation.get("detail", detail)
    if configured and observation.get("checked_at"):
        state = observation.get("state", DEGRADED)
        detail = observation.get("detail", "Run a connection test.")
        timestamp = datetime.fromisoformat(observation["checked_at"])
        if state == READY and ((now or datetime.now(timezone.utc)) - timestamp).total_seconds() > 86400:
            state, detail = DEGRADED, "The last connection check is over 24 hours old. Test again."
        elif state == READY and not enabled:
            state, detail = AUTH, "Connection verified. Enable it when you are ready for JARVIS to use it."
    return {
        "state": state,
        "status": "Connected"
        if state == READY
        else ("Not Connected" if state in {CONFIG, AUTH} else "Needs Attention"),
        "detail": detail,
        "last_success": observation.get("last_success"),
        "last_checked": observation.get("checked_at"),
        "configured": configured,
        "verified": observation.get("state") == READY,
        "enabled": enabled,
    }


def validate_values(provider, values, settings):
    if provider not in FIELDS or not isinstance(values, dict) or not values.keys() <= set(FIELDS[provider]):
        raise ValueError("Unexpected account fields")
    result = {}
    for field in FIELDS[provider]:
        value = values.get(field)
        value = str(value).strip() if value is not None else ""
        if not value:
            value = str(getattr(settings, field) or "")
        if not value or len(value) > 1024 or any(c in value for c in "\n\r\x00"):
            raise ValueError("Complete every required field. Saved credentials can be left blank.")
        result[field] = value
    try:
        if provider == "outlook":
            result = {key: str(UUID(value)) for key, value in result.items()}
        elif provider == "telegram":
            if not re.fullmatch(r"\d{5,15}:[A-Za-z0-9_-]{20,100}", result["telegram_token"]):
                raise ValueError()
            result["telegram_user_id"] = int(result["telegram_user_id"])
            if result["telegram_user_id"] <= 0:
                raise ValueError()
        else:
            if not re.fullmatch(r"AC[0-9a-fA-F]{32}", result["twilio_account_sid"]):
                raise ValueError()
            if not re.fullmatch(r"[0-9a-fA-F]{32}", result["twilio_auth_token"]):
                raise ValueError()
            for key in ("twilio_from", "demo_phone"):
                if not re.fullmatch(r"\+[1-9]\d{7,14}", result[key]):
                    raise ValueError()
            url = urlparse(result["public_webhook_url"])
            if (
                url.scheme != "https"
                or not url.hostname
                or url.username
                or url.password
                or url.query
                or url.fragment
                or url.port not in (None, 443)
            ):
                raise ValueError()
            if url.path not in ("", "/") or url.hostname in {"localhost", "127.0.0.1", "::1"}:
                raise ValueError()
            result["public_webhook_url"] = result["public_webhook_url"].rstrip("/")
    except (ValueError, TypeError):
        raise ValueError(
            "Check the account ID, credential, numeric user ID, phone numbers or HTTPS address format."
        ) from None
    return result


class Setup:
    def __init__(self, settings, db, graph, auth, client_factory=None, browser=None):
        self.settings, self.db, self.graph, self.auth = settings, db, graph, auth
        self.browser = browser
        self.client_factory = client_factory or (
            lambda: httpx.AsyncClient(timeout=10, follow_redirects=False)
        )
        self.lock = asyncio.Lock()

    def configured(self, provider):
        if provider == "outlook" and self.db.get_state("outlook.provider") == "browser":
            return self.browser is not None
        return all(getattr(self.settings, field) for field in FIELDS[provider])

    async def gateway_available(self):
        try:
            _, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", 8190), timeout=1)
            writer.close()
            await writer.wait_closed()
            return True
        except (OSError, TimeoutError):
            return False

    async def save(self, provider, values):
        async with self.lock:
            values = validate_values(provider, values, self.settings)
            AccountVault(self.settings.secret_dir).update(values)
            apply_accounts(self.settings)
            self.db.set_state("setup.enabled." + provider, False)
            self.db.set_state("setup.check." + provider, {})
            if provider == "outlook":
                self.db.set_state("outlook.provider", "graph")
                await self.clear_microsoft()
            if provider == "twilio":
                self.db.set_state("setup.phone_verified", None)
            self.db.audit(
                "setup.saved", provider, {"credentials": "private store"}, actor="demo", source="dashboard"
            )
        return {"saved": True, "message": "Saved securely. The account has not been enabled."}

    async def clear_microsoft(self):
        async with self.auth._lock:
            import msal

            self.auth.flow = None
            self.auth.cache = msal.SerializableTokenCache()
            self.auth.path.unlink(missing_ok=True)

    async def disconnect(self, provider):
        async with self.lock:
            values = {field: 0 if field == "telegram_user_id" else "" for field in FIELDS[provider]}
            AccountVault(self.settings.secret_dir).update(values)
            apply_accounts(self.settings)
            self.db.set_state("setup.enabled." + provider, False)
            self.db.set_state("setup.check." + provider, {})
            if provider == "outlook":
                await self.clear_microsoft()
            if provider == "twilio":
                self.db.set_state("setup.phone_verified", None)
            self.db.audit("setup.disconnected", provider, actor="demo", source="dashboard")
        return {
            "disconnected": True,
            "message": "JARVIS access disabled and existing records retained. For the browser connection, sign out in the private Outlook browser to revoke that session; its profile is preserved.",
        }

    async def test(self, provider):
        # No account request is made until Demo explicitly presses Test connection.
        async with self.lock:
            previous = self.db.get_state("setup.check." + provider, {})
            result = {"checked_at": utcnow(), "last_success": previous.get("last_success")}
            try:
                if not self.configured(provider):
                    result.update(
                        state=CONFIG,
                        detail="Add the required account details first. No external connection was attempted.",
                    )
                elif provider == "outlook" and self.db.get_state("outlook.provider") == "browser":
                    await self.browser.test()
                    result.update(
                        state=READY,
                        detail="Read-only Nano browser access verified for Demo's Inbox, Sent and calendar. Coverage is bounded; 90-day history is not complete.",
                    )
                elif provider == "outlook":
                    await self.graph.request("GET", "/me?$select=id")
                    await self.graph.request("GET", "/me/mailFolders/inbox/messages?$top=1&$select=id")
                    await self.graph.request("GET", "/me/events?$top=1&$select=id")
                    result.update(
                        state=READY,
                        detail="Microsoft identity, mailbox read access and calendar read access verified. No message was sent or event changed.",
                    )
                else:
                    async with self.client_factory() as client:
                        if provider == "telegram":
                            base = "https://api.telegram.org/bot" + self.settings.telegram_token + "/"
                            bot = await client.post(base + "getMe", json={})
                            bot.raise_for_status()
                            chat = await client.post(
                                base + "getChat", json={"chat_id": self.settings.telegram_user_id}
                            )
                            chat.raise_for_status()
                            hook = await client.post(base + "getWebhookInfo", json={})
                            hook.raise_for_status()
                            if not all(r.json().get("ok") for r in (bot, chat, hook)):
                                raise AuthRequired(
                                    "Telegram access was not confirmed. Start a private chat with your bot, then test again."
                                )
                            identity = chat.json()["result"]
                            if (
                                not bot.json()["result"].get("is_bot")
                                or identity.get("type") != "private"
                                or identity.get("id") != self.settings.telegram_user_id
                            ):
                                raise AuthRequired(
                                    "The authorized private Telegram identity could not be verified."
                                )
                            if hook.json()["result"].get("url"):
                                result.update(
                                    state=DEGRADED,
                                    detail="This bot already has a webhook. Use a dedicated bot or deliberately remove its previous integration in Telegram before enabling polling.",
                                )
                            else:
                                result.update(
                                    state=READY,
                                    detail="Bot and your private chat verified. This test sends no message and starts no polling.",
                                )
                        else:
                            base = (
                                "https://api.twilio.com/2010-04-01/Accounts/"
                                + self.settings.twilio_account_sid
                            )
                            credentials = (self.settings.twilio_account_sid, self.settings.twilio_auth_token)
                            account = await client.get(base + ".json", auth=credentials)
                            account.raise_for_status()
                            numbers = await client.get(
                                base + "/IncomingPhoneNumbers.json",
                                auth=credentials,
                                params={"PhoneNumber": self.settings.twilio_from},
                            )
                            numbers.raise_for_status()
                            owned = [
                                n
                                for n in numbers.json().get("incoming_phone_numbers", [])
                                if n.get("phone_number") == self.settings.twilio_from
                                and n.get("capabilities", {}).get("voice")
                            ]
                            if account.json().get("status") != "active" or not owned:
                                result.update(
                                    state=DEGRADED,
                                    detail="Check account billing and that the selected voice-capable number belongs to this account.",
                                )
                            else:
                                result.update(
                                    state=READY,
                                    detail="Twilio account and dedicated number verified. Public ingress and a real conversation still need a test call. No call was placed.",
                                )
                if result["state"] == READY:
                    result["last_success"] = result["checked_at"]
            except AuthRequired:
                result.update(
                    state=AUTH,
                    detail="Authorization could not be verified. Complete Microsoft sign-in or check the saved credentials and authorized private chat.",
                )
            except httpx.HTTPStatusError as error:
                result.update(
                    state=AUTH if error.response.status_code in (400, 401, 403, 404) else DEGRADED,
                    detail="The provider rejected this check. Review permissions, credentials, account status and the setup steps.",
                )
            except (httpx.HTTPError, OSError, RuntimeError):
                result.update(
                    state=OFFLINE,
                    detail="The provider could not be reached. Check the connection and try again. Your saved setup is preserved.",
                )
            except (KeyError, ValueError, TypeError):
                result.update(
                    state=DEGRADED,
                    detail="The provider returned an unexpected response. Try again or review Advanced Details.",
                )
            self.db.set_state("setup.check." + provider, result)
            self.db.audit(
                "setup.tested", provider, {"state": result["state"]}, actor="demo", source="dashboard"
            )
            return result

    async def snapshot(self, health):
        providers = {}
        for provider in FIELDS:
            configured = bool(self.configured(provider))
            observation = self.db.get_state("setup.check." + provider, {})
            enabled = self.db.get_state("setup.enabled." + provider, True)
            browser_mode = provider == "outlook" and self.db.get_state("outlook.provider") == "browser"
            if provider == "outlook" and configured and enabled and not browser_mode:
                last_sync = health["outlook"].get("last_sync")
                if not self.auth.path.exists():
                    observation = {
                        **observation,
                        "state": AUTH,
                        "checked_at": utcnow(),
                        "detail": "Sign in with Microsoft to authorize this connection.",
                    }
                elif health["outlook"].get("error") or (last_sync and health["outlook"].get("stale")):
                    observation = {
                        **observation,
                        "state": DEGRADED,
                        "checked_at": utcnow(),
                        "detail": "Outlook synchronization needs attention. Test the connection and renew sign-in if requested.",
                    }
                elif last_sync and not observation:
                    observation = {
                        "state": READY,
                        "checked_at": last_sync,
                        "last_success": last_sync,
                        "detail": "Existing Outlook synchronization verified access.",
                    }
            providers[provider] = integration_state(configured, observation, enabled)
            if browser_mode:
                providers[provider]["connection_method"] = "browser"
                providers[provider]["coverage"] = self.db.get_state("outlook.ingestion")
                if enabled and health["outlook"].get("error"):
                    error = health["outlook"]["error"]
                    expired = isinstance(error, dict) and error.get("error_class") == "AuthRequired"
                    providers[provider].update(
                        state=AUTH if expired else DEGRADED,
                        status="Needs Attention",
                        detail="Browser sync needs attention. Check the Nano viewer for HP sign-in, then test again.",
                    )
                elif enabled and health["outlook"].get("last_sync") and health["outlook"].get("stale"):
                    providers[provider].update(
                        state=DEGRADED,
                        status="Needs Attention",
                        detail="Browser synchronization is stale. Check the Nano viewer and test the connection.",
                    )
            providers[provider]["fields_saved"] = {
                field: bool(getattr(self.settings, field)) for field in FIELDS[provider]
            }
        models = health["models"]
        local_ready = all(value == "ready" for value in models.values())
        rows = []

        def row(name, state, detail):
            rows.append({"name": name, "state": state, "detail": detail})

        row(
            "Local inference",
            READY if local_ready else DEGRADED if models["primary"] == "ready" else OFFLINE,
            "The selected local models are responding."
            if local_ready
            else "One or more model services need attention.",
        )
        row(
            "Persistent memory",
            READY if health["database"] else OFFLINE,
            "Private PostgreSQL storage and source-linked memory.",
        )
        for role, label in (("stt", "Voice input"), ("tts", "Voice output")):
            speech_ready = health["voice"][role] == "ready"
            detail = (
                (
                    "Local speech recognition is ready; allow the microphone when starting Voice."
                    if role == "stt"
                    else "The original local voice is ready. Hear his voice plays a sample without microphone access."
                )
                if speech_ready
                else "The local speech service is unavailable. Check Advanced Details for its status."
            )
            row(
                label,
                READY if speech_ready else OFFLINE,
                detail,
            )
        for provider, label in (("outlook", "Outlook"), ("telegram", "Telegram")):
            row(label, providers[provider]["state"], providers[provider]["detail"])
        phone_verified = self.db.get_state("setup.phone_verified")
        phone_state = providers["twilio"]["state"]
        gateway_ready = await self.gateway_available() if phone_state == READY else False
        if phone_state == READY and (not phone_verified or not gateway_ready):
            phone_state = DEGRADED
        row(
            "Telephone",
            phone_state,
            "Private gateway available. A signed live call previously played a JARVIS voice response."
            if phone_state == READY
            else "A verified account, enabled gateway, public ingress and a real conversation are required.",
        )
        for table, label in (("approvals", "Approval engine"), ("audit", "Audit log")):
            self.db.one("SELECT count(*) AS n FROM " + table)  # Fixed internal table allowlist.
            row(label, READY, "Durable records available. Existing authorization rules are unchanged.")
        backup = self.settings.data_dir / "backup-validation.json"
        backups = list((self.settings.data_dir / "backups").glob("*.dump"))
        latest = max((p.stat().st_mtime for p in backups), default=0)
        recent = datetime.now(timezone.utc).timestamp() - latest < 172800
        restored = False
        if backup.exists():
            try:
                restored = json.loads(backup.read_text()).get("restore") == "passed"
            except (ValueError, OSError):
                pass
        row(
            "Backup/recovery",
            READY if recent and restored else DEGRADED,
            "Recent local backup and restore validation available. Off-machine recovery and full host reboot remain unverified."
            if recent and restored
            else "A recent local backup or recorded restore check is missing.",
        )
        events = self.db.query(
            "SELECT at,action,entity_id,details->>'state' AS state FROM audit WHERE action LIKE 'setup.%%' ORDER BY id DESC LIMIT 8"
        )
        return {
            "core": {"status": "Online", "state": READY},
            "local_ai": {
                "status": "Connected" if local_ready else "Needs Attention",
                "model": "Qwen/Qwen3.8-27B-FP8",
                "memory": "Ready" if health["database"] else "Offline",
                "voice_input": health["voice"]["stt"],
                "voice_output": health["voice"]["tts"],
            },
            "integrations": providers,
            "readiness": rows,
            "phone_verified": phone_verified,
            "advanced": {
                "endpoints": {name: getattr(self.settings, name + "_url") for name in models},
                "ports": {
                    "dashboard": 3100,
                    "api": 8100,
                    "speech_input": 8185,
                    "speech_output": 8186,
                    "webhooks": 8190,
                    "database": 55432,
                },
                "authentication": {
                    "dashboard": "Authenticated private session or local bearer key",
                    "outlook_cache_present": self.auth.path.exists(),
                    "device_sign_in_pending": bool(self.auth.flow),
                },
                "demo_mode": health["demo_mode"],
                "phone_gateway_available": gateway_ready,
                "diagnostic_log": events,
                "checked_at": utcnow(),
            },
        }
