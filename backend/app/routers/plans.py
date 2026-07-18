import asyncio
import hashlib
import json
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlmodel import Session, select

from ..ai.base import AIError
from ..ai.gates import GATES, gate_for
from ..ai.limits import LimitError
from ..ai.observe import set_ai_context
from ..ai.planner import generate_dish_detail, normalize_shopping
from ..db import get_session
from ..models import PlanRow
from ..schemas import (
    DetailRequest,
    Dish,
    DishVariant,
    PlanSummary,
    ShoppingGroup,
    StatusRequest,
    WeekPlan,
)
from ..services.export_pdf import build_plan_pdf
from ..services.mapping import to_dish, to_summary, to_week_plan

# Провайдер (человекочитаемый) → ключ модели — для миграции legacy-детали в вариант.
_PROVIDER_KEY = {g.provider: g.key for g in GATES.values()}
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


async def _backfill_all(
    session: Session, row: PlanRow, need_steps: bool = False, model: str = ""
) -> list[dict]:
    """Догенерить детали для блюд, у которых их нет, параллельно. Кэш в row.dishes.
    need_steps=False (покупки: нужны только ингредиенты), True (PDF: нужны и шаги).
    model — выбранная модель рецептов (пусто → дефолт из настроек)."""
    dishes = list(row.dishes or [])
    missing = [
        (i, d)
        for i, d in enumerate(dishes)
        if not d.get("ingredients") or (need_steps and not d.get("steps"))
    ]
    if not missing:
        return dishes
    results = await asyncio.gather(
        *(
            generate_dish_detail(d.get("name", ""), d.get("servings", 4), model=model)
            for _, d in missing
        ),
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
    set_ai_context(plan_id=plan_id, endpoint="shopping_list")
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
    except Exception:  # noqa: BLE001 — нормализация не критична: остаётся детерминированная база
        items = base
    if not items:
        items = base
    row.shopping_cache = items
    row.shopping_sig = sig
    session.add(row)
    session.commit()
    return group_items(items)




@router.get("/{plan_id}/pdf")
async def plan_pdf(
    plan_id: str,
    session: SessionDep,
    recipes: bool = True,
    shopping: bool = True,
) -> Response:
    """PDF плана (рецепты и/или список покупок). Детали генерятся лениво — догрузим при экспорте."""
    set_ai_context(plan_id=plan_id, endpoint="plan_pdf")
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
async def full_plan(plan_id: str, req: DetailRequest, session: SessionDep) -> WeekPlan:
    """Полный план со всеми деталями (догенерирует недостающие) — для экспорта в PDF."""
    set_ai_context(plan_id=plan_id, endpoint="full_plan")
    row = _get_plan(session, plan_id)
    try:
        await _backfill_all(session, row, need_steps=True, model=req.recipe_model)
    except AIError as exc:
        raise HTTPException(status_code=502, detail=f"Не удалось собрать рецепты: {exc}") from exc
    return to_week_plan(row)


def _variant_from_detail(detail: dict) -> dict:
    """Деталь модели → вариант рецепта (ингредиенты/шаги/советы/note/провайдер)."""
    return {
        "ingredients": detail.get("ingredients") or [],
        "steps": detail.get("steps") or [],
        "tips": detail.get("tips") or [],
        "note": detail.get("note") or "",
        "provider": detail.get("provider") or "",
    }


def _dish_variants(dish: dict) -> dict:
    """Варианты рецепта по моделям; при отсутствии — мигрируем плоскую деталь (legacy)."""
    variants = dict(dish.get("variants") or {})
    if not variants and (dish.get("ingredients") or dish.get("steps")):
        key = _PROVIDER_KEY.get(dish.get("detail_provider", ""))
        if key:
            variants[key] = {
                "ingredients": dish.get("ingredients") or [],
                "steps": dish.get("steps") or [],
                "tips": dish.get("tips") or [],
                "note": (dish.get("storage") or {}).get("note") or "",
                "provider": dish.get("detail_provider") or "",
            }
    return variants


def _apply_variant(dish: dict, model: str, variants: dict) -> dict:
    """Делает вариант model активным: зеркалим его в плоские поля (для покупок/PDF)."""
    v = variants.get(model) or {}
    return {
        **dish,
        "variants": variants,
        "active_model": model,
        "ingredients": v.get("ingredients") or [],
        "steps": v.get("steps") or [],
        "tips": v.get("tips") or [],
        "detail_provider": v.get("provider") or "",
        "storage": {**(dish.get("storage") or {}), "note": v.get("note") or ""},
    }


# Склейка одинаковых запросов генерации (single-flight): пока идёт генерация рецепта
# для (plan_id, dish_id, model), параллельные такие же запросы ждут ТОТ ЖЕ результат —
# без повторного вызова модели и двойной записи в БД. Процесс один (uvicorn без --workers),
# поэтому in-process словаря достаточно; очереди/брокер не нужны.
_inflight: dict[tuple, "asyncio.Future"] = {}


async def _single_flight(key: tuple, factory):
    running = _inflight.get(key)
    if running is not None:
        return await running
    fut = asyncio.ensure_future(factory())
    _inflight[key] = fut
    try:
        return await fut
    finally:
        _inflight.pop(key, None)


@router.post("/{plan_id}/dishes/{dish_id}/details")
async def dish_details(
    plan_id: str, dish_id: str, req: DetailRequest, session: SessionDep
) -> Dish:
    """Рецепт блюда с вариантами по моделям (лениво, кэш в плане).

    action=open — вернуть активный вариант (сгенерить первый, если деталей ещё нет);
    action=select — сделать recipe_model активным (сгенерить его вариант, если ещё нет).
    Одной моделью повторно не генерим — если вариант уже есть, просто переключаемся.
    Параллельные одинаковые запросы склеиваются (single-flight)."""
    action = (req.action or "open").lower()
    set_ai_context(plan_id=plan_id, dish_id=dish_id, endpoint="dish_details", action=action)
    key = (plan_id, dish_id, action, gate_for(req.recipe_model).key)
    return await _single_flight(
        key, lambda: _resolve_dish_detail(plan_id, dish_id, req, action, session)
    )


async def _resolve_dish_detail(
    plan_id: str, dish_id: str, req: DetailRequest, action: str, session: SessionDep
) -> Dish:
    row = _get_plan(session, plan_id)
    dishes = list(row.dishes or [])
    idx = next((i for i, d in enumerate(dishes) if d.get("id") == dish_id), None)
    if idx is None:
        raise HTTPException(status_code=404, detail="Блюдо не найдено")

    dish = dishes[idx]
    variants = _dish_variants(dish)
    resolved = gate_for(req.recipe_model).key  # реальный ключ модели (учёт дефолта)

    if action == "select":
        target = resolved
    else:  # open — держим активный, если он есть; иначе генерим resolved как первый
        active = dish.get("active_model") or next(iter(variants), "")
        target = active if active in variants else resolved

    if target not in variants:  # этого варианта ещё нет — генерим (один раз на модель)
        try:
            detail = await generate_dish_detail(
                dish.get("name", ""), dish.get("servings", 4), model=target
            )
        except LimitError as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        except AIError as exc:
            raise HTTPException(status_code=502, detail=f"Не удалось получить рецепт: {exc}") from exc
        variants[target] = _variant_from_detail(detail)

    dish = _apply_variant(dish, target, variants)
    dishes[idx] = dish
    row.dishes = dishes
    session.add(row)
    session.commit()

    return to_dish(dish)


@router.get("/{plan_id}/dishes/{dish_id}/variants")
async def dish_variants(plan_id: str, dish_id: str, session: SessionDep) -> list[DishVariant]:
    """Все сгенерированные варианты рецепта блюда (по моделям) — для сравнения бок о бок."""
    row = _get_plan(session, plan_id)
    dish = next((d for d in (row.dishes or []) if d.get("id") == dish_id), None)
    if dish is None:
        raise HTTPException(status_code=404, detail="Блюдо не найдено")
    variants = _dish_variants(dish)
    return [DishVariant.model_validate({"model": m, **v}) for m, v in variants.items()]
