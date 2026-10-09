from datetime import datetime, timezone

from ..models import PlanRow
from ..schemas import CookingPlan, Dish, PlanSummary, WeekPlan
from .variants import active_desc


def _utc(dt: datetime | None) -> datetime | None:
    """SQLite отдаёт naive datetime (пишем в UTC) — помечаем зону, чтобы фронт показал локальное."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def to_dish(d: dict) -> Dish:
    """Блюдо → схема Dish; список ключей вариантов выводим из dict variants."""
    variants = d.get("variants") or {}
    return Dish.model_validate({
        **d, "active_model": d.get("active_model", ""), "variant_models": list(variants.keys()),
        "desc": active_desc(d),  # описание выбранного рецепта (у старых — задумка плана)
    })


def to_week_plan(row: PlanRow) -> WeekPlan:
    dishes = [to_dish(d) for d in (row.dishes or [])]
    return WeekPlan(
        id=row.id,
        conversation_id=row.conversation_id,
        title=row.title,
        week_label=row.week_label,
        status=row.status,
        provider=row.provider,
        dishes=dishes,
        leftovers=[str(x) for x in (row.leftovers or [])],
        created_at=_utc(row.created_at),
        shopping_generated_at=_utc(row.shopping_at),
        shopping_model=row.shopping_model or "",
    )


def to_cook_plan(row: PlanRow) -> CookingPlan:
    """Кэш плана готовки → схема CookingPlan (активный вариант). Пусто → пустой план."""
    cp = row.cooking_plan or {}
    variants = cp.get("variants") or {}
    if not variants:
        return CookingPlan()
    active = cp.get("active_model") or next(iter(variants), "")
    if active not in variants:
        active = next(iter(variants), "")
    v = variants.get(active) or {}
    return CookingPlan.model_validate({
        "active_model": active,
        "variant_models": list(variants.keys()),
        "provider": v.get("provider", ""),
        "steps": v.get("steps") or [],
        "note": v.get("note", ""),
        "generated_at": v.get("generated_at", ""),
    })


def to_summary(row: PlanRow) -> PlanSummary:
    dishes = row.dishes or []
    emoji = dishes[0].get("emoji", "🍽️") if dishes else "🍽️"
    total_cook = sum(int(d.get("prep_min", 0)) + int(d.get("cook_min", 0)) for d in dishes)
    return PlanSummary(
        id=row.id,
        title=row.title,
        week_label=row.week_label,
        status=row.status,
        dishes_count=len(dishes),
        total_cook_min=total_cook,
        emoji=emoji,
        dish_names=[str(d.get("name", "")) for d in dishes if d.get("name")],
        created_at=_utc(row.created_at),
    )
