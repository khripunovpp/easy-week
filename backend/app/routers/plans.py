from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlmodel import Session, select

from ..ai.base import AIError
from ..ai.gates import gate_for
from ..ai.limits import LimitError
from ..ai.observe import set_ai_context
from ..ai.planner import (
    generate_cooking_plan,
    generate_dish_detail,
    normalize_shopping,
    parse_shopping_extras,
)
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
    ShoppingExtrasBody,
    ShoppingGroup,
    ShoppingItem,
    StatusRequest,
    WeekPlan,
)
from ..services import planstore
from ..services import settings as app_settings
from ..services.export_pdf import build_plan_pdf
from ..services.history import original_request, reply_mention
from ..services.mapping import to_cook_plan, to_dish, to_summary, to_week_plan
from ..services.recipebook import LIBRARY_ID, LIBRARY_STATUS
from ..services.regenerate import (
    DishNotFound,
    backfill_all,
    cook_sig,
    regenerate_cooking,
    regenerate_dish,
    regenerate_shopping,
    shopping_base,
)
from ..services.shopping import aggregate_ingredients, group_items, merge_extras, sync_uses
from ..services.singleflight import single_flight
from ..services.variants import apply_variant, now_iso, parent_key, variant_from_detail
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


def _not_library(row: PlanRow) -> None:
    """«Мои рецепты» — служебный план, а не план недели: удалить/переименовать/принять его
    нельзя (удаление стёрло бы все свои рецепты разом)."""
    if row.id == LIBRARY_ID or row.status == LIBRARY_STATUS:
        raise HTTPException(status_code=409, detail="«Мои рецепты» — не план недели")


@router.get("")
async def list_plans(session: SessionDep) -> list[PlanSummary]:
    rows = session.exec(select(PlanRow).order_by(PlanRow.created_at.desc())).all()
    # Промежуточные версии, у которых уже есть более новая (правка в чате), в списке не
    # показываем — только последнюю. Ссылки на них по-прежнему открываются.
    superseded = {r.parent_id for r in rows if r.parent_id}
    # Версии, заменённые правкой (черновик или авто-«отклонён» при правке), тоже прячем —
    # иначе после «убрать блюдо» в списке висели две «Соляночки»: старая и новая.
    # «Мои рецепты» (status library) — не план недели: живут в режиме «Рецепты».
    visible = [
        r for r in rows
        if r.status != "library"
        and not (r.status in ("draft", "rejected") and r.id in superseded)
    ]
    return [to_summary(r) for r in visible]


@router.get("/{plan_id}")
async def get_plan(plan_id: str, session: SessionDep) -> WeekPlan:
    return to_week_plan(_get_plan(session, plan_id))


@router.delete("/{plan_id}", status_code=204)
async def delete_plan(plan_id: str, session: SessionDep) -> None:
    row = _get_plan(session, plan_id)
    _not_library(row)
    session.delete(row)
    session.commit()


@router.patch("/{plan_id}")
async def rename_plan(plan_id: str, req: RenameRequest, session: SessionDep) -> WeekPlan:
    """Переименование плана пользователем (долгое нажатие на заголовок на странице плана)."""
    title = " ".join(req.title.split())  # схлопываем переносы/лишние пробелы из contenteditable
    if not title:
        raise HTTPException(status_code=422, detail="Пустое название")
    row = _get_plan(session, plan_id)
    _not_library(row)
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
    _not_library(row)
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

    # Один вызов модели на план; дальше — из кэша. Кэш модели, которую сняли с задачи
    # (OpenRouter путал количества), — пересобрать текущей.
    cached_ok = not row.shopping_model or app_settings.allowed("shopping", row.shopping_model)
    if row.shopping_sig == sig and row.shopping_cache and cached_ok:
        return group_items(row.shopping_cache, row.leftovers, row.shopping_extras)

    try:
        items = await normalize_shopping(base)  # модель — дефолт «Список покупок» из настроек
    except Exception as exc:  # noqa: BLE001 — нормализация не критична: отдаём базу
        # Базу под этой подписью НЕ кэшируем — иначе сбой нормализации «застывал» навсегда
        # (кэш совпадает по sig, повторной попытки не было бы).
        logger.warning("shopping normalize failed, отдаём базу без кэша: %s", str(exc)[:150])
        return group_items(base, row.leftovers, row.shopping_extras)
    if not items:
        return group_items(base, row.leftovers, row.shopping_extras)
    row.shopping_cache = items
    row.shopping_sig = sig
    row.shopping_at = datetime.now(timezone.utc)
    row.shopping_model = gate_for("", "shopping").key  # GET нормализует дефолтом из настроек
    session.add(row)
    session.commit()
    return group_items(items, row.leftovers, row.shopping_extras)


