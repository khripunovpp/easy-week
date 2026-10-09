"""Генерация/перегенерация целей плана: рецепт блюда, план готовки, список покупок.

Общая логика для роутера плана (кнопка «↻ Перегенерировать», ленивые детали) и обсуждения
в чате (явная просьба «поменяй» → правка/пересборка). Здесь нет HTTP: ошибки модели
(AIError/LimitError) пробрасываются, роутер превращает их в 502/429. Пишем в БД ТОЛЬКО после
успешной генерации — при ошибке старый вариант остаётся нетронутым. Пишем через
services/planstore: только своё блюдо в свежепрочитанный план (параллельные записи целы).
"""

import asyncio
import hashlib
import json
import logging
from datetime import datetime, timezone

from sqlmodel import Session

from ..ai.gates import gate_for
from ..ai.planner import fix_recipe, generate_cooking_plan, generate_dish_detail, normalize_shopping
from ..models import PlanRow
from ..services.mapping import to_week_plan
from . import planstore, singleflight
from .discussion import discussion_text
from .history import original_request, reply_mention
from .recipestore import content_hash
from .shopping import aggregate_ingredients, sync_uses
from .variants import dish_variants, now_iso, variant_summary, with_detail

logger = logging.getLogger("easy_week.regenerate")


class DishNotFound(LookupError):
    """Блюда с таким id в плане нет (роутер → 404)."""


async def backfill_all(
    session: Session, row: PlanRow, need_steps: bool = False, model: str = ""
) -> list[dict]:
    """Догенерить детали для блюд, у которых их нет, параллельно. Кэш — вариантом рецепта
    (kind backfill) в блюде плана, как при открытии рецепта. Возвращает свежие блюда плана;
    row вызывающего перечитается при следующем обращении.
    need_steps=False (покупки: нужны только ингредиенты), True (PDF/готовка: нужны и шаги).
    model — выбранная модель рецептов (пусто → модель рецептов по умолчанию из настроек).

    Параллельные вызовы склеиваются по блюду (services/singleflight, ключ (plan_id, dish_id)):
    страница покупок шлёт /shopping-list и /shopping-list/by-dish разом, рядом PDF/готовка —
    рецепт блюда генерит ОДИН вызов модели и сам его записывает, остальные ждут и читают
    записанное (раньше — по вызову на запрос, лишние выкидывал fill, записывался случайный).
    - need_steps в ключе не нужен: деталь всегда полная (ингредиенты + шаги — один промпт),
      генерация для покупок годится и PDF/готовке;
    - model в ключе нет: задача догенерации — «у блюда есть рецепт», а не «рецепт модели X»;
      вызов с другой моделью ждёт идущую генерацию (её рецепт fill и так не перетёр бы).
      Рецепт конкретной модели — на странице блюда (выбор модели / ↻);
    - сбой генерации получают все ждущие (блюдо без рецепта, как раньше), следующий вызов
      пробует заново."""

    def lacks(d: dict) -> bool:
        return not d.get("ingredients") or (need_steps and not d.get("steps"))

    dishes = list(row.dishes or [])
    missing = [d for d in dishes if d.get("id") and lacks(d)]
    if not missing:
        return dishes
    plan_id, leftovers = row.id, row.leftovers
    request = original_request(session, row.conversation_id)  # фон: исходный запрос беседы

    async def generate_and_save(d: dict) -> None:
        # Рецепт этого блюда уже генерит страница блюда (open/select) — ждём её, а не зовём
        # модель второй раз; не дописала (сбой) — генерим сами.
        joined = await singleflight.join(
            lambda k: isinstance(k, tuple) and len(k) >= 3 and k[0] == plan_id
            and k[1] == d["id"] and k[2] in ("open", "select"),
            own=("backfill", plan_id, d["id"]),
        )
        if joined:
            now, _ = planstore.read_dishes(session, plan_id)
            cur = next((x for x in now if x.get("id") == d["id"]), None)
            if cur is None or not lacks(cur):
                logger.info("backfill: «%s» дописала страница блюда — свою генерацию не начинаем",
                            d.get("name"))
                return
        det = await generate_dish_detail(
            d.get("name", ""), d.get("servings", 4), model=model, dish=d, request=request,
            mention=reply_mention(session, plan_id, d.get("name", "")), leftovers=leftovers,
        )

        def fill(cur: dict) -> dict | None:
            # Пока генерили, рецепт могли открыть или перегенерить — свежий не перетираем.
            if not lacks(cur):
                return None
            # Пустой note модели не стирает заметку о хранении из плана (спека пайплайна
            # Cloudflare; её печатает PDF «Хранение: …») — догенерация её всегда сохраняла.
            note = det.get("note") or (cur.get("storage") or {}).get("note") or ""
            # Слот варианта — модель, реально писавшая рецепт (detail["model"]).
            new = with_detail(cur, det["model"], {**det, "note": note}, kind="backfill", basis=d)
            return sync_uses(new, leftovers)

        # Пишем сразу, внутри склеенной задачи: ждущие соседи читают уже записанное, а
        # пришедший после конца генерации видит рецепт в БД и новую не начинает.
        planstore.patch_dishes(session, plan_id, {d["id"]: fill})

    async def flight(d: dict) -> None:
        key = ("backfill", plan_id, d["id"])
        if singleflight.running(key):
            logger.info("backfill: «%s» уже генерится соседним запросом — ждём его (plan=%s)",
                        d.get("name"), plan_id)
        await singleflight.single_flight(key, lambda: generate_and_save(d))

    results = await asyncio.gather(*(flight(d) for d in missing), return_exceptions=True)
    for d, res in zip(missing, results):
        if isinstance(res, (planstore.PlanNotFound, planstore.PlanConflict)):
            raise res  # план удалили / запись не прошла — роутер отдаст 404/409, как раньше
        if isinstance(res, BaseException):
            logger.warning("backfill: «%s» без рецепта: %s", d.get("name"), str(res)[:150])
    # Рецепты могли записать и наш запрос, и соседний (в своей сессии) — читаем из БД.
    return planstore.reread(session, plan_id)


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
    dish = next((d for d in (row.dishes or []) if d.get("id") == dish_id), None)
    if dish is None:
        raise DishNotFound(dish_id)
    plan_id, leftovers = row.id, row.leftovers
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
        dish=dish, request=request, mention=reply_mention(session, plan_id, name),
        discussion=discussion, current=variant_summary(current), regenerate=regenerate,
        leftovers=leftovers,
    )
    key = detail["model"]  # слот — модель, реально писавшая рецепт
    # ↻ (в т.ч. с уточнением «Что учесть?») или правка из обсуждения — в метаданные варианта.
    kind = "regenerate" if regenerate else "discuss_edit"

    def apply(cur: dict) -> dict:
        new = sync_uses(
            with_detail(cur, key, detail, kind=kind, change=change, basis=dish), leftovers
        )
        # Свой рецепт: уточнение («соус на сливках») — часть рецепта, дописываем к тексту
        # пользователя, чтобы следующие перегенерации и другие модели его не теряли
        # (prompt._source_block).
        if change and new.get("source"):
            new["source"] = f"{new['source'].rstrip()}\n\nУточнение: {change}"
        return new

    dishes = planstore.patch_dishes(session, plan_id, {dish_id: apply})
    new = next((d for d in dishes if d.get("id") == dish_id), None)
    if new is None:
        raise DishNotFound(dish_id)
    logger.info("dish regenerated: plan=%s dish=%s model=%s kind=%s change=%s", plan_id, dish_id,
                key, kind, bool(change))
    return new


