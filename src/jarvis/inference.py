import asyncio
import json
import time
from contextlib import asynccontextmanager

import httpx

from .workload import CURRENT_PRIORITY, WorkloadGuard


class ModelUnavailable(RuntimeError):
    def __init__(self, message="Local model unavailable", *, reason=None):
        super().__init__(message)
        self.reason = reason


class InferenceDeferred(ModelUnavailable):
    """No model request was sent because live interaction has priority."""


class Inference:
    def __init__(self, settings, db=None):
        self.settings = settings
        self.interactive = 0
        self.background_lock = asyncio.Lock()
        self.speech_lock = asyncio.Lock()
        self.workload = WorkloadGuard(self, db)

    @asynccontextmanager
    async def speech_priority(self):
        """Short GPU handoff; the private server auto-resumes even if this process dies."""
        async with self.speech_lock:
            lease = None
            async with httpx.AsyncClient(timeout=2) as client:
                try:
                    response = await client.post(self.settings.primary_url + "/_jarvis/speech/acquire")
                    if response.is_success:
                        lease = response.json().get("lease")
                except (httpx.HTTPError, ValueError):
                    pass  # Speech remains usable against an older model-server deployment.
                try:
                    yield bool(lease)
                finally:
                    if lease:
                        try:
                            await asyncio.shield(
                                client.post(
                                    self.settings.primary_url + "/_jarvis/speech/release",
                                    json={"lease": lease},
                                )
                            )
                        except httpx.HTTPError:
                            pass  # Server-side lease expiration is independent of this connection.

    async def chat(self, messages, **kwargs):
        priority = CURRENT_PRIORITY.get()
        if self.workload.enabled and priority >= 3:
            reason = self.workload.reason(priority)
            if reason:
                raise InferenceDeferred(reason)
            kwargs["defer_to_interactive"] = True
        if kwargs.get("background"):
            async with self.background_lock:
                return await self._chat(messages, **kwargs)
        return await self._chat(messages, **kwargs)

    async def _chat(
        self,
        messages,
        *,
        background=False,
        interactive_routing=False,
        tools=None,
        schema=None,
        effort="low",
        on_first_token=None,
        on_text=None,
        defer_to_interactive=False,
        max_tokens=None,
    ):
        if (background or defer_to_interactive) and self.interactive and not interactive_routing:
            raise InferenceDeferred("Interactive work has priority; retry background job later")
        url = self.settings.background_url if background else self.settings.primary_url
        body = {
            "model": "jarvis-background" if background else "jarvis-primary",
            "messages": messages,
            "max_tokens": max_tokens or (1024 if background else 2048),
            "temperature": 0.3,
        }
        if not background:
            body["chat_template_kwargs"] = {"enable_thinking": effort != "low", "preserve_thinking": False}
            body["reasoning_effort"] = {"high": "xhigh"}.get(effort, effort)
            body["include_reasoning"] = False
            if effort != "low":
                body["thinking_token_budget"] = 512 if effort == "medium" else 1536
        if tools:
            body.update(tools=tools, tool_choice="auto")
        if schema:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "result", "schema": schema},
            }
        started = time.monotonic()
        if not background and not defer_to_interactive:
            self.interactive += 1
        try:
            # The unchanged GB10 primary can need several minutes for medium reasoning.
            # Internal preparation may wait, but its request is cancelled when Demo interacts.
            async with httpx.AsyncClient(timeout=600 if defer_to_interactive else 150) as client:
                if on_first_token is None and on_text is None:
                    if (defer_to_interactive or background) and not interactive_routing:
                        pending = asyncio.create_task(client.post(url + "/v1/chat/completions", json=body))
                        try:
                            while not pending.done():
                                await asyncio.wait({pending}, timeout=0.1)
                                if self.interactive:
                                    raise InferenceDeferred(
                                        "Live interaction interrupted internal preparation"
                                    )
                            response = pending.result()
                        finally:
                            if not pending.done():
                                pending.cancel()
                            await asyncio.gather(pending, return_exceptions=True)
                    else:
                        response = await client.post(url + "/v1/chat/completions", json=body)
                    response.raise_for_status()
                    result = response.json()
                else:
                    # Measure first visible content/tool arguments; hidden reasoning is never retained.
                    body["stream"] = True
                    content, calls, first, finished = [], {}, True, False
                    async with client.stream("POST", url + "/v1/chat/completions", json=body) as response:
                        response.raise_for_status()
                        async for line in response.aiter_lines():
                            if not line.startswith("data: "):
                                continue
                            if line[6:] == "[DONE]":
                                finished = True
                                break
                            event = json.loads(line[6:])
                            for choice in event.get("choices", []):
                                if choice.get("finish_reason") in {"length", "content_filter", "error"}:
                                    raise ValueError("Incomplete local model generation")
                                delta = choice.get("delta", {})
                                if first and (delta.get("content") or delta.get("tool_calls")):
                                    if on_first_token:
                                        on_first_token()
                                    first = False
                                if delta.get("content"):
                                    content.append(delta["content"])
                                    if on_text and not tools and not calls and not delta.get("tool_calls"):
                                        await on_text(delta["content"])
                                for call in delta.get("tool_calls", []):
                                    index = call["index"]
                                    target = calls.setdefault(
                                        index,
                                        {
                                            "id": "",
                                            "type": "function",
                                            "function": {"name": "", "arguments": ""},
                                        },
                                    )
                                    if call.get("id"):
                                        target["id"] = call["id"]
                                    for key in ("name", "arguments"):
                                        target["function"][key] += call.get("function", {}).get(key, "")
                    if not finished:
                        raise ValueError("Incomplete local model stream")
                    # A model can emit a preamble before a later tool call. Never speak
                    # that unverified intermediate text as though it were the answer.
                    if tools and on_text and not calls:
                        await on_text("".join(content))
                    result = {
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "content": "".join(content),
                                    "tool_calls": [calls[index] for index in sorted(calls)],
                                }
                            }
                        ]
                    }
                result["elapsed_ms"] = round((time.monotonic() - started) * 1000)
                if not background and not defer_to_interactive:
                    self.workload.observe(result["elapsed_ms"])
                # Deliberately retain only final answer/tool calls, never hidden reasoning traces.
                message = result["choices"][0]["message"]
                if result["choices"][0].get("finish_reason") in {"length", "content_filter", "error"}:
                    raise ValueError("Incomplete local model generation")
                message.pop("reasoning_content", None)
                message.pop("reasoning", None)
                return result
        except (httpx.HTTPError, ValueError, KeyError, TypeError, IndexError) as e:
            reason = {
                "Incomplete local model generation": "generation_limit_or_filter",
                "Incomplete local model stream": "incomplete_stream",
            }.get(str(e), "transport_error" if isinstance(e, httpx.HTTPError) else "invalid_response")
            raise ModelUnavailable(
                "Local model is unavailable or returned an invalid response", reason=reason
            ) from e
        finally:
            if not background and not defer_to_interactive:
                self.interactive -= 1

    async def embed(self, text):
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                self.settings.embedding_url + "/v1/embeddings",
                json={"model": "jarvis-embedding", "input": text[:12000]},
            )
            r.raise_for_status()
            vector = r.json()["data"][0]["embedding"]
            if len(vector) != 1024:
                raise ValueError("Embedding dimension mismatch")
            return json.dumps(vector)

    async def rerank(self, query, texts):
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                self.settings.reranker_url + "/rerank",
                json={"query": query[:2000], "documents": [t[:8000] for t in texts]},
            )
            r.raise_for_status()
            return r.json()["results"]
