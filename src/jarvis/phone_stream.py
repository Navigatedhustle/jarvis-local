"""Signed outbound-call media bridge into the existing private voice pipeline."""

import asyncio
import audioop
import base64
import io
import json
import wave
from datetime import datetime, timezone

import httpx
import websockets
from fastapi import WebSocketDisconnect

from .phone import valid_signature


def wav_to_mulaw(data):
    with wave.open(io.BytesIO(data), "rb") as audio:
        if audio.getsampwidth() != 2 or audio.getnchannels() != 1:
            raise ValueError("Phone output requires mono PCM16")
        pcm = audio.readframes(audio.getnframes())
        pcm, _ = audioop.ratecv(pcm, 2, 1, audio.getframerate(), 8000, None)
    return audioop.lin2ulaw(pcm, 2)


def start_is_authorized(db, settings, nonce, message):
    start = message.get("start", {})
    call = db.get_state("call." + nonce)
    known = db.get_state("call.sid." + start.get("callSid", ""))
    return bool(
        db.get_state("setup.enabled.twilio", True)
        and call
        and known
        and known.get("nonce") == nonce
        and datetime.fromisoformat(call["expires"]) > datetime.now(timezone.utc)
        and start.get("accountSid") == settings.twilio_account_sid
        and start.get("streamSid") == message.get("streamSid")
        and start.get("mediaFormat") == {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1}
    )


def acknowledge_playback(name, pending, cleared):
    # Twilio also acknowledges marks for audio discarded by a clear command.
    played = name in pending and name not in cleared
    pending.discard(name)
    cleared.discard(name)
    return played


async def bridge(websocket, nonce, settings, db):
    canonical = settings.public_webhook_url.rstrip("/") + websocket.url.path
    signature = websocket.headers.get("x-twilio-signature", "")
    # The same configured HTTPS host/path is used for Twilio's WSS upgrade handshake.
    if not valid_signature(settings.twilio_auth_token, canonical, {}, signature):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    try:
        async with asyncio.timeout(600):
            while True:
                raw = await asyncio.wait_for(websocket.receive_text(), timeout=15)
                if len(raw) > 64000:
                    raise ValueError("Oversized provider message")
                message = json.loads(raw)
                if message.get("event") == "connected":
                    continue
                if message.get("event") != "start" or not start_is_authorized(db, settings, nonce, message):
                    raise ValueError("Unrecognized outbound call")
                break
            stream_id = message["streamSid"]
            # Atomic, durable single-use stream claim; reconnects cannot replay call authority.
            with db.transaction() as conn:
                claimed = conn.execute(
                    "INSERT INTO state(key,value) VALUES (%s,'true'::jsonb) ON CONFLICT DO NOTHING RETURNING key",
                    ("call.stream.used." + nonce,),
                ).fetchone()
                if not claimed:
                    raise ValueError("Call stream already consumed")
            async with httpx.AsyncClient(timeout=15) as client:
                login = await client.post(
                    "http://127.0.0.1:8100/auth/login", json={"token": settings.api_token}
                )
                login.raise_for_status()
                cookie = "jarvis_session=" + login.cookies["jarvis_session"]
                try:
                    async with websockets.connect(
                        "ws://127.0.0.1:8100/voice?channel=phone",
                        origin="http://127.0.0.1:8100",
                        additional_headers={"Cookie": cookie},
                        max_size=8_000_000,
                    ) as local:
                        pending_marks, cleared_marks = set(), set()

                        async def incoming():
                            conversion = None
                            while True:
                                raw = await websocket.receive_text()
                                if len(raw) > 64000:
                                    raise ValueError("Oversized provider message")
                                event = json.loads(raw)
                                if event.get("streamSid") != stream_id:
                                    raise ValueError("Wrong call stream")
                                if event.get("event") == "stop":
                                    return
                                if event.get("event") == "media":
                                    encoded = event["media"]["payload"]
                                    raw_audio = base64.b64decode(encoded, validate=True)
                                    if len(raw_audio) > 8000:
                                        raise ValueError("Oversized media frame")
                                    pcm = audioop.ulaw2lin(raw_audio, 2)
                                    pcm, conversion = audioop.ratecv(pcm, 2, 1, 8000, 16000, conversion)
                                    await local.send(pcm)
                                elif event.get("event") == "mark":
                                    name = event.get("mark", {}).get("name", "")
                                    if acknowledge_playback(name, pending_marks, cleared_marks):
                                        db.set_state(
                                            "setup.phone_verified", datetime.now(timezone.utc).isoformat()
                                        )
                                    db.audit(
                                        "phone.playback_completed", event.get("mark", {}).get("name", "")[:36]
                                    )

                        async def outgoing():
                            async for raw in local:
                                event = json.loads(raw)
                                if event["type"] == "cancel":
                                    cleared_marks.update(pending_marks)
                                    await websocket.send_json({"event": "clear", "streamSid": stream_id})
                                elif event["type"] == "audio":
                                    pending_marks.add(event["turn_id"])
                                    audio = wav_to_mulaw(base64.b64decode(event["data"], validate=True))
                                    await websocket.send_json(
                                        {
                                            "event": "media",
                                            "streamSid": stream_id,
                                            "media": {"payload": base64.b64encode(audio).decode()},
                                        }
                                    )
                                    await websocket.send_json(
                                        {
                                            "event": "mark",
                                            "streamSid": stream_id,
                                            "mark": {"name": event["turn_id"]},
                                        }
                                    )
                                elif event["type"] == "error":
                                    return

                        tasks = [asyncio.create_task(incoming()), asyncio.create_task(outgoing())]
                        try:
                            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                            for task in done:
                                task.result()
                        finally:
                            for task in tasks:
                                task.cancel()
                            await asyncio.gather(*tasks, return_exceptions=True)
                finally:
                    await client.post("http://127.0.0.1:8100/auth/logout", headers={"X-Jarvis-Request": "1"})
    except (
        TimeoutError,
        ValueError,
        KeyError,
        httpx.HTTPError,
        websockets.WebSocketException,
        WebSocketDisconnect,
    ) as error:
        db.audit("phone.stream_ended", details={"error_class": type(error).__name__})
    finally:
        try:
            await websocket.close()
        except (RuntimeError, WebSocketDisconnect):
            pass
