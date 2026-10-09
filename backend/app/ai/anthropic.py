import json
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx

from ..config import settings
from .base import AIError, AINonRetryable, ModelGate, loads_lenient, parse_details
from .observe import log_ai_call

logger = logging.getLogger("easy_week.anthropic")

_API_VERSION = "2023-06-01"
# Claude не имеет JSON-режима как OpenAI — просим строгий JSON в промпте.
_JSON_ONLY = "\n\nВыводи ТОЛЬКО валидный JSON-объект: без пояснений и без markdown-ограждений (```)."

# Модели, которые ОТВЕРГАЮТ assistant-prefill (400): семейство 4.6+ и новее.
# Haiku 4.5 / Sonnet 4.5 / Opus 4.5 и старше prefill принимают.
_NO_PREFILL = (
    "opus-4-6", "opus-4-7", "opus-4-8", "sonnet-4-6", "opus-5", "sonnet-5", "fable", "mythos",
)

# Prefill «{»: модель продолжает уже начатый JSON-объект и не пишет прозу перед ним.
_PREFILL = "{"

# Корректирующая попытка: не повторяем тот же вход, а показываем модели её ответ
# и просим вернуть только JSON.
_FIX_JSON = (
    "Твой ответ выше — не валидный JSON. Верни ТОЛЬКО этот ответ как один валидный "
    "JSON-объект по заданной схеме: без пояснений, без текста до и после, без ```."
)


# Семейства, где размышление включено всегда (Opus 5.x, Sonnet 5.x, Fable, Mythos): оно тратит
# max_tokens ответа. Для рецептного JSON — effort «low» (меньше размышлений) + запас токенов.
_ALWAYS_THINKS = ("opus-5", "sonnet-5", "fable", "mythos")
_THINK_HEADROOM = 6000


def _thinking_kw(model: str, max_tokens: int) -> dict[str, Any]:
    m = (model or "").lower()
    if not any(tag in m for tag in _ALWAYS_THINKS):
        return {"max_tokens": max_tokens}
    return {"max_tokens": max_tokens + _THINK_HEADROOM, "output_config": {"effort": "low"}}


def _timeout(model: str) -> float:
    m = (model or "").lower()
    return 180.0 if any(tag in m for tag in _ALWAYS_THINKS) else 90.0


def _supports_prefill(model: str) -> bool:
    m = (model or "").lower()
    return not any(tag in m for tag in _NO_PREFILL)


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


def _with_prefill(conv: list[dict], model: str) -> tuple[list[dict], str]:
    """Добавляет assistant-prefill «{», если модель его поддерживает → (conv, prefill)."""
    if not _supports_prefill(model) or (conv and conv[-1]["role"] == "assistant"):
        return conv, ""
    return conv + [{"role": "assistant", "content": _PREFILL}], _PREFILL


def _extract_text(body: dict) -> str:
    parts = body.get("content") or []
    return "".join(b.get("text", "") for b in parts if isinstance(b, dict) and b.get("type") == "text")


_loads_lenient = loads_lenient  # общий парсер (ai/base.py); имя оставлено для тестов


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
        # Запись в кэш (1.25x цены input) — отдельно, для учёта затрат.
        "prompt_cache_write_tokens": u.get("cache_creation_input_tokens") or None,
    }


_parse_details = parse_details  # общие поля JSONL-лога битого ответа (ai/base.py)


