import asyncio
import difflib
import logging
import re
from collections.abc import AsyncIterator
from datetime import date, timedelta
from typing import Any

from ..config import settings
from .cloudflare import CloudflareError, run_json
from .deepseek import DeepSeekError, deepseek_json, deepseek_stream, deepseek_tools
from .stream_parse import PlanStreamParser

_MONTHS_GEN = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]


def _week_label(today: date | None = None) -> str:
    # План всегда на СЛЕДУЮЩУЮ неделю: начинаешь чат на этой неделе — готовишь
    # в ближайшие выходные на неделю с понедельника (Пн–Вс следующей недели).
    today = today or date.today()
    monday = today - timedelta(days=today.weekday()) + timedelta(days=7)
    sunday = monday + timedelta(days=6)
    if monday.month == sunday.month:
        return f"{monday.day}–{sunday.day} {_MONTHS_GEN[sunday.month - 1]}"
    return (
        f"{monday.day} {_MONTHS_GEN[monday.month - 1]} – "
        f"{sunday.day} {_MONTHS_GEN[sunday.month - 1]}"
    )


def _clean_title(title: str) -> str:
    title = re.sub(r"\s*\([^)]*\)", "", title).strip(" .,-")
    if len(title) > 34:
        title = title[:34].rsplit(" ", 1)[0] + "…"
    return title or "План на неделю"
from .prompt import (
    DISH_DETAIL_SCHEMA,
    DISH_SCHEMA,
    EDIT_ACTION_SCHEMA,
    NAMES_SCHEMA,
    PLAN_TOOLS,
    SHOP_SCHEMA,
    VALIDATE_SCHEMA,
    build_dish_detail_messages,
    build_dish_messages,
    build_ds_plan_messages,
    build_edit_action_messages,
    build_edit_messages,
    build_names_messages,
    build_shop_normalize_messages,
    build_validate_messages,
)

logger = logging.getLogger("easy_week.planner")

# Метки провайдера — храним у плана/детали, чтобы показать в чате смену модели.
PROVIDER_DEEPSEEK = "DeepSeek"
PROVIDER_CLOUDFLARE = "Cloudflare"

# count из селектора — дефолт; явное число в сообщении важнее (см. _plan_count_hint).
# Поэтому не режем до count, а лишь ограничиваем разумным максимумом.
_MAX_DISHES = 12


def _slug(text: str, i: int) -> str:
    base = re.sub(r"[^a-zа-я0-9]+", "-", text.lower()).strip("-")
    return f"dish-{i}-{base}"[:48] or f"dish-{i}"


def _clean_name(name: str) -> str:
    # Убираем описания в скобках и лишние хвосты, которые иногда добавляет модель.
    name = re.sub(r"\s*\([^)]*\)", "", name)
    return name.strip(" .,-") or "Блюдо"


async def _gen_dish(i: int, name: str, emoji: str, user_message: str) -> dict[str, Any]:
    parsed, _ = await run_json(
        build_dish_messages(name, user_message), DISH_SCHEMA, max_tokens=800,
        label=f"спеки блюда: {name}",
    )
    return {
        "id": _slug(name, i),
        "name": name,
        "emoji": emoji or "🍽️",
        "servings": parsed.get("servings", 2),
        "prep_min": parsed.get("prep_min", 15),
        "cook_min": parsed.get("cook_min", 30),
        "tags": parsed.get("tags", []),
        "storage": parsed.get("storage")
        or {"vacuum": True, "freeze": True, "shelf_life_days": 30, "note": ""},
        "ingredients": parsed.get("ingredients", []),
        "steps": [],
        "tips": [],
    }


