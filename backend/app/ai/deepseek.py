import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx

from ..config import settings
from .base import AIError, ModelGate
from .observe import log_ai_call

logger = logging.getLogger("easy_week.deepseek")


class DeepSeekGate(ModelGate):
    """DeepSeek (OpenAI-совместимый): план, деталь рецепта, стриминг, function calling."""

    key = "deepseek"
    provider = "DeepSeek"
    supports_stream = True
    supports_tools = True

    @property
    def configured(self) -> bool:
        return settings.deepseek_configured

    @property
    def default_model(self) -> str:
        return settings.deepseek_model

    async def _request_json(
        self,
        messages: list[dict[str, Any]],
        schema: dict[str, Any] | None,
        model: str,
        max_tokens: int,
        temperature: float,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        url = f"{settings.deepseek_base_url}/chat/completions"
        payload = {
            "model": model,
            "messages": messages,
            "response_format": {"type": "json_object"},  # схема — в промпте
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        headers = {"Authorization": f"Bearer {settings.deepseek_api_key}"}
        async with httpx.AsyncClient(timeout=90.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
        if resp.status_code != 200:
            raise AIError(f"DeepSeek {resp.status_code}: {resp.text[:300]}")
        body = resp.json()
        content = body["choices"][0]["message"]["content"]
        return json.loads(content), body.get("usage", {}) or {}

    async def stream_json(
        self,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int = 3000,
        model: str | None = None,
        label: str = "",
    ) -> AsyncIterator[str]:
        """Стриминг DeepSeek: отдаёт дельты контента по мере генерации (для SSE)."""
        if not self.configured:
            raise AIError("DeepSeek не настроен: нет DEEPSEEK_API_KEY")

        model = model or self.default_model
        logger.info("AI → DeepSeek · %s · %s (stream)", model, label or "?")
        url = f"{settings.deepseek_base_url}/chat/completions"
        payload = {
            "model": model,
            "messages": messages,
            "response_format": {"type": "json_object"},
            "max_tokens": max_tokens,
            "temperature": 0.7,
            "stream": True,
            "stream_options": {"include_usage": True},  # финальный чанк с usage (в т.ч. кэш)
        }
        headers = {"Authorization": f"Bearer {settings.deepseek_api_key}"}

        full: list[str] = []
        usage: dict = {}
        async with httpx.AsyncClient(timeout=120.0) as client:
            async with client.stream("POST", url, json=payload, headers=headers) as resp:
                if resp.status_code != 200:
                    body = await resp.aread()
                    raise AIError(f"DeepSeek {resp.status_code}: {body[:300]!r}")
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if obj.get("usage"):
                        usage = obj["usage"]
                    choices = obj.get("choices") or []
                    delta = choices[0].get("delta", {}).get("content") if choices else None
                    if isinstance(delta, str) and delta:
                        full.append(delta)
                        yield delta

        log_ai_call("DeepSeek", model, label, messages, "".join(full), usage)

    async def call_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        max_tokens: int = 800,
        model: str | None = None,
        label: str = "",
    ) -> tuple[list[dict[str, Any]], str]:
        """Вызов DeepSeek с function calling. Возвращает (tool_calls, content).

        tool_calls — список {"name": str, "args": dict} (аргументы уже распарсены из JSON).
        content — текстовый ответ модели (обычно пустой, если она вызвала функции)."""
        if not self.configured:
            raise AIError("DeepSeek не настроен: нет DEEPSEEK_API_KEY")

        model = model or self.default_model
        logger.info("AI → DeepSeek · %s · %s (tools)", model, label or "?")
        url = f"{settings.deepseek_base_url}/chat/completions"
        payload = {
            "model": model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "max_tokens": max_tokens,
            "temperature": 0.3,
        }
        headers = {"Authorization": f"Bearer {settings.deepseek_api_key}"}

        async with httpx.AsyncClient(timeout=90.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
        if resp.status_code != 200:
            raise AIError(f"DeepSeek {resp.status_code}: {resp.text[:300]}")
        body = resp.json()
        message = body["choices"][0]["message"]
        content = message.get("content") or ""

        calls: list[dict[str, Any]] = []
        for tc in message.get("tool_calls") or []:
            fn = tc.get("function") or {}
            raw = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw) if isinstance(raw, str) else raw
            except json.JSONDecodeError:
                args = {}
            calls.append(
                {"name": fn.get("name", ""), "args": args if isinstance(args, dict) else {}}
            )

        log_ai_call(
            "DeepSeek", model, label, messages,
            content or json.dumps(calls, ensure_ascii=False), body.get("usage", {}),
        )
        return calls, content
