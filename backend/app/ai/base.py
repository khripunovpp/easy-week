"""Базовый класс модели-гейта (Strategy + Template Method).

Каждый провайдер (DeepSeek, Cloudflare, Gemini) — это подкласс `ModelGate`, который
инкапсулирует подготовку запроса, вызов API и разбор ответа. Общий пайплайн
(guard «настроен?» → ретрай транзиентных ошибок → лог → возврат `(parsed, usage)`)
живёт в `complete_json`; провайдер-специфика — в хуке `_request_json`.

Кросс-провайдерных фолбэков тут нет: выбранная модель либо отвечает, либо падает с
`AIError`, которую ловит роутер и показывает ошибку пользователю.
"""

import asyncio
import json
import logging
import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any

import httpx

from .observe import log_ai_call, log_ai_error

logger = logging.getLogger("easy_week.ai.gate")


class AIError(RuntimeError):
    """Единая ошибка любого гейта модели.

    details — доп. поля для JSONL-лога неудачной попытки (напр. stop_reason, raw-сниппет)."""

    def __init__(self, msg: str = "", details: dict | None = None) -> None:
        super().__init__(msg)
        self.details = details or {}


class AINonRetryable(AIError):
    """Ошибка, которую бессмысленно ретраить тем же входом (гейт уже сделал свою
    корректирующую попытку, либо ответ обрезан по max_tokens) — базовый ретрай пропускаем."""


def loads_lenient(text: str) -> dict:
    """JSON из ответа модели без строгого JSON-режима: снимаем ```-ограждение и обрезаем до
    внешнего объекта (проза до/после). Битый JSON → JSONDecodeError (ValueError) → ретрай в базе."""
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


class ModelGate(ABC):
    """Стратегия работы с одним провайдером модели."""

    # Ключ выбора (deepseek | gemini | cloudflare) и метка для логов/UI.
    key: str = ""
    provider: str = ""
    supports_stream: bool = False
    supports_tools: bool = False

    @property
    @abstractmethod
    def configured(self) -> bool:
        """Есть ли ключи/настройки, чтобы вызывать провайдера."""

    @property
    @abstractmethod
    def default_model(self) -> str:
        """Модель по умолчанию, если вызов не задал `model=`."""

    async def complete_json(
        self,
        messages: list[dict[str, Any]],
        *,
        schema: dict[str, Any] | None = None,
        model: str | None = None,
        max_tokens: int = 2048,
        temperature: float = 0.7,
        retries: int = 2,
        label: str = "",
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Запрос в JSON-режиме с ретраями и единым логом. Возвращает (parsed, usage).

        Ретрай — только транзиентный на ЭТОЙ же модели, а не переход на другого провайдера.
        """
        if not self.configured:
            raise AIError(f"{self.provider} не настроен")

        model = model or self.default_model
        log_model = self._log_model(model)
        logger.info("AI → %s · %s · %s", self.provider, log_model, label or "?")

        last: Exception | None = None
        for attempt in range(retries + 1):
            t0 = time.monotonic()
            try:
                parsed, usage = await self._request_json(
                    messages, schema, model, max_tokens, temperature
                )
                dur = int((time.monotonic() - t0) * 1000)
                log_ai_call(self.provider, log_model, label, messages, parsed, usage, dur)
                return parsed, usage
            except (AIError, httpx.HTTPError, ValueError, KeyError) as exc:
                dur = int((time.monotonic() - t0) * 1000)
                last = exc
                logger.warning(
                    "%s attempt %d failed: %s", self.provider, attempt + 1, str(exc)[:150]
                )
                log_ai_error(
                    self.provider, log_model, label, messages, str(exc), attempt + 1, dur,
                    extra=getattr(exc, "details", None),
                )
                if isinstance(exc, AINonRetryable):
                    break  # тот же вход повторять бесполезно
                if attempt < retries:
                    await asyncio.sleep(0.4 * (attempt + 1))
        raise AIError(f"{self.provider} не ответил после {attempt + 1} попыток: {last}")

    @abstractmethod
    async def _request_json(
        self,
        messages: list[dict[str, Any]],
        schema: dict[str, Any] | None,
        model: str,
        max_tokens: int,
        temperature: float,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Один запрос: URL/headers/payload → вызов → (parsed, usage)."""

    async def stream_json(
        self,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int = 3000,
        model: str | None = None,
        label: str = "",
        temperature: float | None = None,
    ) -> AsyncIterator[str]:
        """Стрим дельт JSON-контента. По умолчанию не поддерживается.
        temperature=None — дефолт провайдера (план шлёт 1.0 ради разнообразия)."""
        raise NotImplementedError(f"{self.provider} не поддерживает стриминг")
        yield  # pragma: no cover — делает функцию асинхронным генератором

    async def call_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        max_tokens: int = 800,
        model: str | None = None,
        label: str = "",
    ) -> tuple[list[dict[str, Any]], str]:
        """Function calling. Возвращает (tool_calls, content). По умолчанию не поддерживается."""
        raise NotImplementedError(f"{self.provider} не поддерживает tools")

    def _log_model(self, model: str) -> str:
        """Как показывать модель в логах/метриках (Cloudflare режет префикс)."""
        return model
