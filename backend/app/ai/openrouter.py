"""OpenRouter — OpenAI-совместимый шлюз ко многим моделям (в т.ч. бесплатным «:free»).

Зачем: попробовать дешёвые/бесплатные модели на «служебных» задачах, где раньше был только
Cloudflare (нормализация покупок, извлечение предпочтений). Какая именно модель — в .env
(`OPENROUTER_MODEL`), по умолчанию nvidia/nemotron-3-super-120b-a12b:free.

Особенности:
- JSON-режим `response_format: json_object` (схема — в промпте, как у DeepSeek); ответ парсим
  лениво (`loads_lenient`: снимаем ```-ограждение, обрезаем прозу вокруг объекта).
- `reasoning.enabled=false`: у reasoning-моделей (nemotron, qwen) размышления съедают
  max_tokens и обрывают JSON — как thinkingBudget=0 у Gemini.
- Ошибку OpenRouter может отдать и с кодом 200 в теле (`{"error": …}`) — проверяем оба случая.
- Ответ, обрезанный по max_tokens (finish_reason=length) и не разобранный — AINonRetryable:
  повтор тем же входом снова обрежется.
- Стрима и tools нет (правки плана — structured actions, план — целиком, как у Gemini).
"""

import json
import logging
from typing import Any

import httpx

from ..config import settings
from .base import AIError, AINonRetryable, ModelGate, loads_lenient

logger = logging.getLogger("easy_week.openrouter")

# OpenRouter просит идентифицировать приложение (попадает в его статистику; необязательно).
_APP_HEADERS = {"HTTP-Referer": "https://github.com/pashtitto/easy-week", "X-Title": "Easy Week"}


class OpenRouterGate(ModelGate):
    """OpenRouter (OpenAI-совместимый /chat/completions): JSON-задачи одним запросом."""

    key = "openrouter"
    provider = "OpenRouter"
    supports_stream = False
    supports_tools = False

    @property
    def configured(self) -> bool:
        return settings.openrouter_configured

    @property
    def default_model(self) -> str:
        return settings.openrouter_model

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {settings.openrouter_api_key}", **_APP_HEADERS}

    async def _post(self, payload: dict[str, Any]) -> dict:
        url = f"{settings.openrouter_base_url}/chat/completions"
        async with httpx.AsyncClient(timeout=90.0) as client:
            resp = await client.post(url, json=payload, headers=self._headers())
        if resp.status_code != 200:
            raise AIError(f"OpenRouter {resp.status_code}: {resp.text[:300]}")
        body = resp.json()
        if isinstance(body, dict) and body.get("error"):
            err = body["error"]
            msg = err.get("message") if isinstance(err, dict) else str(err)
            raise AIError(f"OpenRouter error: {str(msg)[:300]}")
        return body

    async def _request_json(
        self,
        messages: list[dict[str, Any]],
        schema: dict[str, Any] | None,
        model: str,
        max_tokens: int,
        temperature: float,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "response_format": {"type": "json_object"},  # схема — в промпте
            "max_tokens": max_tokens,
            "temperature": temperature,
            "reasoning": {"enabled": False},
        }
        body = await self._post(payload)
        choices = body.get("choices") or []
        if not choices:
            raise AIError(f"Пустой ответ OpenRouter: {str(body)[:200]}")
        choice = choices[0]
        content = (choice.get("message") or {}).get("content") or ""
        finish = choice.get("finish_reason") or ""
        try:
            parsed = loads_lenient(content)
        except json.JSONDecodeError as exc:
            details = {"stop_reason": finish, "raw_head": content[:300]}
            if finish == "length":
                raise AINonRetryable(
                    f"OpenRouter: ответ обрезан по max_tokens: {exc}", details
                ) from exc
            raise AIError(f"OpenRouter: не JSON ({finish}): {exc}", details) from exc
        return parsed, body.get("usage", {}) or {}
