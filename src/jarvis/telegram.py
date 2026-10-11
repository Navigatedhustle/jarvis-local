"""Private-chat polling. No webhook ingress or arbitrary Telegram recipients."""

import asyncio
import re
from uuid import UUID

import httpx

from .policy import Actor, PolicyError
from .scheduler import communication_allowed


class Telegram:
    def __init__(self, settings, db, jarvis, approvals, client=None):
        self.settings, self.db, self.jarvis, self.approvals = settings, db, jarvis, approvals
        self.client = client

    def authorized(self, message):
        expected = self.settings.telegram_user_id
        return bool(
            expected
            and message.get("from", {}).get("id") == expected
            and message.get("chat", {}).get("id") == expected
            and message.get("chat", {}).get("type") == "private"
            and not message.get("from", {}).get("is_bot")
        )

    async def request(self, method, payload):
        if not self.settings.telegram_token or not self.settings.telegram_user_id:
            raise RuntimeError("Telegram account is not configured")
        url = "https://api.telegram.org/bot" + self.settings.telegram_token + "/" + method

        async def send(client):
            result = await client.post(url, json=payload)
            result.raise_for_status()
            data = result.json()
            if not data.get("ok"):
                raise RuntimeError("Telegram rejected the operation")
            return data["result"]

        if self.client:
            return await send(self.client)
        async with httpx.AsyncClient(timeout=40) as client:
            return await send(client)

    async def send(self, text, proactive=False):
        if proactive and not self.db.get_state("setup.enabled.telegram", True):
            return {"suppressed": True}
        if proactive and not communication_allowed(self.db, "telegram"):
            return {"suppressed": True}
        # Explicit target is configured Demo only; caller cannot override it.
        return await self.request(
            "sendMessage", {"chat_id": self.settings.telegram_user_id, "text": text[:4000]}
        )

    async def approval_card(self, identifier):
        import json

        action = self.approvals.get(identifier)
        result = await self.send(
            f"Action {action['id']}\n{action['kind']}\nStatus: {action['status']}\nExpires: {action['expires_at']}\n"
            + json.dumps(action["payload"], ensure_ascii=False)[:2600]
            + f"\nAPPROVE {action['id']}\nDENY {action['id']}\nEDIT {action['id']} followed by a revised email body"
        )
        self.db.set_state("telegram.reviewed." + str(action["id"]), action["digest"])
        return result

    async def process(self, update):
        message = update.get("message", {})
        if not self.authorized(message):
            return {"ignored": True}
        key = "telegram.update." + str(update["update_id"])
        if self.db.get_state(key):
            return {"duplicate": True}
        # Record receipt before any effect. Crash mid-send is uncertain, never auto-replayed.
        self.db.set_state(key, {"status": "processing"})
        text = message.get("text", "").strip()
        try:
            match = re.fullmatch(r"(YES|APPROVE|NO|DENY|MORE)\s+([a-fA-F0-9-]{36})", text, re.I)
            if match:
                command, identifier = match.group(1).upper(), UUID(match.group(2))
                if command == "MORE":
                    await self.approval_card(identifier)
                else:
                    reviewed = self.db.get_state("telegram.reviewed." + str(identifier))
                    if not reviewed:
                        await self.approval_card(identifier)
                        await self.send(
                            "Review the action above, then send your decision with that action ID."
                        )
                    else:
                        action = self.approvals.decide(
                            identifier, command, reviewed, Actor("demo", "telegram")
                        )
                        if action["status"] == "approved" and action["kind"] in self.jarvis.executors:
                            await self.approvals.execute(identifier, self.jarvis.executors)
                            action = self.approvals.get(identifier)
                        await self.send(f"Action {identifier}: {action['status']}.")
            elif text.upper() in {"YES", "APPROVE", "NO", "DENY", "EDIT", "MORE"}:
                await self.send(
                    "Include the full action ID so your decision applies to exactly one proposal."
                )
            elif text.upper().startswith("EDIT "):
                _, identifier, revised = text.split(" ", 2)
                old = self.approvals.get(identifier)
                if old["kind"] != "email.send":
                    raise PolicyError("Edit this action in the dashboard to review every changed field")
                payload = old["payload"]
                payload["message"]["body"] = {"contentType": "Text", "content": revised}
                new = self.approvals.edit(identifier, payload, Actor("demo", "telegram"))
                await self.approval_card(new["id"])
            elif message.get("voice"):
                voice = message["voice"]
                if voice.get("duration", 0) > 120 or voice.get("file_size", 0) > 12_000_000:
                    await self.send("Please keep voice notes under two minutes.")
                else:
                    file = await self.request("getFile", {"file_id": voice["file_id"]})
                    path = file["file_path"]
                    if not re.fullmatch(r"[A-Za-z0-9_./-]+", path) or ".." in path:
                        raise ValueError("Invalid Telegram file path")
                    async with httpx.AsyncClient(timeout=120) as client:
                        audio = await client.get(
                            "https://api.telegram.org/file/bot" + self.settings.telegram_token + "/" + path
                        )
                        audio.raise_for_status()
                        if len(audio.content) > 12_000_000:
                            raise ValueError("Telegram audio too large")
                        transcript = await client.post(
                            "http://127.0.0.1:8185/transcribe",
                            files={"file": ("voice.ogg", audio.content, "audio/ogg")},
                        )
                        transcript.raise_for_status()
                    result = await self.jarvis.reply(
                        transcript.json()["text"], actor=Actor("demo", "telegram")
                    )
                    await self.send(result["reply"])
            elif text:
                result = await self.jarvis.reply(text, actor=Actor("demo", "telegram"))
                await self.send(result["reply"])
            self.db.set_state(key, {"status": "done"})
        except PolicyError as e:
            await self.send(str(e))
            self.db.set_state(key, {"status": "rejected"})
        except Exception:
            self.db.set_state(key, {"status": "uncertain"})
            raise
        return {"processed": True}

    async def poll(self):
        while True:
            if (
                not self.settings.telegram_token
                or not self.settings.telegram_user_id
                or not self.db.get_state("setup.enabled.telegram", True)
            ):
                await asyncio.sleep(2)
                continue
            try:
                offset = self.db.get_state("telegram.offset", 0)
                updates = await self.request(
                    "getUpdates", {"offset": offset, "timeout": 25, "allowed_updates": ["message"]}
                )
                if not self.db.get_state("setup.enabled.telegram", True):
                    continue
                for update in updates:
                    await self.process(update)
                    self.db.set_state("telegram.offset", update["update_id"] + 1)
            except (httpx.HTTPError, RuntimeError, ValueError):
                self.db.audit("telegram.poll_unavailable")
                await asyncio.sleep(15)
