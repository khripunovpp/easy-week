"""Gemini (Google AI Studio, REST generateContent).

Устойчивость (повторы — только той же моделью, фолбэков на другие нет):
- 503/500/502/504 и 429 без исчерпания квоты — `AIOverloaded`: повтор через 2–6 с со случайным
  разбросом (`overload_backoff`), а не через 0.4 с — иначе все попытки попадают в один всплеск
  «high demand». Срок из тела (RetryInfo.retryDelay) или Retry-After уважаем, пока он в бюджете
  ожидания (`ModelGate.retry_sleep_budget`, 20 с); длиннее — не ждём.
- 429 «You exceeded your current quota» — `LimitError` (у роутера 429 с понятным текстом): не
  повторяем и помним «заблокировано до» по модели — до этого в Gemini не ходим. Дневная квота
  (quotaId …PerDay…) — до полуночи по Тихоокеанскому (тогда Google её сбрасывает), иначе — на
  retryDelay из тела, без него — на минуту.
- Остальные 4xx (ключ, модель, параметры) — `AINonRetryable`: тот же вход снова упадёт.
- finishReason SAFETY/RECITATION/… — `AINonRetryable`; MAX_TOKENS — тоже, если JSON не разобрался.
  JSON разбираем лениво (`loads_lenient`); битый ответ — в JSONL с началом/концом сырого текста,
  куском у места ошибки, finishReason и usage (`parse_details`). Тело ошибки API — целиком в JSONL
  (error_status / error_message / quota_ids / retry_delay_s / error_body), а не первые 300 символов.
"""

import json
import logging
import math
import re
import time
from collections.abc import AsyncIterator, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from ..config import settings
from .base import (
    AIError,
    AINonRetryable,
    AIOverloaded,
    LimitError,
    ModelGate,
    loads_lenient,
    parse_details,
)
from .observe import log_ai_call

logger = logging.getLogger("easy_week.gemini")

_OVERLOAD_CODES = {500, 502, 503, 504}
# Ответ остановлен фильтром — повтор тем же входом снова упрётся в него.
_BLOCKED_FINISH = {
    "SAFETY", "RECITATION", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "IMAGE_SAFETY",
}
_QUOTA_RE = re.compile(r"exceeded your current quota|quota exceeded", re.I)
_PER_DAY_RE = re.compile(r"per ?day", re.I)
_RETRY_IN_RE = re.compile(r"retry in (\d+(?:\.\d+)?)\s*s", re.I)
_SECONDS_RE = re.compile(r"\s*(\d+(?:\.\d+)?)\s*s?\s*")
_QUOTA_FALLBACK_SEC = 60.0  # квота не дневная и без срока в теле — поминутная, ждём минуту
_ERROR_BODY_MAX = 4000  # тело ошибки в JSONL (обычно 1–2 КБ: сообщение + details)

# «Заблокировано до» по модели (квоты Gemini — на проект и модель): {model: (epoch, дневная?)}.
_blocked: dict[str, tuple[float, bool]] = {}


def _seconds(raw: Any) -> float | None:
    """'33s' / '33.112s' (RetryInfo.retryDelay) или '33' (Retry-After) → секунды."""
    m = _SECONDS_RE.fullmatch(str(raw)) if raw is not None else None
    return float(m.group(1)) if m else None


def _error_info(status: int, headers: Mapping[str, str], text: str) -> dict[str, Any]:
    """Тело ошибки Gemini (google.rpc.Status) → поля для лога и решения: статус, сообщение,
    quotaId, срок повтора (RetryInfo.retryDelay → «retry in Ns» в тексте → Retry-After)."""
    try:
        body = json.loads(text)
    except ValueError:
        body = None
    if isinstance(body, list) and body:  # стрим отдаёт ошибку массивом [{"error": …}]
        body = body[0]
    err = body.get("error") if isinstance(body, dict) else None
    if not isinstance(err, dict):
        err = {"message": str(err)} if err else {}
    quota_ids: list[str] = []
    delay: float | None = None
    for d in err.get("details") or []:
        if not isinstance(d, dict):
            continue
        kind = str(d.get("@type") or "")
        if kind.endswith("QuotaFailure"):
            for v in d.get("violations") or []:
                if isinstance(v, dict) and (v.get("quotaId") or v.get("quotaMetric")):
                    quota_ids.append(str(v.get("quotaId") or v.get("quotaMetric")))
        elif kind.endswith("RetryInfo"):
            delay = _seconds(d.get("retryDelay"))
    msg = str(err.get("message") or "")
    if delay is None:
        m = _RETRY_IN_RE.search(msg)
        delay = float(m.group(1)) if m else _seconds(headers.get("retry-after"))
    return {
        "http_status": status,
        "error_status": str(err.get("status") or ""),
        "error_message": msg,
        "quota_ids": quota_ids,
        "retry_delay_s": delay,
        "error_body": text[:_ERROR_BODY_MAX],
    }


