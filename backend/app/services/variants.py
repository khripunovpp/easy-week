"""Варианты рецепта блюда по моделям: dish["variants"] = {model_key: {...}} + active_model.

Плоские поля блюда (ingredients/steps/tips/detail_provider/storage.note) — зеркало активного
варианта (их читают покупки/PDF/план готовки). Хелперы общие для роутера деталей
(routers/plans.py), правки рецепта в чате (ai/planner.edit_plan → edit_dish) и обсуждения.
"""

from datetime import datetime, timezone

from ..ai.gates import GATES


def now_iso() -> str:
    """Время генерации для JSON-кэшей (варианты рецепта/плана готовки): ISO-строка в UTC."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

# Провайдер (человекочитаемый) → ключ модели — для миграции legacy-детали в вариант.
_PROVIDER_KEY = {g.provider: g.key for g in GATES.values()}


def variant_from_detail(detail: dict) -> dict:
    """Деталь модели → вариант рецепта (ингредиенты/шаги/советы/note/провайдер)."""
    return {
        "ingredients": detail.get("ingredients") or [],
        "steps": detail.get("steps") or [],
        "tips": detail.get("tips") or [],
        "note": detail.get("note") or "",
        "provider": detail.get("provider") or "",
        "generated_at": now_iso(),
    }


def dish_variants(dish: dict) -> dict:
    """Варианты рецепта по моделям; при отсутствии — мигрируем плоскую деталь (legacy)."""
    variants = dict(dish.get("variants") or {})
    if not variants and (dish.get("ingredients") or dish.get("steps")):
        key = _PROVIDER_KEY.get(dish.get("detail_provider", ""))
        if key:
            variants[key] = {
                "ingredients": dish.get("ingredients") or [],
                "steps": dish.get("steps") or [],
                "tips": dish.get("tips") or [],
                "note": (dish.get("storage") or {}).get("note") or "",
                "provider": dish.get("detail_provider") or "",
            }
    return variants


def apply_variant(dish: dict, model: str, variants: dict) -> dict:
    """Делает вариант model активным: зеркалим его в плоские поля (для покупок/PDF)."""
    v = variants.get(model) or {}
    return {
        **dish,
        "variants": variants,
        "active_model": model,
        "ingredients": v.get("ingredients") or [],
        "steps": v.get("steps") or [],
        "tips": v.get("tips") or [],
        "detail_provider": v.get("provider") or "",
        # Когда сгенерирован активный вариант (пусто у старых данных — подпись не показываем).
        "detail_generated_at": v.get("generated_at") or "",
        "storage": {**(dish.get("storage") or {}), "note": v.get("note") or ""},
    }


def with_detail(dish: dict, model: str, detail: dict) -> dict:
    """Записать свежую деталь как вариант model и сделать его активным (одной операцией)."""
    variants = dish_variants(dish)
    variants[model] = variant_from_detail(detail)
    return apply_variant(dish, model, variants)


def variant_summary(dish: dict, cap: int = 400) -> str:
    """Короткая выжимка активного варианта для промпта перегенерации: ключевые ингредиенты
    + число шагов. Держим компактной, чтобы не раздувать токены."""
    ings = [str(i.get("name", "")).strip() for i in (dish.get("ingredients") or []) if i.get("name")]
    if not ings:
        return ""
    text = "ингредиенты: " + ", ".join(ings[:14])
    steps = len(dish.get("steps") or [])
    if steps:
        text += f"; шагов: {steps}"
    return text if len(text) <= cap else text[: cap - 1].rstrip() + "…"
