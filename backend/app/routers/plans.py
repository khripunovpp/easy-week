import asyncio
import hashlib
import json
from collections.abc import AsyncIterable
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.sse import EventSourceResponse, ServerSentEvent
from sqlmodel import Session, select

from ..ai.cloudflare import CloudflareError
from ..ai.planner import generate_dish_detail, normalize_shopping
from ..db import get_session
from ..models import PlanRow
from ..schemas import Dish, PlanSummary, ShoppingGroup, StatusRequest, WeekPlan
from ..services.export_pdf import build_plan_pdf
from ..services.mapping import to_summary, to_week_plan
from ..services.shopping import aggregate_ingredients, group_items

import logging

router = APIRouter(prefix="/api/plans", tags=["plans"])

logger = logging.getLogger("easy_week.plans")

SessionDep = Annotated[Session, Depends(get_session)]

_VALID_STATUS = {"draft", "accepted", "rejected"}


def _get_plan(session: Session, plan_id: str) -> PlanRow:
    row = session.get(PlanRow, plan_id)
    if row is None:
        raise HTTPException(status_code=404, detail="План не найден")
    return row


def _merge_detail(dish: dict, detail: dict) -> dict:
    """Вливает ленивую деталь (ингредиенты/шаги/советы/note) в блюдо."""
    d = {
        **dish,
        "ingredients": detail.get("ingredients") or [],
        "steps": detail.get("steps") or [],
        "tips": detail.get("tips") or [],
        "detail_provider": detail.get("provider") or "",
    }
    if detail.get("note"):
        d["storage"] = {**(dish.get("storage") or {}), "note": detail["note"]}
    return d


async def _backfill_all(session: Session, row: PlanRow, need_steps: bool = False) -> list[dict]:
    """Догенерить детали для блюд, у которых их нет, параллельно. Кэш в row.dishes.
    need_steps=False (покупки: нужны только ингредиенты), True (PDF: нужны и шаги)."""
    dishes = list(row.dishes or [])
    missing = [
        (i, d)
        for i, d in enumerate(dishes)
        if not d.get("ingredients") or (need_steps and not d.get("steps"))
    ]
    if not missing:
        return dishes
    results = await asyncio.gather(
        *(generate_dish_detail(d.get("name", ""), d.get("servings", 4)) for _, d in missing),
        return_exceptions=True,
    )
    changed = False
    for (i, d), det in zip(missing, results):
        if isinstance(det, dict):
            dishes[i] = _merge_detail(d, det)
            changed = True
    if changed:
        row.dishes = dishes
        session.add(row)
        session.commit()
        session.refresh(row)
    return list(row.dishes or [])


@router.get("")
async def list_plans(session: SessionDep) -> list[PlanSummary]:
    rows = session.exec(select(PlanRow).order_by(PlanRow.created_at.desc())).all()
    # Промежуточные ЧЕРНОВИКИ, у которых уже есть более новая версия (правка в чате),
    # в списке не показываем — только последнюю версию. Принятые/отклонённые остаются
    # всегда (их ссылки могли быть расшарены).
    superseded = {r.parent_id for r in rows if r.parent_id}
    visible = [r for r in rows if not (r.status == "draft" and r.id in superseded)]
    return [to_summary(r) for r in visible]


@router.get("/{plan_id}")
async def get_plan(plan_id: str, session: SessionDep) -> WeekPlan:
    return to_week_plan(_get_plan(session, plan_id))


@router.delete("/{plan_id}", status_code=204)
async def delete_plan(plan_id: str, session: SessionDep) -> None:
    row = _get_plan(session, plan_id)
    session.delete(row)
    session.commit()


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
    await _backfill_all(session, row)  # ингредиенты лениво — догрузить перед агрегацией
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


def _clean_shop_item(it: dict) -> dict | None:
    """Приводит пункт к {name, qty, unit, category}; None — если мусор."""
    name = str(it.get("name", "")).strip()
    if not name:
        return None
    try:
        qty = float(it.get("qty", 0) or 0)
    except (TypeError, ValueError):
        qty = 0.0
    qty = round(qty, 2) if qty % 1 else int(qty)
    cat = str(it.get("category") or "Прочее").strip()
    return {"name": name, "qty": qty, "unit": str(it.get("unit", "")).strip(), "category": cat}


@router.get("/{plan_id}/shopping-list/stream", response_class=EventSourceResponse)
async def shopping_list_stream(
    plan_id: str, session: SessionDep
) -> AsyncIterable[ServerSentEvent]:
    """Потоковый список покупок: пункты прилетают по одному (SSE).

    Ингредиенты генерятся лениво — при первом открытии покупок догружаем их для всех блюд,
    затем детерминированно агрегируем (граммы у источника, каноничное объединение)."""
    row = _get_plan(session, plan_id)
    await _backfill_all(session, row)
    plan = to_week_plan(row)
    count = 0
    for it in aggregate_ingredients(plan.dishes):
        clean = _clean_shop_item(it)
        if clean:
            count += 1
            yield ServerSentEvent(event="item", data=clean)
    yield ServerSentEvent(event="done", data={"count": count})


@router.get("/{plan_id}/pdf")
async def plan_pdf(
    plan_id: str,
    session: SessionDep,
    recipes: bool = True,
    shopping: bool = True,
) -> Response:
    """PDF плана (рецепты и/или список покупок). Детали генерятся лениво — догрузим при экспорте."""
    row = _get_plan(session, plan_id)
    await _backfill_all(session, row, need_steps=recipes)
    plan = to_week_plan(row)
    groups = group_items(aggregate_ingredients(plan.dishes)) if shopping else []
    pdf_bytes = build_plan_pdf(plan, groups, recipes=recipes, shop=shopping)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": 'inline; filename="easy-week.pdf"'},
    )


@router.post("/{plan_id}/full")
async def full_plan(plan_id: str, session: SessionDep) -> WeekPlan:
    """Полный план со всеми деталями (догенерирует недостающие) — для экспорта в PDF."""
    row = _get_plan(session, plan_id)
    try:
        await _backfill_all(session, row, need_steps=True)
    except CloudflareError as exc:
        raise HTTPException(status_code=502, detail=f"Не удалось собрать рецепты: {exc}") from exc
    return to_week_plan(row)


@router.post("/{plan_id}/dishes/{dish_id}/details")
async def dish_details(plan_id: str, dish_id: str, session: SessionDep) -> Dish:
    """Ленивая догенерация ПОЛНОЙ детали блюда (ингредиенты+шаги+советы). Кэшируется в плане."""
    row = _get_plan(session, plan_id)
    dishes = list(row.dishes or [])
    idx = next((i for i, d in enumerate(dishes) if d.get("id") == dish_id), None)
    if idx is None:
        raise HTTPException(status_code=404, detail="Блюдо не найдено")

    dish = dishes[idx]
    if not dish.get("ingredients") or not dish.get("steps"):  # генерим деталь, если её ещё нет
        try:
            detail = await generate_dish_detail(dish.get("name", ""), dish.get("servings", 4))
        except CloudflareError as exc:
            raise HTTPException(status_code=502, detail=f"Не удалось получить рецепт: {exc}") from exc
        dish = _merge_detail(dish, detail)
        dishes[idx] = dish
        row.dishes = dishes
        session.add(row)
        session.commit()

    return Dish.model_validate(dish)
