"""Озвучка текста шага (TTS): OpenRouter `POST /audio/speech`, бесплатная Fish Audio S2.1
(`OPENROUTER_TTS_MODEL`; русский, отдаёт mp3). Ключ — тот же OPENROUTER_API_KEY.

В настройки не выведено: провайдер один. Вызовы логируются через `observe.log_ai_call`
(метка «озвучка шага»), как остальные AI-вызовы; сбой → `AIError` → роутер отдаёт 502.
Кэш аудио — в роутере (`routers/tts.py`).
"""

import logging
import time
from typing import Any

import httpx

from ..config import settings
from .base import AIError
from .observe import log_ai_call, log_ai_error

logger = logging.getLogger("easy_week.tts")

PROVIDER = "OpenRouter"
LABEL = "озвучка шага"


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
    if resp.status_code != 200 or "json" in ctype:
        raise AIError(f"OpenRouter TTS {resp.status_code}: {resp.text[:300]}")
    if not resp.content:
        raise AIError("OpenRouter TTS: пустое аудио")
    return resp.content


async def synthesize(text: str) -> bytes:
    """Озвучить текст → mp3. Без фолбэков: сбой → AIError."""
    model = voice_id()
    messages = [{"role": "user", "content": text}]
    logger.info("AI → %s · %s · %s", PROVIDER, model, LABEL)
    t0 = time.monotonic()
    try:
        audio = await _request(text)
    except (AIError, httpx.HTTPError) as exc:
        log_ai_error(PROVIDER, model, LABEL, messages, str(exc), 1, int((time.monotonic() - t0) * 1000))
        raise AIError(str(exc)) from exc
    log_ai_call(
        PROVIDER, model, LABEL, messages, f"<audio/mpeg, {len(audio)} байт>", {},
        int((time.monotonic() - t0) * 1000),
    )
    return audio
