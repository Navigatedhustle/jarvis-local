"""Same-origin dashboard proxy. No credentials in frontend bundles."""

import asyncio
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import websockets
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .config import settings

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
HOSTS = ["127.0.0.1", "localhost", "testserver"]
ORIGINS = {"http://127.0.0.1:13100", "http://localhost:13100", "http://127.0.0.1:3100"}
if settings.mobile_url:
    HOSTS.append(urlsplit(settings.mobile_url).hostname)
    ORIGINS.add(settings.mobile_url)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=HOSTS)
FRONTEND = Path(__file__).resolve().parents[2] / "frontend"


@app.middleware("http")
async def security_headers(request, call_next):
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    response.headers["Permissions-Policy"] = "microphone=(self), camera=(), geolocation=()"
    return response


@app.api_route("/api/{path:path}", methods=["GET", "POST"])
async def proxy(path: str, request: Request):
    if ".." in path or "\\" in path or "://" in path:
        return Response(status_code=400)
    body = await request.body()
    if len(body) > 2_000_000:
        return Response(status_code=413)
    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() in {"content-type", "cookie", "origin", "x-jarvis-request", "authorization"}
    }
    async with httpx.AsyncClient(timeout=180) as client:
        result = await client.request(
            request.method,
            "http://127.0.0.1:8100/" + path,
            params=request.query_params,
            headers=headers,
            content=body,
        )
    response = Response(
        result.content, status_code=result.status_code, media_type=result.headers.get("content-type")
    )
    for cookie in result.headers.get_list("set-cookie"):
        response.headers.append("set-cookie", cookie)
    return response


@app.get("/")
def index():
    return FileResponse(FRONTEND / "index.html")


@app.get("/{asset}")
def asset(asset: str):
    if asset not in {
        "app.js",
        "setup.js",
        "orb.js",
        "graph-view.js",
        "workspace.js",
        "research.js",
        "packets.js",
        "brain-3d.js",
        "brain-network.js",
        "style.css",
        "voice-worklet.js",
        "speaker-guard.js",
        "mobile.js",
        "mobile.css",
        "command.css",
        "manifest.webmanifest",
        "icon.svg",
    }:
        return Response(status_code=404)
    return FileResponse(FRONTEND / asset)


@app.websocket("/api/voice")
async def voice_proxy(client: WebSocket):
    origin = client.headers.get("origin", "")
    if origin not in ORIGINS:
        await client.close(code=1008)
        return
    try:
        async with websockets.connect(
            "ws://127.0.0.1:8100/voice",
            origin=origin,
            additional_headers={"Cookie": client.headers.get("cookie", "")},
            max_size=8_000_000,
        ) as upstream:
            await client.accept()

            async def forward():
                while True:
                    message = await client.receive()
                    if message["type"] == "websocket.disconnect":
                        return
                    await upstream.send(message.get("bytes") or message.get("text", ""))

            async def backward():
                async for message in upstream:
                    if isinstance(message, bytes):
                        await client.send_bytes(message)
                    else:
                        await client.send_text(message)

            tasks = [asyncio.create_task(forward()), asyncio.create_task(backward())]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    except (WebSocketDisconnect, websockets.WebSocketException, OSError):
        try:
            await client.close(code=1011)
        except RuntimeError:
            pass
