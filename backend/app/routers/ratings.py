"""Оценки 👍/👎 сгенерированных моделью ответов (рецепт/план/готовка/сообщение).

Авторизации нет — одно глобальное хранилище. Один голос на (target_type, target_id, model):
повторный тот же голос — снять; противоположный — переключить."""

from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import Session, select

from ..ai.observe import record_rating
from ..db import get_session
from ..models import RatingRow
from ..schemas import RatingBody, RatingOut

router = APIRouter(prefix="/api", tags=["ratings"])

SessionDep = Annotated[Session, Depends(get_session)]


def _find(session: Session, target_type: str, target_id: str, model: str) -> RatingRow | None:
    return session.exec(
        select(RatingRow).where(
            RatingRow.target_type == target_type,
            RatingRow.target_id == target_id,
            RatingRow.model == (model or ""),
        )
    ).first()


@router.post("/ratings")
async def rate(body: RatingBody, session: SessionDep) -> RatingOut:
    if body.vote not in (1, -1):
        raise HTTPException(status_code=422, detail="vote должен быть 1 или -1")
    row = _find(session, body.target_type, body.target_id, body.model)
    if row is not None and row.vote == body.vote:
        # Повторный тот же голос — снимаем.
        session.delete(row)
        session.commit()
        return RatingOut(vote=0)
    if row is None:
        row = RatingRow(
            id=uuid4().hex,
            target_type=body.target_type,
            target_id=body.target_id,
            model=body.model or "",
        )
    row.vote = body.vote
    row.note = body.note or ""
    row.plan_id = body.plan_id
    row.dish_id = body.dish_id
    row.conversation_id = body.conversation_id
    session.add(row)
    session.commit()
    record_rating(body.target_type, body.model, body.vote)
    return RatingOut(vote=body.vote)


@router.get("/ratings")
async def get_rating(
    session: SessionDep,
    target_type: str = Query(alias="targetType"),
    target_id: str = Query(alias="targetId"),
    model: str = Query("", alias="model"),
) -> RatingOut:
    row = _find(session, target_type, target_id, model)
    return RatingOut(vote=row.vote if row else 0)
