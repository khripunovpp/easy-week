import asyncio
import logging
import re
from typing import Any

from .cloudflare import run_json
from .prompt import (
    DETAILS_SCHEMA,
    DISH_SCHEMA,
    NAMES_SCHEMA,
    build_details_messages,
    build_dish_messages,
    build_names_messages,
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
        build_dish_messages(name, user_message), DISH_SCHEMA, max_tokens=600
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


async def generate_plan(user_message: str, avoid_titles: list[str]) -> dict[str, Any]:
    """Быстрая генерация: сначала названия, потом блюда параллельно."""
    names, _ = await run_json(
        build_names_messages(user_message, avoid_titles), NAMES_SCHEMA, max_tokens=700
    )
    entries = names.get("dishes") or []

    dishes = await asyncio.gather(
        *(
            _gen_dish(i, _clean_name(d.get("name", f"Блюдо {i + 1}")), d.get("emoji", ""), user_message)
            for i, d in enumerate(entries)
        )
    )

    logger.info("plan generated: dishes=%d (parallel)", len(dishes))
    return {
        "reply": names.get("reply") or "Готово — вот план на неделю.",
        "title": names.get("title") or "План на неделю",
        "week_label": names.get("week_label") or "",
        "dishes": list(dishes),
    }


async def generate_details(name: str, ingredients: list[dict]) -> dict[str, list[str]]:
    """Ленивая догенерация шагов и советов для одного блюда."""
    parsed, _ = await run_json(
        build_details_messages(name, ingredients), DETAILS_SCHEMA, max_tokens=700
    )
    return {"steps": parsed.get("steps") or [], "tips": parsed.get("tips") or []}
