"""Генерация/перегенерация целей плана: рецепт блюда, план готовки, список покупок.

Общая логика для роутера плана (кнопка «↻ Перегенерировать», ленивые детали) и обсуждения
в чате (явная просьба «поменяй» → правка/пересборка). Здесь нет HTTP: ошибки модели
(AIError/LimitError) пробрасываются, роутер превращает их в 502/429. Пишем в БД ТОЛЬКО после
успешной генерации — при ошибке старый вариант остаётся нетронутым.
"""

import asyncio
import hashlib
import json
import logging
from datetime import datetime, timezone

from sqlmodel import Session

from ..ai.gates import gate_for
from ..ai.planner import generate_cooking_plan, generate_dish_detail, normalize_shopping
from ..models import PlanRow
from ..services.mapping import to_week_plan
from .discussion import discussion_text
from .history import original_request, reply_mention
from .shopping import aggregate_ingredients
from .variants import dish_variants, now_iso, variant_summary, with_detail

logger = logging.getLogger("easy_week.regenerate")


class DishNotFound(LookupError):
    """Блюда с таким id в плане нет (роутер → 404)."""


def merge_detail(dish: dict, detail: dict) -> dict:
    """Вливает ленивую деталь (ингредиенты/шаги/советы/note) в блюдо (плоские поля)."""
    d = {
        **dish,
        "ingredients": detail.get("ingredients") or [],
        "steps": detail.get("steps") or [],
        "tips": detail.get("tips") or [],
        "detail_provider": detail.get("provider") or "",
        "detail_generated_at": now_iso(),
    }
    if detail.get("note"):
        d["storage"] = {**(dish.get("storage") or {}), "note": detail["note"]}
    return d


async def backfill_all(
    session: Session, row: PlanRow, need_steps: bool = False, model: str = ""
) -> list[dict]:
    """Догенерить детали для блюд, у которых их нет, параллельно. Кэш в row.dishes.
    need_steps=False (покупки: нужны только ингредиенты), True (PDF/готовка: нужны и шаги).
    model — выбранная модель рецептов (пусто → модель рецептов по умолчанию из настроек)."""
    dishes = list(row.dishes or [])
    missing = [
        (i, d)
        for i, d in enumerate(dishes)
        if not d.get("ingredients") or (need_steps and not d.get("steps"))
    ]
    if not missing:
        return dishes
    request = original_request(session, row.conversation_id)  # фон: исходный запрос беседы
    results = await asyncio.gather(
        *(
            generate_dish_detail(
                d.get("name", ""), d.get("servings", 4), model=model, dish=d, request=request,
                mention=reply_mention(session, row.id, d.get("name", "")),
            )
            for _, d in missing
        ),
        return_exceptions=True,
    )
    changed = False
    for (i, d), det in zip(missing, results):
        if isinstance(det, dict):
            dishes[i] = merge_detail(d, det)
            changed = True
    if changed:
        row.dishes = dishes
        session.add(row)
        session.commit()
        session.refresh(row)
    return list(row.dishes or [])


def cook_sig(row: PlanRow) -> str:
    """Подпись СОСТАВА плана (набор блюд) для кэша готовки. Меняется только при
    добавлении/удалении/замене блюда — НЕ при переключении варианта рецепта отдельного
    блюда (иначе план готовки пересобирался бы после каждого касания рецептов)."""
    names = sorted(str(d.get("name", "")) for d in (row.dishes or []))
    return hashlib.md5(json.dumps(names, ensure_ascii=False).encode()).hexdigest()


