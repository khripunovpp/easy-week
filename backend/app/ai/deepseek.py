import asyncio
import json
import logging
from typing import Any

import httpx

from ..config import settings

logger = logging.getLogger("easy_week.deepseek")


class DeepSeekError(RuntimeError):
    pass


async def deepseek_json(
    messages: list[dict[str, str]], *, max_tokens: int = 2048, retries: int = 2
) -> dict[str, Any]:
    """Вызов DeepSeek (OpenAI-совместимый) в JSON-режиме. Схема описывается в промпте."""
    if not settings.deepseek_configured:
        raise DeepSeekError("DeepSeek не настроен: нет DEEPSEEK_API_KEY")

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
