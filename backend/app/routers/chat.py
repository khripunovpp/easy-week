from collections.abc import AsyncIterable
from datetime import datetime, timezone
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from fastapi.sse import EventSourceResponse, ServerSentEvent
from sqlmodel import Session, select

from ..ai import prefs
from ..ai.base import AIError
from ..ai.limits import LimitError, status as limits_status
from ..ai.observe import record_conversation, record_plan, set_ai_context
from ..ai.planner import (
    _week_label,
    add_dish_direct,
    edit_plan,
    generate_plan,
    generate_plan_stream,
    remove_dish_by_id,
    replace_dish_by_id,
)
from ..db import get_session
from ..models import Conversation, MessageRow, PlanRow
from ..schemas import (
    ChatMessageOut,
    ChatRequest,
    ChatResponse,
    CurrentPlanBody,
    Dish,
    MessageSearchHit,
    PreferencesBody,
)
from ..services import appstate
from ..services.mapping import to_week_plan

import logging

router = APIRouter(prefix="/api", tags=["chat"])

logger = logging.getLogger("easy_week.chat")

SessionDep = Annotated[Session, Depends(get_session)]


def _accepted_dish_names(session: Session, limit: int = 12) -> list[str]:
    rows = session.exec(select(PlanRow).where(PlanRow.status == "accepted")).all()
    names: list[str] = []
    for row in rows:
        for dish in row.dishes or []:
            name = dish.get("name")
            if name:
                names.append(name)
    return names[:limit]


@router.get("/limits")
async def get_limits() -> dict:
    """Дневные лимиты генерации Claude за сегодня (used/limit/remaining)."""
    return {"anthropic": limits_status()}


@router.get("/preferences")
async def get_preferences() -> dict:
    """Пищевые предпочтения пользователя (что любит / не любит)."""
    return prefs.load()


@router.put("/preferences")
async def put_preferences(body: PreferencesBody) -> dict:
    """Полная замена предпочтений (правка из профиля)."""
    return prefs.set_lists(body.dislikes, body.likes)


@router.get("/current-plan")
async def get_current_plan(session: SessionDep) -> dict:
    """Выбранный «текущий» план (общий для покупок/готовки). null, если не выбран/удалён."""
    pid = appstate.get_current_plan()
    if pid and session.get(PlanRow, pid) is None:
        pid = None
    return {"planId": pid}


@router.put("/current-plan")
async def set_current_plan(body: CurrentPlanBody, session: SessionDep) -> dict:
    """Выбрать «текущий» план (для покупок/готовки), общий для всех устройств."""
    pid = body.plan_id
    if pid and session.get(PlanRow, pid) is None:
        raise HTTPException(status_code=404, detail="План не найден")
    appstate.set_current_plan(pid)
    return {"planId": pid}


@router.get("/messages/search")
async def search_messages(q: str, session: SessionDep) -> list[MessageSearchHit]:
    """Поиск по тексту сообщений всех бесед (регистронезависимо, в т.ч. кириллица).
    Возвращает само сообщение + контекст беседы (последний план: название + эмодзи)."""
    query = (q or "").strip().lower()
    if not query:
        return []
    # Последний план каждой беседы — для контекста результата.
    plans = session.exec(select(PlanRow).order_by(PlanRow.created_at.desc())).all()
    conv_plan: dict[str, PlanRow] = {}
    for p in plans:
        conv_plan.setdefault(p.conversation_id, p)  # первый встреченный = самый свежий
    rows = session.exec(select(MessageRow).order_by(MessageRow.created_at.desc())).all()
    hits: list[MessageSearchHit] = []
    for m in rows:
        if not m.text or query not in m.text.lower():
            continue
        plan_row = session.get(PlanRow, m.plan_id) if m.plan_id else conv_plan.get(m.conversation_id)
        title = plan_row.title if plan_row else None
        emoji = None
        if plan_row:
            dishes = plan_row.dishes or []
            emoji = dishes[0].get("emoji", "🍽️") if dishes else "🍽️"
        hits.append(
            MessageSearchHit(
                id=m.id,
                conversation_id=m.conversation_id,
                role=m.role,
                text=m.text,
                plan_title=title,
                plan_emoji=emoji,
            )
        )
        if len(hits) >= 50:
            break
    return hits


