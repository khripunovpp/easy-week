"""Единственный путь записи блюд плана (planrow.dishes) и кэша плана готовки (cooking_plan).

Зачем: генерация рецепта идёт десятки секунд, а раньше каждая точка записи брала список блюд
ДО вызова модели и после него записывала его целиком. Параллельная запись того же плана
(↻ одного блюда во время догенерации для покупок, свой рецепт во время ↻ своего рецепта,
рецепт в родителе, пока модель правит план в чате) молча терялась. Здесь:

- patch_dishes(session, plan_id, changes={dish_id: fn}, append=…) — перечитывает строку из БД
  прямо перед записью (мимо identity map сессии; между чтением и записью нет await, поэтому
  внутри процесса никто не вклинится), меняет только свои блюда (по id) и пишет
  UPDATE … WHERE coalesce(dishes_version, 0) = прочитанной. Строку сдвинул другой процесс
  (dev-uvicorn на той же базе) — перечитываем и пробуем снова, после 3 попыток PlanConflict
  (роутер → 409);
- new_row(…) — новая строка плана (новый план чата, версия после правки, «Мои рецепты»);
- carry_current(…) — версия после правки берёт у родителя его ТЕКУЩИЕ блюда, которые правка
  не трогала;
- patch_cooking(…) — вариант плана готовки вливается в свежепрочитанный кэш (две модели,
  собранные параллельно, не затирают друг друга).

Когда миграция рецептов применена (маркер recipes_v1), patch_dishes и new_row в той же
транзакции закрепляют блюда за версиями в таблицах рецептов (services/recipestore.dual_write —
в SAVEPOINT: сбой таблиц запись JSON не роняет). Читается пока только JSON.

Присваивать row.dishes / row.cooking_plan и передавать PlanRow(dishes=…) вне этого модуля
нельзя — это ловит tests/test_planstore.py::test_no_direct_plan_writes.
"""

import copy
import logging
from collections.abc import Callable

from sqlalchemy import func, update
from sqlalchemy.orm.util import identity_key
from sqlmodel import Session, select

from ..models import PlanRow
from . import recipestore
from .shopping import sync_uses

logger = logging.getLogger("easy_week.planstore")

_ATTEMPTS = 3

# fn(свежее блюдо) → новое блюдо; None (или то же содержимое) — менять нечего. fn получает
# копию и при повторной попытке вызывается заново, поэтому должна быть без побочных эффектов
# (кроме запоминания результата в замыкании).
DishFn = Callable[[dict], dict | None]


class PlanConflict(RuntimeError):
    """Строку плана всё время перезаписывает кто-то ещё — запись не прошла (HTTP 409)."""


class PlanNotFound(LookupError):
    """План удалили, пока шла генерация (HTTP 404)."""


def read_dishes(session: Session, plan_id: str) -> tuple[list[dict], int]:
    """Блюда плана и их версия прямо из БД. Выборка колонок, а не объекта: identity map
    сессии могла держать строку, загруженную до вызова модели."""
    got = session.exec(
        select(PlanRow.dishes, PlanRow.dishes_version).where(PlanRow.id == plan_id)
    ).first()
    if got is None:
        raise PlanNotFound(plan_id)
    dishes, version = got
    return list(dishes or []), int(version or 0)


def _apply(
    current: list[dict],
    changes: dict[str, DishFn],
    append: list[dict] | Callable[[list[dict]], list[dict]] | None,
) -> tuple[list[dict], bool]:
    new = list(current)
    touched = False
    seen: set[str] = set()
    for i, dish in enumerate(new):
        did = dish.get("id")
        fn = changes.get(did) if did not in seen else None
        if fn is None:
            continue
        seen.add(did)
        out = fn(copy.deepcopy(dish))
        if out is None or out == dish:
            continue
        new[i] = out
        touched = True
    missing = set(changes) - seen
    if missing:
        logger.warning("в плане нет блюд %s — их правку пропускаем", sorted(missing))
    if append is not None:
        extra = append(copy.deepcopy(new)) if callable(append) else list(append)
        if extra:
            new.extend(extra)
            touched = True
    return new, touched