@router.get("/{plan_id}/shopping-list/by-dish")
async def shopping_by_dish(plan_id: str, session: SessionDep) -> list[DishShopping]:
    """Покупки по рецептам: для каждого блюда — его ингредиенты (слиты формы одного продукта,
    единицы приведены), без вызова модели. Группировку по категориям внутри блюда делает фронт."""
    set_ai_context(plan_id=plan_id, endpoint="shopping_list")
    row = _get_plan(session, plan_id)
    await backfill_all(session, row)  # ингредиенты лениво — догрузить (модель рецептов)
    out: list[DishShopping] = []
    for dish in to_week_plan(row).dishes:
        items = [
            it for g in group_items(aggregate_ingredients([dish]), row.leftovers) for it in g.items
        ]
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
        items = await single_flight(
            (plan_id, "shopping", "regenerate"),
            lambda: regenerate_shopping(session, row, req.recipe_model),
        )
    except LimitError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except AIError as exc:
        raise HTTPException(
            status_code=502, detail=f"Не удалось пересобрать список покупок: {exc}"
        ) from exc
    return group_items(items, row.leftovers, row.shopping_extras)


def _extras_out(extras: list[dict] | None) -> list[ShoppingItem]:
    return [it for g in group_items([], extras=extras) for it in g.items]


@router.post("/{plan_id}/shopping-list/extras")
async def shopping_extras_add(
    plan_id: str, req: ShoppingExtrasBody, session: SessionDep
) -> list[ShoppingItem]:
    """Свои товары мимо рецептов («ещё хлеб, йогурт 2 шт»): текст разбирает модель списка
    покупок (recipe_model — выпадашка страницы; пусто → дефолт задачи) по отделам, позиции
    добавляются к уже добавленным (тот же продукт — количество складывается). Возвращает все
    свои товары плана. Сбой модели — 502, ничего не пишем (без подмены моделью)."""
    set_ai_context(plan_id=plan_id, endpoint="shopping_list", action="extras")
    row = _get_plan(session, plan_id)
    _not_library(row)
    try:
        new = await parse_shopping_extras(req.text, req.recipe_model)
    except LimitError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except AIError as exc:
        raise HTTPException(status_code=502, detail=f"Не удалось разобрать список: {exc}") from exc
    # Пока модель думала, мог добавить кто-то ещё из семьи — сливаем со свежими из базы
    # (дальше до коммита await нет).
    session.refresh(row, attribute_names=["shopping_extras"])
    row.shopping_extras = merge_extras(row.shopping_extras, new)
    session.add(row)
    session.commit()
    logger.info("shopping extras: plan=%s +%d → %d", plan_id, len(new), len(row.shopping_extras))
    return _extras_out(row.shopping_extras)


@router.delete("/{plan_id}/shopping-list/extras/{item_id}")
async def shopping_extras_delete(
    plan_id: str, item_id: str, session: SessionDep
) -> list[ShoppingItem]:
    """Убрать свой товар из списка покупок (повторное удаление — не ошибка)."""
    row = _get_plan(session, plan_id)
    row.shopping_extras = [
        it for it in (row.shopping_extras or []) if it.get("id") != item_id
    ] or None
    session.add(row)
    session.commit()
    return _extras_out(row.shopping_extras)


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
    groups = (
        group_items(aggregate_ingredients(plan.dishes), row.leftovers, row.shopping_extras)
        if shopping else []
    )
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


# Склейка одинаковых запросов генерации — services/singleflight: пока идёт генерация рецепта
# для (plan_id, dish_id, action, model), параллельные такие же запросы ждут ТОТ ЖЕ результат —
# без повторного вызова модели и двойной записи в БД.


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
    key = (plan_id, dish_id, action, gate_for(req.recipe_model, "recipe").key, req.note.strip())
    return await single_flight(
        key, lambda: _resolve_dish_detail(plan_id, dish_id, req, action, session)
    )


