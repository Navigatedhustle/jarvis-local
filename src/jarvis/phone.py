import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
from xml.sax.saxutils import escape

import httpx
from twilio.request_validator import RequestValidator

from .scheduler import communication_allowed


def valid_signature(token, canonical_url, form, signature):
    return bool(token and signature and RequestValidator(token).validate(canonical_url, form, signature))


class Phone:
    def __init__(self, settings, db, telegram=None, client=None):
        self.settings, self.db, self.telegram, self.client = settings, db, telegram, client

    async def call_demo(self, briefing_id):
        if not self.db.get_state("setup.enabled.twilio", True):
            return {"blocked": "Phone connection is not enabled"}
        if not communication_allowed(self.db, "phone"):
            return {"suppressed": True}
        s = self.settings
        if not all(
            [s.twilio_account_sid, s.twilio_auth_token, s.twilio_from, s.demo_phone, s.public_webhook_url]
        ):
            return {"blocked": "Twilio account, number and authenticated ingress are required"}
        if urlparse(s.public_webhook_url).scheme != "https":
            raise ValueError("Phone ingress requires HTTPS")
        nonce = secrets.token_urlsafe(32)
        self.db.set_state(
            "call." + nonce,
            {
                "briefing_id": briefing_id,
                "expires": (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat(),
            },
        )
        data = {
            "To": s.demo_phone,
            "From": s.twilio_from,
            "Url": s.public_webhook_url.rstrip("/") + "/twilio/answer/" + nonce,
            "StatusCallback": s.public_webhook_url.rstrip("/") + "/twilio/status/" + nonce,
            "StatusCallbackEvent": ["completed"],
            "Timeout": "25",
            "MachineDetection": "Enable",
        }

        async def request(client):
            response = await client.post(
                "https://api.twilio.com/2010-04-01/Accounts/" + s.twilio_account_sid + "/Calls.json",
                auth=(s.twilio_account_sid, s.twilio_auth_token),
                data=data,
            )
            response.raise_for_status()
            result = response.json()
            self.db.set_state("call.sid." + result["sid"], {"nonce": nonce, "briefing_id": briefing_id})
            self.db.audit("phone.call_submitted", result["sid"], {"briefing_id": briefing_id})
            return {"call_sid": result["sid"]}

        if self.client:
            return await request(self.client)
        async with httpx.AsyncClient(timeout=30) as client:
            return await request(client)

    def answer(self, nonce, answered_by=None):
        if not self.db.get_state("setup.enabled.twilio", True):
            return "<Response><Hangup/></Response>"
        call = self.db.get_state("call." + nonce)
        if not call or datetime.fromisoformat(call["expires"]) < datetime.now(timezone.utc):
            raise ValueError("Expired call")
        if not communication_allowed(self.db, "phone"):
            return "<Response><Hangup/></Response>"
        if answered_by != "human":
            # Voicemail/unknown answers are not delivery to Demo. Do not play private work into them.
            self.db.set_state("call.not_human." + nonce, True)
            return "<Response><Hangup/></Response>"
        briefing = self.db.get_state(call["briefing_id"])
        if not briefing or not briefing.get("audio_url"):
            # Do not silently use a cloud voice in place of the chosen local speech model.
            return "<Response><Hangup/></Response>"
        stream = (
            self.settings.public_webhook_url.rstrip("/").replace("https://", "wss://", 1)
            + "/twilio/media/"
            + nonce
        )
        return (
            "<Response><Play>"
            + escape(briefing["audio_url"])
            + '</Play><Connect><Stream url="'
            + escape(stream, {'"': "&quot;"})
            + '"/></Connect></Response>'
        )

    async def status(self, nonce, form):
        call = self.db.get_state("call." + nonce)
        known = self.db.get_state("call.sid." + form.get("CallSid", ""))
        if not call or not known or known["nonce"] != nonce:
            raise ValueError("Unknown call")
        status = form.get("CallStatus", "")
        if status not in {
            "queued",
            "ringing",
            "in-progress",
            "completed",
            "no-answer",
            "busy",
            "failed",
            "canceled",
        }:
            raise ValueError("Unrecognized call status")
        key = "call.status." + form["CallSid"] + "." + status
        claimed = self.db.one(
            "INSERT INTO state(key,value) VALUES (%s,'{\"received\":true}'::jsonb) ON CONFLICT DO NOTHING RETURNING key",
            (key,),
        )
        if not claimed:
            return {"duplicate": True}
        missed = status in {"no-answer", "busy", "failed", "canceled"} or (
            status == "completed" and self.db.get_state("call.not_human." + nonce, False)
        )
        if missed and self.telegram:
            briefing = self.db.get_state(call["briefing_id"], {})
            await self.telegram.send(
                briefing.get("text", "Your morning briefing could not be completed."), proactive=True
            )
        self.db.audit("phone.status", form["CallSid"], {"status": status})
        return {"recorded": True}