@router.get("/conversations/{conversation_id}/messages")
async def conversation_messages(
    conversation_id: str, session: SessionDep
) -> list[ChatMessageOut]:
    """Сообщения диалога (для продолжения обсуждения плана в чате)."""
    rows = session.exec(
        select(MessageRow)
        .where(MessageRow.conversation_id == conversation_id)
        .order_by(MessageRow.created_at)
    ).all()
    out: list[ChatMessageOut] = []
    for m in rows:
        plan = None
        if m.plan_id:
            plan_row = session.get(PlanRow, m.plan_id)
            if plan_row:
                plan = to_week_plan(plan_row)
        out.append(ChatMessageOut(id=m.id, role=m.role, text=m.text, plan=plan, model=m.model or ""))
    return out


@router.post("/chat/stream", response_class=EventSourceResponse)
async def chat_stream(
    req: ChatRequest, session: SessionDep
) -> AsyncIterable[ServerSentEvent]:
    """Потоковый чат: события meta → dish (по одному) → done. То же, что /chat,
    но блюда прилетают по мере генерации (SSE). Токенов не больше — один вызов модели."""
    conv = session.get(Conversation, req.conversation_id) if req.conversation_id else None
    if conv is None:
        conv = Conversation(id=uuid4().hex)
        session.add(conv)
        session.commit()
        record_conversation()
    session.add(
        MessageRow(id=uuid4().hex, conversation_id=conv.id, role="user", text=req.message)
    )
    session.commit()

    prefs.learn_async(req.message)  # фоново запоминаем предпочтения из сообщения (CF, бесплатно)
    avoid = _accepted_dish_names(session)
    plan_id = uuid4().hex
    set_ai_context(conversation_id=conv.id, plan_id=plan_id, endpoint="chat_stream")

    dishes: list[dict] = []
    title = "План на неделю"
    reply = "Готово — вот план на неделю."
    week = ""  # заполнится из события meta (оно всегда раньше блюд)
    provider = ""

    err_msg = ""
    try:
        async for kind, payload in generate_plan_stream(
            req.message, avoid, req.dishes_count, req.gender, req.recipe_model
        ):
            if kind == "meta":
                title, week, reply = payload["title"], payload["week_label"], payload["reply"]
                provider = payload.get("provider", "")
                yield ServerSentEvent(
                    event="meta",
                    data={
                        "conversationId": conv.id,
                        "planId": plan_id,
                        "title": title,
                        "weekLabel": week,
                        "reply": reply,
                        "provider": provider,
                    },
                )
            elif kind == "dish":
                dishes.append(payload)
                yield ServerSentEvent(
                    event="dish",
                    data=Dish.model_validate(payload).model_dump(by_alias=True),
                )
    except AIError as exc:  # лимит/недоступность модели — покажем понятный текст
        err_msg = str(exc)
    except Exception as exc:  # noqa: BLE001 — сохраняем то, что успели собрать
        logger.warning("chat_stream оборвался: %s", str(exc)[:150])

    if not dishes:
        yield ServerSentEvent(
            event="error", data={"message": err_msg or "Не удалось составить план"}
        )
        return

    plan_row = PlanRow(
        id=plan_id,
        conversation_id=conv.id,
        title=title,
        week_label=week or _week_label(),
        status="draft",
        provider=provider,
        dishes=dishes,
    )
    session.add(plan_row)
    msg_id = uuid4().hex
    session.add(
        MessageRow(
            id=msg_id,
            conversation_id=conv.id,
            role="assistant",
            text=reply,
            plan_id=plan_id,
            model=req.recipe_model,
        )
    )
    session.commit()
    record_plan("create")
    yield ServerSentEvent(
        event="done",
        data={
            "planId": plan_id,
            "dishesCount": len(dishes),
            "messageId": msg_id,
            "model": req.recipe_model,
        },
    )