async def _validate_and_fix(dishes: list[dict], user_message: str) -> None:
    """Валидатор (mistral) даёт вердикты; плохие блюда перегенерирует спекер (8b)."""
    if not dishes:
        return
    try:
        parsed, _ = await run_json(
            build_validate_messages(dishes),
            VALIDATE_SCHEMA,
            model=settings.cf_model_judge,
            max_tokens=500,
            label="валидатор блюд",
        )
    except Exception as exc:  # валидатор не критичен — не роняем генерацию
        logger.warning("validator skipped: %s", str(exc)[:150])
        return

    bad: list[tuple[int, list[str]]] = []
    for r in parsed.get("results", []):
        i = r.get("index")
        if isinstance(i, int) and 0 <= i < len(dishes) and not r.get("ok"):
            bad.append((i, r.get("issues", [])))
    if not bad:
        return

    logger.info("validator: перегенерируем %d блюд", len(bad))
    fixes = await asyncio.gather(
        *(
            _gen_dish(
                i,
                dishes[i].get("name", ""),
                dishes[i].get("emoji", ""),
                f"{user_message}. Исправь ингредиенты/количества: {'; '.join(issues)}",
            )
            for i, issues in bad
        ),
        return_exceptions=True,
    )
    for (i, _), fixed in zip(bad, fixes):
        if isinstance(fixed, dict):
            dishes[i] = fixed


_DEFAULT_STORAGE = {"vacuum": True, "freeze": True, "shelf_life_days": 45, "note": ""}


def _clean_dish(i: int, d: dict) -> dict:
    name = _clean_name(str(d.get("name", f"Блюдо {i + 1}")))
    return {
        "id": _slug(name, i),
        "name": name,
        "emoji": d.get("emoji") or "🍽️",
        "servings": d.get("servings", 4),
        "prep_min": d.get("prep_min", 15),
        "cook_min": d.get("cook_min", 30),
        "tags": d.get("tags", []),
        "storage": d.get("storage") or dict(_DEFAULT_STORAGE),
        "ingredients": d.get("ingredients", []),
        "steps": d.get("steps", []),
        "tips": d.get("tips", []),
    }


async def generate_plan(
    user_message: str, avoid_titles: list[str], count: int = 5, gender: str = "f"
) -> dict[str, Any]:
    """План через DeepSeek (блюда + короткие шаги); фолбэк — пайплайн Cloudflare."""
    if settings.deepseek_configured:
        try:
            parsed = await deepseek_json(
                build_ds_plan_messages(user_message, avoid_titles, count, gender),
                max_tokens=3000,
                label=f"план: {count} блюд + короткие шаги",
            )
            dishes = [_clean_dish(i, d) for i, d in enumerate((parsed.get("dishes") or [])[:_MAX_DISHES])]
            if dishes:
                logger.info("plan via DeepSeek: dishes=%d", len(dishes))
                return {
                    "reply": parsed.get("reply") or "Готово — вот план на неделю.",
                    "title": _clean_title(parsed.get("title") or "План на неделю"),
                    "week_label": _week_label(),
                    "dishes": dishes,
                    "provider": PROVIDER_DEEPSEEK,
                }
            logger.warning("DeepSeek вернул пустой план — фолбэк на Cloudflare")
        except DeepSeekError as exc:
            logger.warning("DeepSeek недоступен (%s) — фолбэк на Cloudflare", str(exc)[:120])

    return await _generate_plan_cloudflare(user_message, avoid_titles, count, gender)


