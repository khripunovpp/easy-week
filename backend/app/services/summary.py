"""Сводка беседы чата: одна, последняя — или ничего на старте.

После каждой реплики пользователя (новый план, правка, обсуждение) роутер зовёт `schedule()`:
через DEBOUNCE_SEC тишины фоном собираем обновлённую сводку дешёвой моделью задачи `summary`
(настройки; по умолчанию Cloudflare). Пользователь написал ещё раз раньше, чем прошло 5 с, —
таймер перезапускается (дебаунс), модель зовём один раз на серию реплик. Если сводка уже
считается, новый таймер дождётся её (lock на беседу) — две сводки одной беседы не пишутся
параллельно.

Сводка инкрементальная: прошлая сводка + реплики после `summary_upto` → новая сводка
(перезаписывает поле `Conversation.summary`). Пока в беседе одна реплика пользователя, сводку
не делаем: первое сообщение и так всегда идёт в контекст (`memory()`), а сводка из одной
реплики его бы просто повторила.

`memory()` — блок для промптов: первое сообщение пользователя + сводка (если есть).
Процесс один (uvicorn без воркеров), поэтому таймеры — в памяти; рестарт их теряет — не беда,
следующая реплика запустит сводку заново и захватит всё пропущенное.
"""

import asyncio
import logging
from datetime import datetime, timezone

from sqlmodel import Session, select

from ..ai.base import AIError
from ..ai.gates import cloudflare, gate_for
from ..ai.observe import set_ai_context
from ..ai.prompt import (
    SUMMARY_MAX_CHARS,
    SUMMARY_SCHEMA,
    _clip,
    build_summary_messages,
    chat_memory_block,
)
from ..config import settings
from ..db import engine
from ..models import Conversation, MessageRow, PlanRow
from .history import original_request

logger = logging.getLogger("easy_week.summary")

DEBOUNCE_SEC = 5.0
MAX_NEW_MESSAGES = 30  # сколько новых реплик максимум отдаём в одно обновление
MSG_CLIP = 500  # длина одной реплики в промпте сводки

_timers: dict[str, asyncio.Task] = {}
_locks: dict[str, asyncio.Lock] = {}
_TARGET_RU = {"recipe": "обсуждение рецепта", "cooking": "обсуждение плана готовки",
              "shopping": "обсуждение списка покупок"}


def schedule(conversation_id: str) -> None:
    """Перезапустить таймер сводки беседы (дебаунс). Зовётся после сохранения реплики."""
    if not conversation_id:
        return
    old = _timers.get(conversation_id)
    if old is not None and not old.done():
        old.cancel()  # ещё спит — отменяем; уже считает — таймер снят раньше, не трогаем
    _timers[conversation_id] = asyncio.create_task(_debounced(conversation_id))


async def _debounced(conversation_id: str) -> None:
    try:
        await asyncio.sleep(DEBOUNCE_SEC)
    except asyncio.CancelledError:
        return
    if _timers.get(conversation_id) is asyncio.current_task():
        _timers.pop(conversation_id, None)  # дальше считаем — новые реплики поставят новый таймер
    lock = _locks.setdefault(conversation_id, asyncio.Lock())
    async with lock:
        try:
            await summarize(conversation_id)
        except Exception as exc:  # noqa: BLE001 — фоновая задача: лог и дальше
            logger.warning("summary failed for %s: %s", conversation_id, str(exc)[:150])


def _line(m: MessageRow, plan_names: dict[str, list[str]]) -> str:
    who = "Пользователь" if m.role == "user" else "Ассистент"
    text = _clip(m.text or "", MSG_CLIP)
    tag = f" ({_TARGET_RU[m.discuss_target]})" if m.discuss_target in _TARGET_RU else ""
    names = plan_names.get(m.plan_id or "")
    if m.role == "assistant" and names:
        text = (text + " " if text else "") + f"[план: {', '.join(names[:8])}]"
    return f"{who}{tag}: {text}" if text else ""


async def summarize(conversation_id: str, session: Session | None = None) -> str | None:
    """Обновить сводку беседы. Возвращает новую сводку или None (нечего/рано обновлять).
    AIError пробрасывается (фоновая обёртка логирует)."""
    if session is None:
        with Session(engine) as s:
            return await summarize(conversation_id, s)

    conv = session.get(Conversation, conversation_id)
    if conv is None:
        return None
    msgs = session.exec(
        select(MessageRow)
        .where(MessageRow.conversation_id == conversation_id)
        .order_by(MessageRow.created_at)
    ).all()
    user_msgs = [m for m in msgs if m.role == "user" and (m.text or "").strip()]
    if len(user_msgs) < 2:
        return None  # старт беседы: первое сообщение и так в контексте, сводка не нужна

    ids = [m.id for m in msgs]
    start = ids.index(conv.summary_upto) + 1 if conv.summary_upto in ids else 0
    new = msgs[start:][-MAX_NEW_MESSAGES:]
    if not any(m.role == "user" for m in new):
        return None  # с прошлой сводки пользователь ничего не писал

    plan_ids = {m.plan_id for m in new if m.plan_id}
    plan_names: dict[str, list[str]] = {}
    for pid in plan_ids:
        row = session.get(PlanRow, pid)
        if row is not None:
            plan_names[pid] = [str(d.get("name")) for d in (row.dishes or []) if d.get("name")]
    lines = [ln for m in new if (ln := _line(m, plan_names))]
    if not lines:
        return None

    set_ai_context(conversation_id=conversation_id, endpoint="chat_summary")
    gate = gate_for("", "summary")
    cf_kw = {"schema": SUMMARY_SCHEMA, "model": settings.cf_model_judge} if gate is cloudflare else {}
    parsed, _ = await gate.complete_json(
        build_summary_messages(conv.summary or "", lines),
        **cf_kw,
        max_tokens=450,
        temperature=0.2,
        label="сводка беседы",
    )
    text = str(parsed.get("summary") or "").strip()
    if not text:
        raise AIError(f"{gate.provider} вернул пустую сводку")
    text = text[: SUMMARY_MAX_CHARS + 200]

    # Перечитываем беседу: пока модель думала, могли прийти новые реплики — upto ставим по
    # последнему сообщению, которое реально вошло в сводку (новые попадут в следующую).
    conv = session.get(Conversation, conversation_id)
    if conv is None:
        return None
    conv.summary = text
    conv.summary_upto = new[-1].id
    conv.summary_at = datetime.now(timezone.utc)
    session.add(conv)
    session.commit()
    logger.info("summary updated: conv=%s msgs=%d chars=%d via %s",
                conversation_id, len(lines), len(text), gate.provider)
    return text


def memory(session: Session, conversation_id: str | None, current_text: str = "") -> str:
    """Память беседы для промптов: первое сообщение пользователя + последняя сводка.
    current_text — текущая реплика: если это и есть первое сообщение (старт чата), его не
    дублируем — оно уже идёт как сам запрос."""
    if not conversation_id:
        return ""
    conv = session.get(Conversation, conversation_id)
    first = original_request(session, conversation_id)
    if first and first == (current_text or "").strip():
        first = ""
    return chat_memory_block(first, (conv.summary or "") if conv else "")
