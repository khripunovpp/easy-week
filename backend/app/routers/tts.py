"""Озвучка шага: GET /api/tts?text=… → mp3 (OpenRouter Fish Audio, см. ai/tts.py);
POST /api/tts/warm {texts} → фоновая догенерация остальных шагов рецепта/плана готовки.

Почему GET с текстом в query: кнопка 🔊 на фронте ставит `audio.src` и зовёт `play()` прямо в
обработчике тапа — iOS разрешает воспроизведение только из жеста пользователя, а асинхронный
POST → play() блокирует. Кука сессии уезжает сама (same-origin), так что /api/tts за паролем.

Кэш: `data/tts/<sha1(модель·голос|текст)>.mp3` — один и тот же шаг озвучиваем один раз;
параллельные запросы одного шага склеиваются (lock по ключу). Кэш не бэкапим (восстановим).

Прогрев: при первом тапе в рецепте фронт шлёт остальные шаги в /warm (по порядку от нажатого,
по кругу) — бэк генерит их фоном по WARM_PARALLEL штук (бесплатная модель, не душим её),
и следующие тапы играют мгновенно. Уже закэшированное пропускается; шаг, который как раз
греется, обычный GET просто дожидается через тот же lock.
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
from pydantic import BaseModel, Field

from ..ai import tts as tts_ai
from ..ai.base import AIError
from ..ai.observe import set_ai_context
from ..config import settings

router = APIRouter(prefix="/api/tts", tags=["tts"])
logger = logging.getLogger("easy_week.tts")

_locks: dict[str, asyncio.Lock] = {}
_CACHE_HEADERS = {"Cache-Control": "private, max-age=2592000"}  # URL детерминирован по тексту

WARM_MAX_TEXTS = 40  # рецепт/план готовки длиннее не бывает
WARM_PARALLEL = 2  # одновременных синтезов в прогреве (free-tier: ~20 запр./мин)
_warm_tasks: set[asyncio.Task] = set()


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
    except AIError as exc:
        raise HTTPException(status_code=502, detail=f"Не удалось озвучить: {exc}") from exc
    return FileResponse(f, media_type="audio/mpeg", headers=_CACHE_HEADERS)


class WarmBody(BaseModel):
    # Тексты шагов в порядке прогрева (фронт ставит первыми те, что после нажатого).
    texts: list[Annotated[str, Field(max_length=settings.tts_max_chars)]] = Field(
        max_length=WARM_MAX_TEXTS
    )


class WarmOut(BaseModel):
    queued: int  # поставлено в фоновую генерацию
    cached: int  # уже было в кэше (или греется)


async def _warm(texts: list[str]) -> None:
    """Фон: синтез по WARM_PARALLEL штук, порядок — как прислали. Сбой одного шага не мешает
    остальным (следующий тап по нему просто повторит попытку обычным GET)."""
    sem = asyncio.Semaphore(WARM_PARALLEL)

    async def one(t: str) -> None:
        async with sem:
            try:
                await ensure_audio(t)
            except AIError as exc:
                logger.warning("tts warm skipped «%s…»: %s", t[:30], str(exc)[:150])

    await asyncio.gather(*(one(t) for t in texts))
    logger.info("tts warm done: %d шагов", len(texts))


@router.post("/warm")
async def warm(body: WarmBody) -> WarmOut:
    set_ai_context(endpoint="tts_warm")
    seen: set[str] = set()
    todo: list[str] = []
    cached = 0
    for raw in body.texts:
        t = clean_text(raw)
        h = cache_key(t) if t else ""
        if not t or h in seen:
            continue
        seen.add(h)
        if _cached(h) is not None or h in _locks:
            cached += 1  # уже есть или прямо сейчас греется/играет
        else:
            todo.append(t)
    if todo:
        task = asyncio.create_task(_warm(todo))
        _warm_tasks.add(task)
        task.add_done_callback(_warm_tasks.discard)
    return WarmOut(queued=len(todo), cached=cached)
