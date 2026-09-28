"""Оценки 👍/👎 сгенерированных моделью ответов (рецепт/план/готовка/сообщение).

Авторизации нет — одно глобальное хранилище. Один голос на (target_type, target_id, model):
повторный тот же голос — снять; противоположный — переключить."""

import logging
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import Session, select

from ..ai.observe import record_rating, record_rating_reasons
from ..db import get_session
from ..models import RatingRow
from ..schemas import RatingBody, RatingOut, RatingReasonsBody
from ..services import rating_reasons

logger = logging.getLogger("easy_week.ratings")

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
    row.reasons = ""  # смена голоса — старые причины уже не про него
    row.note = body.note or ""
    row.plan_id = body.plan_id
    row.dish_id = body.dish_id
    row.conversation_id = body.conversation_id
    session.add(row)
    session.commit()
    record_rating(body.target_type, body.model, body.vote)
    return RatingOut(vote=body.vote)


@router.get("/ratings/reasons")
async def reasons_catalog() -> dict[str, list[dict[str, str]]]:
    """Каталог причин 👎 по типам ответов (для выпадашки)."""
    return rating_reasons.catalog()


@router.patch("/ratings/reasons")
async def set_reasons(body: RatingReasonsBody, session: SessionDep) -> RatingOut:
    """Причины к уже поставленному 👎 (PATCH). Голос не трогаем (без 👎 — 409)."""
    row = _find(session, body.target_type, body.target_id, body.model)
    if row is None or row.vote != -1:
        raise HTTPException(status_code=409, detail="причины — только к 👎")
    keys = rating_reasons.clean(body.target_type, body.reasons)
    note = (body.note or "").strip()[:1000]
    if note and rating_reasons.OTHER not in keys:
        keys.append(rating_reasons.OTHER)
    # Метрику считаем только по новым причинам — повторная отправка не задваивает.
    old = set(filter(None, row.reasons.split(",")))
    row.reasons = ",".join(keys)
    row.note = note
    session.add(row)
    session.commit()
    record_rating_reasons(body.target_type, body.model, [k for k in keys if k not in old])
    logger.info(
        "👎 %s/%s model=%s reasons=%s note=%r",
        body.target_type, body.target_id, body.model or "?", keys, note[:200],
    )
    return RatingOut(vote=row.vote)


@router.get("/ratings")
async def get_rating(
    session: SessionDep,
    target_type: str = Query(alias="targetType"),
    target_id: str = Query(alias="targetId"),
    model: str = Query("", alias="model"),
) -> RatingOut:
    row = _find(session, target_type, target_id, model)
    return RatingOut(vote=row.vote if row else 0)
