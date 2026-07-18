from ..models import PlanRow
from ..schemas import Dish, PlanSummary, WeekPlan


def to_dish(d: dict) -> Dish:
    """Блюдо → схема Dish; список ключей вариантов выводим из dict variants."""
    variants = d.get("variants") or {}
    return Dish.model_validate(
        {**d, "active_model": d.get("active_model", ""), "variant_models": list(variants.keys())}
    )


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
    )


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
    )
