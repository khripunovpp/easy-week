import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx

from ..config import settings
from .base import AIError, ModelGate
from .observe import log_ai_call

logger = logging.getLogger("easy_week.gemini")


def _to_contents(messages: list[dict[str, Any]]) -> tuple[list[dict], dict | None]:
    """OpenAI-формат → Gemini: (contents, systemInstruction).

    role=system → systemInstruction; user→user; assistant→model; content → parts:[{text}].
    """
    contents: list[dict] = []
    system_parts: list[dict] = []
    for m in messages:
        role = m.get("role", "user")
        text = m.get("content", "")
        if not text:
            continue
        if role == "system":
            system_parts.append({"text": text})
        else:
            g_role = "model" if role == "assistant" else "user"
            contents.append({"role": g_role, "parts": [{"text": text}]})
    system = {"parts": system_parts} if system_parts else None
    return contents, system


def _norm_usage(meta: dict | None) -> dict[str, Any]:
    """usageMetadata Gemini → ключи как у OpenAI, чтобы observe считал метрики без правок."""
    u = meta or {}
    return {
        "prompt_tokens": u.get("promptTokenCount"),
        "completion_tokens": u.get("candidatesTokenCount"),
        "total_tokens": u.get("totalTokenCount"),
    }


def _extract_text(candidate: dict) -> str:
    parts = (candidate.get("content") or {}).get("parts") or []
    return "".join(p.get("text", "") for p in parts if isinstance(p, dict))


def _gen_config(temperature: float, max_tokens: int) -> dict[str, Any]:
    """generationConfig для JSON-задач.

    thinkingBudget=0 отключает «размышление» Gemini 3.x: рецептам оно не нужно, а иначе
    reasoning съедает бюджет и обрывает JSON. Заодно быстрее (меньше таймаутов)."""
    return {
        "temperature": temperature,
        "maxOutputTokens": max_tokens,
        "responseMimeType": "application/json",  # схема — в промпте
        "thinkingConfig": {"thinkingBudget": 0},
    }


class GeminiGate(ModelGate):
    """Gemini (Google AI Studio) через REST: план и деталь рецепта, стриминг.

    Правки плана идут через structured-actions (см. planner), поэтому tools здесь не нужны.
    """

    key = "gemini"
    provider = "Gemini"
    supports_stream = True
    supports_tools = False

    @property
    def configured(self) -> bool:
        return settings.gemini_configured

    @property
    def default_model(self) -> str:
        return settings.gemini_model

    async def _request_json(
        self,
        messages: list[dict[str, Any]],
        schema: dict[str, Any] | None,
        model: str,
        max_tokens: int,
        temperature: float,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        contents, system = _to_contents(messages)
        url = f"{settings.gemini_base_url}/models/{model}:generateContent"
        payload: dict[str, Any] = {
            "contents": contents,
            "generationConfig": _gen_config(temperature, max_tokens),
        }
        if system:
            payload["systemInstruction"] = system
        headers = {"x-goog-api-key": settings.gemini_api_key}

        async with httpx.AsyncClient(timeout=90.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
        if resp.status_code != 200:
            raise AIError(f"Gemini {resp.status_code}: {resp.text[:300]}")
        body = resp.json()
        candidates = body.get("candidates") or []
        if not candidates:
            raise AIError(f"Пустой ответ Gemini: {str(body)[:200]}")
        content = _extract_text(candidates[0])
        return json.loads(content), _norm_usage(body.get("usageMetadata"))

    async def stream_json(
        self,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int = 3000,
        model: str | None = None,
        label: str = "",
    ) -> AsyncIterator[str]:
        """Стрим Gemini (SSE): отдаёт дельты текста по мере генерации."""
        if not self.configured:
            raise AIError("Gemini не настроен: нет GEMINI_API_KEY")

        model = model or self.default_model
        logger.info("AI → Gemini · %s · %s (stream)", model, label or "?")
        contents, system = _to_contents(messages)
        url = f"{settings.gemini_base_url}/models/{model}:streamGenerateContent?alt=sse"
        payload: dict[str, Any] = {
            "contents": contents,
            "generationConfig": _gen_config(0.7, max_tokens),
        }
        if system:
            payload["systemInstruction"] = system
        headers = {"x-goog-api-key": settings.gemini_api_key}

        full: list[str] = []
        usage: dict = {}
        async with httpx.AsyncClient(timeout=120.0) as client:
            async with client.stream("POST", url, json=payload, headers=headers) as resp:
                if resp.status_code != 200:
                    body = await resp.aread()
                    raise AIError(f"Gemini {resp.status_code}: {body[:300]!r}")
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    try:
                        obj = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if obj.get("usageMetadata"):
                        usage = _norm_usage(obj["usageMetadata"])
                    for cand in obj.get("candidates") or []:
                        delta = _extract_text(cand)
                        if delta:
                            full.append(delta)
                            yield delta

        log_ai_call("Gemini", model, label, messages, "".join(full), usage)