async def generate_plan_stream(
    user_message: str, avoid_titles: list[str], count: int = 5, gender: str = "f"
) -> AsyncIterator[tuple[str, Any]]:
    """Потоковый план: yield ('meta', {reply,title,week_label}) → ('dish', dish)… по одному.

    Идёт через стриминг DeepSeek с инкрементальным разбором JSON. Если DeepSeek не
    настроен/упал/не дал ни одного блюда — фолбэк на обычный generate_plan одним куском.
    """
    week = _week_label()
    meta_sent = False
    if settings.deepseek_configured:
        parser = PlanStreamParser()
        emitted = 0
        try:
            async for delta in deepseek_stream(
                build_ds_plan_messages(user_message, avoid_titles, count, gender),
                max_tokens=3000,
                label=f"план (поток): {count} блюд",
            ):
                parser.feed(delta)
                if not meta_sent:
                    meta = parser.meta()
                    if meta:
                        meta_sent = True
                        yield "meta", {
                            "reply": meta["reply"] or "Готово — вот план на неделю.",
                            "title": _clean_title(meta["title"] or "План на неделю"),
                            "week_label": week,
                            "provider": PROVIDER_DEEPSEEK,
                        }
                for d in parser.new_dishes():
                    if emitted >= _MAX_DISHES:
                        break
                    if not meta_sent:
                        meta_sent = True
                        yield "meta", {
                            "reply": "Готово — вот план на неделю.",
                            "title": "План на неделю",
                            "week_label": week,
                            "provider": PROVIDER_DEEPSEEK,
                        }
                    yield "dish", _clean_dish(emitted, d)
                    emitted += 1
            if emitted:
                logger.info("plan stream via DeepSeek: dishes=%d", emitted)
                return
            logger.warning("DeepSeek-поток пуст — фолбэк на generate_plan")
        except DeepSeekError as exc:
            if emitted:
                logger.warning("DeepSeek-поток оборвался после %d блюд: %s", emitted, str(exc)[:120])
                return
            logger.warning("DeepSeek-поток недоступен (%s) — фолбэк", str(exc)[:120])

    # Фолбэк: собираем план целиком и отдаём теми же событиями.
    data = await _generate_plan_cloudflare(user_message, avoid_titles, count, gender)
    if not meta_sent:
        yield "meta", {
            "reply": data["reply"],
            "title": data["title"],
            "week_label": data["week_label"],
            "provider": data.get("provider", PROVIDER_CLOUDFLARE),
        }
    for d in data["dishes"]:
        yield "dish", d


async def _generate_plan_cloudflare(
    user_message: str, avoid_titles: list[str], count: int = 5, gender: str = "f"
) -> dict[str, Any]:
    """Фолбэк-пайплайн: меню (mistral) → спеки (8b, параллельно) → валидация (mistral)."""
    names, _ = await run_json(
        build_names_messages(user_message, avoid_titles, count, gender),
        NAMES_SCHEMA,
        model=settings.cf_model_menu,
        max_tokens=120 + count * 110,
        label="меню (фолбэк)",
    )
    entries = (names.get("dishes") or [])[:_MAX_DISHES]  # число блюд решает модель (дефолт — count)

    dishes = list(
        await asyncio.gather(
            *(
                _gen_dish(i, _clean_name(d.get("name", f"Блюдо {i + 1}")), d.get("emoji", ""), user_message)
                for i, d in enumerate(entries)
            )
        )
    )

    await _validate_and_fix(dishes, user_message)

    logger.info("plan generated: dishes=%d (menu+specs+validate)", len(dishes))
    return {
        "reply": names.get("reply") or "Готово — вот план на неделю.",
        "title": _clean_title(names.get("title") or "План на неделю"),
        "week_label": _week_label(),
        "dishes": dishes,
        "provider": PROVIDER_CLOUDFLARE,
    }


async def generate_dish_detail(name: str, servings: int = 4, change: str = "") -> dict:
    """Полная деталь блюда (ингредиенты + шаги + советы + note) — лениво при открытии.
    change — правка рецепта (напр. «убрать болгарский перец»): перегенерирует рецепт с учётом.

    Рецепты — лучшей моделью (DeepSeek, правило CLAUDE.md); фолбэк — Cloudflare mistral."""
    label = f"деталь блюда: {name}" + (f" ({change})" if change else "")
    if settings.deepseek_configured:
        try:
            parsed = await deepseek_json(
                build_dish_detail_messages(name, servings, change),
                max_tokens=1400,
                label=label,
            )
            if parsed.get("ingredients") or parsed.get("steps"):
                return _clean_detail(parsed, PROVIDER_DEEPSEEK)
        except DeepSeekError as exc:
            logger.warning("DeepSeek деталь недоступна (%s) — фолбэк на Cloudflare", str(exc)[:120])
    parsed, _ = await run_json(
        build_dish_detail_messages(name, servings, change),
        DISH_DETAIL_SCHEMA,
        model=settings.cf_model_judge,
        max_tokens=1400,
        label=f"деталь блюда (фолбэк): {name}",
    )
    return _clean_detail(parsed, PROVIDER_CLOUDFLARE)