def _expire(session: Session, plan_id: str) -> None:
    # Строку плана в identity map — перечитать при следующем обращении. Нужно и без записи:
    # вызывающий держит row, загруженную ДО вызова модели, и дальше строит по ней покупки/PDF/
    # план готовки, а свежие блюда мог записать другой запрос (read_dishes — выборка колонок,
    # объект она не обновляет). После commit — на случай сессии без expire_on_commit.
    obj = session.identity_map.get(identity_key(PlanRow, plan_id))
    if obj is not None:
        session.expire(obj)


def reread(session: Session, plan_id: str) -> list[dict]:
    """Текущие блюда плана из БД, без записи; PlanRow этой сессии перечитается при следующем
    обращении. Для того, кто ждал чужую запись: склеенная догенерация (services/regenerate) —
    рецепт записал соседний запрос в своей сессии, а row вызывающего загружена до этого."""
    dishes, _ = read_dishes(session, plan_id)
    _expire(session, plan_id)
    return dishes


def patch_dishes(
    session: Session,
    plan_id: str,
    changes: dict[str, DishFn] | None = None,
    append: list[dict] | Callable[[list[dict]], list[dict]] | None = None,
) -> list[dict]:
    """Точечная запись блюд плана с compare-and-swap. Возвращает записанный (или текущий,
    если менять нечего) список блюд; в обоих случаях PlanRow этой сессии перечитается при
    следующем обращении — вызывающий может дальше читать свою row.

    changes — {dish_id: fn}: fn получает свежее блюдо и возвращает новое (None — не менять;
    например, догенерация видит, что рецепт уже успели открыть). append — блюда в конец
    списка или fn(свежий список) → блюда (id нового блюда зависит от текущих). Коммитит сам."""
    changes = changes or {}
    for attempt in range(1, _ATTEMPTS + 1):
        current, version = read_dishes(session, plan_id)
        new, touched = _apply(current, changes, append)
        if not touched:
            # Менять нечего — чаще всего потому, что блюдо уже записал другой запрос, пока
            # этот ждал модель: row вызывающего тогда устарела (см. _expire).
            _expire(session, plan_id)
            return current
        res = session.exec(
            update(PlanRow)
            .where(PlanRow.id == plan_id, func.coalesce(PlanRow.dishes_version, 0) == version)
            .values(dishes=new, dishes_version=version + 1)
            .execution_options(synchronize_session=False)
        )
        if res.rowcount == 1:
            # UPDATE уже открыл транзакцию — двойная запись в таблицы рецептов идёт в ней же
            # (SAVEPOINT) и дописывает в блюда закрепления recipe_id/rev_ids.
            new = recipestore.dual_write(session, plan_id, new)
            session.commit()
            _expire(session, plan_id)
            if changes:
                _carry_forward(session, plan_id, current, new, set(changes))
            return new
        logger.warning(
            "план %s: блюда записали параллельно (версия %d) — перечитываем, попытка %d/%d",
            plan_id, version, attempt, _ATTEMPTS,
        )
    raise PlanConflict(f"план {plan_id} одновременно меняется в другом месте")


# Поля блюда, которые у версий плана свои: закрепления в таблицах рецептов и остатки (uses
# считаются по остаткам своей версии). Остальное у нетронутого правкой блюда — одинаковое.
_OWN_FIELDS = ("recipe_id", "rev_ids", "uses")


def _same_dish(a: dict, b: dict) -> bool:
    def strip(d: dict) -> dict:
        return {k: v for k, v in d.items() if k not in _OWN_FIELDS}

    return strip(a) == strip(b)


