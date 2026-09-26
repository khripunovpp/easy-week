"""Обсуждение цели плана в чате («💬 Обсудить в чате»): рецепт, план готовки, список покупок.

POST /api/chat/discuss — реплика пользователя в режиме «Обсуждение: …». Сохраняем user- и
assistant-сообщения с пометкой discuss_target (+ dish_id), версий плана НЕ создаём.
Модель отвечает кратко (markdown) и ничего не меняет без явной просьбы; явная просьба →
правка рецепта (перегенерация детали с `change` + обсуждением, варианты на месте) или
пересборка плана готовки/списка покупок; «замени блюдо» → suggest_replace (фронт покажет
кнопку, переключающую бейдж в режим замены). Без фолбэков: AIError → 502, лимит → 429.
Предпочтения из реплик обсуждения не извлекаем (это вопросы про конкретную цель).
"""

import logging
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session

from ..ai.base import AIError
from ..ai.gates import gate_for
from ..ai.limits import LimitError
from ..ai.observe import set_ai_context
from ..ai.planner import discuss_reply
from ..ai.prompt import (
    discuss_cooking_context,
    discuss_recipe_context,
    discuss_shopping_context,
)
from ..db import get_session
from ..models import Conversation, MessageRow, PlanRow
from ..schemas import DiscussRequest, DiscussResponse
from ..services.discussion import TARGETS, discuss_turns
from ..services.history import original_request
from ..services.mapping import to_cook_plan, to_dish
from ..services.regenerate import (
    regenerate_cooking,
    regenerate_dish,
    regenerate_shopping,
    shopping_base,
)
from ..services.shopping import group_items

router = APIRouter(prefix="/api", tags=["chat"])

logger = logging.getLogger("easy_week.discuss")

SessionDep = Annotated[Session, Depends(get_session)]

# Хвост реплики после применённого действия (модель сама не утверждает, что изменила).
_APPLIED = {
    "recipe": "✅ Рецепт обновлён — откройте его, чтобы посмотреть.",
    "cooking": "✅ План готовки пересобран.",
    "shopping": "✅ Список покупок пересобран.",
}


def _context(session: Session, row: PlanRow, target: str, dish_id: str | None) -> str:
    """Полный контекст цели для промпта: содержимое + другие блюда + исходный запрос."""
    dishes = list(row.dishes or [])
    names = [str(d.get("name", "")) for d in dishes if d.get("name")]
    request = original_request(session, row.conversation_id)
    if target == "recipe":
        dish = next((d for d in dishes if d.get("id") == dish_id), None)
        if dish is None:
            raise HTTPException(status_code=404, detail="Блюдо не найдено")
        others = [n for n in names if n != dish.get("name")]
        return discuss_recipe_context(dish, others, request)
    if target == "cooking":
        return discuss_cooking_context(to_cook_plan(row).model_dump(), names, request)
    # shopping: нормализованный кэш, если он актуален, иначе детерминированная база.
    base, sig = shopping_base(row)
    items = row.shopping_cache if (row.shopping_sig == sig and row.shopping_cache) else base
    return discuss_shopping_context(list(items), names, request)


@router.post("/chat/discuss")
async def chat_discuss(req: DiscussRequest, session: SessionDep) -> DiscussResponse:
    target = (req.target or "").lower()
    if target not in TARGETS:
        raise HTTPException(status_code=422, detail="Неизвестная цель обсуждения")
    if target == "recipe" and not req.dish_id:
        raise HTTPException(status_code=422, detail="Не указано блюдо")
    if not req.message.strip():
        raise HTTPException(status_code=422, detail="Пустое сообщение")
    row = session.get(PlanRow, req.plan_id)
    if row is None:
        raise HTTPException(status_code=404, detail="План не найден")
    conv_id = req.conversation_id or row.conversation_id
    if session.get(Conversation, conv_id) is None:
        raise HTTPException(status_code=404, detail="Диалог не найден")
    dish_id = req.dish_id if target == "recipe" else None
    set_ai_context(
        conversation_id=conv_id, plan_id=row.id, dish_id=dish_id,
        endpoint="chat_discuss", action=target,
    )

    context = _context(session, row, target, dish_id)
    # Прошлые реплики обсуждения этой цели — до сохранения текущей (она идёт вопросом).
    turns = discuss_turns(session, conv_id, target, dish_id)
    session.add(MessageRow(
        id=uuid4().hex, conversation_id=conv_id, role="user", text=req.message.strip(),
        discuss_target=target, dish_id=dish_id,
    ))
    session.commit()

    try:
        res = await discuss_reply(
            target, context, turns, req.message, req.gender, req.recipe_model
        )
    except LimitError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except AIError as exc:
        raise HTTPException(status_code=502, detail=f"Обсуждение недоступно: {exc}") from exc

    op = res["op"]
    out = DiscussResponse(
        conversation_id=conv_id, reply="", model=req.recipe_model, target=target,
        plan_id=row.id, dish_id=dish_id,
    )
    reply = res["reply"]
    model = gate_for(req.recipe_model).key
    # Явная просьба изменить → применяем. Сбой применения не роняет ответ: реплика уже есть,
    # ошибку показываем в ней же (и полем apply_error).
    try:
        if op == "edit":
            new = await regenerate_dish(
                session, row, dish_id or "", model, change=res["change"], regenerate=False
            )
            out.dish = to_dish(new)
        elif op == "regenerate" and target == "cooking":
            await regenerate_cooking(session, row, model)
            out.cooking = to_cook_plan(row)
        elif op == "regenerate" and target == "shopping":
            out.shopping = group_items(await regenerate_shopping(session, row, model))
        elif op == "replace":
            out.suggest_replace = True
            out.replace_query = res["query"]
        if op in ("edit", "regenerate"):
            reply = (reply + "\n\n" if reply else "") + _APPLIED[target]
    except (LimitError, AIError) as exc:
        logger.warning("discuss apply %s failed: %s", op, str(exc)[:150])
        out.apply_error = str(exc)
        reply = (reply + "\n\n" if reply else "") + f"⚠️ Не удалось применить изменение: {exc}"
        op = "none"
    if not reply:
        reply = "Готово." if op != "none" else "Не понял вопрос — уточните, пожалуйста."
    out.op = op
    out.reply = reply

    msg_id = uuid4().hex
    session.add(MessageRow(
        id=msg_id, conversation_id=conv_id, role="assistant", text=reply,
        model=req.recipe_model, discuss_target=target, dish_id=dish_id,
    ))
    session.commit()
    out.message_id = msg_id
    logger.info("discuss: target=%s op=%s provider=%s", target, op, res.get("provider"))
    return out
