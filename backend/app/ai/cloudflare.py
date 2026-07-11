import json
from typing import Any

import httpx

from ..config import settings

_BASE = "https://api.cloudflare.com/client/v4/accounts"


class CloudflareError(RuntimeError):
    pass


async def run_json(
    messages: list[dict[str, str]],
    json_schema: dict[str, Any],
    *,
    model: str | None = None,
    max_tokens: int = 2048,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Вызов Workers AI со структурированным выводом (json_schema).

    Возвращает (parsed_json, usage). Экономия токенов: max_tokens ограничен,
    ответ строго по схеме — без лишней прозы.
    """
    if not settings.cf_configured:
        raise CloudflareError("Cloudflare не настроен: нет CF_ACCOUNT_ID / CF_API_TOKEN")

    model = model or settings.cf_model
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
        raise CloudflareError(f"Workers AI {resp.status_code}: {resp.text[:400]}")

    body = resp.json()
    if not body.get("success", True):
        raise CloudflareError(f"Workers AI errors: {body.get('errors')}")

    result = body.get("result", {})
    response = result.get("response")
    if response is None:
        raise CloudflareError(f"Пустой ответ Workers AI: {str(body)[:300]}")

    if isinstance(response, str):
        try:
            parsed = json.loads(response)
        except json.JSONDecodeError as exc:
            raise CloudflareError(f"Ответ не JSON: {response[:300]}") from exc
    else:
        parsed = response

    usage = result.get("usage", {}) or {}
    return parsed, usage