class AnthropicGate(ModelGate):
    """Anthropic Claude через REST (/v1/messages). План и деталь рецепта, стриминг.

    Правки идут через structured-actions (см. planner), поэтому tools здесь не нужны.
    Температуру не шлём: Opus 4.8/4.7 её отвергают (400). Thinking по умолчанию выключен
    на Opus 4.8 (не шлём параметр) — рецептному JSON рассуждения не нужны.

    Строгий JSON: где модель позволяет — assistant-prefill «{» (дописываем его обратно при
    разборе); при битом JSON — ОДНА корректирующая попытка («верни только JSON»), а не
    повтор того же входа базовым ретраем.
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
        return self._model_override or settings.anthropic_model

    def _headers(self) -> dict[str, str]:
        return {"x-api-key": settings.anthropic_api_key, "anthropic-version": _API_VERSION}

    async def _post(self, payload: dict[str, Any]) -> dict:
        url = f"{settings.anthropic_base_url}/v1/messages"
        async with httpx.AsyncClient(timeout=_timeout(payload.get("model", ""))) as client:
            resp = await client.post(url, json=payload, headers=self._headers())
        if resp.status_code != 200:
            raise AIError(f"Claude {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    async def _request_json(
        self,
        messages: list[dict[str, Any]],
        schema: dict[str, Any] | None,
        model: str,
        max_tokens: int,
        temperature: float,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        system, conv = _to_system_and_messages(messages)
        conv, prefill = _with_prefill(conv, model)
        payload: dict[str, Any] = {"model": model, "messages": conv, **_thinking_kw(model, max_tokens)}
        if system:
            payload["system"] = system
        body = await self._post(payload)
        text = prefill + _extract_text(body)
        stop = body.get("stop_reason") or ""
        try:
            return _loads_lenient(text), _norm_usage(body.get("usage"))
        except json.JSONDecodeError as exc:
            details = _parse_details(text, stop, exc)
            logger.warning("Claude: не JSON (stop_reason=%s): %r", stop, text[:200])
            if stop == "max_tokens":
                # Обрезан по лимиту — повтор тем же входом снова обрежется.
                raise AINonRetryable(f"Claude: ответ обрезан по max_tokens: {exc}", details) from exc

        # Одна корректирующая попытка: показываем модели её ответ и просим только JSON.
        fix_conv = [
            *(conv[:-1] if prefill else conv),  # без prefill-сообщения первой попытки
            {"role": "assistant", "content": text.strip() or "(пусто)"},
            {"role": "user", "content": _FIX_JSON},
        ]
        fix_conv, prefill2 = _with_prefill(fix_conv, model)
        body2 = await self._post({**payload, "messages": fix_conv})
        text2 = prefill2 + _extract_text(body2)
        try:
            parsed = _loads_lenient(text2)
        except json.JSONDecodeError as exc:
            raise AINonRetryable(
                f"Claude: не JSON и после корректирующей попытки: {exc}",
                _parse_details(text2, body2.get("stop_reason") or "", exc),
            ) from exc
        # usage суммируем по обеим попыткам — чтобы метрики токенов были честными.
        u1, u2 = _norm_usage(body.get("usage")), _norm_usage(body2.get("usage"))
        usage = {k: ((u1.get(k) or 0) + (u2.get(k) or 0)) or None for k in u1}
        return parsed, usage

    async def stream_json(
        self,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int = 3000,
        model: str | None = None,
        label: str = "",
        temperature: float | None = None,
    ) -> AsyncIterator[str]:
        """Стрим Claude (SSE): отдаёт дельты текста по мере генерации.

        temperature игнорируется (Opus 4.7+ её отвергают). Prefill «{» — если модель
        поддерживает: отдаём его первой дельтой, чтобы парсер видел цельный JSON."""
        if not self.configured:
            raise AIError("Claude не настроен: нет ANTHROPIC_API_KEY")

        model = model or self.default_model
        logger.info("AI → Claude · %s · %s (stream)", model, label or "?")
        t0 = time.monotonic()
        system, conv = _to_system_and_messages(messages)
        conv, prefill = _with_prefill(conv, model)
        payload: dict[str, Any] = {
            "model": model,
            "messages": conv,
            "stream": True,
            **_thinking_kw(model, max_tokens),
        }
        if system:
            payload["system"] = system
        url = f"{settings.anthropic_base_url}/v1/messages"

        full: list[str] = []
        raw_usage: dict = {}  # сырой usage Claude: input из message_start, output — из message_delta
        stop_reason = ""
        if prefill:
            full.append(prefill)
            yield prefill
        async with httpx.AsyncClient(timeout=max(120.0, _timeout(model))) as client:
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
                        raw_usage = dict((obj.get("message") or {}).get("usage") or {})
                    elif kind == "message_delta":
                        # output_tokens в message_delta — накопительный итог; total считаем в конце.
                        raw_usage.update({
                            k: v for k, v in (obj.get("usage") or {}).items() if v is not None
                        })
                        stop_reason = (obj.get("delta") or {}).get("stop_reason") or stop_reason

        if stop_reason and stop_reason != "end_turn":
            logger.warning("Claude stream: stop_reason=%s (%s)", stop_reason, label or "?")
        log_ai_call(
            "Claude", model, label, messages, "".join(full), _norm_usage(raw_usage),
            int((time.monotonic() - t0) * 1000),
        )
