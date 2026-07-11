from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from ..ai.cloudflare import CloudflareError
from ..ai.planner import generate_details
from ..db import get_session
from ..models import PlanRow
from ..schemas import Dish, PlanSummary, ShoppingGroup, StatusRequest, WeekPlan
from ..services.mapping import to_summary, to_week_plan
from ..services.shopping import build_shopping_list

router = APIRouter(prefix="/api/plans", tags=["plans"])

SessionDep = Annotated[Session, Depends(get_session)]

_VALID_STATUS = {"draft", "accepted", "rejected"}


def _get_plan(session: Session, plan_id: str) -> PlanRow:
    row = session.get(PlanRow, plan_id)
    if row is None:
        raise HTTPException(status_code=404, detail="План не найден")
    return row


@router.get("")
async def list_plans(session: SessionDep) -> list[PlanSummary]:
    rows = session.exec(select(PlanRow).order_by(PlanRow.created_at.desc())).all()
    return [to_summary(r) for r in rows]


@router.get("/{plan_id}")
async def get_plan(plan_id: str, session: SessionDep) -> WeekPlan:
    return to_week_plan(_get_plan(session, plan_id))


@router.post("/{plan_id}/status")
async def set_status(plan_id: str, req: StatusRequest, session: SessionDep) -> WeekPlan:
    if req.status not in _VALID_STATUS:
        raise HTTPException(status_code=422, detail="Недопустимый статус")
    row = _get_plan(session, plan_id)
    row.status = req.status
    row.decided_at = datetime.now(timezone.utc) if req.status != "draft" else None
    session.add(row)
    session.commit()
    session.refresh(row)
    return to_week_plan(row)


@router.get("/{plan_id}/shopping-list")
async def shopping_list(plan_id: str, session: SessionDep) -> list[ShoppingGroup]:
    plan = to_week_plan(_get_plan(session, plan_id))
    return build_shopping_list(plan.dishes)


@router.post("/{plan_id}/dishes/{dish_id}/details")
async def dish_details(plan_id: str, dish_id: str, session: SessionDep) -> Dish:
    """Ленивая догенерация шагов и советов блюда. Кэшируется в плане."""
    row = _get_plan(session, plan_id)
    dishes = list(row.dishes or [])
    idx = next((i for i, d in enumerate(dishes) if d.get("id") == dish_id), None)
    if idx is None:
        raise HTTPException(status_code=404, detail="Блюдо не найдено")

    dish = dishes[idx]
    if not dish.get("steps"):  # генерим только если ещё нет
        try:
            details = await generate_details(dish.get("name", ""), dish.get("ingredients", []))
        except CloudflareError as exc:
            raise HTTPException(status_code=502, detail=f"Не удалось получить рецепт: {exc}") from exc
        dish = {**dish, **details}
        dishes[idx] = dish
        row.dishes = dishes  # переприсваиваем целиком, чтобы JSON-колонка обновилась
        session.add(row)
        session.commit()

    return Dish.model_validate(dish)
