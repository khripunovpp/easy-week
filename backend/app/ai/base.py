"""Базовый класс модели-гейта (Strategy + Template Method).

Каждый провайдер (DeepSeek, Cloudflare, Gemini) — это подкласс `ModelGate`, который
инкапсулирует подготовку запроса, вызов API и разбор ответа. Общий пайплайн
(guard «настроен?» → «не заблокирован квотой?» → ретрай транзиентных ошибок с паузой →
лог → возврат `(parsed, usage)`) живёт в `complete_json`; провайдер-специфика — в хуке
`_request_json`, пауза перед повтором — `_retry_delay` (`overload_backoff`, `retry_after`),
блок по квоте — `_check_available`.

Кросс-провайдерных фолбэков тут нет: выбранная модель либо отвечает, либо падает с
`AIError`, которую ловит роутер и показывает ошибку пользователю.
"""

import asyncio
import copy
import json
import logging
import random
import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any

import httpx

from .observe import log_ai_call, log_ai_error

logger = logging.getLogger("easy_week.ai.gate")


class AIError(RuntimeError):
    """Единая ошибка любого гейта модели.

    details — доп. поля для JSONL-лога неудачной попытки (напр. stop_reason, raw-сниппет).
    retry_after — сколько секунд ждать перед повтором, если провайдер сам сказал
    (RetryInfo / Retry-After); None — пауза по правилам гейта (`ModelGate._retry_delay`)."""

    def __init__(
        self, msg: str = "", details: dict | None = None, *, retry_after: float | None = None
    ) -> None:
        super().__init__(msg)
        self.details = details or {}
        self.retry_after = retry_after


class AINonRetryable(AIError):
    """Ошибка, которую бессмысленно ретраить тем же входом (гейт уже сделал свою
    корректирующую попытку, либо ответ обрезан по max_tokens) — базовый ретрай пропускаем."""


class AIOverloaded(AIError):
    """Провайдер перегружен (503/500/502/504, 429 без исчерпания квоты): повторять стоит, но
    не сразу — гейт с `overload_backoff` ждёт дольше обычного, со случайным разбросом."""


class LimitError(AINonRetryable):
    """Лимит/квота исчерпаны: свой дневной лимит (Claude, ai/limits.enforce_daily) или квота
    провайдера (Gemini 429 «You exceeded your current quota»). Повтор бессмысленен:
    complete_json пробрасывает её как есть, роутеры отдают 429 с этим текстом (фронт показывает
    его и не предлагает «Повторить» той же моделью). until — когда снова можно (epoch, с;
    0 — неизвестно)."""

    def __init__(
        self, msg: str = "", details: dict | None = None, *, until: float = 0.0
    ) -> None:
        super().__init__(msg, details)
        self.until = until


def parse_details(text: str, stop_reason: str, exc: json.JSONDecodeError | None = None) -> dict:
    """Поля для JSONL-лога битого ответа: причина остановки, длина, начало/конец сырого текста
    и (если есть ошибка разбора) кусок вокруг места, где парсер споткнулся."""
    out = {
        "stop_reason": stop_reason or "",
        "raw_len": len(text),
        "raw_head": text[:300],
        "raw_tail": text[-200:] if len(text) > 300 else "",
    }
    if exc is not None and isinstance(getattr(exc, "doc", None), str):
        out["raw_near_error"] = exc.doc[max(0, exc.pos - 120) : exc.pos + 80]  # noqa: E203
    return out


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
    # Конкретная модель, выбранная в настройках (with_model); None — модель провайдера из .env.
    _model_override: str | None = None
    # Пауза перед n-м повтором при перегрузке (AIOverloaded), с: случайно в [от, до] — чтобы
    # повторы не попадали в тот же всплеск нагрузки. None — как любой сбой (0.4·n с).
    overload_backoff: tuple[tuple[float, float], ...] | None = None
    # Сколько всего можно проспать между повторами одного complete_json: пользователь ждёт
    # ответа, nginx рвёт запрос через 120 с. Пауза сверх остатка — не ждём, отдаём ошибку.
    retry_sleep_budget: float = 20.0

    def with_model(self, model_id: str | None) -> "ModelGate":
        """Тот же гейт с другой моделью по умолчанию (выбор в настройках: «провайдер:id»).
        Копия — модульный синглтон не трогаем; все вызовы берут `model or self.default_model`."""
        if not model_id or model_id == self.default_model:
            return self
        clone = copy.copy(self)
        clone._model_override = model_id
        return clone

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
        self._check_available(model, label)  # квота уже исчерпана → LimitError без вызова
        logger.info("AI → %s · %s · %s", self.provider, log_model, label or "?")

        last: Exception | None = None
        slept = 0.0
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
                if isinstance(exc, LimitError):
                    raise  # лимит/квота: свой текст и 429 у роутера, без обёртки «не ответил»
                if isinstance(exc, AINonRetryable):
                    break  # тот же вход повторять бесполезно
                if attempt < retries:
                    delay = self._retry_delay(exc, attempt)
                    if slept + delay > self.retry_sleep_budget:
                        logger.warning(
                            "%s: пауза %.1f с сверх бюджета ожидания (%.0f с) — не повторяем",
                            self.provider, delay, self.retry_sleep_budget,
                        )
                        break
                    slept += delay
                    await asyncio.sleep(delay)
        raise AIError(f"{self.provider} не ответил после {attempt + 1} попыток: {last}")

    def _check_available(self, model: str, label: str = "") -> None:
        """Хук: провайдер помнит «заблокировано до» (квота) → LimitError сразу, без вызова API.
        По умолчанию — ничего (лимиты Claude проверяет planner через limits.enforce_daily)."""

    def _retry_delay(self, exc: Exception, attempt: int) -> float:
        """Пауза перед повтором № attempt+1, с. Провайдер сам назвал (`retry_after`) — столько;
        перегрузка у гейта с `overload_backoff` — дольше и со случайным разбросом; иначе 0.4·n."""
        after = getattr(exc, "retry_after", None)
        if after is not None:
            return max(0.0, float(after))
        if isinstance(exc, AIOverloaded) and self.overload_backoff:
            lo, hi = self.overload_backoff[min(attempt, len(self.overload_backoff) - 1)]
            return random.uniform(lo, hi)
        return 0.4 * (attempt + 1)

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
