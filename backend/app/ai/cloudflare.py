import asyncio
import json
import logging
from typing import Any

import httpx

from ..config import settings

_BASE = "https://api.cloudflare.com/client/v4/accounts"
logger = logging.getLogger("easy_week.cloudflare")


class CloudflareError(RuntimeError):
    pass


async def _run_once(
    messages: list[dict[str, str]],
    json_schema: dict[str, Any],
    model: str,
    max_tokens: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    url = f"{_BASE}/{settings.cf_account_id}/ai/run/{model}"
    payload = {
        "messages": messages,
        "response_format": {"type": "json_schema", "json_schema": json_schema},
        "max_tokens": max_tokens,
    }
    headers = {"Authorization": f"Bearer {settings.cf_api_token}"}

    async with httpx.AsyncClient(timeout=90.0) as client:
        resp = await client.post(url, json=payload, headers=headers)

    if resp.status_code != 200:
        raise CloudflareError(f"Workers AI {resp.status_code}: {resp.text[:300]}")

    body = resp.json()
    if not body.get("success", True):
        raise CloudflareError(f"Workers AI errors: {body.get('errors')}")

    result = body.get("result", {})
    response = result.get("response")
    if response is None:
        raise CloudflareError(f"Пустой ответ Workers AI: {str(body)[:200]}")

    if isinstance(response, str):
        try:
            parsed = json.loads(response)
        except json.JSONDecodeError as exc:
            raise CloudflareError(f"Ответ не JSON: {response[:200]}") from exc
    else:
        parsed = response

    return parsed, result.get("usage", {}) or {}


async def run_json(
    messages: list[dict[str, str]],
    json_schema: dict[str, Any],
    *,
    model: str | None = None,
    max_tokens: int = 2048,
    retries: int = 2,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Вызов Workers AI со структурированным выводом (json_schema), с ретраями.

    8b иногда возвращает обрезанный/битый JSON — повторяем несколько раз.
    """
    if not settings.cf_configured:
        raise CloudflareError("Cloudflare не настроен: нет CF_ACCOUNT_ID / CF_API_TOKEN")

    model = model or settings.cf_model
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return await _run_once(messages, json_schema, model, max_tokens)
        except (CloudflareError, httpx.HTTPError) as exc:
            last = exc
            logger.warning("Workers AI attempt %d failed: %s", attempt + 1, str(exc)[:150])
            if attempt < retries:
                await asyncio.sleep(0.4 * (attempt + 1))

    raise CloudflareError(f"Workers AI не ответил после {retries + 1} попыток: {last}")