def _carry_forward(
    session: Session, plan_id: str, before: list[dict], after: list[dict], ids: set[str]
) -> None:
    """Правка блюда в версии плана, у которой уже есть более новые (правка в чате, часто — с
    другого устройства: там добавили блюдо, а здесь открыт рецепт старой версии и нажат ↻),
    переезжает в те новые версии, где это блюдо осталось нетронутым — иначе ↻ «откатывался»:
    рецепт без болгарского перца жил в отменённой версии, а покупки шли по новой. Блюдо, которое
    в новой версии уже другое (заменили, правили), не трогаем. Внуки — тем же путём
    (patch_dishes версии-ребёнка сам несёт правку дальше)."""
    old = {d.get("id"): d for d in before}
    new = {d.get("id"): d for d in after}
    moved = {
        did: (old[did], new[did]) for did in ids
        if did in old and did in new and old[did] != new[did]
    }
    if not moved:
        return
    children = session.exec(
        select(PlanRow.id, PlanRow.leftovers).where(PlanRow.parent_id == plan_id)
    ).all()
    for child_id, child_leftovers in children:
        def carry(was: dict, now: dict, lo):
            def fn(cur: dict) -> dict | None:
                if not _same_dish(cur, was):
                    return None
                out = {**now, "uses": cur.get("uses")} if "uses" in cur else dict(now)
                return sync_uses(out, lo)
            return fn

        try:
            patch_dishes(session, child_id, {
                did: carry(was, now, child_leftovers) for did, (was, now) in moved.items()
            })
            logger.info("план %s: правку блюд %s перенесли в новую версию %s",
                        plan_id, sorted(moved), child_id)
        except (PlanConflict, PlanNotFound) as exc:
            logger.warning("план %s: правку блюд в новую версию %s не перенесли: %s",
                           plan_id, child_id, exc)


def new_row(session: Session, *, dishes: list[dict], **fields) -> PlanRow:
    """Новая строка плана (добавляется в сессию; коммит — вместе с сообщениями у вызывающего).
    С миграцией рецептов строка сразу уходит в базу (flush) и её блюда закрепляются за
    версиями — в той же транзакции вызывающего."""
    row = PlanRow(dishes=list(dishes), dishes_version=0, **fields)
    session.add(row)
    recipestore.dual_write_new(session, row)
    return row


def carry_current(
    snapshot: list[dict], result: list[dict], current: list[dict]
) -> list[dict]:
    """Блюда новой версии плана после правки в чате.

    snapshot — блюда родителя ДО вызова модели (глубокая копия), result — что вернула правка,
    current — блюда родителя СЕЙЧАС (read_dishes прямо перед созданием версии). Блюдо, которое
    правка вернула нетронутым (равно снимку), берём из current: пока модель правила план,
    в родителе могли открыть рецепт или сделать ↻ — иначе новая версия теряла этот вариант.
    Изменённые, новые и пересобранные блюда — как вернула правка."""
    before = {d.get("id"): d for d in snapshot if d.get("id")}
    now = {d.get("id"): d for d in current if d.get("id")}
    out: list[dict] = []
    for d in result:
        did = d.get("id")
        if did in before and did in now and d == before[did]:
            out.append(now[did])
        else:
            out.append(d)
    return out


def patch_cooking(
    session: Session, plan_id: str, sig: str, model: str, variant: dict | None = None
) -> dict:
    """Записать вариант плана готовки model (variant=None — только сделать его активным) в
    свежепрочитанный кэш: варианты других моделей с той же подписью состава остаются.
    Возвращает записанный кэш (или текущий, если менять нечего)."""
    got = session.exec(
        select(PlanRow.id, PlanRow.cooking_plan).where(PlanRow.id == plan_id)
    ).first()
    if got is None:
        raise PlanNotFound(plan_id)
    cp = dict(got[1] or {})
    variants = dict(cp.get("variants") or {}) if cp.get("sig") == sig else {}
    if variant is not None:
        variants[model] = variant
    if model not in variants:
        return cp
    new = {"variants": variants, "active_model": model, "sig": sig}
    if new == cp:
        return cp
    session.exec(
        update(PlanRow)
        .where(PlanRow.id == plan_id)
        .values(cooking_plan=new)
        .execution_options(synchronize_session=False)
    )
    session.commit()
    _expire(session, plan_id)
    return new
