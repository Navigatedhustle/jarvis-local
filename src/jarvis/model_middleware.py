"""Private model-server middleware: bounded speech leases, never weight/admin APIs."""

import asyncio
from uuid import uuid4

from starlette.requests import Request
from starlette.responses import JSONResponse


class SpeechPriorityMiddleware:
    lease_seconds = 6

    def __init__(self, app):
        self.app = app
        self.lock = asyncio.Lock()
        self.lease = None
        self.timer = None

    async def release(self, engine, lease):
        async with self.lock:
            if self.lease != lease:
                return
            await engine.resume_generation()
            self.lease = None

    async def expire(self, engine, lease):
        await asyncio.sleep(self.lease_seconds)
        # Retry resume on a transient failure. A new lease cannot be granted until released.
        for _ in range(3):
            try:
                await self.release(engine, lease)
                return
            except Exception:
                await asyncio.sleep(0.5)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] not in {
            "/_jarvis/speech/acquire",
            "/_jarvis/speech/release",
        }:
            return await self.app(scope, receive, send)
        if scope["method"] != "POST":
            return await JSONResponse({"error": "Method not allowed"}, status_code=405)(scope, receive, send)
        engine = scope["app"].state.engine_client
        try:
            if scope["path"].endswith("/acquire"):
                async with self.lock:
                    if self.lease:
                        return await JSONResponse({"error": "Speech lease busy"}, status_code=409)(
                            scope, receive, send
                        )
                    lease = uuid4().hex
                    # mode=keep retains in-flight requests and KV state. Never abort/clear.
                    self.lease = lease
                    self.timer = asyncio.create_task(self.expire(engine, lease))
                    await asyncio.wait_for(engine.pause_generation(mode="keep", clear_cache=False), 2)
                response = {"lease": lease, "expires_in_seconds": 6}
            else:
                request = Request(scope, receive)
                data = await request.json()
                if not isinstance(data, dict) or not isinstance(data.get("lease"), str):
                    raise ValueError("Invalid speech lease")
                await self.release(engine, data["lease"])
                response = {"released": True}
            await JSONResponse(response)(scope, receive, send)
        except Exception:
            await JSONResponse({"error": "Speech scheduling unavailable"}, status_code=503)(
                scope, receive, send
            )