def _quota_reset() -> float:
    """Следующая полночь по Тихоокеанскому времени — тогда Google сбрасывает дневные квоты."""
    try:
        tz: Any = ZoneInfo("America/Los_Angeles")
    except Exception:  # noqa: BLE001 — нет tzdata: грубо, PST
        tz = timezone(timedelta(hours=-8))
    now = datetime.now(tz)
    return (now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)).timestamp()


def _when(until: float) -> str:
    left = until - time.time()
    if left < 90:
        return f"через {max(1, math.ceil(left))} с"
    if left < 3600:
        return f"через {math.ceil(left / 60)} мин"
    return "в " + datetime.fromtimestamp(until).strftime("%H:%M")


def _quota_text(until: float, daily: bool) -> str:
    """Текст для пользователя (роутер отдаёт его в 429, фронт показывает как есть)."""
    if daily:
        return (
            f"Дневная квота Gemini исчерпана — снова заработает {_when(until)}. "
            "Пока выберите другую модель."
        )
    return f"Квота запросов Gemini исчерпана — повторите {_when(until)} или выберите другую модель."


def _block(model: str, info: dict[str, Any]) -> LimitError:
    """Квотный 429: запоминаем «заблокировано до» для модели → LimitError с понятным текстом."""
    daily = bool(_PER_DAY_RE.search(" ".join(info["quota_ids"]) + " " + info["error_message"]))
    delay = info["retry_delay_s"]
    if daily:
        until = _quota_reset()  # retryDelay у дневной квоты бывает в секундах — ему не верим
    else:
        until = time.time() + max(1.0, delay if delay is not None else _QUOTA_FALLBACK_SEC)
    _blocked[model] = (until, daily)
    at = datetime.fromtimestamp(until).isoformat(timespec="seconds")
    logger.warning(
        "Gemini · %s: квота исчерпана (%s, retryDelay=%s) — не вызываем до %s",
        model, ", ".join(info["quota_ids"]) or "quotaId нет", delay, at,
    )
    return LimitError(_quota_text(until, daily), {**info, "blocked_until": at}, until=until)


def _api_error(model: str, status: int, headers: Mapping[str, str], text: str) -> AIError:
    """Не-200 от Gemini → ошибка нужного рода (решает, повторять ли и сколько ждать)."""
    info = _error_info(status, headers, text)
    msg = f"Gemini {status}"
    if info["error_status"]:
        msg += f" {info['error_status']}"
    msg += f": {info['error_message'] or text[:300]}"
    if info["quota_ids"]:
        msg += f" [quotaId: {', '.join(info['quota_ids'])}]"
    if status == 429 and (info["quota_ids"] or _QUOTA_RE.search(info["error_message"])):
        return _block(model, info)
    if status == 429 or status in _OVERLOAD_CODES:
        return AIOverloaded(msg, info, retry_after=info["retry_delay_s"])
    if 400 <= status < 500 and status != 408:
        return AINonRetryable(msg, info)  # ключ/модель/параметры — тот же вход снова упадёт
    return AIError(msg, info)


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


# Реальная модель за алиасом (gemini-flash-latest → gemini-…-flash): Gemini присылает её в
# modelVersion каждого ответа. Запоминаем последнюю — показываем в выпадашках моделей.
resolved_model: str = ""


def _remember_version(body: dict) -> None:
    global resolved_model
    if body.get("modelVersion"):
        resolved_model = str(body["modelVersion"])


def _norm_usage(meta: dict | None) -> dict[str, Any]:
    """usageMetadata Gemini → ключи как у OpenAI, чтобы observe считал метрики без правок."""
    u = meta or {}
    return {
        "prompt_tokens": u.get("promptTokenCount"),
        "completion_tokens": u.get("candidatesTokenCount"),
        "total_tokens": u.get("totalTokenCount"),
        # Неявный кэш Gemini: сколько токенов промпта отдано из кэша (входят в promptTokenCount).
        "prompt_cache_hit_tokens": u.get("cachedContentTokenCount"),
    }


def _extract_text(candidate: dict) -> str:
    parts = (candidate.get("content") or {}).get("parts") or []
    return "".join(p.get("text", "") for p in parts if isinstance(p, dict))


def _gen_config(temperature: float, max_tokens: int, model: str = "") -> dict[str, Any]:
    """generationConfig для JSON-задач.

    thinkingBudget=0 отключает «размышление» Gemini 3.x: рецептам оно не нужно, а иначе
    reasoning съедает бюджет и обрывает JSON. Заодно быстрее (меньше таймаутов)."""
    return {
        "temperature": temperature,
        "maxOutputTokens": max_tokens,
        "responseMimeType": "application/json",  # схема — в промпте
        # flash-lite (3.5) отвергает thinkingConfig (400 INVALID_ARGUMENT) — ему не шлём.
        **({} if "lite" in model else {"thinkingConfig": {"thinkingBudget": 0}}),
    }