def shopping_base(row: PlanRow) -> tuple[list[dict], str]:
    """Детерминированная база списка покупок + её подпись (ключ кэша нормализации)."""
    base = aggregate_ingredients(to_week_plan(row).dishes)
    sig = hashlib.md5(json.dumps(base, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return base, sig


async def regenerate_dish(
    session: Session,
    row: PlanRow,
    dish_id: str,
    model: str = "",
    *,
    change: str = "",
    regenerate: bool = True,
) -> dict:
    """Новый вариант рецепта блюда моделью model с учётом обсуждения рецепта в чате.

    regenerate=True — кнопка «↻»: есть пожелания — применить, нет — заметно другой вариант.
    change — явная правка (из обсуждения). Пишем variants[модель] + active_model только после
    успеха; при AIError блюдо в БД не трогаем. Возвращает обновлённое блюдо (dict)."""
    dishes = list(row.dishes or [])
    idx = next((i for i, d in enumerate(dishes) if d.get("id") == dish_id), None)
    if idx is None:
        raise DishNotFound(dish_id)
    dish = dishes[idx]
    key = gate_for(model, "recipe").key
    name = str(dish.get("name", ""))
    request = original_request(session, row.conversation_id)
    discussion = discussion_text(
        session, row.conversation_id, "recipe", dish_id, name, skip_first_user=request
    )
    # Выжимка того варианта, который перегенерим (он же обычно открыт на экране).
    current = dish_variants(dish).get(key) or dish
    detail = await generate_dish_detail(
        name, dish.get("servings", 4), change, key,
        dish=dish, request=request, mention=reply_mention(session, row.id, name),
        discussion=discussion, current=variant_summary(current), regenerate=regenerate,
    )
    new = with_detail(dish, key, detail)
    dishes[idx] = new
    row.dishes = dishes
    session.add(row)
    session.commit()
    session.refresh(row)
    logger.info("dish regenerated: plan=%s dish=%s model=%s change=%s", row.id, dish_id, key,
                bool(change))
    return new


async def regenerate_cooking(
    session: Session, row: PlanRow, model: str = "", *, regenerate: bool = True
) -> None:
    """Принудительно пересобрать план готовки моделью model с учётом обсуждения плана готовки.
    model пусто → модель плана готовки по умолчанию. Недостающие рецепты догенерит модель
    рецептов по умолчанию (не модель готовки). Вариант модели заменяется и становится
    активным — только после успеха."""
    await backfill_all(session, row, need_steps=True)
    key = gate_for(model, "cooking").key
    request = original_request(session, row.conversation_id)
    discussion = discussion_text(
        session, row.conversation_id, "cooking", skip_first_user=request
    )
    detail = await generate_cooking_plan(
        list(row.dishes or []), key, discussion=discussion, regenerate=regenerate
    )
    sig = cook_sig(row)
    cp = dict(row.cooking_plan or {})
    variants = dict(cp.get("variants") or {}) if cp.get("sig") == sig else {}
    variants[key] = {
        "steps": detail.get("steps") or [],
        "note": detail.get("note") or "",
        "provider": detail.get("provider") or "",
        "generated_at": now_iso(),
    }
    row.cooking_plan = {"variants": variants, "active_model": key, "sig": sig}
    session.add(row)
    session.commit()
    session.refresh(row)
    logger.info("cooking regenerated: plan=%s model=%s", row.id, key)


async def regenerate_shopping(session: Session, row: PlanRow, model: str = "") -> list[dict]:
    """Принудительная нормализация списка покупок мимо кэша по подписи — с учётом обсуждения
    списка в чате. model — нормализатор (пусто → модель списка покупок по умолчанию из
    настроек); недостающие ингредиенты догенерит модель рецептов по умолчанию.
    Кэш — только после успеха."""
    await backfill_all(session, row)
    base, sig = shopping_base(row)
    request = original_request(session, row.conversation_id)
    discussion = discussion_text(
        session, row.conversation_id, "shopping", skip_first_user=request
    )
    items = await normalize_shopping(base, discussion, model)
    if not items:
        return base
    row.shopping_cache = items
    row.shopping_sig = sig
    row.shopping_at = datetime.now(timezone.utc)
    session.add(row)
    session.commit()
    logger.info(
        "shopping regenerated: plan=%s model=%s items=%d",
        row.id, gate_for(model, "shopping").key, len(items),
    )
    return items