def _clean_detail(parsed: dict, provider: str = "") -> dict:
    return {
        "ingredients": parsed.get("ingredients") or [],
        "steps": parsed.get("steps") or [],
        "tips": parsed.get("tips") or [],
        "note": (parsed.get("note") or "").strip(),
        "provider": provider,
    }


def _dish_names(dishes: list[dict]) -> list[str]:
    return [str(d.get("name", "")) for d in dishes if d.get("name")]


def _match_index(dishes: list[dict], name: str) -> int | None:
    """Индекс блюда, лучше всего совпадающего с name (fuzzy). None — если не нашли."""
    name = (name or "").strip().lower()
    if not name:
        return None
    names = [str(d.get("name", "")).lower() for d in dishes]
    for i, n in enumerate(names):  # точное/подстрочное совпадение — в приоритете
        if n == name or name in n or n in name:
            return i
    close = difflib.get_close_matches(name, names, n=1, cutoff=0.6)
    return names.index(close[0]) if close else None


def _reid(dish: dict, i: int, existing_ids: set[str]) -> dict:
    """Присваивает блюду уникальный id, не конфликтующий с existing_ids."""
    base = _slug(dish.get("name", "блюдо"), i)
    new_id, k = base, i
    while new_id in existing_ids:
        k += 1
        new_id = _slug(dish.get("name", "блюдо"), k)
    dish = {**dish, "id": new_id}
    existing_ids.add(new_id)
    return dish


