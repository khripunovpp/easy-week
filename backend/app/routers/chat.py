from collections.abc import AsyncIterable
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from fastapi.sse import EventSourceResponse, ServerSentEvent
from sqlmodel import Session, select

from ..ai.cloudflare import CloudflareError
from ..ai.planner import _week_label, edit_plan, generate_plan, generate_plan_stream
from ..db import get_session
from ..models import Conversation, MessageRow, PlanRow
from ..schemas import ChatMessageOut, ChatRequest, ChatResponse, Dish
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
        out.append(ChatMessageOut(id=m.id, role=m.role, text=m.text, plan=plan))
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
    session.add(
        MessageRow(id=uuid4().hex, conversation_id=conv.id, role="user", text=req.message)
    )
    session.commit()

    avoid = _accepted_dish_names(session)
    plan_id = uuid4().hex

    dishes: list[dict] = []
    title = "План на неделю"
    reply = "Готово — вот план на неделю."
    week = ""  # заполнится из события meta (оно всегда раньше блюд)
    provider = ""

    try:
        async for kind, payload in generate_plan_stream(req.message, avoid, req.dishes_count):
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
    except Exception as exc:  # noqa: BLE001 — сохраняем то, что успели собрать
        logger.warning("chat_stream оборвался: %s", str(exc)[:150])

    if not dishes:
        yield ServerSentEvent(event="error", data={"message": "Не удалось составить план"})
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
    session.add(
        MessageRow(
            id=uuid4().hex,
            conversation_id=conv.id,
            role="assistant",
            text=reply,
            plan_id=plan_id,
        )
    )
    session.commit()
    yield ServerSentEvent(event="done", data={"planId": plan_id, "dishesCount": len(dishes)})


@router.post("/chat")
async def chat(req: ChatRequest, session: SessionDep) -> ChatResponse:
    conv = session.get(Conversation, req.conversation_id) if req.conversation_id else None
    if conv is None:
        conv = Conversation(id=uuid4().hex)
        session.add(conv)
        session.commit()

    session.add(
        MessageRow(id=uuid4().hex, conversation_id=conv.id, role="user", text=req.message)
    )
    session.commit()

    avoid = _accepted_dish_names(session)

    try:
        data = await generate_plan(req.message, avoid, req.dishes_count)
    except CloudflareError as exc:
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
    session.add(
        MessageRow(
            id=uuid4().hex,
            conversation_id=conv.id,
            role="assistant",
            text=data["reply"],
            plan_id=plan_row.id,
        )
    )
    session.commit()
    session.refresh(plan_row)

    return ChatResponse(
        conversation_id=conv.id,
        reply=data["reply"],
        plan=to_week_plan(plan_row),
    )


def _latest_plan(session: Session, conversation_id: str) -> PlanRow | None:
    return session.exec(
        select(PlanRow)
        .where(PlanRow.conversation_id == conversation_id)
        .order_by(PlanRow.created_at.desc())
    ).first()


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

    session.add(
        MessageRow(id=uuid4().hex, conversation_id=conv.id, role="user", text=req.message)
    )
    session.commit()

    try:
        result = await edit_plan(row.dishes or [], row.title, req.message)
    except CloudflareError as exc:
        raise HTTPException(status_code=502, detail=f"Правка недоступна: {exc}") from exc

    row.dishes = result["dishes"]
    row.title = result["title"]
    if result.get("provider"):
        row.provider = result["provider"]
    row.shopping_sig = ""  # состав изменился — сбросить кэш списка покупок
    session.add(row)
    session.add(
        MessageRow(
            id=uuid4().hex,
            conversation_id=conv.id,
            role="assistant",
            text=result["reply"],
            plan_id=row.id,
        )
    )
    session.commit()
    session.refresh(row)

    return ChatResponse(
        conversation_id=conv.id,
        reply=result["reply"],
        plan=to_week_plan(row),
    )