class GeminiGate(ModelGate):
    """Gemini (Google AI Studio) через REST: план и деталь рецепта, стриминг.

    Правки плана идут через structured-actions (см. planner), поэтому tools здесь не нужны.
    """

    key = "gemini"
    provider = "Gemini"
    # Стриминг JSON у Gemini ненадёжен (обрывается после reply, до dishes не доходит) —
    # план собираем не-стримом через complete_json. stream_json оставлен для будущего.
    supports_stream = False
    supports_tools = False
    # «high demand» (503) длится секунды: повтор через 2–4 с, затем 4–6 с (сумма ≤ 10 с).
    overload_backoff = ((2.0, 4.0), (4.0, 6.0))

    @property
    def configured(self) -> bool:
        return settings.gemini_configured

    @property
    def default_model(self) -> str:
        return self._model_override or settings.gemini_model

    def _check_available(self, model: str, label: str = "") -> None:
        until, daily = _blocked.get(model, (0.0, False))
        if time.time() < until:
            logger.info("Gemini · %s: квота исчерпана — не вызываем (%s)", model, label or "?")
            raise LimitError(_quota_text(until, daily), until=until)

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
            "generationConfig": _gen_config(temperature, max_tokens, model),
        }
        if system:
            payload["systemInstruction"] = system
        headers = {"x-goog-api-key": settings.gemini_api_key}

        async with httpx.AsyncClient(timeout=90.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
        if resp.status_code != 200:
            raise _api_error(model, resp.status_code, resp.headers, resp.text)
        body = resp.json()
        _remember_version(body)
        usage = _norm_usage(body.get("usageMetadata"))
        candidates = body.get("candidates") or []
        if not candidates:
            blocked = (body.get("promptFeedback") or {}).get("blockReason")
            if blocked:  # запрос отклонён фильтром — повтор тем же входом бесполезен
                raise AINonRetryable(
                    f"Gemini отклонил запрос фильтром ({blocked})",
                    {"stop_reason": f"prompt:{blocked}", "usage": usage},
                )
            raise AIError(
                f"Пустой ответ Gemini: {str(body)[:300]}",
                {"error_body": json.dumps(body, ensure_ascii=False)[:_ERROR_BODY_MAX]},
            )
        cand = candidates[0]
        finish = str(cand.get("finishReason") or "")
        content = _extract_text(cand)
        if finish in _BLOCKED_FINISH:
            raise AINonRetryable(
                f"Gemini остановил ответ фильтром ({finish})",
                {**parse_details(content, finish), "usage": usage},
            )
        try:
            return loads_lenient(content), usage
        except json.JSONDecodeError as exc:
            details = {**parse_details(content, finish, exc), "usage": usage}
            logger.warning(
                "Gemini: не JSON (finishReason=%s, %d симв.): %s", finish or "?", len(content), exc
            )
            if finish == "MAX_TOKENS":  # обрезан по лимиту — повтор тем же входом снова обрежется
                raise AINonRetryable(
                    f"Gemini: ответ обрезан по лимиту токенов (MAX_TOKENS): {exc}", details
                ) from exc
            raise AIError(f"Gemini: не JSON ({finish or '?'}): {exc}", details) from exc

    async def stream_json(
        self,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int = 3000,
        model: str | None = None,
        label: str = "",
        temperature: float | None = None,
    ) -> AsyncIterator[str]:
        """Стрим Gemini (SSE): отдаёт дельты текста по мере генерации."""
        if not self.configured:
            raise AIError("Gemini не настроен: нет GEMINI_API_KEY")

        model = model or self.default_model
        self._check_available(model, label)
        logger.info("AI → Gemini · %s · %s (stream)", model, label or "?")
        t0 = time.monotonic()
        contents, system = _to_contents(messages)
        url = f"{settings.gemini_base_url}/models/{model}:streamGenerateContent?alt=sse"
        payload: dict[str, Any] = {
            "contents": contents,
            "generationConfig": _gen_config(0.7 if temperature is None else temperature, max_tokens, model),
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
                    raise _api_error(
                        model, resp.status_code, resp.headers, body.decode("utf-8", "replace")
                    )
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    try:
                        obj = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    _remember_version(obj)
                    if obj.get("usageMetadata"):
                        usage = _norm_usage(obj["usageMetadata"])
                    for cand in obj.get("candidates") or []:
                        delta = _extract_text(cand)
                        if delta:
                            full.append(delta)
                            yield delta

        log_ai_call(
            "Gemini", model, label, messages, "".join(full), usage,
            int((time.monotonic() - t0) * 1000),
        )
