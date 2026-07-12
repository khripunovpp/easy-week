import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx

from ..config import settings

logger = logging.getLogger("easy_week.deepseek")


class DeepSeekError(RuntimeError):
    pass


async def deepseek_stream(
    messages: list[dict[str, str]], *, max_tokens: int = 3000, label: str = ""
) -> AsyncIterator[str]:
    """Стриминг DeepSeek: отдаёт дельты контента по мере генерации (для SSE)."""
    if not settings.deepseek_configured:
        raise DeepSeekError("DeepSeek не настроен: нет DEEPSEEK_API_KEY")

    logger.info("AI → DeepSeek · %s · %s (stream)", settings.deepseek_model, label or "?")
    url = f"{settings.deepseek_base_url}/chat/completions"
    payload = {
        "model": settings.deepseek_model,
        "messages": messages,
        "response_format": {"type": "json_object"},
        "max_tokens": max_tokens,
        "temperature": 0.7,
        "stream": True,
    }
    headers = {"Authorization": f"Bearer {settings.deepseek_api_key}"}

    async with httpx.AsyncClient(timeout=120.0) as client:
        async with client.stream("POST", url, json=payload, headers=headers) as resp:
            if resp.status_code != 200:
                body = await resp.aread()
                raise DeepSeekError(f"DeepSeek {resp.status_code}: {body[:300]!r}")
            async for line in resp.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    delta = json.loads(data)["choices"][0]["delta"].get("content")
                except (json.JSONDecodeError, KeyError, IndexError):
                    continue
                if delta:
                    yield delta


async def deepseek_json(
    messages: list[dict[str, str]], *, max_tokens: int = 2048, retries: int = 2, label: str = ""
) -> dict[str, Any]:
    """Вызов DeepSeek (OpenAI-совместимый) в JSON-режиме. Схема описывается в промпте."""
    if not settings.deepseek_configured:
        raise DeepSeekError("DeepSeek не настроен: нет DEEPSEEK_API_KEY")

    logger.info("AI → DeepSeek · %s · %s", settings.deepseek_model, label or "?")
    url = f"{settings.deepseek_base_url}/chat/completions"
    payload = {
        "model": settings.deepseek_model,
        "messages": messages,
        "response_format": {"type": "json_object"},
        "max_tokens": max_tokens,
        "temperature": 0.7,
    }
    headers = {"Authorization": f"Bearer {settings.deepseek_api_key}"}

    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            async with httpx.AsyncClient(timeout=90.0) as client:
                resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code != 200:
                raise DeepSeekError(f"DeepSeek {resp.status_code}: {resp.text[:300]}")
            content = resp.json()["choices"][0]["message"]["content"]
            return json.loads(content)
        except (DeepSeekError, httpx.HTTPError, json.JSONDecodeError, KeyError) as exc:
            last = exc
            logger.warning("DeepSeek attempt %d failed: %s", attempt + 1, str(exc)[:150])
            if attempt < retries:
                await asyncio.sleep(0.4 * (attempt + 1))
    raise DeepSeekError(f"DeepSeek не ответил после {retries + 1} попыток: {last}")
