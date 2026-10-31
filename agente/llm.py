"""OpenRouter chat client (OpenAI-compatible) with image input and JSON replies."""
import base64
import io
import json
import os
import re
import time

import httpx
from PIL import Image

API_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL_FAST = os.environ.get("MODEL_FAST", "anthropic/claude-sonnet-5.5")
MODEL_SMART = os.environ.get("MODEL_SMART", "anthropic/claude-opus-5.5")


class LLMError(Exception):
    pass


def image_part(img: Image.Image) -> dict:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG", optimize=True)
    return {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()}}


def parse_json(text: str) -> dict:
    """Take the first JSON object in the reply (models sometimes wrap it in prose or fences)."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fence:
        text = fence.group(1)
    start = text.find("{")
    if start < 0:
        raise LLMError(f"no JSON in reply: {text[:200]}")
    depth, in_str, esc = 0, False, False
    for i, ch in enumerate(text[start:], start):
        if in_str:
            esc = ch == "\\" and not esc
            if ch == '"' and not esc:
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1])
    raise LLMError(f"unterminated JSON in reply: {text[:200]}")


class LLM:
    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.environ["OPENROUTER_API_KEY"]
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(60, connect=10))
        self.usage = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "seconds": 0.0}

    async def close(self) -> None:
        await self.client.aclose()

    async def json(self, system: str, content: list[dict], model: str | None = None, max_tokens: int = 1200, retries: int = 2) -> dict:
        body = {
            "model": model or MODEL_FAST,
            "temperature": 0,
            "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}],
        }
        last: Exception | None = None
        for attempt in range(retries + 1):
            t0 = time.monotonic()
            try:
                r = await self.client.post(API_URL, headers={"Authorization": f"Bearer {self.api_key}"}, json=body)
                r.raise_for_status()
                data = r.json()
                self.usage["calls"] += 1
                self.usage["seconds"] += time.monotonic() - t0
                for k in ("prompt_tokens", "completion_tokens"):
                    self.usage[k] += (data.get("usage") or {}).get(k, 0)
                return parse_json(data["choices"][0]["message"]["content"] or "")
            except (httpx.HTTPError, KeyError, LLMError, json.JSONDecodeError) as exc:
                last = exc
                if attempt < retries:
                    body["messages"][1]["content"] = content + [{"type": "text", "text": "Responde SOLO con un objeto JSON válido."}]
        raise LLMError(f"LLM failed after {retries + 1} attempts: {last}")