async def edit_plan(
    dishes: list[dict], title: str, user_message: str, gender: str = "f"
) -> dict[str, Any]:
    """Правит существующий план по просьбе через function calling.

    Модель выбирает функции (add/remove/replace/create); исполняем их над копией dishes,
    переиспользуя generate_plan для новых «шапок». Возвращает {reply, title, dishes, provider}."""
    work = [dict(d) for d in dishes]
    calls: list[dict[str, Any]] = []
    tool_provider = PROVIDER_DEEPSEEK
    reply_hint = ""

    if settings.deepseek_configured:
        try:
            calls, reply_hint = await deepseek_tools(
                build_edit_messages(title, _dish_names(work), user_message, gender),
                PLAN_TOOLS,
                label="правка плана (tools)",
            )
        except DeepSeekError as exc:
            logger.warning("DeepSeek tools недоступны (%s) — фолбэк на Cloudflare", str(exc)[:120])
            calls, reply_hint, tool_provider = await _edit_actions_cloudflare(
                title, work, user_message
            )
    else:
        calls, reply_hint, tool_provider = await _edit_actions_cloudflare(title, work, user_message)

    providers = {tool_provider}
    changed: list[str] = []
    new_title = title

    for call in calls:
        op, args = call.get("name", ""), call.get("args", {})
        if op == "remove_dish":
            idx = _match_index(work, args.get("name", ""))
            if idx is not None:
                changed.append(f"убрано «{work[idx].get('name')}»")
                work.pop(idx)
        elif op == "add_dishes":
            cnt = max(1, min(int(args.get("count", 1) or 1), 6))
            gen = await generate_plan(args.get("query", ""), _dish_names(work), cnt, gender)
            providers.add(gen.get("provider", PROVIDER_DEEPSEEK))
            ids = {d["id"] for d in work}
            for j, d in enumerate(gen["dishes"][:cnt]):
                nd = _reid(d, len(work) + j, ids)
                work.append(nd)
                changed.append(f"добавлено «{nd.get('name')}»")
        elif op == "replace_dish":
            idx = _match_index(work, args.get("old_name", ""))
            gen = await generate_plan(args.get("query", ""), _dish_names(work), 1, gender)
            providers.add(gen.get("provider", PROVIDER_DEEPSEEK))
            if gen["dishes"]:
                ids = {d["id"] for d in work}
                nd = _reid(gen["dishes"][0], (idx if idx is not None else len(work)), ids)
                old = work[idx].get("name") if idx is not None else None
                if idx is not None:
                    work[idx] = nd
                else:
                    work.append(nd)
                changed.append(
                    f"«{old}» заменено на «{nd.get('name')}»" if old else f"добавлено «{nd.get('name')}»"
                )
        elif op == "edit_dish":
            idx = _match_index(work, args.get("name", ""))
            change = args.get("change", "")
            if idx is not None and change:
                dish = work[idx]
                detail = await generate_dish_detail(
                    dish.get("name", ""), dish.get("servings", 4), change
                )
                providers.add(detail.get("provider", PROVIDER_DEEPSEEK))
                nd = {
                    **dish,
                    "ingredients": detail.get("ingredients") or [],
                    "steps": detail.get("steps") or [],
                    "tips": detail.get("tips") or [],
                    "detail_provider": detail.get("provider") or "",
                }
                if detail.get("note"):
                    nd["storage"] = {**(dish.get("storage") or {}), "note": detail["note"]}
                work[idx] = nd
                changed.append(f"рецепт «{dish.get('name')}» обновлён ({change})")
        elif op == "create_plan":
            cnt = max(2, min(int(args.get("count") or len(work) or 5), 12))
            gen = await generate_plan(args.get("note", user_message), [], cnt, gender)
            providers.add(gen.get("provider", PROVIDER_DEEPSEEK))
            work = gen["dishes"]
            new_title = gen.get("title", title)
            changed = ["меню пересобрано"]

    if changed:
        reply = "Готово: " + ", ".join(changed) + "."
    else:
        reply = reply_hint.strip() or "Не понятно, что изменить в плане. Уточните?"

    provider = PROVIDER_CLOUDFLARE if PROVIDER_CLOUDFLARE in providers else PROVIDER_DEEPSEEK
    logger.info("plan edited: ops=%d changed=%d provider=%s", len(calls), len(changed), provider)
    return {
        "reply": reply,
        "title": new_title,
        "dishes": work,
        "provider": provider,
        "changed": changed,
    }


async def replace_dish_by_id(
    dishes: list[dict], title: str, dish_id: str, query: str, gender: str = "f"
) -> dict[str, Any]:
    """Точечная замена конкретного блюда (кнопка «заменить» в карточке): без выбора функции
    моделью — сразу генерим замену. query — пожелание пользователя (может быть пустым)."""
    work = [dict(d) for d in dishes]
    idx = next((i for i, d in enumerate(work) if d.get("id") == dish_id), None)
    if idx is None:
        idx = _match_index(work, dish_id)  # запасной путь: трактуем как название
    if idx is None:
        return {"reply": "Не нашлось блюдо для замены.", "title": title,
                "dishes": dishes, "provider": PROVIDER_DEEPSEEK, "changed": []}

    old = work[idx].get("name", "")
    q = query.strip() or f"другое блюдо взамен «{old}», отличное от остальных"
    gen = await generate_plan(q, _dish_names(work), 1, gender)
    provider = gen.get("provider", PROVIDER_DEEPSEEK)
    if not gen["dishes"]:
        return {"reply": "Не удалось подобрать замену. Попробуйте ещё раз.", "title": title,
                "dishes": dishes, "provider": provider, "changed": []}

    ids = {d["id"] for d in work}
    nd = _reid(gen["dishes"][0], idx, ids)
    work[idx] = nd
    changed = [f"«{old}» заменено на «{nd.get('name')}»"]
    return {"reply": "Готово: " + changed[0] + ".", "title": title,
            "dishes": work, "provider": provider, "changed": changed}


