from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from ..ai.cloudflare import CloudflareError
from ..ai.planner import generate_plan
from ..db import get_session
from ..models import Conversation, MessageRow, PlanRow
from ..schemas import ChatRequest, ChatResponse
from ..services.mapping import to_week_plan

router = APIRouter(prefix="/api", tags=["chat"])

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
        data = await generate_plan(req.message, avoid)
    except CloudflareError as exc:
        raise HTTPException(status_code=502, detail=f"Генерация недоступна: {exc}") from exc

    plan_row = PlanRow(
        id=uuid4().hex,
        conversation_id=conv.id,
        title=data["title"],
        week_label=data["week_label"],
        status="draft",
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