@router.post("/chat")
async def chat(req: ChatRequest, session: SessionDep) -> ChatResponse:
    conv = session.get(Conversation, req.conversation_id) if req.conversation_id else None
    if conv is None:
        conv = Conversation(id=uuid4().hex)
        session.add(conv)
        session.commit()
        record_conversation()

    session.add(
        MessageRow(id=uuid4().hex, conversation_id=conv.id, role="user", text=req.message)
    )
    session.commit()

    prefs.learn_async(req.message)  # фоново запоминаем предпочтения из сообщения (CF, бесплатно)
    avoid = _accepted_dish_names(session)
    set_ai_context(conversation_id=conv.id, endpoint="chat")

    try:
        data = await generate_plan(
            req.message, avoid, req.dishes_count, req.gender, req.recipe_model
        )
    except LimitError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except AIError as exc:
        raise HTTPException(status_code=502, detail=f"Генерация недоступна: {exc}") from exc

    plan_row = PlanRow(
        id=uuid4().hex,
        conversation_id=conv.id,
        title=data["title"],
        week_label=data["week_label"],
        status="draft",
        provider=data.get("provider", ""),
        dishes=data["dishes"],
    )
    session.add(plan_row)
    chat_msg_id = uuid4().hex
    session.add(
        MessageRow(
            id=chat_msg_id,
            conversation_id=conv.id,
            role="assistant",
            text=data["reply"],
            plan_id=plan_row.id,
            model=req.recipe_model,
        )
    )
    session.commit()
    session.refresh(plan_row)
    record_plan("create")

    return ChatResponse(
        conversation_id=conv.id,
        reply=data["reply"],
        plan=to_week_plan(plan_row),
        message_id=chat_msg_id,
        model=req.recipe_model,
    )


def _latest_plan(session: Session, conversation_id: str) -> PlanRow | None:
    return session.exec(
        select(PlanRow)
        .where(PlanRow.conversation_id == conversation_id)
        .order_by(PlanRow.created_at.desc())
    ).first()


def _edit_context(session: Session, conversation_id: str, current_text: str, max_recent: int = 2) -> str:
    """Узкий контекст для правки плана: исходный запрос + последние реплики диалога.

    Даёт модели понять, какое блюдо имеется в виду (напр. «один суп» → изначально куриный),
    не пересобирая весь чат. Текущее сообщение (последняя user-реплика) в контекст не включаем —
    оно уже приходит как «Просьба»."""
    msgs = session.exec(
        select(MessageRow)
        .where(MessageRow.conversation_id == conversation_id)
        .order_by(MessageRow.created_at)
    ).all()
    if not msgs:
        return ""
    original = next((m for m in msgs if m.role == "user" and m.text.strip()), None)
    # Хвост без текущей user-реплики (её текст == current_text).
    tail = msgs[:-1] if msgs[-1].role == "user" and msgs[-1].text == current_text else msgs
    recent = [m for m in tail[-max_recent:] if m is not original and m.text.strip()]
    parts: list[str] = []
    if original:
        parts.append(f"Исходный запрос: {original.text.strip()}")
    if recent:
        hist = "\n".join(
            f"{'Пользователь' if m.role == 'user' else 'Ты'}: {m.text.strip()}" for m in recent
        )
        parts.append("Недавно в диалоге:\n" + hist)
    return "\n".join(parts)


