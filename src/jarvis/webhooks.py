"""Narrow public-ingress target. Never mount the admin API on this listener."""

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, Response

from .config import settings
from .db import Database
from .phone import Phone, valid_signature
from .phone_stream import bridge
from .telegram import Telegram

db = None
phone = None


@asynccontextmanager
async def lifespan(app):
    global db, phone
    db = Database(settings.database_url)
    phone = Phone(settings, db, Telegram(settings, db, None, None))
    yield
    db.close()


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


@app.websocket("/twilio/media/{nonce}")
async def media(nonce: str, websocket: WebSocket):
    await bridge(websocket, nonce, settings, db)


async def validated_form(request):
    size = 0
    parts = []
    async for part in request.stream():
        size += len(part)
        if size > 64000:
            raise HTTPException(413)
        parts.append(part)
    if not request.headers.get("content-type", "").startswith("application/x-www-form-urlencoded"):
        raise HTTPException(415)
    try:
        pairs = parse_qsl(b"".join(parts).decode("utf-8"), keep_blank_values=True, max_num_fields=128)
    except (ValueError, UnicodeError):
        raise HTTPException(400, "Invalid callback form") from None
    form = dict(pairs)
    if len(form) != len(pairs):
        raise HTTPException(400, "Duplicate callback fields")
    canonical = settings.public_webhook_url.rstrip("/") + request.url.path
    if not valid_signature(
        settings.twilio_auth_token, canonical, form, request.headers.get("X-Twilio-Signature", "")
    ):
        raise HTTPException(403, "Invalid provider signature")
    return form


@app.post("/twilio/answer/{nonce}")
async def answer(nonce: str, request: Request):
    form = await validated_form(request)
    try:
        known = db.get_state("call.sid." + form.get("CallSid", ""))
        if not known or known.get("nonce") != nonce or form.get("AccountSid") != settings.twilio_account_sid:
            raise ValueError("Unknown outbound call")
        return Response(phone.answer(nonce, form.get("AnsweredBy")), media_type="application/xml")
    except ValueError:
        raise HTTPException(404, "Call not found") from None


@app.post("/twilio/status/{nonce}")
async def status(nonce: str, request: Request):
    form = await validated_form(request)
    try:
        return await phone.status(nonce, form)
    except ValueError:
        raise HTTPException(404, "Call not found") from None


@app.get("/twilio/audio/{nonce}")
async def briefing_audio(nonce: str):
    if not db.get_state("setup.enabled.twilio", True):
        raise HTTPException(404)
    record = db.get_state("audio." + nonce)
    if (
        not record
        or (datetime.now(timezone.utc) - datetime.fromisoformat(record["created_at"])).total_seconds() > 1800
    ):
        raise HTTPException(404)
    if Path(record["file"]).name != record["file"]:
        raise HTTPException(404)
    return FileResponse(
        settings.data_dir / "briefings" / record["file"],
        media_type="audio/wav",
        headers={"Cache-Control": "no-store"},
    )