class NoRecipe(LookupError):
    """У блюда ещё нет рецепта — исправлять нечего."""


class FixNothing(ValueError):
    """Модель не нашла, что менять (продукта в рецепте нет / просьба непонятна)."""


async def fix_dish(
    session: Session, row: PlanRow, dish_id: str, request: str, model: str = ""
) -> tuple[dict, str]:
    """«Исправить»: точечная правка активного варианта рецепта (убрать/заменить продукт) —
    модель задачи «Правка рецепта» отдаёт только изменения строк, текст остального рецепта
    остаётся дословно. Новая версия — в тот же слот (модель, писавшая рецепт), kind fix,
    change = просьба, fix_ref — модель правки. Пока правили, рецепт поменяли (↻ на другом
    устройстве) — PlanConflict (409), ничего не пишем. Возвращает (блюдо, reply модели)."""
    dish = next((d for d in (row.dishes or []) if d.get("id") == dish_id), None)
    if dish is None:
        raise DishNotFound(dish_id)
    variants = dish_variants(dish)
    slot = dish.get("active_model") if dish.get("active_model") in variants else next(iter(variants), "")
    if not slot or not dish.get("ingredients"):
        raise NoRecipe(dish_id)
    plan_id, leftovers = row.id, row.leftovers
    base = variants[slot]
    fixed = await fix_recipe(dish, request, model)
    if not fixed["changed"]:
        raise FixNothing(fixed["reply"] or "Модель не нашла, что поменять в рецепте.")
    detail = {**fixed, "provider": base.get("provider") or dish.get("detail_provider") or "",
              "model_ref": base.get("model_ref") or ""}
    change = request.strip()

    def apply(cur: dict) -> dict:
        now = dish_variants(cur).get(slot) or {}
        if content_hash(now) != content_hash(base):
            raise planstore.PlanConflict("рецепт изменился, пока его правили — повторите правку")
        new = sync_uses(
            with_detail(cur, slot, detail, kind="fix", change=change, basis=dish), leftovers
        )
        if new.get("source"):  # свой рецепт: правка — часть рецепта (как уточнение ↻)
            new["source"] = f"{new['source'].rstrip()}\n\nУточнение: {change}"
        return new

    dishes = planstore.patch_dishes(session, plan_id, {dish_id: apply})
    new = next((d for d in dishes if d.get("id") == dish_id), None)
    if new is None:
        raise DishNotFound(dish_id)
    logger.info("dish fixed: plan=%s dish=%s slot=%s by=%s changes=%d", plan_id, dish_id, slot,
                fixed["fix_ref"], fixed["changed"])
    return new, fixed["reply"]


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
    planstore.patch_cooking(session, row.id, sig, key, {
        "steps": detail.get("steps") or [],
        "note": detail.get("note") or "",
        "provider": detail.get("provider") or "",
        "generated_at": now_iso(),
    })
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
    row.shopping_model = gate_for(model, "shopping").key
    session.add(row)
    session.commit()
    logger.info(
        "shopping regenerated: plan=%s model=%s items=%d",
        row.id, gate_for(model, "shopping").key, len(items),
    )
    return items