@router.post("/chat/edit")
async def chat_edit(req: ChatRequest, session: SessionDep) -> ChatResponse:
    """Правка текущего плана диалога через function calling (добавить/убрать/заменить блюдо
    или пересобрать меню). Обновляет существующий план на месте, а не создаёт новый."""
    conv = session.get(Conversation, req.conversation_id) if req.conversation_id else None
    if conv is None:
        raise HTTPException(status_code=404, detail="Диалог не найден")

    row = _latest_plan(session, conv.id)
    if row is None:
        # Плана ещё нет — вести себя как обычное создание.
        return await chat(req, session)
    set_ai_context(conversation_id=conv.id, plan_id=row.id, endpoint="chat_edit")

    # Текст пользователя в ленте: для кнопок — понятный лейбл действия.
    user_text = req.message
    if req.replace_dish_id:
        tgt = next(
            (d.get("name") for d in (row.dishes or []) if d.get("id") == req.replace_dish_id),
            None,
        )
        user_text = f"Замена «{tgt}»" if tgt else "Замена блюда"
        if req.message.strip():
            user_text += f": {req.message.strip()}"
    elif req.add_dish:
        user_text = "Добавить блюдо"
        if req.message.strip():
            user_text += f": {req.message.strip()}"

    # Крестик (удаление) — мгновенное действие без реплики: пользовательское сообщение не пишем.
    if not req.remove_dish_id:
        session.add(
            MessageRow(id=uuid4().hex, conversation_id=conv.id, role="user", text=user_text)
        )
        session.commit()

    context = _edit_context(session, conv.id, req.message)
    # Вкусы извлекаем ТОЛЬКО из свободного текста пользователя. Правки по кнопкам без текста
    # (replace/remove/add с пустым сообщением) вкусов не несут — CF не дёргаем. Текстовые правки
    # («без свинины») разбираем: экстрактору даём структурный хинт + контекст, чтобы «замени на
    # не-суп»/«где суп» не улетали в предпочтения (см. prefs._EXTRACT_SYSTEM).
    if req.message.strip():
        prefs.learn_async(req.message, "Это правка уже составленного плана.\n" + context)
    try:
        if req.remove_dish_id:
            # Крестик — детерминированное удаление, вообще без модели.
            result = remove_dish_by_id(row.dishes or [], row.title, req.remove_dish_id)
        elif req.replace_dish_id:
            # Точечная замена по кнопке — минуя тул-коллинг (выбор функции).
            result = await replace_dish_by_id(
                row.dishes or [], row.title, req.replace_dish_id, req.message,
                req.gender, req.recipe_model,
            )
        elif req.add_dish:
            # Добавление по кнопке — минуя тул-коллинг.
            result = await add_dish_direct(
                row.dishes or [], row.title, req.message, req.gender, req.recipe_model
            )
        else:
            result = await edit_plan(
                row.dishes or [], row.title, req.message, req.gender, req.recipe_model, context
            )
    except LimitError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except AIError as exc:
        raise HTTPException(status_code=502, detail=f"Правка недоступна: {exc}") from exc

    reply = result["reply"]

    # Ничего не изменили (модель не поняла правку) — новую версию не создаём.
    if not result.get("changed"):
        nc_id = uuid4().hex
        session.add(
            MessageRow(
                id=nc_id,
                conversation_id=conv.id,
                role="assistant",
                text=reply,
                model=req.recipe_model,
            )
        )
        session.commit()
        return ChatResponse(
            conversation_id=conv.id,
            reply=reply,
            plan=None,
            message_id=nc_id,
            model=req.recipe_model,
        )

    # Правка создаёт НОВУЮ версию плана (копию), исходный план остаётся доступен по ссылке.
    new_plan = PlanRow(
        id=uuid4().hex,
        conversation_id=conv.id,
        title=result["title"],
        week_label=row.week_label,
        status="draft",
        provider=result.get("provider") or row.provider,
        parent_id=row.id,
        dishes=result["dishes"],
    )
    session.add(new_plan)
    # Исходная версия заменена новой — сразу отменяем её (остаётся доступной по ссылке,
    # в истории/при перезагрузке чата свернётся как «отменён»).
    row.status = "rejected"
    row.decided_at = datetime.now(timezone.utc)
    session.add(row)
    # Крестик — без реплики: сообщение несёт только новую версию плана (пустой текст),
    # чтобы карточка отрисовалась при перезагрузке чата.
    edit_msg_id = uuid4().hex
    session.add(
        MessageRow(
            id=edit_msg_id,
            conversation_id=conv.id,
            role="assistant",
            text="" if req.remove_dish_id else reply,
            plan_id=new_plan.id,
            model=req.recipe_model,
        )
    )
    session.commit()
    session.refresh(new_plan)
    record_plan("edit")

    return ChatResponse(
        conversation_id=conv.id,
        reply=reply,
        plan=to_week_plan(new_plan),
        message_id=edit_msg_id,
        model=req.recipe_model,
    )
