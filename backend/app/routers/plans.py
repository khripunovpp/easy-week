import asyncio
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlmodel import Session, select

from ..ai.base import AIError
from ..ai.gates import gate_for
from ..ai.limits import LimitError
from ..ai.observe import set_ai_context
from ..ai.planner import generate_cooking_plan, generate_dish_detail, normalize_shopping
from ..db import get_session
from ..models import PlanRow
from ..schemas import (
    CookingPlan,
    CookingPlanVariant,
    DetailRequest,
    Dish,
    DishShopping,
    DishVariant,
    PlanSummary,
    RenameRequest,
    ShoppingGroup,
    StatusRequest,
    WeekPlan,
)
from ..services.export_pdf import build_plan_pdf
from ..services.history import original_request, reply_mention
from ..services.mapping import to_cook_plan, to_dish, to_summary, to_week_plan
from ..services.regenerate import (
    DishNotFound,
    backfill_all,
    cook_sig,
    regenerate_cooking,
    regenerate_dish,
    regenerate_shopping,
    shopping_base,
)
from ..services.shopping import aggregate_ingredients, group_items
from ..services.variants import apply_variant, now_iso, variant_from_detail
from ..services.variants import dish_variants as variants_of  # имя dish_variants занято роутом

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


@router.get("")
async def list_plans(session: SessionDep) -> list[PlanSummary]:
    rows = session.exec(select(PlanRow).order_by(PlanRow.created_at.desc())).all()
    # Промежуточные версии, у которых уже есть более новая (правка в чате), в списке не
    # показываем — только последнюю. Ссылки на них по-прежнему открываются.
    superseded = {r.parent_id for r in rows if r.parent_id}
    # Версии, заменённые правкой (черновик или авто-«отклонён» при правке), тоже прячем —
    # иначе после «убрать блюдо» в списке висели две «Соляночки»: старая и новая.
    visible = [
        r for r in rows if not (r.status in ("draft", "rejected") and r.id in superseded)
    ]
    return [to_summary(r) for r in visible]


@router.get("/{plan_id}")
async def get_plan(plan_id: str, session: SessionDep) -> WeekPlan:
    return to_week_plan(_get_plan(session, plan_id))


@router.delete("/{plan_id}", status_code=204)
async def delete_plan(plan_id: str, session: SessionDep) -> None:
    row = _get_plan(session, plan_id)
    session.delete(row)
    session.commit()


@router.patch("/{plan_id}")
async def rename_plan(plan_id: str, req: RenameRequest, session: SessionDep) -> WeekPlan:
    """Переименование плана пользователем (долгое нажатие на заголовок на странице плана)."""
    title = " ".join(req.title.split())  # схлопываем переносы/лишние пробелы из contenteditable
    if not title:
        raise HTTPException(status_code=422, detail="Пустое название")
    row = _get_plan(session, plan_id)
    row.title = title
    session.add(row)
    session.commit()
    session.refresh(row)
    return to_week_plan(row)


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
    await backfill_all(session, row)  # ингредиенты лениво — догрузить перед агрегацией
    base, sig = shopping_base(row)

    # Один вызов модели на план; дальше — из кэша.
    if row.shopping_sig == sig and row.shopping_cache:
        return group_items(row.shopping_cache)

    try:
        items = await normalize_shopping(base)  # модель — дефолт «Список покупок» из настроек
    except Exception as exc:  # noqa: BLE001 — нормализация не критична: отдаём базу
        # Базу под этой подписью НЕ кэшируем — иначе сбой нормализации «застывал» навсегда
        # (кэш совпадает по sig, повторной попытки не было бы).
        logger.warning("shopping normalize failed, отдаём базу без кэша: %s", str(exc)[:150])
        return group_items(base)
    if not items:
        return group_items(base)
    row.shopping_cache = items
    row.shopping_sig = sig
    row.shopping_at = datetime.now(timezone.utc)
    row.shopping_model = gate_for("", "shopping").key  # GET нормализует дефолтом из настроек
    session.add(row)
    session.commit()
    return group_items(items)