def remove_dish_by_id(dishes: list[dict], title: str, dish_id: str) -> dict[str, Any]:
    """Детерминированное удаление блюда (крестик) — БЕЗ модели, только правка списка."""
    work = [dict(d) for d in dishes]
    idx = next((i for i, d in enumerate(work) if d.get("id") == dish_id), None)
    if idx is None:
        idx = _match_index(work, dish_id)
    if idx is None:
        return {"reply": "Не нашлось блюдо для удаления.", "title": title,
                "dishes": dishes, "provider": "", "changed": []}
    name = work.pop(idx).get("name", "")
    changed = [f"убрано «{name}»"]
    return {"reply": "Готово: " + changed[0] + ".", "title": title,
            "dishes": work, "provider": "", "changed": changed}


async def add_dish_direct(
    dishes: list[dict], title: str, query: str, gender: str = "f"
) -> dict[str, Any]:
    """Добавить одно блюдо в существующий план (кнопка «Добавить блюдо») — без выбора функции
    моделью. query — пожелание пользователя (может быть пустым)."""
    work = [dict(d) for d in dishes]
    q = query.strip() or "ещё одно блюдо, отличное от остальных"
    gen = await generate_plan(q, _dish_names(work), 1, gender)
    provider = gen.get("provider", PROVIDER_DEEPSEEK)
    if not gen["dishes"]:
        return {"reply": "Не удалось подобрать блюдо. Попробуйте ещё раз.", "title": title,
                "dishes": dishes, "provider": provider, "changed": []}
    ids = {d["id"] for d in work}
    nd = _reid(gen["dishes"][0], len(work), ids)
    work.append(nd)
    changed = [f"добавлено «{nd.get('name')}»"]
    return {"reply": "Готово: " + changed[0] + ".", "title": title,
            "dishes": work, "provider": provider, "changed": changed}


async def _edit_actions_cloudflare(
    title: str, dishes: list[dict], user_message: str
) -> tuple[list[dict[str, Any]], str, str]:
    """Фолбэк без tools API: structured actions → приводим к формату tool_calls."""
    try:
        parsed, _ = await run_json(
            build_edit_action_messages(title, _dish_names(dishes), user_message),
            EDIT_ACTION_SCHEMA,
            model=settings.cf_model_judge,
            max_tokens=500,
            label="правка плана (actions, фолбэк)",
        )
    except CloudflareError as exc:
        logger.warning("CF actions недоступны: %s", str(exc)[:120])
        return [], "", PROVIDER_CLOUDFLARE

    op_map = {
        "add": "add_dishes",
        "remove": "remove_dish",
        "replace": "replace_dish",
        "edit": "edit_dish",
        "create": "create_plan",
    }
    calls: list[dict[str, Any]] = []
    for a in parsed.get("actions") or []:
        name = op_map.get(str(a.get("op", "")).lower())
        if not name:
            continue
        calls.append({
            "name": name,
            "args": {
                "query": a.get("query", ""),
                "note": a.get("query", ""),
                "name": a.get("name", ""),
                "old_name": a.get("name", ""),
                "change": a.get("change", "") or a.get("query", ""),
                "count": a.get("count", 1),
            },
        })
    return calls, parsed.get("reply", ""), PROVIDER_CLOUDFLARE


async def normalize_shopping(items: list[dict]) -> list[dict]:
    """Доводит детерминированную базу списка покупок моделью (mistral)."""
    if not items:
        return []
    parsed, _ = await run_json(
        build_shop_normalize_messages(items),
        SHOP_SCHEMA,
        model=settings.cf_model_judge,
        max_tokens=1400,
        label="список покупок (нормализация)",
    )
    return parsed.get("items") or items
