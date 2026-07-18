import json
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx

from ..config import settings
from .base import AIError, ModelGate
from .observe import log_ai_call

logger = logging.getLogger("easy_week.anthropic")

_API_VERSION = "2023-06-01"
# Claude не имеет JSON-режима как OpenAI — просим строгий JSON в промпте.
_JSON_ONLY = "\n\nВыводи ТОЛЬКО валидный JSON-объект: без пояснений и без markdown-ограждений (```)."


def _to_system_and_messages(messages: list[dict[str, Any]]) -> tuple[str, list[dict]]:
    """OpenAI-формат → Anthropic: (system-текст, messages без system).

    У Claude system — отдельное top-level поле, а не сообщение с ролью system.
    """
    system_parts: list[str] = []
    conv: list[dict] = []
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content", "")
        if not content:
            continue
        if role == "system":
            system_parts.append(content)
        else:
            conv.append({"role": "assistant" if role == "assistant" else "user", "content": content})
    system = "\n\n".join(system_parts)
    return (system + _JSON_ONLY if system else _JSON_ONLY.strip()), conv


def _extract_text(body: dict) -> str:
    parts = body.get("content") or []
    return "".join(b.get("text", "") for b in parts if isinstance(b, dict) and b.get("type") == "text")


def _loads_lenient(text: str) -> dict:
    """JSON из ответа Claude: снимаем ```-ограждение и обрезаем до внешнего объекта."""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t[3:]
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
        t = t.strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        i, j = t.find("{"), t.rfind("}")
        if i != -1 and j > i:
            return json.loads(t[i : j + 1])  # noqa: E203
        raise


def _norm_usage(u: dict | None) -> dict[str, Any]:
    """usage Claude → ключи как у OpenAI, чтобы observe считал метрики без правок."""
    u = u or {}
    cache_read = u.get("cache_read_input_tokens") or 0
    prompt = (u.get("input_tokens") or 0) + cache_read + (u.get("cache_creation_input_tokens") or 0)
    completion = u.get("output_tokens")
    return {
        "prompt_tokens": prompt or None,
        "completion_tokens": completion,
        "total_tokens": (prompt + (completion or 0)) or None,
        "prompt_cache_hit_tokens": cache_read or None,
    }


class AnthropicGate(ModelGate):
    """Anthropic Claude через REST (/v1/messages). План и деталь рецепта, стриминг.

    Правки идут через structured-actions (см. planner), поэтому tools здесь не нужны.
    Температуру не шлём: Opus 4.8/4.7 её отвергают (400). Thinking по умолчанию выключен
    на Opus 4.8 (не шлём параметр) — рецептному JSON рассуждения не нужны.
    """

    key = "anthropic"
    provider = "Claude"
    supports_stream = True
    supports_tools = False

    @property
    def configured(self) -> bool:
        return settings.anthropic_configured

    @property
    def default_model(self) -> str:
        return settings.anthropic_model

    def _headers(self) -> dict[str, str]:
        return {"x-api-key": settings.anthropic_api_key, "anthropic-version": _API_VERSION}

    async def _request_json(
        self,
        messages: list[dict[str, Any]],
        schema: dict[str, Any] | None,
        model: str,
        max_tokens: int,
        temperature: float,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        system, conv = _to_system_and_messages(messages)
        payload: dict[str, Any] = {"model": model, "max_tokens": max_tokens, "messages": conv}
        if system:
            payload["system"] = system
        url = f"{settings.anthropic_base_url}/v1/messages"
        async with httpx.AsyncClient(timeout=90.0) as client:
            resp = await client.post(url, json=payload, headers=self._headers())
        if resp.status_code != 200:
            raise AIError(f"Claude {resp.status_code}: {resp.text[:300]}")
        body = resp.json()
        return _loads_lenient(_extract_text(body)), _norm_usage(body.get("usage"))

    async def stream_json(
        self,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int = 3000,
        model: str | None = None,
        label: str = "",
    ) -> AsyncIterator[str]:
        """Стрим Claude (SSE): отдаёт дельты текста по мере генерации."""
        if not self.configured:
            raise AIError("Claude не настроен: нет ANTHROPIC_API_KEY")

        model = model or self.default_model
        logger.info("AI → Claude · %s · %s (stream)", model, label or "?")
        t0 = time.monotonic()
        system, conv = _to_system_and_messages(messages)
        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": conv,
            "stream": True,
        }
        if system:
            payload["system"] = system
        url = f"{settings.anthropic_base_url}/v1/messages"

        full: list[str] = []
        usage: dict = {}
        async with httpx.AsyncClient(timeout=120.0) as client:
            async with client.stream("POST", url, json=payload, headers=self._headers()) as resp:
                if resp.status_code != 200:
                    body = await resp.aread()
                    raise AIError(f"Claude {resp.status_code}: {body[:300]!r}")
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    try:
                        obj = json.loads(line[5:].strip())
                    except json.JSONDecodeError:
                        continue
                    kind = obj.get("type")
                    if kind == "content_block_delta":
                        delta = obj.get("delta") or {}
                        if delta.get("type") == "text_delta":
                            txt = delta.get("text", "")
                            if txt:
                                full.append(txt)
                                yield txt
                    elif kind == "message_start":
                        usage = _norm_usage((obj.get("message") or {}).get("usage"))
                    elif kind == "message_delta":
                        out = (obj.get("usage") or {}).get("output_tokens")
                        if out is not None:
                            usage["completion_tokens"] = out

        log_ai_call(
            "Claude", model, label, messages, "".join(full), usage,
            int((time.monotonic() - t0) * 1000),
        )
