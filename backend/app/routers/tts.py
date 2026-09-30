"""Озвучка шага: GET /api/tts?text=… → mp3 (OpenRouter Fish Audio, см. ai/tts.py).

Почему GET с текстом в query: кнопка 🔊 на фронте ставит `audio.src` и зовёт `play()` прямо в
обработчике тапа — iOS разрешает воспроизведение только из жеста пользователя, а асинхронный
POST → play() блокирует. Кука сессии уезжает сама (same-origin), так что /api/tts за паролем.

Кэш: `data/tts/<sha1(модель·голос|текст)>.mp3` — один и тот же шаг озвучиваем один раз;
параллельные запросы одного шага склеиваются (lock по ключу). Кэш не бэкапим (восстановим).
"""

import asyncio
import hashlib
import logging
import os
import tempfile
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
_CACHE_HEADERS = {"Cache-Control": "private, max-age=2592000"}  # URL детерминирован по тексту


def cache_dir() -> Path:
    return Path(settings.db_path).parent / "tts"


def cache_key(text: str) -> str:
    return hashlib.sha1(f"{tts_ai.voice_id()}|{text}".encode("utf-8")).hexdigest()


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


def _file_response(f: Path) -> FileResponse:
    return FileResponse(f, media_type="audio/mpeg", headers=_CACHE_HEADERS)


@router.get("")
async def speak(
    text: Annotated[str, Query(min_length=1, max_length=settings.tts_max_chars)],
) -> FileResponse:
    clean = " ".join(text.split())
    if not clean:
        raise HTTPException(status_code=422, detail="Пустой текст")
    h = cache_key(clean)
    set_ai_context(endpoint="tts")

    if (f := _cached(h)) is not None:
        return _file_response(f)

    lock = _locks.setdefault(h, asyncio.Lock())
    try:
        async with lock:
            if (f := _cached(h)) is not None:  # пока ждали lock, сосед уже озвучил
                return _file_response(f)
            try:
                audio = await tts_ai.synthesize(clean)
            except AIError as exc:
                raise HTTPException(status_code=502, detail=f"Не удалось озвучить: {exc}") from exc
            f = cache_dir() / f"{h}.mp3"
            _write_atomic(f, audio)
            logger.info("tts cached: bytes=%d chars=%d", len(audio), len(clean))
    finally:
        _locks.pop(h, None)
    return _file_response(f)
