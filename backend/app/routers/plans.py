import asyncio
import hashlib
import json
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from ..ai.cloudflare import CloudflareError
from ..ai.planner import generate_details, normalize_shopping
from ..db import get_session
from ..models import PlanRow
from ..schemas import Dish, PlanSummary, ShoppingGroup, StatusRequest, WeekPlan
from ..services.mapping import to_summary, to_week_plan
from ..services.shopping import aggregate_ingredients, group_items

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
    row = _get_plan(session, plan_id)
    plan = to_week_plan(row)
    base = aggregate_ingredients(plan.dishes)
    sig = hashlib.md5(
        json.dumps(base, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()

    # Один вызов модели на план; дальше — из кэша.
    if row.shopping_sig == sig and row.shopping_cache:
        return group_items(row.shopping_cache)

    try:
        items = await normalize_shopping(base)
    except Exception:  # noqa: BLE001 — модель не критична, есть фолбэк
        items = base
    if not items:
        items = base
    row.shopping_cache = items
    row.shopping_sig = sig
    session.add(row)
    session.commit()
    return group_items(items)


@router.post("/{plan_id}/full")
async def full_plan(plan_id: str, session: SessionDep) -> WeekPlan:
    """Полный план со всеми шагами (догенерирует недостающие) — для экспорта в PDF."""
    row = _get_plan(session, plan_id)
    dishes = list(row.dishes or [])
    missing = [(i, d) for i, d in enumerate(dishes) if not d.get("steps")]
    if missing:
        try:
            results = await asyncio.gather(
                *(generate_details(d.get("name", ""), d.get("ingredients", [])) for _, d in missing)
            )
        except CloudflareError as exc:
            raise HTTPException(status_code=502, detail=f"Не удалось собрать рецепты: {exc}") from exc
        for (i, d), det in zip(missing, results):
            dishes[i] = {**d, **det}
        row.dishes = dishes
        session.add(row)
        session.commit()
        session.refresh(row)
    return to_week_plan(row)


@router.post("/{plan_id}/dishes/{dish_id}/details")
async def dish_details(plan_id: str, dish_id: str, session: SessionDep) -> Dish:
    """Ленивая догенерация шагов и советов блюда. Кэшируется в плане."""
    row = _get_plan(session, plan_id)
    dishes = list(row.dishes or [])
    idx = next((i for i, d in enumerate(dishes) if d.get("id") == dish_id), None)
    if idx is None:
        raise HTTPException(status_code=404, detail="Блюдо не найдено")

    dish = dishes[idx]
    if not dish.get("steps"):  # генерим только если ещё нет (старые планы без шагов)
        try:
            details = await generate_details(dish.get("name", ""), dish.get("ingredients", []))
        except CloudflareError as exc:
            raise HTTPException(status_code=502, detail=f"Не удалось получить рецепт: {exc}") from exc
        dish = {**dish, **details}
        dishes[idx] = dish
        row.dishes = dishes
        session.add(row)
        session.commit()

    return Dish.model_validate(dish)
