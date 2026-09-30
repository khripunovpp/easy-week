"""Озвучка шага: GET /api/tts?text=… → mp3 (OpenRouter Fish Audio, см. ai/tts.py);
GET /api/tts/status → доступна ли озвучка (лимит бесплатных моделей) — фронт показывает причину,
т.к. <audio> текст ошибки не видит. Лимит → 429 + Retry-After, прочий сбой → 502.

Почему GET с текстом в query: кнопка 🔊 на фронте ставит `audio.src` и зовёт `play()` прямо в
обработчике тапа — iOS разрешает воспроизведение только из жеста пользователя, а асинхронный
POST → play() блокирует. Кука сессии уезжает сама (same-origin), так что /api/tts за паролем.

Кэш: `data/tts/<sha1(модель·голос|текст)>.mp3` — один и тот же шаг озвучиваем один раз;
параллельные запросы одного шага склеиваются (lock по ключу). Кэш не бэкапим (восстановим).
Прогрев остальных шагов рецепта делает фронт (shared/tts-player: тянет их этим же GET по два
параллельно и держит аудио в памяти) — так он точно знает, какой шаг уже готов, и подсвечивает
кнопки; тап по шагу, который как раз генерится, дожидается его через тот же lock.
"""

import asyncio
import hashlib
import logging
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from ..ai import tts as tts_ai
from ..ai.base import AIError
from ..ai.observe import set_ai_context
from ..config import settings

router = APIRouter(prefix="/api/tts", tags=["tts"])
logger = logging.getLogger("easy_week.tts")

_locks: dict[str, asyncio.Lock] = {}
# Ревалидация по ETag, а не max-age: URL — только текст, а голос/модель меняются в .env —
# со сменой голоса другой файл → другой ETag → браузер сразу получит новое аудио (иначе 304).
_CACHE_HEADERS = {"Cache-Control": "private, no-cache"}


def cache_dir() -> Path:
    return Path(settings.db_path).parent / "tts"


def cache_key(text: str) -> str:
    return hashlib.sha1(f"{tts_ai.voice_id()}|{text}".encode("utf-8")).hexdigest()


def clean_text(text: str) -> str:
    return " ".join((text or "").split())


def _cached(h: str) -> Path | None:
    f = cache_dir() / f"{h}.mp3"
    return f if f.exists() and f.stat().st_size > 0 else None


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tts-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


async def ensure_audio(clean: str) -> Path:
    """Путь к mp3 шага: из кэша или синтез (single-flight по ключу). AIError пробрасываем."""
    h = cache_key(clean)
    if (f := _cached(h)) is not None:
        return f
    lock = _locks.setdefault(h, asyncio.Lock())
    try:
        async with lock:
            if (f := _cached(h)) is not None:  # пока ждали lock, сосед уже озвучил
                return f
            audio = await tts_ai.synthesize(clean)
            f = cache_dir() / f"{h}.mp3"
            _write_atomic(f, audio)
            logger.info("tts cached: bytes=%d chars=%d", len(audio), len(clean))
            return f
    finally:
        _locks.pop(h, None)


@router.get("")
async def speak(
    text: Annotated[str, Query(min_length=1, max_length=settings.tts_max_chars)],
) -> FileResponse:
    clean = clean_text(text)
    if not clean:
        raise HTTPException(status_code=422, detail="Пустой текст")
    set_ai_context(endpoint="tts")
    try:
        f = await ensure_audio(clean)
    except tts_ai.TtsLimitError as exc:
        retry = max(1, int(exc.until - time.time()))
        raise HTTPException(
            status_code=429, detail=str(exc), headers={"Retry-After": str(retry)}
        ) from exc
    except AIError as exc:
        raise HTTPException(status_code=502, detail=f"Не удалось озвучить: {exc}") from exc
    return FileResponse(f, media_type="audio/mpeg", headers=_CACHE_HEADERS)


@router.get("/status")
async def status() -> dict:
    """{available, detail, resetAt} — лимит бесплатной озвучки (без обращения к OpenRouter)."""
    ok, reason, until = tts_ai.limit_status()
    reset = datetime.fromtimestamp(until, tz=timezone.utc).isoformat() if until else None
    return {"available": ok, "detail": reason, "resetAt": reset}
