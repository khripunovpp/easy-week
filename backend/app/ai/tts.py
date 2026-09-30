"""Озвучка текста шага (TTS): OpenRouter `POST /audio/speech`, бесплатная Fish Audio S2.1
(`OPENROUTER_TTS_MODEL`; русский, отдаёт mp3). Ключ — тот же OPENROUTER_API_KEY.

В настройки не выведено: провайдер один. Вызовы логируются через `observe.log_ai_call`
(метка «озвучка шага»), как остальные AI-вызовы; сбой → `AIError` → роутер отдаёт 502.
Кэш аудио — в роутере (`routers/tts.py`).

Лимиты: у бесплатных моделей OpenRouter дневной лимит на аккаунт (50 запросов в сутки без
купленных кредитов, общий для ВСЕХ :free-моделей — озвучка, покупки, предпочтения) и
перегрузка общего пула провайдера. На 429 кидаем `TtsLimitError` с понятным текстом и
запоминаем «заблокировано до» (дневной лимит — до X-RateLimit-Reset, перегрузка — на 20 с):
до этого времени в OpenRouter не ходим вовсе, роутер сразу отдаёт 429.
"""

import logging
import time
from datetime import datetime, timedelta
from typing import Any

import httpx

from ..config import settings
from . import limits
from .base import AIError
from .observe import log_ai_call, log_ai_error

logger = logging.getLogger("easy_week.tts")

PROVIDER = "OpenRouter"
LABEL = "озвучка шага"
_BUSY_BLOCK_SEC = 20  # перегрузка общего пула провайдера — короткая пауза

# «Заблокировано до» (epoch, сек) и текст причины — после 429 не долбим OpenRouter.
blocked_until: float = 0.0
blocked_reason: str = ""


class TtsLimitError(AIError):
    """Лимит озвучки (дневной лимит бесплатных моделей или перегрузка) — роутер отдаёт 429."""

    def __init__(self, msg: str, until: float) -> None:
        super().__init__(msg)
        self.until = until


def _next_midnight() -> float:
    now = datetime.now()
    return (now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)).timestamp()


def _own_limit_reason() -> str:
    n = settings.tts_daily_limit
    return (
        f"Лимит озвучки на сегодня исчерпан: {n} новых шагов в день. Уже озвученные шаги "
        "играют как обычно, новые — после полуночи."
    )


def limit_status() -> tuple[bool, str, float]:
    """(доступна ли НОВАЯ генерация, причина, до какого момента заблокирована — epoch сек).
    Уже закэшированные шаги играют и при исчерпанном лимите."""
    if time.time() < blocked_until:
        return False, blocked_reason, blocked_until
    st = limits.tts_status()
    if st["limit"] > 0 and st["remaining"] == 0:
        return False, _own_limit_reason(), _next_midnight()
    return True, "", 0.0


def _block(reason: str, until: float) -> TtsLimitError:
    global blocked_until, blocked_reason
    blocked_until, blocked_reason = until, reason
    return TtsLimitError(reason, until)


def _on_429(resp: httpx.Response) -> TtsLimitError:
    """Разбор 429 OpenRouter: дневной лимит бесплатных моделей или перегрузка пула."""
    try:
        body = resp.json()
        err = (body.get("error") if isinstance(body, dict) else None) or {}
    except Exception:  # noqa: BLE001 — тело не JSON (текст/HTML прокси) → считаем перегрузкой
        err = {}
    if not isinstance(err, dict):
        err = {"message": str(err)}
    meta = err.get("metadata") or {}
    source = str(meta.get("limit_source") or "")
    msg = str(err.get("message") or "")
    reset_raw = (meta.get("headers") or {}).get("X-RateLimit-Reset") or resp.headers.get(
        "x-ratelimit-reset"
    )
    if "daily" in source or "per-day" in msg:
        try:
            until = int(reset_raw) / 1000
        except (TypeError, ValueError):
            until = time.time() + 3600
        at = datetime.fromtimestamp(until).strftime("%H:%M")
        return _block(
            "Бесплатная озвучка на сегодня закончилась: лимит OpenRouter — 50 запросов в сутки "
            f"на все бесплатные модели. Снова заработает в {at}.",
            until,
        )
    return _block(
        "Сервис озвучки сейчас перегружен — попробуйте через минуту.",
        time.time() + _BUSY_BLOCK_SEC,
    )


def voice_id() -> str:
    """«Модель · голос» — подпись в логах и часть ключа кэша."""
    v = settings.openrouter_tts_voice
    return settings.openrouter_tts_model + (f" · {v}" if v else "")


async def _request(text: str) -> bytes:
    if not settings.openrouter_configured:
        raise AIError("OpenRouter не настроен: нет OPENROUTER_API_KEY")
    payload: dict[str, Any] = {
        "model": settings.openrouter_tts_model, "input": text, "response_format": "mp3",
    }
    if settings.openrouter_tts_voice:
        payload["voice"] = settings.openrouter_tts_voice
    headers = {
        "Authorization": f"Bearer {settings.openrouter_api_key}",
        "HTTP-Referer": "https://github.com/pashtitto/easy-week", "X-Title": "Easy Week",
    }
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            f"{settings.openrouter_base_url}/audio/speech", json=payload, headers=headers
        )
    ctype = resp.headers.get("content-type", "")
    if resp.status_code == 429:
        raise _on_429(resp)
    if resp.status_code != 200 or "json" in ctype:
        raise AIError(f"OpenRouter TTS {resp.status_code}: {resp.text[:300]}")
    if not resp.content:
        raise AIError("OpenRouter TTS: пустое аудио")
    return resp.content


async def synthesize(text: str) -> bytes:
    """Озвучить текст → mp3. Без фолбэков: сбой → AIError, лимит → TtsLimitError."""
    ok, reason, until = limit_status()
    if not ok:
        raise TtsLimitError(reason, until)  # ещё заблокировано — в OpenRouter не ходим
    if not limits.tts_reserve():  # свой дневной лимит (гонка с параллельным прогревом)
        raise TtsLimitError(_own_limit_reason(), _next_midnight())
    try:
        return await _synthesize(text)
    except BaseException:
        limits.tts_refund()  # не удалось — лимит не тратим
        raise


async def _synthesize(text: str) -> bytes:
    model = voice_id()
    messages = [{"role": "user", "content": text}]
    logger.info("AI → %s · %s · %s", PROVIDER, model, LABEL)
    t0 = time.monotonic()
    try:
        audio = await _request(text)
    except TtsLimitError as exc:
        log_ai_error(PROVIDER, model, LABEL, messages, str(exc), 1, int((time.monotonic() - t0) * 1000))
        raise
    except (AIError, httpx.HTTPError) as exc:
        log_ai_error(PROVIDER, model, LABEL, messages, str(exc), 1, int((time.monotonic() - t0) * 1000))
        raise AIError(str(exc)) from exc
    log_ai_call(
        PROVIDER, model, LABEL, messages, f"<audio/mpeg, {len(audio)} байт>", {},
        int((time.monotonic() - t0) * 1000),
    )
    return audio
