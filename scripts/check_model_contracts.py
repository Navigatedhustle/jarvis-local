import asyncio
import json

import httpx

from jarvis.config import settings
from jarvis.inference import Inference


async def main():
    inference = Inference(settings)
    schema = {
        "type": "object",
        "properties": {"ready": {"type": "boolean"}},
        "required": ["ready"],
        "additionalProperties": False,
    }
    for background in (False, True):
        result = await inference.chat(
            [{"role": "user", "content": "Return ready true in the required JSON."}],
            background=background,
            schema=schema,
        )
        parsed = json.loads(result["choices"][0]["message"]["content"])
        assert parsed == {"ready": True}, parsed
        print(("background" if background else "primary") + " structured JSON passed")
    payload = {
        "model": "jarvis-primary",
        "messages": [{"role": "user", "content": "Use the attention tool to check my workload."}],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "attention",
                    "description": "Read workload",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
        "tool_choice": {"type": "function", "function": {"name": "attention"}},
        "max_tokens": 100,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    async with httpx.AsyncClient(timeout=90) as client:
        response = await client.post(settings.primary_url + "/v1/chat/completions", json=payload)
        response.raise_for_status()
        assert response.json()["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "attention"
    print("primary tool schema passed")


if __name__ == "__main__":
    asyncio.run(main())
