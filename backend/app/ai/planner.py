import asyncio
import logging
import re
from datetime import date, timedelta
from typing import Any

from ..config import settings
from .cloudflare import run_json
from .deepseek import DeepSeekError, deepseek_json

_MONTHS_GEN = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]


def _week_label(today: date | None = None) -> str:
    today = today or date.today()
    monday = today - timedelta(days=today.weekday())
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
    DETAILS_SCHEMA,
    DISH_SCHEMA,
    NAMES_SCHEMA,
    SHOP_SCHEMA,
    VALIDATE_SCHEMA,
    build_details_messages,
    build_dish_messages,
    build_ds_plan_messages,
    build_names_messages,
    build_shop_normalize_messages,
    build_validate_messages,
)

logger = logging.getLogger("easy_week.planner")


def _slug(text: str, i: int) -> str:
    base = re.sub(r"[^a-zа-я0-9]+", "-", text.lower()).strip("-")
    return f"dish-{i}-{base}"[:48] or f"dish-{i}"


def _clean_name(name: str) -> str:
    # Убираем описания в скобках и лишние хвосты, которые иногда добавляет модель.
    name = re.sub(r"\s*\([^)]*\)", "", name)
    return name.strip(" .,-") or "Блюдо"


async def _gen_dish(i: int, name: str, emoji: str, user_message: str) -> dict[str, Any]:
    parsed, _ = await run_json(
        build_dish_messages(name, user_message), DISH_SCHEMA, max_tokens=800
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
    user_message: str, avoid_titles: list[str], count: int = 5
) -> dict[str, Any]:
    """План через DeepSeek (блюда + короткие шаги); фолбэк — пайплайн Cloudflare."""
    if settings.deepseek_configured:
        try:
            parsed = await deepseek_json(
                build_ds_plan_messages(user_message, avoid_titles, count),
                max_tokens=3000,
            )
            dishes = [_clean_dish(i, d) for i, d in enumerate((parsed.get("dishes") or [])[:count])]
            if dishes:
                logger.info("plan via DeepSeek: dishes=%d", len(dishes))
                return {
                    "reply": parsed.get("reply") or "Готово — вот план на неделю.",
                    "title": _clean_title(parsed.get("title") or "План на неделю"),
                    "week_label": _week_label(),
                    "dishes": dishes,
                }
            logger.warning("DeepSeek вернул пустой план — фолбэк на Cloudflare")
        except DeepSeekError as exc:
            logger.warning("DeepSeek недоступен (%s) — фолбэк на Cloudflare", str(exc)[:120])

    return await _generate_plan_cloudflare(user_message, avoid_titles, count)


async def _generate_plan_cloudflare(
    user_message: str, avoid_titles: list[str], count: int = 5
) -> dict[str, Any]:
    """Фолбэк-пайплайн: меню (mistral) → спеки (8b, параллельно) → валидация (mistral)."""
    names, _ = await run_json(
        build_names_messages(user_message, avoid_titles, count),
        NAMES_SCHEMA,
        model=settings.cf_model_menu,
        max_tokens=120 + count * 110,
    )
    entries = (names.get("dishes") or [])[:count]  # держим ровно count блюд

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
    }


async def generate_details(name: str, ingredients: list[dict]) -> dict[str, list[str]]:
    """Развёрнутый рецепт (mistral) — по клику на блюдо."""
    parsed, _ = await run_json(
        build_details_messages(name, ingredients),
        DETAILS_SCHEMA,
        model=settings.cf_model_judge,
        max_tokens=1100,
    )
    return {"steps": parsed.get("steps") or [], "tips": parsed.get("tips") or []}


async def normalize_shopping(items: list[dict]) -> list[dict]:
    """Доводит детерминированную базу списка покупок моделью (mistral)."""
    if not items:
        return []
    parsed, _ = await run_json(
        build_shop_normalize_messages(items),
        SHOP_SCHEMA,
        model=settings.cf_model_judge,
        max_tokens=1400,
    )
    return parsed.get("items") or items