async def _resolve_dish_detail(
    plan_id: str, dish_id: str, req: DetailRequest, action: str, session: SessionDep
) -> Dish:
    row = _get_plan(session, plan_id)
    dish = next((d for d in (row.dishes or []) if d.get("id") == dish_id), None)
    if dish is None:
        raise HTTPException(status_code=404, detail="Блюдо не найдено")
    leftovers = row.leftovers

    variants = variants_of(dish)
    explicit = (req.recipe_model or "").strip().lower()
    # Реальный ключ: явная модель, если годится для рецептов (карта TASK_MODELS), иначе дефолт.
    # Переключение на УЖЕ сгенерированный вариант карте не подчиняется — старые варианты
    # (напр. Cloudflare) остаются открываемыми, просто новых такой моделью не делаем.
    resolved = explicit if explicit in variants else gate_for(req.recipe_model, "recipe").key

    if action == "regenerate":
        try:
            # Уточнение из окна «Что учесть?» — обязательная правка этого варианта.
            new = await regenerate_dish(session, row, dish_id, resolved, change=req.note.strip())
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

    fresh = None
    if target not in variants:  # этого варианта ещё нет — генерим (один раз на модель)
        try:
            detail = await generate_dish_detail(
                dish.get("name", ""), dish.get("servings", 4), model=target,
                dish=dish, request=original_request(session, row.conversation_id),
                mention=reply_mention(session, row.id, dish.get("name", "")),
                leftovers=leftovers,
            )
        except LimitError as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        except AIError as exc:
            raise HTTPException(status_code=502, detail=f"Не удалось получить рецепт: {exc}") from exc
        target = detail["model"]  # слот — модель, реально писавшая рецепт
        fresh = variant_from_detail(
            detail, kind="generate", parent_id=parent_key(dish, target), ctx_uses=dish.get("uses"),
        )

    kept = {"meanwhile": False}

    def apply(cur: dict) -> dict | None:
        # Свежее блюдо из БД: за время генерации у него могли появиться другие варианты.
        cur_variants = variants_of(cur)
        # Генерим, только если слота не было в снимке, — значит, слот в свежем блюде появился,
        # пока ждали модель (↻ с «Что учесть?», правка из обсуждения, догенерация). Он
        # побеждает: одной моделью рецепт не генерим дважды, а явный ↻ пользователя не теряем.
        kept["meanwhile"] = fresh is not None and target in cur_variants
        if target not in cur_variants:
            if fresh is None:
                return None
            cur_variants[target] = fresh
        # uses — по фактическим ингредиентам варианта (карточка плана показывает правду).
        return sync_uses(apply_variant(cur, target, cur_variants), leftovers)

    dishes = planstore.patch_dishes(session, plan_id, {dish_id: apply})
    if kept["meanwhile"]:
        logger.info("рецепт %s/%s: вариант %s записали, пока генерили, — свой не пишем",
                    plan_id, dish_id, target)
    dish = next((d for d in dishes if d.get("id") == dish_id), None)
    if dish is None:
        raise HTTPException(status_code=404, detail="Блюдо не найдено")
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
    explicit = (req.recipe_model or "").strip().lower()
    # Явная модель, если годится для плана готовки (карта TASK_MODELS), иначе дефолт задачи.
    # Уже собранный вариант (напр. старый Cloudflare) остаётся переключаемым — см. ниже.
    row = _get_plan(session, plan_id)
    if explicit in _cook_variants(row) and (row.cooking_plan or {}).get("sig") == cook_sig(row):
        target = explicit
    else:
        target = gate_for(req.recipe_model, "cooking").key  # пусто → дефолт плана готовки
    key = (plan_id, "cooking", action, target)
    return await single_flight(
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

    fresh = None
    if active not in variants:  # этого варианта ещё нет — генерим (один раз на модель)
        try:
            detail = await generate_cooking_plan(list(row.dishes or []), active)
        except LimitError as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        except AIError as exc:
            raise HTTPException(
                status_code=502, detail=f"Не удалось собрать план готовки: {exc}"
            ) from exc
        fresh = {
            "steps": detail.get("steps") or [],
            "note": detail.get("note") or "",
            "provider": detail.get("provider") or "",
            "generated_at": now_iso(),
        }

    # Вливаем в свежепрочитанный кэш: вариант другой модели, собранный параллельно, цел.
    planstore.patch_cooking(session, plan_id, sig, active, fresh)
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