@router.get("/{plan_id}/shopping-list/by-dish")
async def shopping_by_dish(plan_id: str, session: SessionDep) -> list[DishShopping]:
    """Покупки по рецептам: для каждого блюда — его ингредиенты (слиты формы одного продукта,
    единицы приведены), без вызова модели. Группировку по категориям внутри блюда делает фронт."""
    set_ai_context(plan_id=plan_id, endpoint="shopping_list")
    row = _get_plan(session, plan_id)
    await backfill_all(session, row)  # ингредиенты лениво — догрузить (модель рецептов)
    out: list[DishShopping] = []
    for dish in to_week_plan(row).dishes:
        items = [it for g in group_items(aggregate_ingredients([dish])) for it in g.items]
        out.append(DishShopping(dish_id=dish.id, name=dish.name, emoji=dish.emoji, items=items))
    return out


@router.post("/{plan_id}/shopping-list/regenerate")
async def shopping_regenerate(
    plan_id: str, req: DetailRequest, session: SessionDep
) -> list[ShoppingGroup]:
    """«↻ Перегенерировать» список покупок: нормализация мимо кэша по подписи, с учётом
    обсуждения списка в чате. recipe_model — нормализатор (выпадашка на странице покупок;
    пусто → модель списка покупок по умолчанию). В отличие от GET, сбой нормализации — честная ошибка (502):
    пользователь явно просил пересобрать, тихо отдавать базу нельзя."""
    set_ai_context(plan_id=plan_id, endpoint="shopping_list", action="regenerate")
    row = _get_plan(session, plan_id)
    try:
        items = await _single_flight(
            (plan_id, "shopping", "regenerate"),
            lambda: regenerate_shopping(session, row, req.recipe_model),
        )
    except LimitError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except AIError as exc:
        raise HTTPException(
            status_code=502, detail=f"Не удалось пересобрать список покупок: {exc}"
        ) from exc
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
    await backfill_all(session, row, need_steps=recipes)
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
        await backfill_all(session, row, need_steps=True, model=req.recipe_model)
    except AIError as exc:
        raise HTTPException(status_code=502, detail=f"Не удалось собрать рецепты: {exc}") from exc
    return to_week_plan(row)


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
    action=select — сделать recipe_model активным (сгенерить его вариант, если ещё нет);
    action=regenerate — «↻ Перегенерировать»: ВСЕГДА новый вариант для recipe_model с учётом
    обсуждения рецепта в чате; пишем только после успеха (при ошибке старый вариант цел).
    Кроме regenerate одной моделью повторно не генерим — если вариант есть, переключаемся.
    Параллельные одинаковые запросы склеиваются (single-flight)."""
    action = (req.action or "open").lower()
    set_ai_context(plan_id=plan_id, dish_id=dish_id, endpoint="dish_details", action=action)
    # Пусто → модель рецептов по умолчанию из настроек (ключ склейки — реальная модель).
    key = (plan_id, dish_id, action, gate_for(req.recipe_model, "recipe").key)
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
    variants = variants_of(dish)
    resolved = gate_for(req.recipe_model, "recipe").key  # реальный ключ (учёт дефолта рецептов)

    if action == "regenerate":
        try:
            new = await regenerate_dish(session, row, dish_id, resolved)
        except DishNotFound as exc:
            raise HTTPException(status_code=404, detail="Блюдо не найдено") from exc
        except LimitError as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        except AIError as exc:
            raise HTTPException(
                status_code=502, detail=f"Не удалось перегенерировать рецепт: {exc}"
            ) from exc
        return to_dish(new)

    if action == "select":
        target = resolved
    else:  # open — держим активный, если он есть; иначе генерим resolved как первый
        active = dish.get("active_model") or next(iter(variants), "")
        target = active if active in variants else resolved

    if target not in variants:  # этого варианта ещё нет — генерим (один раз на модель)
        try:
            detail = await generate_dish_detail(
                dish.get("name", ""), dish.get("servings", 4), model=target,
                dish=dish, request=original_request(session, row.conversation_id),
                mention=reply_mention(session, row.id, dish.get("name", "")),
            )
        except LimitError as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        except AIError as exc:
            raise HTTPException(status_code=502, detail=f"Не удалось получить рецепт: {exc}") from exc
        variants[target] = variant_from_detail(detail)

    dish = apply_variant(dish, target, variants)
    dishes[idx] = dish
    row.dishes = dishes
    session.add(row)
    session.commit()

    return to_dish(dish)


def _cook_variants(row: PlanRow) -> dict:
    """Варианты плана готовки по моделям (пусто, если ещё не генерили)."""
    return dict((row.cooking_plan or {}).get("variants") or {})


@router.post("/{plan_id}/cooking")
async def cooking_plan(
    plan_id: str, req: DetailRequest, session: SessionDep
) -> CookingPlan:
    """Единый оптимизированный план готовки по всем блюдам недели (лениво, кэш в плане).

    action=open — вернуть активный вариант (сгенерить первый, если ещё нет);
    action=select — сделать recipe_model активным (сгенерить его вариант, если ещё нет);
    action=regenerate — «↻ Перегенерировать»: принудительно пересобрать вариант recipe_model
    с учётом обсуждения плана готовки в чате (запись — только после успеха).
    Кэш протухает, если поменялись блюда/рецепты (по cook_sig). Одной моделью повторно
    не генерим. Параллельные одинаковые запросы склеиваются (single-flight)."""
    action = (req.action or "open").lower()
    set_ai_context(plan_id=plan_id, endpoint="cooking", action=action)
    target = gate_for(req.recipe_model, "cooking").key  # пусто → дефолт плана готовки
    key = (plan_id, "cooking", action, target)
    return await _single_flight(
        key, lambda: _resolve_cooking_plan(plan_id, req, action, target, session)
    )


async def _resolve_cooking_plan(
    plan_id: str, req: DetailRequest, action: str, target: str, session: SessionDep
) -> CookingPlan:
    row = _get_plan(session, plan_id)
    if action == "regenerate":
        try:
            await regenerate_cooking(session, row, target)
        except LimitError as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        except AIError as exc:
            raise HTTPException(
                status_code=502, detail=f"Не удалось пересобрать план готовки: {exc}"
            ) from exc
        return to_cook_plan(row)
    # План готовки строится по РАЗВЁРНУТЫМ рецептам — догенерим шаги всем блюдам
    # моделью рецептов по умолчанию (модель готовки выбирает только сам план готовки).
    try:
        await backfill_all(session, row, need_steps=True)
    except AIError as exc:
        raise HTTPException(status_code=502, detail=f"Не удалось собрать рецепты: {exc}") from exc

    sig = cook_sig(row)
    cp = dict(row.cooking_plan or {})
    variants = dict(cp.get("variants") or {})
    if cp.get("sig") != sig:  # состав/рецепты поменялись — старые варианты протухли
        variants = {}

    if action == "select":
        active = target
    else:  # open — держим активный, если он есть; иначе генерим target как первый
        active = cp.get("active_model") or next(iter(variants), "")
        active = active if active in variants else target

    if active not in variants:  # этого варианта ещё нет — генерим (один раз на модель)
        try:
            detail = await generate_cooking_plan(list(row.dishes or []), active)
        except LimitError as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        except AIError as exc:
            raise HTTPException(
                status_code=502, detail=f"Не удалось собрать план готовки: {exc}"
            ) from exc
        variants[active] = {
            "steps": detail.get("steps") or [],
            "note": detail.get("note") or "",
            "provider": detail.get("provider") or "",
            "generated_at": now_iso(),
        }

    row.cooking_plan = {"variants": variants, "active_model": active, "sig": sig}
    session.add(row)
    session.commit()
    session.refresh(row)

    return to_cook_plan(row)


@router.get("/{plan_id}/cooking/variants")
async def cooking_variants(plan_id: str, session: SessionDep) -> list[CookingPlanVariant]:
    """Все сгенерированные варианты плана готовки (по моделям) — для сравнения."""
    row = _get_plan(session, plan_id)
    variants = _cook_variants(row)
    return [CookingPlanVariant.model_validate({"model": m, **v}) for m, v in variants.items()]


@router.get("/{plan_id}/dishes/{dish_id}/variants")
async def dish_variants(plan_id: str, dish_id: str, session: SessionDep) -> list[DishVariant]:
    """Все сгенерированные варианты рецепта блюда (по моделям) — для сравнения бок о бок."""
    row = _get_plan(session, plan_id)
    dish = next((d for d in (row.dishes or []) if d.get("id") == dish_id), None)
    if dish is None:
        raise HTTPException(status_code=404, detail="Блюдо не найдено")
    variants = variants_of(dish)
    return [DishVariant.model_validate({"model": m, **v}) for m, v in variants.items()]
